"""Aircraft type database: build from the two source zips, swap the file, look airframes up."""

import functools
import json
import subprocess
import sys
import tracemalloc
import zipfile
from pathlib import Path
from unittest import mock

import httpx
import numpy as np
import pytest

from services import aircraft_db
from services.tasks import aircraft_db_refresh

BACKEND_DIR = Path(__file__).resolve().parents[1]

MASTER_HEADER = (
    "N-NUMBER,SERIAL NUMBER,MFR MDL CODE,ENG MFR MDL,YEAR MFR,TYPE REGISTRANT,NAME,STREET,STREET2,CITY,STATE,"
    "ZIP CODE,REGION,COUNTY,COUNTRY,LAST ACTION DATE,CERT ISSUE DATE,CERTIFICATION,TYPE AIRCRAFT,TYPE ENGINE,"
    "STATUS CODE,MODE S CODE,FRACT OWNER,AIR WORTH DATE,OTHER NAMES(1),OTHER NAMES(2),OTHER NAMES(3),OTHER NAMES(4),"
    "OTHER NAMES(5),EXPIRATION DATE,UNIQUE ID,KIT MFR, KIT MODEL,MODE S CODE HEX,"
)
ACFTREF_HEADER = (
    "CODE,MFR,MODEL,TYPE-ACFT,TYPE-ENG,AC-CAT,BUILD-CERT-IND,NO-ENG,NO-SEATS,AC-WEIGHT,SPEED,"
    "TC-DATA-SHEET,TC-DATA-HOLDER,"
)


def write_sources(folder: Path, aircraft: dict, master: list[tuple[str, str]]) -> tuple[Path, Path]:
    """Write both source zips in the real shapes and return their paths.

    ``aircraft`` maps upper-case hex to type; ``master`` lists (hex, model code).
    """
    folder.mkdir(parents=True, exist_ok=True)
    mictronics_zip, faa_zip = folder / "indexedDB_old.zip", folder / "ReleasableAircraft.zip"
    rows = {key: {"r": "N0000", "t": icao_type, "f": "00", "d": "Invented"} for key, icao_type in aircraft.items()}
    with zipfile.ZipFile(mictronics_zip, "w") as archive:
        archive.writestr("aircrafts.json", json.dumps(rows))

    master_lines = [MASTER_HEADER]
    for key, code in master:
        fields = [f"FILLER{column}" for column in range(35)]
        fields[2], fields[6], fields[21], fields[33], fields[34] = code, "INVENTED OWNER", "52000000", key.ljust(10), ""
        master_lines.append(",".join(fields))
    models = sorted({code for _, code in master})
    acftref_lines = [ACFTREF_HEADER, *(f"{code},INVENTED,MODEL,4,1,1,1,01,004,CLASS 1,0,,," for code in models)]
    with zipfile.ZipFile(faa_zip, "w") as archive:
        for name, lines in (("MASTER.txt", master_lines), ("ACFTREF.txt", acftref_lines)):
            archive.writestr(name, "﻿" + "\r\n".join(lines) + "\r\n")
    return mictronics_zip, faa_zip


def test_build_and_lookup_follow_the_order_of_trust(tmp_path):
    aircraft = {
        "3C0001": "A320",
        "A00001": "C172",
        "A00002": "C172",
        "A00003": "PA28",
        "A00004": "ZZZZ",
        "A00007": "BE20",
        "A00008": "C208",
    }
    master = [
        ("A00001", "0000001"),
        ("A00002", "0000001"),
        ("A00003", "0000001"),
        ("A00004", "0000001"),
        ("A00005", "0000001"),
        ("A00006", "0000009"),
        ("A00007", "0000002"),
        ("A00008", "0000002"),
        ("A00009", "0000002"),
    ]
    out = tmp_path / "out" / aircraft_db.FILE_NAME

    stats = aircraft_db.build(*write_sources(tmp_path, aircraft, master), out)

    assert stats == {
        "rows": 8,
        "mictronics_typed": 6,
        "faa_filled": 2,
        "models_learned": 1,
        "faa_models": 3,
        "types": 5,
    }
    # Mictronics only, both sources disagreeing, FAA only, ZZZZ, an FAA model nothing taught, a tied one, and a miss.
    answers = aircraft_db.load(out).types_for(["3c0001", "a00003", "a00005", "a00004", "a00006", "a00009", "ffffff"])
    assert answers == ["A320", "PA28", "C172", "C172", None, None, None]
    assert type(answers[0]) is str
    assert aircraft_db.load(out).types_for([None, "~abc123", "mn-dark-7", "a00003"]) == [None, None, None, "PA28"]


def test_corrupt_download_leaves_the_last_good_file(tmp_path, monkeypatch):
    out = tmp_path / "db" / aircraft_db.FILE_NAME
    monkeypatch.setattr(aircraft_db, "DB_DIR", out.parent)
    good_mictronics, good_faa = write_sources(tmp_path / "good", {"A00001": "C172"}, [("A00001", "0000001")])
    served = {"/mictronics.zip": good_mictronics.read_bytes(), "/faa.zip": good_faa.read_bytes()}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=served[request.url.path]))
    monkeypatch.setattr(aircraft_db.httpx, "Client", functools.partial(httpx.Client, transport=transport))
    refresh = functools.partial(
        aircraft_db.refresh,
        "https://example.com/mictronics.zip",
        "https://example.com/faa.zip",
        faa_user_agent="Browser/1.0",
    )

    refresh()
    last_good = out.read_bytes()

    served["/faa.zip"] = b"not a zip"
    with pytest.raises(aircraft_db.AircraftDbError):
        refresh()
    assert out.read_bytes() == last_good
    assert aircraft_db.current().types_for(["a00001"]) == ["C172"]
    assert [path.name for path in out.parent.iterdir()] == [aircraft_db.FILE_NAME]

    # A torn write gets past parsing, so only the validation stops it.
    served["/faa.zip"] = good_faa.read_bytes()
    with monkeypatch.context() as torn, pytest.raises(aircraft_db.AircraftDbError):
        torn.setattr(aircraft_db.np, "savez", lambda file, **_: file.write(b"torn"))
        refresh()
    assert out.read_bytes() == last_good
    assert [path.name for path in out.parent.iterdir()] == [aircraft_db.FILE_NAME]

    newer_mictronics, _ = write_sources(tmp_path / "newer", {"A00001": "PA28"}, [("A00001", "0000001")])
    served["/mictronics.zip"] = newer_mictronics.read_bytes()
    refresh()
    assert aircraft_db.current().types_for(["a00001"]) == ["PA28"]


def test_a_source_past_a_cap_leaves_the_last_good_file(tmp_path, monkeypatch):
    out = tmp_path / "db" / aircraft_db.FILE_NAME
    sources = write_sources(tmp_path, {"A00001": "C172"}, [(f"A0000{n}", "0000001") for n in (1, 2, 3)])
    aircraft_db.build(*sources, out)
    last_good = out.read_bytes()

    for cap, value, reason in (
        ("_MAX_JSON_BYTES", 10, "aircrafts.json is too large"),
        ("_MAX_ROWS", 1, "aircrafts.json has too many rows"),
        ("_MAX_ROWS", 2, "over 2 rows"),
    ):
        with monkeypatch.context() as capped, pytest.raises(aircraft_db.AircraftDbError, match=reason):
            capped.setattr(aircraft_db, cap, value)
            aircraft_db.build(*sources, out)
        assert out.read_bytes() == last_good

    monkeypatch.setattr(aircraft_db, "_MAX_ROWS", 2)
    for document in ('{"a":[[]]}', json.dumps(dict.fromkeys("abcdefghij", 1))):
        with zipfile.ZipFile(sources[0], "w") as archive:
            archive.writestr("aircrafts.json", document)
        with pytest.raises(aircraft_db.AircraftDbError, match="aircrafts.json has too many rows"):
            aircraft_db.build(*sources, out)
    assert out.read_bytes() == last_good


def test_a_text_line_is_one_row_and_one_past_the_bound_stops_the_build(tmp_path):
    out = tmp_path / aircraft_db.FILE_NAME
    master = [("A00001", '"0000001'), ("A00002", "0000002"), ("A00003", "0000002")]
    assert aircraft_db.build(*write_sources(tmp_path, {"A00002": "C172"}, master), out)["faa_filled"] == 1

    sources = write_sources(tmp_path, {"A00002": "C172"}, [*master, ("A00004", "0" * 4096)])
    with pytest.raises(aircraft_db.AircraftDbError, match="MASTER.txt has a line over 4096 characters"):
        aircraft_db.build(*sources, out)


def test_a_member_declaring_less_is_refused_without_inflating_past_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(aircraft_db, "_MAX_JSON_BYTES", 1 << 20)
    mictronics_zip, faa_zip = write_sources(tmp_path, {"A00001": "C172"}, [("A00001", "0000001")])
    for method in (zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2):
        with zipfile.ZipFile(mictronics_zip, "w", method) as archive:
            archive.writestr("aircrafts.json", b" " * (32 << 20))
            archive.getinfo("aircrafts.json").file_size = 1000
        tracemalloc.start()
        with pytest.raises(aircraft_db.AircraftDbError):
            aircraft_db.build(mictronics_zip, faa_zip, tmp_path / aircraft_db.FILE_NAME)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert peak < 8 << 20


def test_the_browser_string_goes_to_the_faa_host_alone(tmp_path, monkeypatch):
    seen = []

    def serve(request):
        seen.append((request.url.host, request.headers["User-Agent"]))
        if request.url.host == "registry.faa.gov":
            return httpx.Response(302, headers={"Location": "https://example.com/faa.zip"})
        return httpx.Response(200, content=b"zip")

    transport = httpx.MockTransport(serve)
    monkeypatch.setattr(aircraft_db.httpx, "Client", functools.partial(httpx.Client, transport=transport))
    for url in ("https://registry.faa.gov/faa.zip", "https://example.com/faa.zip"):
        aircraft_db.download_sources("https://example.com/m.zip", url, tmp_path, faa_user_agent="Browser/1.0")

    own = ("example.com", "retina-server/1.0 (+https://github.com/offworldlabs/retina-server)")
    assert seen == [own, ("registry.faa.gov", "Browser/1.0"), own, own, own]


def test_a_failed_read_does_not_condemn_a_good_file(tmp_path, monkeypatch):
    out = tmp_path / "db" / aircraft_db.FILE_NAME
    monkeypatch.setattr(aircraft_db, "DB_DIR", out.parent)
    assert aircraft_db.current() is None
    aircraft_db.build(*write_sources(tmp_path, {"A00001": "C172"}, [("A00001", "0000001")]), out)
    real_load = aircraft_db.np.load

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(aircraft_db.np, "load", real_load)
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(aircraft_db.np, "load", fail_once)
    assert aircraft_db.current() is None
    assert aircraft_db.current().types_for(["a00001"]) == ["C172"]


def test_no_file_means_no_database(tmp_path, monkeypatch):
    out = tmp_path / "db" / aircraft_db.FILE_NAME
    monkeypatch.setattr(aircraft_db, "DB_DIR", out.parent)
    assert aircraft_db.current() is None

    aircraft_db.build(*write_sources(tmp_path, {"A00001": "C172"}, [("A00001", "0000001")]), out)
    assert aircraft_db.current() is not None

    out.unlink()
    assert aircraft_db.current() is None


def test_import_leaves_the_app_out():
    code = "import sys, services.aircraft_db; print(sorted({'main', 'core.state'} & set(sys.modules)))"
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND_DIR, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"


def test_a_crafted_file_raises_past_no_reader(tmp_path, monkeypatch):
    out = tmp_path / aircraft_db.FILE_NAME
    monkeypatch.setattr(aircraft_db, "DB_DIR", tmp_path)
    aircraft_db.build(*write_sources(tmp_path, {"A00001": "C172"}, [("A00001", "0000001")]), out)
    assert aircraft_db.current() is not None
    arrays = dict(np.load(out))
    header = b"{'descr': '<u4', 'fortran_order': False, 'shape': (18446744073709551616,)}"
    with zipfile.ZipFile(out, "w") as archive:  # a shape past int64 stops the read itself
        archive.writestr("hex.npy", b"\x93NUMPY\x01\x00" + len(header).to_bytes(2, "little") + header)
    assert aircraft_db.current().types_for(["a00001"]) == ["C172"]
    assert aircraft_db_refresh._seconds_until_due() == 0
    # A character past U+10FFFF reads fine, and only fails on the way to a Python string.
    np.savez(out, **arrays | {"types": np.array([0x110000], dtype="<u4").view("<U1")})
    assert aircraft_db.current().types_for(["a00001"]) == ["C172"]
    assert aircraft_db_refresh._seconds_until_due() == 0


def test_a_failed_read_of_the_zip_end_record_does_not_condemn_a_good_file(tmp_path, monkeypatch):
    out = tmp_path / aircraft_db.FILE_NAME
    monkeypatch.setattr(aircraft_db, "DB_DIR", tmp_path)
    assert aircraft_db.current() is None
    aircraft_db.build(*write_sources(tmp_path, {"A00001": "C172"}, [("A00001", "0000001")]), out)
    with monkeypatch.context() as failing:
        failing.setattr(zipfile, "_EndRecData", mock.Mock(side_effect=OSError(5, "Input/output error")))
        assert aircraft_db.current() is None
    assert aircraft_db.current().types_for(["a00001"]) == ["C172"]
