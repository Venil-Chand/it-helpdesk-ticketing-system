
"""
IT Helpdesk Ticketing System

Run locally:
    pip install -r requirements.txt
    python setup_accounts.py
    python app.py

Open in browser:
    http://127.0.0.1:5000

User accounts are managed using setup_accounts.py.
"""

import math
import os

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    abort
)

from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import check_password_hash
from dotenv import load_dotenv

import database as db


# ---------------------------------------------------
# APPLICATION CONFIGURATION
# ---------------------------------------------------

# Load environment variables from .env
load_dotenv()

# Create Flask application
app = Flask(__name__)

# Read secret key
_secret = os.environ.get("SECRET_KEY")

# Require a valid secret key configuration
if not _secret:
    raise RuntimeError(
        "SECRET_KEY is not set. "
        "Copy .env.example to .env and configure a secret key."
    )

# Configure Flask sessions
app.secret_key = _secret

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Enable CSRF protection
csrf = CSRFProtect(app)

# Enable rate limiting
# In-memory storage is for local development
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri="memory://"
)


# ---------------------------------------------------
# APPLICATION CONSTANTS
# ---------------------------------------------------

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

# Number of tickets shown on each dashboard page
TICKETS_PER_PAGE = 10


# ---------------------------------------------------
# AUTHENTICATION AND AUTHORIZATION HELPERS
# ---------------------------------------------------

def logged_in():
    """Check whether the user is logged in."""

    return "username" in session


def require_login():
    """Redirect unauthenticated visitors to login."""

    if not logged_in():

        flash(
            "Please log in first.",
            "warning"
        )

        return redirect(url_for("login"))

    return None


def is_tech_or_admin():
    """Check whether the user has a support staff role."""

    return session.get("role") in (
        "technician",
        "admin"
    )


def can_view_ticket(ticket):
    """Check whether the current user may access a ticket."""

    # A nonexistent ticket cannot be viewed
    if ticket is None:
        return False

    # Administrators and technicians can view all tickets
    if is_tech_or_admin():
        return True

    # Regular users can view only their own tickets
    return ticket["created_by"] == session.get("username")


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
# LOGIN
# ---------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    """Authenticate users with login rate limiting."""

    # Redirect users who are already logged in
    if logged_in():
        return redirect(url_for("index"))

    if request.method == "POST":

        username = request.form.get(
            "username", ""
        ).strip()

        password = request.form.get(
            "password", ""
        )

        # Find the user in the database
        user = db.get_user(username)

        # Verify the password hash
        if user and check_password_hash(
            user["password_hash"],
            password
        ):

            # Clear previous session data
            session.clear()

            # Store authenticated user information
            session["username"] = user["username"]
            session["role"] = user["role"]

            flash(
                f"Welcome back, {user['username']}!",
                "success"
            )

            return redirect(url_for("index"))

        flash(
            "Invalid username or password.",
            "danger"
        )

    return render_template("login.html")


# ---------------------------------------------------
# SECURE LOGOUT
# ---------------------------------------------------

@app.route("/logout", methods=["POST"])
def logout():
    """End the current session using a CSRF-protected POST."""

    session.clear()

    flash(
        "You have been logged out.",
        "info"
    )

    return redirect(url_for("login"))


# ---------------------------------------------------
# DASHBOARD WITH SEARCH AND PAGINATION
# ---------------------------------------------------

@app.route("/")
def index():
    """Display one page of the tickets the current user may see."""

    # Check whether the user is logged in
    redir = require_login()

    if redir:
        return redir

    # Read search text from the URL.
    # The maxlength attribute in the template is client-side only, so the
    # limit is enforced again here for requests that bypass the browser.
    search_query = request.args.get(
        "q", ""
    ).strip()[:MAX_SEARCH_LENGTH]

    # Read existing dashboard filters
    status_filter = request.args.get(
        "status", "All"
    )

    priority_filter = request.args.get(
        "priority", "All"
    )

    # Read the requested page; anything invalid becomes page 1
    page = parse_page(request.args.get("page"))

    # Decide ownership once and reuse it for every query below.
    # Staff (None) see all tickets; regular users see only their own.
    if is_tech_or_admin():
        owner = None
    else:
        owner = session["username"]

    filters = {
        "search_text": search_query,
        "username": owner,
        "status_filter": status_filter,
        "priority_filter": priority_filter
    }

    # Count matching tickets to find out how many pages exist
    total_tickets = db.count_tickets(**filters)

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

    # A page past the end (old bookmark, edited URL) goes to the last page
    if page > pagination["total_pages"]:
        return redirect(
            url_for(
                "index",
                page=pagination["total_pages"],
                **link_args
            )
        )

    # Read only the tickets shown on this page
    tickets = db.get_tickets_page(
        **filters,
        page=page,
        per_page=TICKETS_PER_PAGE
    )

    # Statistics cover every ticket the user may see, not just this page
    stats = db.get_stats(username=owner)

    # Display the dashboard
    return render_template(
        "index.html",
        tickets=tickets,
        stats=stats,
        status_filter=status_filter,
        priority_filter=priority_filter,
        statuses=["All"] + STATUSES,
        priorities=["All"] + PRIORITIES,
        search_query=search_query,
        pagination=pagination,
        link_args=link_args
    )




# ---------------------------------------------------
# CREATE NEW TICKET
# ---------------------------------------------------

@app.route("/tickets/new", methods=["GET", "POST"])
def new_ticket():
    """Create a new IT support ticket."""

    redir = require_login()

    if redir:
        return redir

    if request.method == "POST":

        title = request.form.get(
            "title", ""
        ).strip()

        description = request.form.get(
            "description", ""
        ).strip()

        priority = request.form.get(
            "priority", "Medium"
        )

        # Validate required fields
        if not title or not description:

            flash(
                "Title and description are required.",
                "danger"
            )

        elif priority not in PRIORITIES:

            flash(
                "Invalid priority.",
                "danger"
            )

        else:

            # Create a ticket with the current username
            ticket_id = db.create_ticket(
                title,
                description,
                priority,
                session["username"]
            )

            flash(
                f"Ticket #{ticket_id} created successfully!",
                "success"
            )

            return redirect(
                url_for(
                    "ticket_detail",
                    ticket_id=ticket_id
                )
            )

    return render_template(
        "new_ticket.html",
        priorities=PRIORITIES
    )


# ---------------------------------------------------
# VIEW TICKET DETAILS
# ---------------------------------------------------

@app.route("/tickets/<int:ticket_id>")
def ticket_detail(ticket_id):
    """Display a ticket only if the user is authorised."""

    # Step 1: Check login
    redir = require_login()

    if redir:
        return redir

    # Step 2: Retrieve the ticket
    ticket = db.get_ticket(ticket_id)

    # Step 3: Check whether the ticket exists
    if ticket is None:

        abort(404)

    # Step 4: Check ticket ownership or staff access
    if not can_view_ticket(ticket):

        abort(404)

    # Step 5: Retrieve comments and activity history.
    # This runs only after the access check above, so history is
    # protected by exactly the same rule as the ticket itself.
    comments = db.get_comments(ticket_id)
    history = db.get_ticket_history(ticket_id)

    # Only support staff need the assignment list
    if is_tech_or_admin():

        all_users = db.get_all_usernames()

    else:

        all_users = []

    # Step 6: Display ticket details
    return render_template(
        "ticket_detail.html",
        ticket=ticket,
        comments=comments,
        history=history,
        statuses=STATUSES,
        all_users=all_users
    )


# ---------------------------------------------------
# UPDATE TICKET
# ---------------------------------------------------

@app.route(
    "/tickets/<int:ticket_id>/update",
    methods=["POST"]
)
def update_ticket(ticket_id):
    """Update ticket status and technician assignment."""

    # Check login
    redir = require_login()

    if redir:
        return redir

    # Only administrators and technicians may update tickets
    if not is_tech_or_admin():

        flash(
            "Only technicians and admins can update ticket status.",
            "danger"
        )

        return redirect(
            url_for(
                "ticket_detail",
                ticket_id=ticket_id
            )
        )

    # Read submitted data
    status = request.form.get("status")

    assigned_to = request.form.get(
        "assigned_to", ""
    ).strip() or None

    # Validate ticket status
    if status not in STATUSES:

        flash(
            "Invalid status.",
            "danger"
        )

    else:

        # Update the ticket and record each change in one transaction.
        # The actor always comes from the login session, never the form.
        changes = db.update_ticket(
            ticket_id,
            status,
            assigned_to,
            actor=session["username"]
        )

        if changes is None:
            abort(404)

        if changes:
            flash(
                "Ticket updated.",
                "success"
            )
        else:
            flash(
                "No changes to save.",
                "info"
            )

    return redirect(
        url_for(
            "ticket_detail",
            ticket_id=ticket_id
        )
    )


# ---------------------------------------------------
# ADD COMMENT
# ---------------------------------------------------

@app.route(
    "/tickets/<int:ticket_id>/comment",
    methods=["POST"]
)
def add_comment(ticket_id):
    """Add a comment only to an authorised ticket."""

    # Step 1: Check login
    redir = require_login()

    if redir:
        return redir

    # Step 2: Retrieve the ticket
    ticket = db.get_ticket(ticket_id)

    # Step 3: Check that ticket exists
    if ticket is None:

        abort(404)

    # Step 4: Verify access permissions
    if not can_view_ticket(ticket):

        abort(404)

    # Step 5: Read submitted comment
    content = request.form.get(
        "content", ""
    ).strip()

    if not content:

        flash(
            "Comment cannot be empty.",
            "danger"
        )

    else:

        # Save the comment
        db.add_comment(
            ticket_id,
            session["username"],
            content
        )

        flash(
            "Note added.",
            "success"
        )

    return redirect(
        url_for(
            "ticket_detail",
            ticket_id=ticket_id
        )
    )


# ---------------------------------------------------
# START APPLICATION
# ---------------------------------------------------

if __name__ == "__main__":

    # Create database tables if needed
    db.init_db()

    print(
        "\nIT Helpdesk running at "
        "http://127.0.0.1:5000\n"
    )

    # Disable debug mode
    app.run(debug=False)
