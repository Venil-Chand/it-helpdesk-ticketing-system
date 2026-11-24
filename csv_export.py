"""
CSV export helpers for helpdesk tickets.

Python's csv module takes care of commas, quotation marks and line
breaks. It does NOT stop spreadsheet formula injection: Excel will still
evaluate a correctly quoted cell such as "=HYPERLINK(...)". Every text
value therefore goes through safe_cell() before it is written.
"""

import csv
import io

from sla_storage import parse_stored_utc


# (Column heading, ticket field) in the order they appear in the file.
# Only ticket fields are exported: no user accounts, hashes or sessions.
TICKET_COLUMNS = [
    ("Ticket ID", "id"),
    ("Title", "title"),
    ("Description", "description"),
    ("Priority", "priority"),
    ("Status", "status"),
    ("Assigned To", "assigned_to"),
    ("Created By", "created_by"),
    ("Created (UTC)", "created_at"),
    ("Last Updated (UTC)", "updated_at"),
]

# Response SLA columns, added after the ticket columns so that existing
# spreadsheets reading columns by position keep working. The values are
# the dashboard's own SQL results (database.search_tickets with
# include_sla=True), so the file and the dashboard always agree.
SLA_COLUMNS = [
    ("SLA Status", "sla_status"),
    ("Response Due (UTC)", "sla_response_due_at"),
    ("First Response (UTC)", "sla_first_response_at"),
]

EXPORT_COLUMNS = TICKET_COLUMNS + SLA_COLUMNS

# SLA times are written in the same layout as SQLite's CURRENT_TIMESTAMP,
# which the Created and Last Updated columns already use
SLA_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# A cell starting with one of these is treated as a formula by Excel,
# LibreOffice or Google Sheets. The full-width forms are included
# because some applications convert them to the ordinary characters.
FORMULA_TRIGGERS = ("=", "+", "-", "@", "＝", "＋", "－", "＠")

# Characters that could hide a trigger at the start of a value
LEADING_WHITESPACE = " \t\r\n\x00\x0b\x0c "


def safe_cell(value):
    """
    Prepare one value so a spreadsheet shows it as text, never runs it.

    Text that starts with a formula trigger, possibly after whitespace
    or control characters, gets a leading apostrophe, which spreadsheet
    applications treat as "this cell is text".
    """

    if value is None:
        return ""

    # Whole numbers come from the database (ticket IDs), not from users
    if isinstance(value, int) and not isinstance(value, bool):
        return value

    text = str(value)

    starts_with_control = text[:1] in ("\t", "\r", "\n")
    hidden_trigger = text.lstrip(LEADING_WHITESPACE).startswith(
        FORMULA_TRIGGERS
    )

    if starts_with_control or hidden_trigger:
        return "'" + text

    return text


def _row_value(ticket, field):
    """Read a field from a sqlite3.Row or a dict; None if it is absent."""

    return ticket[field] if field in ticket.keys() else None


def sla_cells(ticket):
    """
    The three SLA cells for one ticket row.

    A ticket the dashboard shows as "Not Tracked" (no SLA row, or SLA data
    that cannot be read) gets that status and empty times, just as the
    dashboard shows no deadline for it. Other times are converted to UTC.
    A row without SLA data at all gets three empty cells.
    """

    status = _row_value(ticket, "sla_status")

    if status is None:
        return ["", "", ""]

    if status == "Not Tracked":
        return [status, "", ""]

    times = []

    for field in ("sla_response_due_at", "sla_first_response_at"):
        value = _row_value(ticket, field)

        if value is None:
            times.append("")
            continue

        # The SQL has already checked these are readable. If one somehow
        # is not, it is written as it is (safe_cell still neutralises any
        # formula) rather than making the whole export fail.
        try:
            times.append(parse_stored_utc(value).strftime(SLA_TIME_FORMAT))
        except (TypeError, ValueError):
            times.append(value)

    return [status] + times


def build_tickets_csv(tickets):
    """Return CSV text for the given ticket rows, header row first."""

    output = io.StringIO()

    # The default dialect writes \r\n line endings and quotes any value
    # containing a comma, quotation mark or line break
    writer = csv.writer(output)

    writer.writerow([heading for heading, _ in EXPORT_COLUMNS])

    for ticket in tickets:
        cells = [ticket[field] for _, field in TICKET_COLUMNS]
        cells += sla_cells(ticket)

        writer.writerow([safe_cell(value) for value in cells])

    return output.getvalue()
