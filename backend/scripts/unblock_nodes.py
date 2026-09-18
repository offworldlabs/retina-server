#!/usr/bin/env python3
"""Clear `blocked` reputations out of a persisted state snapshot.

A node whose reputation fell below the block threshold stops being heard:
``NodeAnalyticsManager.record_detection_frame`` returns False for it, so every
frame it sends is dropped, and ``apply_reward`` is a no-op while blocked, so
the node cannot earn its way back.  The block is persisted — ``reputations``
in ``backend/data/state_snapshot.json`` — so a restart restores it.  There is
no admin unblock route, and ``NodeReputation.unblock()`` only resets the
reputation to 0.3, one penalty above re-blocking.  This script is the way out.

REPUTATION_PENALTY_SCALE (default 0) stops *new* penalties from being
recorded; it deliberately does not unblock anything already written down.
That is this script's job.

**The server must be stopped while this runs.**  Two reasons, both fatal on
their own: the save loop rewrites the snapshot every 60 s and would overwrite
the edit, and the block that actually gates frames lives in memory — the file
only matters because the restart restores from it.  Stop, edit, start.

Node *ids*, not node_refs
-------------------------
``reputations`` is keyed by node_id (``retce36dbb4``, ``synth-GVL-0004``); the
analytics API renames entries to node_ref only at publication, so the ref you
read off ``/api/radar/analytics`` is not a key here.  Resolve it first, inside
the running container, before you stop anything::

    docker compose exec -w /app/backend server \\
        python3 -c "from services import node_refs as n; print(n.id_for_ref('ndebvzgeoij5t2l'))"

(For a mirrored real node the ref comes from
``state.connected_nodes[node_id]["node_ref"]`` — see ``services/node_refs.py``,
``_mirrored_ref``.)  Or skip the lookup entirely with ``--all-blocked``.

Usage (droplet, snapshot on the backend-data volume)::

    docker compose stop server
    docker run --rm -v retina-server_backend-data:/data -v $PWD/backend/scripts:/s \\
        python:3.12-slim python /s/unblock_nodes.py --path /data/state_snapshot.json --all-blocked
    docker compose up -d server

or straight on the host against the volume's mountpoint::

    python3 backend/scripts/unblock_nodes.py \\
        --path /var/lib/docker/volumes/<project>_backend-data/_data/state_snapshot.json \\
        --node retce36dbb4

Add ``--dry-run`` first to see what would change.  Stdlib only, deliberately:
it has to run in a bare ``python:3.12-slim`` with the repo's dependencies
nowhere in sight.
"""

import argparse
import hashlib
import json
import os
import sys

# backend/scripts/unblock_nodes.py → backend/data/state_snapshot.json, the
# same path services/state_snapshot.py computes.  Resolved here rather than
# imported because this script must not import the backend (no dependencies
# beyond the stdlib — see the module docstring).
_DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "state_snapshot.json",
)


def _read_envelope(path: str, force: bool) -> dict:
    """Return the decoded payload dict, verifying the schema-2 checksum.

    Schema 1 (a bare payload, with a non-atomic ``.sha256`` side file) is read
    too — it is written back as schema 2, which is what the server writes now.
    """
    with open(path) as f:
        raw = f.read()
    parsed = json.loads(raw)

    if isinstance(parsed, dict) and "payload" in parsed and "sha256" in parsed:
        actual = hashlib.sha256(parsed["payload"].encode()).hexdigest()
        if actual != parsed["sha256"]:
            msg = f"checksum mismatch (envelope says {str(parsed['sha256'])[:12]}, payload hashes to {actual[:12]})"
            if not force:
                raise SystemExit(
                    f"refusing to edit {path}: {msg}.\n"
                    "The file is corrupt or was written by something else; the server would "
                    "reject it on boot too. Re-run with --force to edit it anyway."
                )
            print(f"WARNING: {msg} — continuing because --force was given", file=sys.stderr)
        return json.loads(parsed["payload"])

    # Legacy schema 1: the payload IS the file.
    print(f"note: {path} is a legacy schema-1 snapshot; it will be written back as schema 2", file=sys.stderr)
    return parsed


def _write_envelope(path: str, snap: dict) -> str:
    """Write the schema-2 envelope and its side file, exactly as
    services/state_snapshot.save_snapshot does: payload and checksum travel
    together inside one atomic os.replace, and the ``.sha256`` side file is
    refreshed after it.  Returns the checksum.
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        st = None

    payload = json.dumps(snap)
    checksum = hashlib.sha256(payload.encode()).hexdigest()
    envelope = json.dumps({"schema": 2, "sha256": checksum, "payload": payload})

    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(envelope)
    _match_owner(tmp, st)
    os.replace(tmp, path)

    sha_path = path + ".sha256"
    sha_tmp = sha_path + ".tmp"
    with open(sha_tmp, "w") as f:
        f.write(checksum)
    _match_owner(sha_tmp, st)
    os.replace(sha_tmp, sha_path)
    return checksum


def _match_owner(path: str, st) -> None:
    """Give a freshly written temp file the mode and owner of the file it is
    about to replace, so editing a container-owned snapshot as root does not
    leave a file the server cannot rewrite."""
    if st is None:
        return
    try:
        os.chmod(path, st.st_mode & 0o7777)
    except OSError:
        pass
    try:
        os.chown(path, st.st_uid, st.st_gid)
    except (OSError, AttributeError):
        pass


def _describe(entry: dict) -> str:
    return (
        f"reputation={entry.get('reputation')!r} blocked={entry.get('blocked')!r} "
        f"block_reason={entry.get('block_reason')!r} penalties={len(entry.get('penalties') or [])}"
    )


def _reset(entry: dict) -> None:
    entry["reputation"] = 1.0
    entry["blocked"] = False
    entry["block_reason"] = ""
    entry["penalties"] = []
    # A condition that is still true (a heartbeat still stale) would not
    # re-fire while its flag says "already active", so a leftover True hides
    # the next onset rather than suppressing it harmlessly.  Clear it: the
    # next evaluator pass observes the condition fresh.
    if "_condition_active" in entry:
        entry["_condition_active"] = {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Reset blocked node reputations in a RETINA state snapshot. "
        "Stop the server first — the save loop rewrites the snapshot every 60 s.",
        epilog="--node takes node_ids (retce36dbb4, synth-GVL-0004), NOT node_refs; "
        "resolve a ref with services.node_refs.id_for_ref inside the container.",
    )
    ap.add_argument(
        "--path",
        default=_DEFAULT_PATH,
        help=f"state snapshot to edit (default: {_DEFAULT_PATH})",
    )
    ap.add_argument(
        "--node",
        action="append",
        default=[],
        metavar="NODE_ID",
        help="node_id to unblock; repeatable",
    )
    ap.add_argument(
        "--all-blocked",
        action="store_true",
        help="unblock every entry whose `blocked` is true",
    )
    ap.add_argument("--dry-run", action="store_true", help="report what would change and write nothing")
    ap.add_argument(
        "--force",
        action="store_true",
        help="edit even if the snapshot fails its own checksum",
    )
    args = ap.parse_args(argv)

    if not args.node and not args.all_blocked:
        ap.error("nothing selected: pass --node NODE_ID (repeatable) and/or --all-blocked")

    if not os.path.exists(args.path):
        print(f"no such snapshot: {args.path}", file=sys.stderr)
        return 2

    snap = _read_envelope(args.path, args.force)
    reps = snap.get("reputations")
    if not isinstance(reps, dict):
        print(f"{args.path} has no `reputations` map — nothing to do", file=sys.stderr)
        return 2

    selected: list[str] = []
    missing: list[str] = []
    for node_id in args.node:
        if node_id in reps:
            selected.append(node_id)
        else:
            missing.append(node_id)
    if args.all_blocked:
        for node_id, entry in reps.items():
            if isinstance(entry, dict) and entry.get("blocked") and node_id not in selected:
                selected.append(node_id)

    for node_id in missing:
        print(f"NOT IN SNAPSHOT: {node_id}", file=sys.stderr)

    if not selected:
        print(f"{args.path}: {len(reps)} reputations, none selected — nothing to change")
        return 1 if missing else 0

    for node_id in selected:
        entry = reps[node_id]
        print(f"{node_id}:")
        print(f"    before: {_describe(entry)}")
        _reset(entry)
        print(f"    after:  {_describe(entry)}")

    if args.dry_run:
        print(f"\n--dry-run: {len(selected)} entries would be reset, {args.path} not written")
        return 1 if missing else 0

    checksum = _write_envelope(args.path, snap)
    print(f"\n{len(selected)} entries reset; wrote {args.path} (sha256={checksum[:12]})")
    print("Start the server now — it restores from this file.")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
