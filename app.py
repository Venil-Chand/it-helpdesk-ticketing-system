# Import Flask tools
from flask import Flask, render_template, request, redirect, url_for

# Import database functions
from database import get_tickets, add_ticket

# Create the Flask application
app = Flask(__name__)


# Homepage
@app.route("/")
def home():
    return """
    <h1>IT Helpdesk Ticketing System</h1>
    <p>Welcome to the IT Helpdesk!</p>
    <p>Manage and track IT support tickets.</p>
    <a href="/tickets">View Tickets</a>
    """


# Display tickets
@app.route("/tickets")
def tickets():

    all_tickets = get_tickets()

    return render_template("index.html", tickets=all_tickets)


# Create a new ticket
@app.route("/tickets/new", methods=["GET", "POST"])
def new_ticket():

    if request.method == "POST":

        # Get information from the form
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

        # Save the new ticket into SQLite
        add_ticket(title, description, priority)

        # Return to the tickets dashboard
        return redirect(url_for("tickets"))

    # Display an empty form
    return render_template(
        "new_ticket.html",
        error=None,
        title="",
        description="",
        priority="Medium"
    )
