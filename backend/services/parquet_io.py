"""Publish complete, uniquely named Parquet batches for both archive writers."""

import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def write_parquet_batch(table: pa.Table, directory: Path, write_ts: datetime) -> str:
    """Return the published filename, preserving the caller's schema and partition.

    A unique suffix keeps same-second flushes from replacing each other. The
    temporary file shares the destination filesystem, so readers and offload
    jobs see a complete .parquet file only after the atomic rename succeeds.
    """
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"part-{write_ts:%H%M%S}-{uuid.uuid4().hex}.parquet"
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".parquet-", suffix=".tmp", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        pq.write_table(table, tmp_path, compression="zstd", compression_level=3)
        os.replace(tmp_path, directory / filename)
    finally:
        tmp_path.unlink(missing_ok=True)
    return filename
