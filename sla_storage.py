
from datetime import timezone

from sla import (
    calculate_deadline,
    require_aware_datetime
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
