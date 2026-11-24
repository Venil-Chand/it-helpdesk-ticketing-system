"""
Tickets blueprint: creating, viewing, updating and commenting on tickets.

Routes:
    /tickets/new                     GET, POST  endpoint "tickets.new_ticket"
    /tickets/<int:ticket_id>         GET        endpoint "tickets.ticket_detail"
    /tickets/<int:ticket_id>/update  POST       endpoint "tickets.update_ticket"
    /tickets/<int:ticket_id>/comment POST       endpoint "tickets.add_comment"

Ownership rule: staff (technicians and admins) may see every ticket;
everyone else only the tickets they created. Other people's tickets get
the same 404 as tickets that do not exist.
"""

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

import database as db
import validation

from . import clock
from .auth import (
    is_positive_integer,
    is_tech_or_admin,
    logged_in,
    login_required,
    staff_required,
)
from .ticket_filters import PRIORITIES, STATUSES

bp = Blueprint("tickets", __name__)


# ---------------------------------------------------
# TICKET ACCESS
# ---------------------------------------------------

def can_view_ticket(ticket):
    """Check whether the current user may access a ticket."""

    # A nonexistent ticket cannot be viewed
    if ticket is None:
        return False

    # Administrators and technicians can view all tickets
    if is_tech_or_admin():
        return True

    # Regular users can view only their own tickets
    return logged_in() and ticket["created_by"] == g.user.username


def get_ticket_or_404(ticket_id):
    """
    Load a ticket the current user may access, or stop with a 404.

    Missing tickets and other people's tickets get the same response, so
    ticket numbers cannot be probed to learn which ones exist.
    """

    # SQLite cannot look up a number this large, and no such ticket can exist
    if not is_positive_integer(ticket_id):
        abort(404)

    ticket = db.get_ticket(ticket_id)

    if not can_view_ticket(ticket):
        abort(404)

    return ticket


# ---------------------------------------------------
# CREATE NEW TICKET
# ---------------------------------------------------

def render_new_ticket_form(title="", description="", priority="Medium",
                           status=200):
    """Show the form, keeping what the user typed if it was rejected."""

    return render_template(
        "new_ticket.html",
        priorities=PRIORITIES,
        draft_title=title,
        draft_description=description,
        draft_priority=priority if priority in PRIORITIES else "Medium",
        max_title_length=validation.MAX_TITLE_LENGTH,
        max_description_length=validation.MAX_DESCRIPTION_LENGTH
    ), status


@bp.route("/tickets/new", methods=["GET", "POST"])
@login_required
def new_ticket():
    """Create a new IT support ticket."""

    if request.method == "POST":

        title = validation.clean_text(
            request.form.get("title", ""),
            label="Title",
            max_length=validation.MAX_TITLE_LENGTH,
            multiline=False
        )

        description = validation.clean_text(
            request.form.get("description", ""),
            label="Description",
            max_length=validation.MAX_DESCRIPTION_LENGTH,
            multiline=True
        )

        priority = request.form.get(
            "priority", "Medium"
        )

        # Too long or invalid: nothing is saved or shortened, and the user
        # gets their text back to correct it
        errors = [item.error for item in (title, description) if item.error]

        if errors:

            for message in errors:
                flash(message, "danger")

            return render_new_ticket_form(
                title.value,
                description.value,
                priority,
                status=400
            )

        # Validate required fields
        if not title.value or not description.value:

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
                title.value,
                description.value,
                priority,
                g.user.username
            )

            flash(
                f"Ticket #{ticket_id} created successfully!",
                "success"
            )

            return redirect(
                url_for(
                    "tickets.ticket_detail",
                    ticket_id=ticket_id
                )
            )

        return render_new_ticket_form(
            title.value,
            description.value,
            priority
        )

    return render_new_ticket_form()


# ---------------------------------------------------
# VIEW TICKET DETAILS
# ---------------------------------------------------

def render_ticket_page(ticket, comment_draft="", status=200):
    """
    Show a ticket the caller has already authorised.

    A rejected note is passed back as comment_draft so it is not lost.
    """

    ticket_id = ticket["id"]

    # These run only after the caller's access check, so history is
    # protected by exactly the same rule as the ticket itself.
    comments = db.get_comments(ticket_id)
    history = db.get_ticket_history(ticket_id)

    # Response SLA, or None for tickets created before SLA tracking.
    # Also loaded only after the access check. The time comes from the
    # same clock the dashboard and CSV export use.
    sla = db.get_ticket_sla_status(ticket_id, now=clock.utc_now())

    # Only support staff need the assignment list: active technicians and
    # admins only, so the form never offers an account that would be
    # refused. If the current assignee no longer qualifies, the page says
    # so and the form keeps that name selected, so saving a status change
    # never unassigns the ticket by accident.
    assignee_problem = None

    if is_tech_or_admin():

        all_users = db.get_assignable_users()

        if ticket["assigned_to"]:
            assignee_problem = db.get_assignee_problem(ticket["assigned_to"])

    else:

        all_users = []

    return render_template(
        "ticket_detail.html",
        ticket=ticket,
        comments=comments,
        history=history,
        sla=sla,
        statuses=STATUSES,
        all_users=all_users,
        assignee_problem=assignee_problem,
        comment_draft=comment_draft,
        max_comment_length=validation.MAX_COMMENT_LENGTH
    ), status


@bp.route("/tickets/<int:ticket_id>")
@login_required
def ticket_detail(ticket_id):
    """Display a ticket only if the user is authorised."""

    return render_ticket_page(get_ticket_or_404(ticket_id))


# ---------------------------------------------------
# UPDATE TICKET
# ---------------------------------------------------

@bp.route(
    "/tickets/<int:ticket_id>/update",
    methods=["POST"]
)
@staff_required
def update_ticket(ticket_id):
    """Update ticket status and technician assignment."""

    # SQLite cannot look up a number this large, and no such ticket can exist
    if not is_positive_integer(ticket_id):
        abort(404)

    # Read submitted data
    status = request.form.get("status")

    assignee = validation.clean_text(
        request.form.get("assigned_to", ""),
        label="Assignee",
        max_length=validation.MAX_USERNAME_LENGTH,
        multiline=False
    )

    assigned_to = assignee.value or None

    # The form only offers valid names, so this is a forged request. The
    # current value stays accepted so an older ticket can still be updated.
    if assignee.error and (
        assigned_to != get_ticket_or_404(ticket_id)["assigned_to"]
    ):
        abort(400)

    # Validate ticket status
    if status not in STATUSES:

        flash(
            "Invalid status.",
            "danger"
        )

    else:

        # Update the ticket and record each change in one transaction.
        # The actor is always the verified account, never a form value.
        # The database checks a new assignee inside that transaction.
        try:
            changes = db.update_ticket(
                ticket_id,
                status,
                assigned_to,
                actor=g.user.username
            )

        except db.InvalidAssigneeError as error:
            # Nothing was saved, not even the status. The message is the
            # same for every reason, so the form cannot be used to learn
            # which accounts exist or are deactivated.
            current_app.logger.warning(
                "Refused assignment of ticket %s: %s",
                ticket_id, error.reason
            )

            flash(
                "Ticket not updated: tickets can only be assigned to an "
                "active technician or administrator.",
                "danger"
            )

        else:
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
            "tickets.ticket_detail",
            ticket_id=ticket_id
        )
    )


# ---------------------------------------------------
# ADD COMMENT
# ---------------------------------------------------

@bp.route(
    "/tickets/<int:ticket_id>/comment",
    methods=["POST"]
)
@login_required
def add_comment(ticket_id):
    """Add a comment only to an authorised ticket."""

    # Step 1: Stop unless this user may access the ticket
    ticket = get_ticket_or_404(ticket_id)

    # Step 2: Read and check the submitted comment
    content = validation.clean_text(
        request.form.get("content", ""),
        label="Note",
        max_length=validation.MAX_COMMENT_LENGTH,
        multiline=True
    )

    if content.error:

        flash(content.error, "danger")

        return render_ticket_page(
            ticket,
            comment_draft=content.value,
            status=400
        )

    if not content.value:

        flash(
            "Comment cannot be empty.",
            "danger"
        )

    else:

        # Save the comment
        db.add_comment(
            ticket_id,
            g.user.username,
            content.value
        )

        flash(
            "Note added.",
            "success"
        )

    return redirect(
        url_for(
            "tickets.ticket_detail",
            ticket_id=ticket_id
        )
    )
