# Import SQLite to manage the database
import sqlite3

# Import Path to locate the database file
from pathlib import Path

# Define the database file location
DB_PATH = Path(__file__).resolve().parent / "helpdesk.db"


# Function to create the database tables
def create_database():

    # Connect to the database
    with sqlite3.connect(DB_PATH) as connection:

        # Create the tickets table
        connection.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                priority TEXT NOT NULL DEFAULT 'Medium',
                status TEXT NOT NULL DEFAULT 'Open',
                assigned_to TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create the technician comments table
        connection.execute("""
            CREATE TABLE IF NOT EXISTS ticket_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                author TEXT NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (ticket_id) REFERENCES tickets(id)
            )
        """)

    print("Helpdesk database created successfully!")
# Add a new IT support ticket
def add_ticket(title, description, priority="Medium"):

    connection = sqlite3.connect(DB_PATH)

    try:
        cursor = connection.execute("""
            INSERT INTO tickets (title, description, priority)
            VALUES (?, ?, ?)
        """, (title, description, priority))

        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()

# Retrieve all IT support tickets
def get_tickets():

    connection = sqlite3.connect(DB_PATH)

    try:
        # Access database columns by their names
        connection.row_factory = sqlite3.Row

        # Retrieve tickets, newest first
        cursor = connection.execute("""
            SELECT * FROM tickets
            ORDER BY id DESC
        """)

        tickets = [dict(row) for row in cursor.fetchall()]

        return tickets

    finally:
        connection.close()


# Update the status of an existing ticket
def update_ticket_status(ticket_id, new_status):

    # Define the permitted ticket statuses
    valid_statuses = ("Open", "In Progress", "Resolved", "Closed")

    # Reject invalid status values
    if new_status not in valid_statuses:
        raise ValueError("Invalid ticket status")

    # Connect to the database
    connection = sqlite3.connect(DB_PATH)

    try:
        # Update the status for the specified ticket
        cursor = connection.execute("""
            UPDATE tickets
            SET status = ?
            WHERE id = ?
        """, (new_status, ticket_id))

        # Save the changes
        connection.commit()

        # Return True if a ticket was updated
        return cursor.rowcount > 0

    finally:
        connection.close()



# Assign an IT support ticket to a technician
def assign_ticket(ticket_id, technician):

    # List of available technicians
    valid_technicians = (
        "Unassigned",
        "Technician A",
        "Technician B",
        "Technician C"
    )

    # Check whether the selected technician is valid
    if technician not in valid_technicians:
        raise ValueError("Invalid technician")

    # Store NULL when a ticket is unassigned
    if technician == "Unassigned":
        technician = None

    # Connect to the SQLite database
    connection = sqlite3.connect(DB_PATH)

    try:
        # Update the technician assigned to the ticket
        cursor = connection.execute("""
            UPDATE tickets
            SET assigned_to = ?
            WHERE id = ?
        """, (technician, ticket_id))

        # Save the changes permanently
        connection.commit()

        # Return True if a ticket was found and updated
        return cursor.rowcount > 0

    finally:
        # Close the database connection
        connection.close()


# Add a troubleshooting comment to a ticket
def add_comment(ticket_id, author, note):

    # Remove unnecessary spaces
    author = author.strip()
    note = note.strip()

    # Validate the information
    if not author or not note:
        raise ValueError("Author and note cannot be empty")

    if len(author) > 80 or len(note) > 2000:
        raise ValueError("Comment is too long")

    # Connect to the database
    connection = sqlite3.connect(DB_PATH)

    try:
        # Enforce the relationship between tickets and comments
        connection.execute("PRAGMA foreign_keys = ON")

        # Save the comment
        cursor = connection.execute("""
            INSERT INTO ticket_comments (ticket_id, author, note)
            VALUES (?, ?, ?)
        """, (ticket_id, author, note))

        connection.commit()

        # Return the new comment ID
        return cursor.lastrowid

    finally:
        connection.close()

# Retrieve troubleshooting comments for a specific ticket
def get_comments(ticket_id):

    # Connect to the SQLite database
    connection = sqlite3.connect(DB_PATH)

    try:
        # Access database columns by name
        connection.row_factory = sqlite3.Row

        # Retrieve comments belonging to the selected ticket
        cursor = connection.execute("""
            SELECT * FROM ticket_comments
            WHERE ticket_id = ?
            ORDER BY id ASC
        """, (ticket_id,))

        # Convert database records into dictionaries
        comments = [dict(row) for row in cursor.fetchall()]

        return comments

    finally:
        # Close the database connection
        connection.close()


if __name__ == "__main__":
    create_database()

# Retrieve a single ticket using its ID
def get_ticket(ticket_id):

    # Connect to the SQLite database
    connection = sqlite3.connect(DB_PATH)

    try:
        # Access columns by their names
        connection.row_factory = sqlite3.Row

        # Retrieve the selected ticket
        cursor = connection.execute("""
            SELECT * FROM tickets
            WHERE id = ?
        """, (ticket_id,))

        # Retrieve one record
        ticket = cursor.fetchone()

        # Return None if the ticket does not exist
        if ticket is None:
            return None

        # Convert the record into a dictionary
        return dict(ticket)

    finally:
        # Close the database connection
        connection.close()


if __name__ == "__main__":
    create_database()

