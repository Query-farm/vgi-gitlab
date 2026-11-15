"""Type-driven conversion of GitLab JSON values into Arrow columns.

Every conversion is total: one malformed value from one row becomes NULL
instead of failing the batch it arrived in.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

import pyarrow as pa

#: GitLab timestamps are ISO 8601 with ``Z`` or an offset.
TSTZ = pa.timestamp("us", tz="UTC")

_FLOOR = dt.datetime(1678, 1, 1, tzinfo=dt.UTC)
_CEILING = dt.datetime(2262, 1, 1, tzinfo=dt.UTC)


def to_timestamp(value: Any) -> dt.datetime | None:
    """Parse an ISO 8601 string (or pass a datetime) into an aware UTC datetime, else None."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        try:
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    if not _FLOOR <= parsed <= _CEILING:
        return None
    return parsed.astimezone(dt.UTC)


def to_date(value: Any) -> dt.date | None:
    """Parse ``YYYY-MM-DD`` (or the date part of a timestamp), else None."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    """Parse an integer, else None."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def to_float(value: Any) -> float | None:
    """Parse a float, else None."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get(obj: Any, *path: str) -> Any:
    """``obj[path[0]][path[1]]...``, or None as soon as a level is missing."""
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def names(items: Any, *path: str) -> list[str] | None:
    """``[get(item, *path) for item in items]`` over a list of objects, dropping missing values."""
    if not isinstance(items, list):
        return None
    out = []
    for item in items:
        value = get(item, *path) if path else item
        if value is not None:
            out.append(str(value))
    return out


def seconds_between(start: Any, end: Any) -> float | None:
    """Seconds from ``start`` to ``end`` (both ISO strings or datetimes), else None."""
    a, b = to_timestamp(start), to_timestamp(end)
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


def _array(values: list[Any], t: pa.DataType) -> pa.Array[Any]:
    out: pa.Array[Any] = pa.array(values, type=t)
    return out


def _convert(values: list[Any], f: pa.Field[Any]) -> pa.Array[Any]:
    t = f.type
    if pa.types.is_timestamp(t):
        return _array([to_timestamp(v) for v in values], t)
    if pa.types.is_date32(t):
        return _array([to_date(v) for v in values], t)
    if pa.types.is_boolean(t):
        return _array([None if v is None else bool(v) for v in values], t)
    if pa.types.is_integer(t):
        return _array([to_int(v) for v in values], t)
    if pa.types.is_floating(t):
        return _array([to_float(v) for v in values], t)
    if pa.types.is_string(t):
        return _array([None if v is None else str(v) for v in values], t)
    if pa.types.is_list(t) and pa.types.is_string(t.value_type):
        return _array(
            [[str(i) for i in v if i is not None] if isinstance(v, list | tuple) else None for v in values], t
        )
    try:
        return _array(values, t)
    except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError, ValueError):
        safe = []
        for v in values:
            try:
                _array([v], t)
                safe.append(v)
            except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError, ValueError):
                safe.append(None)
        return _array(safe, t)


def batch_from_rows(rows: Sequence[dict[str, Any]], schema: pa.Schema) -> pa.RecordBatch:
    """Build one RecordBatch by pulling each schema field out of ``rows`` by name."""
    return pa.RecordBatch.from_arrays([_convert([r.get(f.name) for r in rows], f) for f in schema], schema=schema)
