"""
Ticket values and filters shared by several blueprints.

The dashboard and the CSV export both call current_ticket_filters(), so
they can never disagree about which tickets a user may see.
"""

from flask import g, request

from .auth import is_tech_or_admin

PRIORITIES = [
    "Low",
    "Medium",
    "High",
    "Critical"
]

STATUSES = [
    "Open",
    "In Progress",
    "Resolved",
    "Closed"
]

# Longest accepted search term, matching the template's maxlength
MAX_SEARCH_LENGTH = 100

# SLA filter values accepted in the URL (?sla=overdue), mapped to the
# status labels the database query produces. This dictionary is an allow
# list: any other ?sla= value is ignored, so it can never reach the SQL.
SLA_FILTERS = {
    "within": "Within SLA",
    "due_soon": "Due Soon",
    "overdue": "Overdue",
    "met": "Met",
    "breached": "Breached",
    "not_tracked": "Not Tracked",
}

# The reverse lookup, label -> URL value, for building links
SLA_FILTER_KEYS = {label: key for key, label in SLA_FILTERS.items()}


def current_ticket_filters():
    """
    Read the search and filters from the URL, and ownership from the account.

    Ownership comes only from the verified account in g.user; URL
    parameters such as ?username= or ?role= are never read.
    """

    # The maxlength attribute in the template is client-side only, so the
    # limit is enforced again here for requests that bypass the browser.
    search_query = request.args.get("q", "").strip()[:MAX_SEARCH_LENGTH]

    # Staff (None) see all tickets; regular users see only their own
    if is_tech_or_admin():
        owner = None
    else:
        owner = g.user.username

    # Unknown or missing ?sla= values mean "no SLA filter"
    sla_filter = SLA_FILTERS.get(request.args.get("sla", ""), "All")

    return {
        "search_text": search_query,
        "username": owner,
        "status_filter": request.args.get("status", "All"),
        "priority_filter": request.args.get("priority", "All"),
        "sla_filter": sla_filter
    }
