"""A single, non-deprecated source of "now" for every timestamp in this repo.

`datetime.utcnow()` is deprecated (Python 3.12+) in favor of timezone-aware
`datetime.now(timezone.utc)`. Every `DateTime` column in `db/models.py` is a
plain (timezone-naive) column, and SQLite always returns naive datetimes
regardless of what's stored -- so this repo stays internally consistent by
computing the *value* through the non-deprecated, timezone-aware API and then
stripping the tzinfo back off, rather than switching to
`DateTime(timezone=True)` columns (a larger, separately-scoped schema change
this fix doesn't need to make).
"""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current UTC time as a naive `datetime` (no tzinfo)."""
    return datetime.now(UTC).replace(tzinfo=None)
