"""
One writer process for the SQLite concurrency rehearsal.

    python rehearsals/concurrency_worker.py INDEX ITERATIONS

Each iteration creates a ticket as a user, adds a technician comment
(the SLA first response), and assigns the ticket and changes its status
as the administrator: four writes through the application's own
functions. Prints one JSON line: {"index": .., "tickets": [...],
"errors": [...]}. Run by rehearsals/reliability.py, many at once, inside
a workspace whose HELPDESK_DATABASE_PATH names a disposable database.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database as db  # noqa: E402

TITLE_PREFIX = "Concurrency ticket"


def main():
    index, iterations = int(sys.argv[1]), int(sys.argv[2])
    if not os.environ.get(db.DB_PATH_VARIABLE):
        sys.exit("HELPDESK_DATABASE_PATH must be set")

    tickets, errors = [], []
    for number in range(iterations):
        try:
            ticket_id = db.create_ticket(
                f"{TITLE_PREFIX} {index}-{number}", "Concurrent write.",
                "Medium", "user1")
            tickets.append(ticket_id)
            db.add_comment(ticket_id, "tech1", "First response.")
            db.update_ticket(ticket_id, "In Progress", "tech2",
                             actor="admin")
        except (sqlite3.Error, db.InvalidAssigneeError) as error:
            errors.append(f"{type(error).__name__}: {error}")

    print(json.dumps({"index": index, "tickets": tickets, "errors": errors}))


if __name__ == "__main__":
    main()
