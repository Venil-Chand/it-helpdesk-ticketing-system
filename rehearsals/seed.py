"""
Fill a disposable rehearsal database through the application's own code.

    python rehearsals/seed.py TICKETS

Run by rehearsals/common.py (Workspace.seed) inside a workspace, after
"flask --app app init-db". HELPDESK_DATABASE_PATH must name the
workspace database; REHEARSAL_PASSWORD is the password given to every
account (a new random value for each run, never printed).

Creates an administrator ("admin"), two technicians and three users with
example.invalid addresses, then TICKETS tickets. Each ticket gets a staff
comment, an assignment and a status change, so tickets, comments,
history, SLA rows, audit events and queued notifications all have rows.

The only direct SQL is the first administrator (as setup_accounts.py
does) and clearing the "must change password" flag on these disposable
accounts, so a rehearsal can sign in with them.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import accounts  # noqa: E402
import database as db  # noqa: E402
import migrations  # noqa: E402

TECHNICIANS = ("tech1", "tech2")
USERS = ("user1", "user2", "user3")
PRIORITIES = ("Low", "Medium", "High", "Critical")
OPERATOR = "rehearsal seed"


def main():
    count = int(sys.argv[1])
    password = os.environ["REHEARSAL_PASSWORD"]

    if not os.environ.get(db.DB_PATH_VARIABLE):
        sys.exit("HELPDESK_DATABASE_PATH must be set")
    if not migrations.schema_status(db.DB_PATH).is_current:
        sys.exit("run 'flask --app app init-db' first")

    conn = db.get_db()
    try:
        with conn:
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
                conn.execute(
                    "INSERT INTO users (username, password_hash, role) "
                    "VALUES ('admin', ?, 'admin')",
                    (accounts.hash_password(password),))
        admin_id = conn.execute(
            "SELECT id FROM users WHERE username = 'admin'").fetchone()[0]
    finally:
        conn.close()

    for name in TECHNICIANS:
        accounts.create_account(admin_id, name, "technician", password)
    for name in USERS:
        accounts.create_account(admin_id, name, "user", password)
    for name in ("admin",) + TECHNICIANS + USERS:
        accounts.operator_set_email(name, f"{name}@example.invalid",
                                    operator=OPERATOR)

    conn = db.get_db()
    try:
        with conn:
            conn.execute("UPDATE users SET must_change_password = 0")
    finally:
        conn.close()

    for number in range(count):
        requester = USERS[number % len(USERS)]
        technician = TECHNICIANS[number % len(TECHNICIANS)]
        ticket_id = db.create_ticket(
            f"Rehearsal ticket {number}",
            f"Disposable rehearsal data, ticket {number}.",
            PRIORITIES[number % len(PRIORITIES)], requester)
        db.add_comment(ticket_id, technician, "Looking into it.")
        db.update_ticket(ticket_id, "In Progress", technician, actor="admin")
        if number % 3 == 0:
            db.update_ticket(ticket_id, "Resolved", technician,
                             actor=technician)

    print(f"seeded {count} tickets")


if __name__ == "__main__":
    main()
