
# IT Helpdesk Ticketing System
# Main Flask Application

# Import Flask tools
from flask import Flask, render_template, request, redirect, url_for, abort

# Import database functions
from database import get_tickets, add_ticket, update_ticket_status, assign_ticket, get_ticket, add_comment, get_comments

# Create the Flask application
app = Flask(__name__)


# --------------------------------------------------
# 1. HOMEPAGE
# --------------------------------------------------

@app.route("/")
def home():

    return """
    <h1>IT Helpdesk Ticketing System</h1>
    <p>Welcome to the IT Helpdesk!</p>
    <p>Manage and track IT support tickets.</p>
    <a href="/tickets">View Tickets</a>
    """


# --------------------------------------------------
# 2. DISPLAY ALL TICKETS
# --------------------------------------------------

@app.route("/tickets")
def tickets():

    # Retrieve all tickets from SQLite
    all_tickets = get_tickets()

    # Display tickets using the HTML template
    return render_template("index.html", tickets=all_tickets)


# --------------------------------------------------
# 3. CREATE A NEW TICKET
# --------------------------------------------------

@app.route("/tickets/new", methods=["GET", "POST"])
def new_ticket():

    # Check whether the user submitted the form
    if request.method == "POST":

        # Get information entered by the user
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        priority = request.form.get("priority", "Medium")

        # Validate the submitted information
        if (
            not title
            or not description
            or len(title) > 120
            or len(description) > 2000
            or priority not in ("Low", "Medium", "High", "Critical")
        ):

            return render_template(
                "new_ticket.html",
                error="Please enter valid ticket details.",
                title=title,
                description=description,
                priority=priority
            ), 400

        # Save the new ticket in SQLite
        add_ticket(title, description, priority)

        # Return to the tickets dashboard
        return redirect(url_for("tickets"))

    # Display the empty ticket form
    return render_template(
        "new_ticket.html",
        error=None,
        title="",
        description="",
        priority="Medium"
    )


# --------------------------------------------------
# 4. UPDATE TICKET STATUS
# --------------------------------------------------

@app.route("/tickets/<int:ticket_id>/status", methods=["POST"])
def change_status(ticket_id):

    # Get the status selected from the webpage
    new_status = request.form.get("status", "")

    try:

        # Update the ticket status in SQLite
        updated = update_ticket_status(ticket_id, new_status)

    except ValueError:

        # Handle invalid ticket status
        return "Invalid ticket status", 400

    # Check whether the ticket exists
    if not updated:
        return "Ticket not found", 404

    # Return to the tickets dashboard
    return redirect(url_for("tickets"))




# Assign a ticket to an IT technician
@app.route("/tickets/<int:ticket_id>/assign", methods=["POST"])
def assign_technician(ticket_id):

    # Get the selected technician from the HTML form
    technician = request.form.get("technician", "")

    try:
        # Save the technician assignment in SQLite
        updated = assign_ticket(ticket_id, technician)

    except ValueError:
        return "Invalid technician selection", 400

    # Check whether the ticket exists
    if not updated:
        return "Ticket not found", 404

    # Return to the tickets dashboard
    return redirect(url_for("tickets"))





# Display the details of a specific ticket
@app.route("/tickets/<int:ticket_id>", methods=["GET", "POST"])
def ticket_detail(ticket_id):

    # Retrieve the selected ticket from SQLite
    ticket = get_ticket(ticket_id)

    # Show a 404 error if the ticket does not exist
    if ticket is None:
        abort(404)

    error = None

    # Check whether a technician submitted a comment
    if request.method == "POST":

        # Retrieve information from the HTML form
        author = request.form.get("author", "").strip()
        note = request.form.get("note", "").strip()

        # Validate the submitted information
        if (
            not author
            or not note
            or len(author) > 80
            or len(note) > 2000
        ):
            error = "Please enter valid comment details."

        else:
            # Save the comment into SQLite
            add_comment(ticket_id, author, note)

            # Redirect to prevent duplicate submissions
            return redirect(
                url_for("ticket_detail", ticket_id=ticket_id)
            )

    # Retrieve all comments for this ticket
    comments = get_comments(ticket_id)

    # Display the ticket details webpage
    return render_template(
        "ticket_detail.html",
        ticket=ticket,
        comments=comments,
        error=error
    )
