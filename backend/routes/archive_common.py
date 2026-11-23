"""Query-parameter rules the public archive routes share: the fleet filter, the
time instants and the bounding box, each answered with a 422 when malformed."""

import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import HTTPException

from services import tracks_store

Fleet = Literal["real", "synthetic", "all"]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_EPOCH_MS = re.compile(r"-?[0-9]{1,15}")
_NUMBER = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")


def invalid_parameter(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def parse_instant_ms(value: str, name: str) -> int:
    """Epoch ms from integer ms or ISO-8601 (Z or an offset; none means UTC; a date is its midnight)."""
    text = value.strip()
    try:
        if _EPOCH_MS.fullmatch(text):
            moment = _EPOCH + timedelta(milliseconds=int(text))
        else:
            moment = datetime.fromisoformat(text)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
        # Rounded up: the first whole millisecond at or after the instant.
        return -((_EPOCH - moment) // timedelta(milliseconds=1))
    except (ValueError, OverflowError):
        raise invalid_parameter(f"{name} must be epoch milliseconds or an ISO-8601 time") from None


def parse_bbox(value: str) -> tracks_store.BBox:
    parts = value.split(",")
    message = "bbox must be south,west,north,east in degrees, with south <= north"
    if len(parts) != 4 or not all(_NUMBER.fullmatch(part.strip()) for part in parts):
        raise invalid_parameter(message)
    south, west, north, east = (float(part) for part in parts)
    # NaN and infinities fail these comparisons too.
    if not (-90 <= south <= north <= 90 and -180 <= west <= 180 and -180 <= east <= 180):
        raise invalid_parameter(message)
    return tracks_store.BBox(south, west, north, east)
