
import re
from datetime import datetime, timezone

from sla import (
    assess_response_sla,
    calculate_deadline,
    require_aware_datetime
)


# ---------------------------------------------------
# THE ONE ACCEPTED STORED TIMESTAMP FORMAT
# ---------------------------------------------------
#
#   YYYY-MM-DD HH:MM:SS          (T or a space between date and time)
#   optional .f, .ff or .fff     (up to milliseconds)
#   optional Z or +HH:MM/-HH:MM  (offset up to 14:59; none means UTC)
#
# This covers everything the application writes (SQLite CURRENT_TIMESTAMP
# and datetime.isoformat() in UTC) plus common hand-entered variants.
# Python's fromisoformat() and SQLite's julianday() each accept extra,
# different forms (such as "20261117T090000" or "2026-02-30"), which made
# the ticket page and the dashboard disagree about damaged rows. Both
# sides now accept exactly this format:
#   Python  _STORED_TIMESTAMP below, then fromisoformat()
#   SQL     STORED_TIMESTAMP_GLOBS, used by database._readable_timestamp_sql
# Milliseconds are the limit because julianday() rounds to milliseconds;
# more digits would let the two sides compare times differently.

_STORED_TIMESTAMP = re.compile(
    r"[1-9]\d{3}-\d{2}-\d{2}"
    r"[T ](?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:\.\d{1,3})?"
    r"(?:Z|[+-](?:0\d|1[0-4]):[0-5]\d)?",
    re.ASCII
)

_GLOB_DATE_TIME = (
    "[1-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
    "[T ][0-9][0-9]:[0-5][0-9]:[0-5][0-9]"
)

STORED_TIMESTAMP_GLOBS = tuple(
    _GLOB_DATE_TIME + fraction + offset
    for fraction in ("", ".[0-9]", ".[0-9][0-9]", ".[0-9][0-9][0-9]")
    for offset in (
        "", "Z", "[+-]0[0-9]:[0-5][0-9]", "[+-]1[0-4]:[0-5][0-9]"
    )
)


def initialize_sla_schema(connection):
    """Create the SLA table if it does not exist."""

    connection.execute("""
        CREATE TABLE IF NOT EXISTS ticket_sla (
            ticket_id INTEGER PRIMARY KEY,
            response_due_at TEXT NOT NULL,
            first_response_at TEXT,
            policy_version TEXT NOT NULL
                DEFAULT 'calendar-hours-v1',

            FOREIGN KEY (ticket_id)
                REFERENCES tickets(id)
        )
    """)


def parse_stored_utc(value):
    """
    Convert a timestamp read from SQLite into an aware UTC datetime.

    SQLite's CURRENT_TIMESTAMP is UTC but has no offset, for example
    "2026-11-17 08:00:00", so text without an offset is treated as UTC.
    Text with an offset is converted to UTC. Text outside the accepted
    format (see _STORED_TIMESTAMP) raises ValueError, and a non-text
    value raises TypeError.
    """

    if not isinstance(value, str):
        raise TypeError("stored timestamp must be text")

    if not _STORED_TIMESTAMP.fullmatch(value):
        raise ValueError("stored timestamp is not in the accepted format")

    # fromisoformat() also rejects impossible dates such as 30 February
    parsed = datetime.fromisoformat(value)

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=timezone.utc)

    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError:
        # e.g. 9999-12-31T23:00:00-05:00 is after the last datetime
        raise ValueError("stored timestamp is out of range") from None


def get_ticket_sla(connection, ticket_id):
    """Retrieve SLA information for one ticket."""

    cursor = connection.execute("""
        SELECT ticket_id,
               response_due_at,
               first_response_at,
               policy_version
        FROM ticket_sla
        WHERE ticket_id = ?
    """, (ticket_id,))

    row = cursor.fetchone()

    if row is None:
        return None

    # Do not change the connection's row_factory.
    column_names = [
        column[0] for column in cursor.description
    ]

    return dict(zip(column_names, row))


def create_ticket_sla(
    connection, ticket_id, priority, created_at
):
    """Insert an SLA deadline for an existing ticket."""

    deadline = calculate_deadline(
        created_at,
        priority
    )

    connection.execute("""
        INSERT INTO ticket_sla (
            ticket_id,
            response_due_at,
            policy_version
        )
        VALUES (?, ?, ?)
    """, (
        ticket_id,
        deadline.isoformat(),
        "calendar-hours-v1"
    ))

    return deadline


def record_first_response(
    connection, ticket_id, responded_at
):
    """Record the first qualifying response only once."""

    require_aware_datetime(
        responded_at,
        "responded_at"
    )

    response_utc = responded_at.astimezone(
        timezone.utc
    )

    cursor = connection.execute("""
        UPDATE ticket_sla
        SET first_response_at = ?
        WHERE ticket_id = ?
          AND first_response_at IS NULL
    """, (
        response_utc.isoformat(),
        ticket_id
    ))

    return cursor.rowcount == 1


def get_ticket_sla_status(connection, ticket_id, now=None):
    """Retrieve a ticket's SLA and calculate its current status."""

    # Get the SLA information stored in SQLite.
    record = get_ticket_sla(connection, ticket_id)

    # Some historical tickets may have no SLA record.
    if record is None:
        return None

    # Convert stored deadline text into a UTC datetime.
    deadline = parse_stored_utc(record["response_due_at"])

    # Get the first qualifying response timestamp, if present.
    first_response = None

    if record["first_response_at"] is not None:
        first_response = parse_stored_utc(
            record["first_response_at"]
        )

    # Calculate the SLA status.
    status = assess_response_sla(
        deadline,
        now=now,
        first_response_at=first_response
    )

    return {
        **record,
        "status": status
    }
