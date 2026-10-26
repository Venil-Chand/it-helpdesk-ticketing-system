
# Import SQLite to manage the database
import sqlite3

# Import Path to locate the database file
from pathlib import Path

# Define the database file location
DB_PATH = Path(__file__).resolve().parent / "helpdesk.db"


# Function to create the database table
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


if __name__ == "__main__":
    create_database()

