"""
Dashboard blueprint: the ticket list with search, filters, SLA monitoring
and pagination.

Routes:
    /   GET   endpoint "dashboard.index"
"""

import math

from flask import Blueprint, redirect, render_template, request, url_for

import database as db

from . import clock
from .auth import login_required
from .ticket_filters import (
    PRIORITIES,
    SLA_FILTER_KEYS,
    SLA_FILTERS,
    STATUSES,
    current_ticket_filters,
)

bp = Blueprint("dashboard", __name__)

# Number of tickets shown on each dashboard page
TICKETS_PER_PAGE = 10


# ---------------------------------------------------
# PAGINATION HELPERS
# ---------------------------------------------------

def parse_page(value):
    """
    Convert the ?page= value from the URL into a page number.

    Missing, non-numeric, zero and negative values all become page 1,
    so a bad page parameter can never cause an error.
    """

    try:
        page = int(value)
    except (TypeError, ValueError):
        return 1

    return max(1, page)


def page_window(current, total_pages, radius=2):
    """
    Choose which page numbers to show as links.

    Always includes the first and last page, plus `radius` pages either
    side of the current one. None marks a gap, shown as an ellipsis.
    For example, page 10 of 20 gives [1, None, 8, 9, 10, 11, 12, None, 20].
    """

    shown = {1, total_pages}
    shown.update(
        range(
            max(1, current - radius),
            min(total_pages, current + radius) + 1
        )
    )

    pages = []
    previous = 0

    for number in sorted(shown):
        gap = number - previous

        # A gap of exactly one page shows that page, not an ellipsis
        if gap == 2:
            pages.append(previous + 1)
        elif gap > 2:
            pages.append(None)

        pages.append(number)
        previous = number

    return pages


def build_pagination(page, total_items, per_page):
    """Work out the page details the dashboard template displays."""

    # Round up, so 21 tickets at 10 per page needs 3 pages.
    # An empty result still has one (empty) page.
    total_pages = max(1, math.ceil(total_items / per_page))

    if total_items == 0:
        first_item = 0
    else:
        first_item = (page - 1) * per_page + 1

    return {
        "page": page,
        "total_pages": total_pages,
        "total_items": total_items,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "first_item": first_item,
        "last_item": min(page * per_page, total_items),
        "pages": page_window(page, total_pages)
    }


# ---------------------------------------------------
# DASHBOARD WITH SEARCH AND PAGINATION
# ---------------------------------------------------

@bp.route("/")
@login_required
def index():
    """Display one page of the tickets the current user may see."""

    filters = current_ticket_filters()

    search_query = filters["search_text"]
    status_filter = filters["status_filter"]
    priority_filter = filters["priority_filter"]
    sla_filter = filters["sla_filter"]
    owner = filters["username"]

    # One moment in UTC for the whole request. The SLA cards, the SLA
    # filter, the page count and every row's SLA badge are all evaluated
    # at this same time, so they can never disagree with each other.
    now = clock.utc_now()

    # Read the requested page; anything invalid becomes page 1
    page = parse_page(request.args.get("page"))

    # Count matching tickets to find out how many pages exist.
    # The SLA filter is part of the count, so it runs before pagination.
    total_tickets = db.count_tickets(**filters, now=now)

    pagination = build_pagination(
        page,
        total_tickets,
        TICKETS_PER_PAGE
    )

    # Carry the active search and filters into every pagination link.
    # Default values are left out to keep the URLs short.
    link_args = {}

    if search_query:
        link_args["q"] = search_query

    if status_filter != "All":
        link_args["status"] = status_filter

    if priority_filter != "All":
        link_args["priority"] = priority_filter

    if sla_filter != "All":
        link_args["sla"] = SLA_FILTER_KEYS[sla_filter]

    # A page past the end (old bookmark, edited URL) goes to the last page
    if page > pagination["total_pages"]:
        return redirect(
            url_for(
                "dashboard.index",
                page=pagination["total_pages"],
                **link_args
            )
        )

    # Read only the tickets shown on this page, each with its SLA status
    # from the same joined query (no extra query per ticket)
    tickets = db.get_tickets_page(
        **filters,
        page=page,
        per_page=TICKETS_PER_PAGE,
        now=now
    )

    # Statistics cover every ticket the user may see, not just this page
    stats = db.get_stats(username=owner)
    sla_stats = db.get_sla_stats(username=owner, now=now)

    # Display the dashboard
    return render_template(
        "index.html",
        tickets=tickets,
        stats=stats,
        sla_stats=sla_stats,
        status_filter=status_filter,
        priority_filter=priority_filter,
        sla_filter=sla_filter,
        statuses=["All"] + STATUSES,
        priorities=["All"] + PRIORITIES,
        sla_filters=SLA_FILTERS,
        search_query=search_query,
        pagination=pagination,
        link_args=link_args,
        evaluated_at=now
    )
