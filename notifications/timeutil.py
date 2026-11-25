"""
The notification queue's clock and its one timestamp format.

Every queue timestamp is written as UTC text with milliseconds and an
explicit offset, for example "2026-11-25T09:30:00.000+00:00". All values
have the same width and offset, so SQLite can compare them as text and
use the indexes on them. Tests freeze time by replacing utc_now().
"""

from datetime import datetime, timezone


def utc_now():
    """The current moment as a timezone-aware UTC datetime."""

    return datetime.now(timezone.utc)


def format_utc(moment):
    """Stored text for an aware datetime. Naive datetimes are refused."""

    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("notification timestamps must be timezone-aware")

    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def parse_utc(text):
    """An aware UTC datetime from text written by format_utc()."""

    moment = datetime.fromisoformat(text)

    if moment.tzinfo is None:
        raise ValueError("stored notification timestamp has no offset")

    return moment.astimezone(timezone.utc)
