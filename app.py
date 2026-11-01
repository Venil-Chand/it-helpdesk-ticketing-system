
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

# Ensure the secret key exists
if not _secret:
    raise RuntimeError(
        "SECRET_KEY is not set. "
        "Copy .env.example to .env and add a real key: "
        "python3 -c \"import secrets; print(secrets.token_hex(32))\""
    )

# Configure Flask secret key
app.secret_key = _secret

# Enable CSRF protection
csrf = CSRFProtect(app)


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
    """Check whether the user is logged in."""
    return "username" in session


def require_login():
    """Redirect unauthenticated users to the login page."""

    if not logged_in():
        flash("Please log in first.", "warning")
        return redirect(url_for("login"))

    return None


def is_tech_or_admin():
    """Check whether the user has technician or admin access."""

    return session.get("role") in (
        "technician",
        "admin"
    )


# ---------------------------------------------------
# LOGIN
# ---------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    """Authenticate a user."""

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
            user["password_hash"], password
        ):

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
# LOGOUT
# ---------------------------------------------------

@app.route("/logout")
def logout():
    """Log out the current user."""

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
    """Display the helpdesk dashboard."""

    redir = require_login()

    if redir:
        return redir

    # Read selected filters
    status_filter = request.args.get(
        "status", "All"
    )

    priority_filter = request.args.get(
        "priority", "All"
    )

    # Retrieve tickets and statistics
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

            # Save the ticket
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
    """Display a ticket and its comments."""

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

    # Retrieve comments and usernames
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

    # Check user permissions
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

    # Retrieve submitted values
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

        # Update database
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
    """Add a comment to a support ticket."""

    redir = require_login()

    if redir:
        return redir

    content = request.form.get(
        "content", ""
    ).strip()

    # Validate comment content
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

    # Create missing database tables
    db.init_db()

    print(
        "\nIT Helpdesk running at "
        "http://127.0.0.1:5000\n"
    )

    # Debug mode disabled by default
    app.run(debug=False)
