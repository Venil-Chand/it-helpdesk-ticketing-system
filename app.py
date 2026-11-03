
"""
IT Helpdesk Ticketing System

Run locally:
    pip install -r requirements.txt
    python setup_accounts.py
    python app.py

Open:
    http://127.0.0.1:5000

Accounts are managed using setup_accounts.py.
"""

import os

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash
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

# Create the Flask application
app = Flask(__name__)

# Obtain the secret key
_secret = os.environ.get("SECRET_KEY")

if not _secret:
    raise RuntimeError(
        "SECRET_KEY is not set. "
        "Copy .env.example to .env and configure a secret key."
    )

# Configure Flask session security
app.secret_key = _secret

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Enable CSRF protection
csrf = CSRFProtect(app)

# Enable login rate limiting
# In-memory storage is for local development only
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


# ---------------------------------------------------
# AUTHENTICATION HELPERS
# ---------------------------------------------------

def logged_in():
    """Check whether a user is logged in."""
    return "username" in session


def require_login():
    """Redirect unauthenticated visitors to login."""

    if not logged_in():
        flash("Please log in first.", "warning")
        return redirect(url_for("login"))

    return None


def is_tech_or_admin():
    """Check whether the user can manage tickets."""

    return session.get("role") in (
        "technician",
        "admin"
    )


# ---------------------------------------------------
# LOGIN
# ---------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    """Authenticate users with rate limiting."""

    # Already authenticated
    if logged_in():
        return redirect(url_for("index"))

    if request.method == "POST":

        username = request.form.get(
            "username", ""
        ).strip()

        password = request.form.get(
            "password", ""
        )

        # Retrieve user from database
        user = db.get_user(username)

        # Check password against stored hash
        if user and check_password_hash(
            user["password_hash"],
            password
        ):

            # Clear any previous session information
            session.clear()

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
    """End a user session using a CSRF-protected POST."""

    session.clear()

    flash(
        "You have been logged out.",
        "info"
    )

    return redirect(url_for("login"))


# ---------------------------------------------------
# DASHBOARD
# ---------------------------------------------------

@app.route("/")
def index():
    """Display support tickets and dashboard statistics."""

    redir = require_login()

    if redir:
        return redir

    status_filter = request.args.get(
        "status", "All"
    )

    priority_filter = request.args.get(
        "priority", "All"
    )

    tickets = db.get_all_tickets(
        status_filter,
        priority_filter
    )

    stats = db.get_stats()

    return render_template(
        "index.html",
        tickets=tickets,
        stats=stats,
        status_filter=status_filter,
        priority_filter=priority_filter,
        statuses=["All"] + STATUSES,
        priorities=["All"] + PRIORITIES
    )


# ---------------------------------------------------
# CREATE TICKET
# ---------------------------------------------------

@app.route("/tickets/new", methods=["GET", "POST"])
def new_ticket():
    """Create a new IT support request."""

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
    """Show ticket details, comments and assignments."""

    redir = require_login()

    if redir:
        return redir

    ticket = db.get_ticket(ticket_id)

    if not ticket:

        flash(
            f"Ticket #{ticket_id} not found.",
            "danger"
        )

        return redirect(url_for("index"))

    comments = db.get_comments(ticket_id)
    all_users = db.get_all_usernames()

    return render_template(
        "ticket_detail.html",
        ticket=ticket,
        comments=comments,
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

    redir = require_login()

    if redir:
        return redir

    # Restrict updates to technicians and administrators
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

    status = request.form.get("status")

    assigned_to = request.form.get(
        "assigned_to", ""
    ).strip() or None

    # Validate submitted status
    if status not in STATUSES:

        flash(
            "Invalid status.",
            "danger"
        )

    else:

        db.update_ticket(
            ticket_id,
            status,
            assigned_to
        )

        flash(
            "Ticket updated.",
            "success"
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
    """Add a note or comment to a helpdesk ticket."""

    redir = require_login()

    if redir:
        return redir

    content = request.form.get(
        "content", ""
    ).strip()

    if not content:

        flash(
            "Comment cannot be empty.",
            "danger"
        )

    else:

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

    # Ensure database tables exist
    db.init_db()

    print(
        "\nIT Helpdesk running at "
        "http://127.0.0.1:5000\n"
    )

    # Local development only
    # Debug mode remains disabled
    app.run(debug=False)
