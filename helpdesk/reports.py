"""
Reports blueprint: downloadable exports of ticket data.

Routes:
    /tickets/export   GET   endpoint "reports.export_tickets"
"""

from flask import Blueprint, Response

import database as db
from csv_export import build_tickets_csv

from . import clock
from .auth import login_required
from .ticket_filters import current_ticket_filters

bp = Blueprint("reports", __name__)


@bp.route("/tickets/export")
@login_required
def export_tickets():
    """Download every ticket the user may see that matches the filters."""

    filters = current_ticket_filters()

    # One UTC moment for the SLA filter and the filename timestamp
    now = clock.utc_now()

    # All matching tickets, not one dashboard page. Ownership is part of
    # the SQL WHERE clause, so other users' tickets are never loaded.
    # An ?sla= filter selects which tickets are exported, and each row's
    # SLA columns come from the same SQL as the dashboard, both evaluated
    # at `now`, so the file always matches what the dashboard showed.
    tickets = db.search_tickets(**filters, include_sla=True, now=now)

    csv_text = build_tickets_csv(tickets)

    # The filename is generated here, never taken from the request
    timestamp = now.strftime("%Y%m%d-%H%M%S")
    filename = f"helpdesk-tickets-{timestamp}.csv"

    return Response(
        # utf-8-sig adds a byte order mark so Excel reads UTF-8 correctly
        csv_text.encode("utf-8-sig"),
        content_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Ticket data must not be kept by shared caches or proxies
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff"
        }
    )
