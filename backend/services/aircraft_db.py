"""Aircraft type database: ICAO hex to ICAO type designator, from two public sources.

Mictronics ``indexedDB_old.zip`` types most airframes outright.  The FAA
``ReleasableAircraft.zip`` fills the rest: every airframe both sources know
teaches a map from FAA model code to ICAO type, and that map types the airframes
only the FAA knows.  Anything else has no row.  The result is one small npz of
sorted arrays, swapped in atomically and read through ``current()``.

Downloading and building run in a child process, ``python -m services.aircraft_db``,
because parsing these files would hold the GIL against the frame threads.  So this
module imports neither ``main`` nor ``core.state``, and every setting arrives as an
argument.  The FAA file holds owner names and addresses: only the model code and
the hex are ever taken from it.
"""

import argparse
import contextlib
import csv
import io
import json
import logging
import os
import re
import shutil
import sys
import threading
import zipfile
import zlib
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from itertools import compress
from pathlib import Path

import httpx
import numpy as np

from services.id_utils import normalize_hex_key

logger = logging.getLogger(__name__)

DB_DIR = Path(__file__).resolve().parents[1] / "coverage_data" / "aircraft_db"
FILE_NAME = "aircraft_db.npz"
WORK_DIR_NAME = "download"

_SCHEMA = 1
_ARRAYS = ("hex", "type_idx", "types", "schema")
_MAX_TYPES = int(np.iinfo(np.uint16).max) + 1

_ICAO_HEX_RE = re.compile(r"[0-9a-f]{6}")
_TYPE_RE = re.compile(r"[A-Z0-9]{2,4}")
# Mictronics' placeholder for "no designator", unlike GLID or BALL, which are real ICAO groups.
_NO_DESIGNATOR = "ZZZZ"

_USER_AGENT = "retina-server/1.0 (+https://github.com/offworldlabs/retina-server)"
# The FAA's CDN answers 403 to anything but a browser string, so this host alone gets the caller's.
_FAA_HOST = "registry.faa.gov"
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
# Per downloaded file, and per zip member once unpacked: a small zip can unpack to anything.
_MAX_SOURCE_BYTES = 512 * 1024 * 1024
# The child's memory follows these sizes, and it shares the server's container.
# aircrafts.json is read whole; every model code kept stays in memory to the end.
_MAX_JSON_BYTES = 64 * 1024 * 1024
_MAX_ROWS = 1_000_000
_MAX_MODEL_CODE = 16
_MAX_LINE = 4096  # Chars.  The real longest is 614; csv keeps every field of a row it reads.

# What a damaged source raises.  ValueError is bad JSON or bad text; RuntimeError is
# an encrypted member, or JSON nested past the recursion limit.
_SOURCE_ERRORS = (zipfile.BadZipFile, zlib.error, csv.Error, ValueError, EOFError, OSError, RuntimeError)


class AircraftDbError(ValueError):
    """A source file or a database file is not usable."""


def _hex_value(raw: object) -> int | None:
    """The 24-bit address behind a hex key, or None for a key that isn't an ICAO address."""
    key = normalize_hex_key(raw)
    return int(key, 16) if _ICAO_HEX_RE.fullmatch(key) else None


class AircraftDb:
    """Airframe types by ICAO address, held as sorted arrays."""

    def __init__(self, hexes: np.ndarray, type_idx: np.ndarray, names: list[str]) -> None:
        self._hex = hexes
        self._type_idx = type_idx
        self._names = names

    def types_for(self, hexes: Sequence[str]) -> list[str | None]:
        """Types for these hexes, in order: None for a miss or a key that isn't an ICAO address.

        Answers are plain ``str``, since rows go on to Parquet and orjson.
        """
        if len(hexes) == 0:
            return []
        keys = [raw if isinstance(raw, str) else None for raw in hexes]
        values = {raw: _hex_value(raw) for raw in set(keys)}
        known = [raw for raw, value in values.items() if value is not None]
        wanted = np.array([values[raw] for raw in known], dtype=np.uint32)
        positions = np.searchsorted(self._hex, wanted)
        found = positions < self._hex.size
        found[found] = self._hex[positions[found]] == wanted[found]
        names = (self._names[index] for index in self._type_idx[positions[found]].tolist())
        answers = dict(zip(compress(known, found.tolist()), names, strict=True))
        return [answers.get(key) for key in keys]


def load(path: Path) -> AircraftDb:
    """Read and check one database file.

    AircraftDbError means the file itself is bad.  OSError or MemoryError means it
    couldn't be read, which says nothing about it; a missing file raises
    FileNotFoundError.  Nothing else leaves here: a crafted file can make numpy raise
    anything, even in the checks, so all of it sits under one handler.
    """
    try:
        with np.load(path, allow_pickle=False) as npz:
            # asarray: a zip member that isn't an array comes back as bytes, and fails the checks below.
            hexes, type_idx, types, schema = (np.asarray(npz[name]) for name in _ARRAYS)
        if schema.shape != () or schema.item() != _SCHEMA:
            raise AircraftDbError(f"{path.name} has the wrong schema")
        if hexes.dtype != np.uint32 or type_idx.dtype != np.uint16 or types.dtype.kind != "U":
            raise AircraftDbError(f"{path.name} has the wrong array types")
        if not hexes.ndim == type_idx.ndim == types.ndim == 1 or hexes.size != type_idx.size:
            raise AircraftDbError(f"{path.name} has arrays of unequal length")
        if np.any(hexes[1:] <= hexes[:-1]):
            raise AircraftDbError(f"{path.name} has hexes out of order")
        if type_idx.size and int(type_idx.max()) >= types.size:
            raise AircraftDbError(f"{path.name} has a type index past the end of its types")
        return AircraftDb(hexes, type_idx, types.tolist())
    except (AircraftDbError, OSError, MemoryError):
        raise
    except Exception as exc:
        # zipfile turns a failed read of the end record into BadZipFile: the read failed, not the file.
        if isinstance(exc.__context__, OSError):
            raise exc.__context__ from None
        raise AircraftDbError(f"{path.name} is not a database file: {exc}") from exc


_lock = threading.Lock()
_db: AircraftDb | None = None
_db_identity: tuple[int, int, int] | None = None
_bad_identity: tuple[int, int, int] | None = None


def current() -> AircraftDb | None:
    """The file in ``DB_DIR`` as loaded, or None when there is none.  Never raises.

    One stat per call: a swapped file is reloaded, and a file that won't load
    leaves the last good one in service.  A bad file isn't read again until it
    changes; a failed read is tried again on the next call.  The flush calls this
    from an executor thread, so the lock covers the whole check.
    """
    global _db, _db_identity, _bad_identity
    path = DB_DIR / FILE_NAME
    with _lock:
        try:
            stat = os.stat(path)
        except OSError:
            _db = _db_identity = _bad_identity = None
            return None
        identity = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        if identity in (_db_identity, _bad_identity):
            return _db
        try:
            _db = load(path)
        except AircraftDbError as exc:
            _bad_identity = identity
            logger.warning("aircraft database file ignored, keeping the last good one: %s", exc)
            return _db
        except (OSError, MemoryError) as exc:
            logger.warning("aircraft database file not read, keeping the last good one: %s", exc)
            return _db
        _db_identity, _bad_identity = identity, None
        return _db


@contextmanager
def _source(path: Path) -> Iterator[zipfile.ZipFile]:
    """Open one source zip.  Anything that goes wrong reading it becomes an AircraftDbError."""
    try:
        with zipfile.ZipFile(path) as archive:
            yield archive
    except AircraftDbError:
        raise
    except _SOURCE_ERRORS as exc:
        raise AircraftDbError(f"{path.name}: {exc}") from exc


def _member(archive: zipfile.ZipFile, name: str, max_bytes: int = _MAX_SOURCE_BYTES) -> zipfile.ZipInfo:
    """Find a zip member by base name, in any case."""
    for info in archive.infolist():
        if info.filename.rsplit("/", 1)[-1].lower() == name.lower():
            # The declared size is binding: reading a member stops there.
            if info.file_size > max_bytes:
                raise AircraftDbError(f"{name} is too large")
            # Only deflate inflates a piece at a time: bzip2 and lzma unpack all of a read in one call.
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise AircraftDbError(f"{name} is neither stored nor deflated")
            return info
    raise AircraftDbError(f"{name} is missing from the archive")


def _lines(stream: io.TextIOBase, member: str) -> Iterator[str]:
    """The stream's lines, stopped before one can reach csv at any length."""
    while line := stream.readline(_MAX_LINE + 1):
        if len(line) > _MAX_LINE:
            raise AircraftDbError(f"{member} has a line over {_MAX_LINE} characters")
        yield line


def _columns(archive: zipfile.ZipFile, member: str, *names: str) -> Iterator[tuple[str, ...]]:
    """Stream the named columns of one CSV member, stripped.  The rest of a row stays here.

    Headers carry stray spaces and an empty last column, so columns go by stripped
    name.  A row too short to hold them is skipped.
    """
    with io.TextIOWrapper(
        archive.open(_member(archive, member)), encoding="utf-8-sig", errors="replace", newline=""
    ) as stream:
        # QUOTE_NONE, because a quoted newline would join short lines into one row of any length.
        reader = csv.reader(_lines(stream, member), quoting=csv.QUOTE_NONE)
        header = [name.strip() for name in next(reader, [])]
        for name in names:
            if name not in header:
                raise AircraftDbError(f"{member} has no {name} column")
        indexes = [header.index(name) for name in names]
        width = max(indexes)
        for row in reader:
            if len(row) > width:
                yield tuple(row[index].strip() for index in indexes)


def _keep_type(obj: dict) -> dict | tuple:
    """Shrink an aircraft object as it parses, so 452 k inner dicts never exist at once.

    The tuple marks a real aircraft object: a bare string in a row isn't one.
    """
    return (obj["t"],) if "t" in obj else obj


def _clean_type(raw: object) -> str | None:
    """An ICAO designator, or None for an empty, malformed or placeholder value."""
    if not isinstance(raw, str):
        return None
    value = raw.strip().upper()
    return value if _TYPE_RE.fullmatch(value) and value != _NO_DESIGNATOR else None


def _read_mictronics(path: Path) -> dict[int, str]:
    """Address to type, for every Mictronics airframe with a real designator."""
    with _source(path) as archive:
        # Not archive.read(): that inflates up to 1 GiB at a go, however little the member declares.
        with archive.open(_member(archive, "aircrafts.json", _MAX_JSON_BYTES)) as member:
            raw = member.read(_MAX_JSON_BYTES + 1)
        # json.loads costs per element, not per byte.  Rows are objects, every container opens with a brace
        # or a bracket, and every element but the last in one is followed by a comma, so these counts bound
        # what it can build.  A real row holds three commas and is followed by a fourth.
        if raw.count(b"{") + raw.count(b"[") > _MAX_ROWS or raw.count(b",") > 4 * _MAX_ROWS:
            raise AircraftDbError("aircrafts.json has too many rows")
        rows = json.loads(raw, object_hook=_keep_type)
    if not isinstance(rows, dict):
        raise AircraftDbError("aircrafts.json is not an object keyed by hex")
    typed = {}
    for key, row in rows.items():
        value = _hex_value(key)
        icao_type = _clean_type(row[0]) if isinstance(row, tuple) else None
        if value is not None and icao_type is not None:
            typed[value] = icao_type
    return typed


def _read_faa(path: Path) -> tuple[dict[int, str], int]:
    """Address to FAA model code from ``MASTER.txt``, and the model rows in ``ACFTREF.txt``."""
    with _source(path) as archive:
        models = {}
        for hex_text, code in _columns(archive, "MASTER.txt", "MODE S CODE HEX", "MFR MDL CODE"):
            value = _hex_value(hex_text)
            if value is not None and 0 < len(code) <= _MAX_MODEL_CODE:
                models[value] = code
                if len(models) > _MAX_ROWS:
                    raise AircraftDbError(f"MASTER.txt has over {_MAX_ROWS} rows")
        reference = sum(1 for (code,) in _columns(archive, "ACFTREF.txt", "CODE") if code)
    # A Mictronics-only build would bias the classes, so an empty registry stops it too.
    if not models:
        raise AircraftDbError("MASTER.txt has no usable rows")
    return models, reference


def _learn_models(typed: dict[int, str], models: dict[int, str]) -> dict[str, str]:
    """Each FAA model code's most common type among airframes both sources know.

    A tie means the sources don't say, so that code teaches nothing.
    """
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for value, code in models.items():
        icao_type = typed.get(value)
        if icao_type is not None:
            counts[code][icao_type] += 1
    learned = {}
    for code, seen in counts.items():
        top = seen.most_common(2)
        if len(top) == 1 or top[0][1] > top[1][1]:
            learned[code] = top[0][0]
    return learned


def build(mictronics_zip: Path, faa_zip: Path, out: Path) -> dict:
    """Build the database file from the two source zips and swap it in at ``out``.

    Order of trust: a Mictronics type, else the learned type of the FAA model code,
    else no row.  Raises AircraftDbError for bad input.  On any failure ``out`` is
    untouched and no temporary file stays behind.  Returns the build's stats.
    """
    typed = _read_mictronics(mictronics_zip)
    models, faa_models = _read_faa(faa_zip)
    learned = _learn_models(typed, models)
    filled = {value: learned[code] for value, code in models.items() if value not in typed and code in learned}
    merged = filled | typed
    if not merged:
        raise AircraftDbError("the sources hold no typed rows")

    names = sorted(set(merged.values()))
    if len(names) > _MAX_TYPES:
        raise AircraftDbError(f"{len(names)} distinct types don't fit a uint16 index")
    index = {name: position for position, name in enumerate(names)}
    keys = sorted(merged)
    hexes = np.array(keys, dtype=np.uint32)
    type_idx = np.array([index[merged[key]] for key in keys], dtype=np.uint16)

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    try:
        # A handle, because numpy appends .npz to a path.
        with open(tmp, "wb") as file:
            np.savez(file, allow_pickle=False, hex=hexes, type_idx=type_idx, types=np.array(names), schema=_SCHEMA)
            file.flush()
            os.fsync(file.fileno())
        load(tmp)
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    return {
        "rows": len(merged),
        "mictronics_typed": len(typed),
        "faa_filled": len(filled),
        "models_learned": len(learned),
        "faa_models": faa_models,
        "types": len(names),
    }


def _fetch(client: httpx.Client, url: str, dest: Path) -> None:
    """Stream one URL to ``dest``, failing past the size cap."""
    received = 0
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with open(dest, "wb") as file:
            for chunk in response.iter_bytes():
                received += len(chunk)
                if received > _MAX_SOURCE_BYTES:
                    raise AircraftDbError(f"{dest.name} is over the download cap")
                file.write(chunk)


def download_sources(mictronics_url: str, faa_url: str, dest_dir: Path, *, faa_user_agent: str) -> tuple[Path, Path]:
    """Download both source zips into ``dest_dir``.  Returns the Mictronics and FAA paths."""
    mictronics_zip = dest_dir / "indexedDB_old.zip"
    faa_zip = dest_dir / "ReleasableAircraft.zip"

    def set_agent(request: httpx.Request) -> None:
        # A hook runs on every hop, and a redirect would carry the last hop's headers to the next host.
        request.headers["User-Agent"] = faa_user_agent if request.url.host == _FAA_HOST else _USER_AGENT

    with httpx.Client(follow_redirects=True, timeout=_TIMEOUT, event_hooks={"request": [set_agent]}) as client:
        _fetch(client, mictronics_url, mictronics_zip)
        _fetch(client, faa_url, faa_zip)
    return mictronics_zip, faa_zip


def refresh(mictronics_url: str, faa_url: str, *, faa_user_agent: str, out: Path | None = None) -> dict:
    """Download both sources, build, and swap in the new file.  Returns the build's stats.

    Both sources are required: one failure means no swap, and the last good file
    stands.  The work folder goes in every case, since the FAA file holds owner names.
    """
    target = DB_DIR / FILE_NAME if out is None else out
    work = target.parent / WORK_DIR_NAME
    shutil.rmtree(work, ignore_errors=True)  # left behind by a killed run
    work.mkdir(parents=True)
    try:
        mictronics_zip, faa_zip = download_sources(mictronics_url, faa_url, work, faa_user_agent=faa_user_agent)
        return build(mictronics_zip, faa_zip, target)
    finally:
        shutil.rmtree(work)


def _yield_to_the_server() -> None:
    """If the container runs out of memory, the kernel takes this process, not the server."""
    with contextlib.suppress(OSError):
        Path("/proc/self/oom_score_adj").write_text("1000")


def main(argv: Sequence[str] | None = None) -> int:
    """Child process entry: refresh the file and report through the exit code."""
    parser = argparse.ArgumentParser(description="Download the aircraft sources and swap in a new database file.")
    parser.add_argument("--mictronics-url", required=True)
    parser.add_argument("--faa-url", required=True)
    parser.add_argument("--faa-user-agent", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    _yield_to_the_server()
    try:
        stats = refresh(args.mictronics_url, args.faa_url, faa_user_agent=args.faa_user_agent, out=args.out)
    except Exception as exc:
        logger.error("aircraft database refresh failed: %s", exc)
        return 1
    logger.info("aircraft database refreshed: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
