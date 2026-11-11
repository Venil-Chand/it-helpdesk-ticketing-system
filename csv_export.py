"""
CSV export helpers for helpdesk tickets.

Python's csv module takes care of commas, quotation marks and line
breaks. It does NOT stop spreadsheet formula injection: Excel will still
evaluate a correctly quoted cell such as "=HYPERLINK(...)". Every text
value therefore goes through safe_cell() before it is written.
"""

import csv
import io


# (Column heading, ticket field) in the order they appear in the file.
# Only ticket fields are exported: no user accounts, hashes or sessions.
EXPORT_COLUMNS = [
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


def build_tickets_csv(tickets):
    """Return CSV text for the given ticket rows, header row first."""

    output = io.StringIO()

    # The default dialect writes \r\n line endings and quotes any value
    # containing a comma, quotation mark or line break
    writer = csv.writer(output)

    writer.writerow([heading for heading, _ in EXPORT_COLUMNS])

    for ticket in tickets:
        writer.writerow([
            safe_cell(ticket[field]) for _, field in EXPORT_COLUMNS
        ])

    return output.getvalue()
