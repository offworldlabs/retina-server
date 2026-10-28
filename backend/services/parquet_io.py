"""Publish complete, uniquely named Parquet batches for both archive writers."""

import os
import tempfile
import uuid
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def write_parquet_batch(
    table: pa.Table,
    directory: Path,
    write_ts: datetime,
    *,
    row_group_sizes: Sequence[int] | None = None,
) -> str:
    """Return the published filename, preserving the caller's schema and partition.

    A unique suffix keeps same-second flushes from replacing each other. The
    temporary file shares the destination filesystem, so readers and offload
    jobs see a complete .parquet file only after the atomic rename succeeds.

    ``row_group_sizes`` splits the file into consecutive row groups of exactly
    those lengths, in order, so a caller that sorted its rows can give each
    group one value of a column and readers can filter on the footer's
    statistics alone. They must be positive and sum to the table's rows.
    Without it the table is written as pyarrow lays it out.
    """
    if row_group_sizes is not None and (any(n <= 0 for n in row_group_sizes) or sum(row_group_sizes) != table.num_rows):
        raise ValueError(f"row_group_sizes {list(row_group_sizes)} do not partition {table.num_rows} rows")
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"part-{write_ts:%H%M%S}-{uuid.uuid4().hex}.parquet"
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".parquet-", suffix=".tmp", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        if row_group_sizes is None:
            pq.write_table(table, tmp_path, compression="zstd", compression_level=3)
        else:
            _write_row_groups(table, tmp_path, row_group_sizes)
        os.replace(tmp_path, directory / filename)
    finally:
        tmp_path.unlink(missing_ok=True)
    return filename


def _write_row_groups(table: pa.Table, path: Path, sizes: Sequence[int]) -> None:
    # Each write_table call closes its own row group, and row_group_size equal
    # to the slice keeps pyarrow from cutting it into more.
    with pq.ParquetWriter(path, table.schema, compression="zstd", compression_level=3) as writer:
        offset = 0
        for n in sizes:
            writer.write_table(table.slice(offset, n), row_group_size=n)
            offset += n
