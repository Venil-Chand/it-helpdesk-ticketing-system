
from datetime import datetime, timedelta, timezone


# Example response SLA targets in calendar hours
SLA_TARGETS = {
    "Critical": 1,
    "High": 4,
    "Medium": 24,
    "Low": 72
}

ALERT_WINDOW = timedelta(minutes=30)

# Support staff roles whose comments count as a response
RESPONDER_ROLES = frozenset({"technician", "admin"})


def is_qualifying_response(author, author_role, ticket_created_by):
    """
    Return True if a comment counts as the ticket's first response.

    Only support staff replying on someone else's ticket qualify. A
    requester commenting on their own ticket never stops the SLA clock,
    even when that requester is a technician or administrator.
    """

    return (
        author_role in RESPONDER_ROLES
        and author != ticket_created_by
    )


def require_aware_datetime(value, name):
    """Reject timestamps without timezone information."""

    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


def calculate_deadline(created_at, priority):
    """Return the UTC response deadline for a ticket."""

    require_aware_datetime(created_at, "created_at")

    if priority not in SLA_TARGETS:
        raise ValueError(f"Unknown priority: {priority}")

    created_utc = created_at.astimezone(timezone.utc)

    deadline = created_utc + timedelta(
        hours=SLA_TARGETS[priority]
    )

    return deadline


def assess_response_sla(deadline, now=None, first_response_at=None):
    """Assess whether the response SLA has been met or breached."""

    require_aware_datetime(deadline, "deadline")

    if now is None:
        now = datetime.now(timezone.utc)

    require_aware_datetime(now, "now")

    deadline_utc = deadline.astimezone(timezone.utc)
    now_utc = now.astimezone(timezone.utc)

    # A qualifying response has already been recorded.
    if first_response_at is not None:
        require_aware_datetime(
            first_response_at, "first_response_at"
        )

        response_utc = first_response_at.astimezone(timezone.utc)

        if response_utc <= deadline_utc:
            return "Met"

        return "Breached"

    # No qualifying response has been recorded yet.
    if now_utc >= deadline_utc:
        return "Overdue"

    remaining = deadline_utc - now_utc

    if remaining <= ALERT_WINDOW:
        return "Due Soon"

    return "Within SLA"
