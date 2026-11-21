
# IT Helpdesk Ticketing System

[![Python Helpdesk CI](https://github.com/Venil-Chand/it-helpdesk-ticketing-system/actions/workflows/python-ci.yml/badge.svg?branch=main)](https://github.com/Venil-Chand/it-helpdesk-ticketing-system/actions/workflows/python-ci.yml)


A Python and Flask web application for managing IT support requests.

Built as a portfolio and practical learning project by Venil Chand, a computing student based in Auckland, New Zealand.

The project demonstrates Python programming, web development, relational databases, authentication, role-based permissions, software testing, and Git version control.

---

## Technologies Used

| Layer | Technology |
|---|---|
| Programming language | Python 3 |
| Web framework | Flask 3.1 |
| Database | SQLite (`sqlite3`) |
| Password security | Werkzeug password hashing |
| Templates | Jinja2 |
| Frontend | Bootstrap 5.3 and Bootstrap Icons |
| Testing | Python `unittest` |
| Development environment | Ubuntu WSL2 on Windows |
| Version control | Git and GitHub |

---

## Features

- **User authentication** — login and logout using Flask sessions
- **Password hashing** — passwords are stored as hashes, not plaintext
- **Role-based permissions** — administrator, technician and regular user
- **Ticket creation** — create support requests with title, description and priority
- **Dashboard** — view ticket statistics and support requests
- **Ticket search** — search tickets by title or description, combined with filters
- **Ticket filtering** — filter tickets by status and priority
- **Dashboard pagination** — 10 tickets per page with numbered navigation that keeps the search and filters
- **Response SLA tracking** — every new ticket gets a response deadline based on its priority; the first qualifying staff response is recorded
- **SLA monitoring dashboard** — summary cards for Within SLA, Due Soon, Overdue, Met, Breached and Not Tracked, an SLA column on every ticket row, and an SLA status filter
- **CSV export** — download every ticket matching the current search and filters, with spreadsheet formula injection neutralised
- **Ticket activity history** — an append-only timeline of creation, status, assignment and comment events
- **Ticket details** — view descriptions, updates and comments
- **Ticket management** — update status and technician assignment
- **Comments** — add notes to existing tickets
- **Account setup** — create the first administrator using a terminal script
- **Password changes** — verify the current password before changing it
- **Versioned database migrations** — explicit `flask --app app init-db` and read-only `db-status` commands; upgrades run in one transaction and never seed accounts
- **Automated tests** — 429 tests covering authentication, authorization, CSRF, search, pagination, CSV export, history, SLA monitoring and database migrations

---

## User Roles

| Role | Permissions |
|---|---|
| Administrator | Manage support tickets and assignments |
| Technician | Update ticket status, manage assignments and add comments |
| Regular User | Create tickets, view permitted pages and add comments |

Ticket ownership is enforced for regular users across the dashboard, search, ticket detail and comment routes. Regular users cannot reach another person's ticket by URL, by search, or by posting a comment request.

---

## Installation (Ubuntu / WSL2)

### 1. Clone the repository

```bash
git clone https://github.com/Venil-Chand/it-helpdesk-ticketing-system.git
cd it-helpdesk-ticketing-system
```

### 2. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Create your local environment file:

```bash
cp .env.example .env
```

Generate a secret key:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Open `.env` and set `SECRET_KEY` to the generated value.

Never upload `.env` or your actual secret key to GitHub.

### 5. Initialise the database

```bash
flask --app app init-db
```

This creates `helpdesk.db` with the latest schema. It is safe to run again: an up-to-date database is left unchanged. See [Database Schema and Migrations](#database-schema-and-migrations) before using it on an existing database.

`python app.py` and `setup_accounts.py` no longer create tables themselves. If the schema is missing or outdated they stop and tell you to run this command.

### 6. Set up the administrator account

For a new installation, run:

```bash
python setup_accounts.py
```

If no accounts exist, the program creates the initial `admin` account after prompting for a password.

Passwords must contain at least 16 characters.

No fixed default password is created during database initialization.

### 7. Start the application

```bash
python app.py
```

Open your browser at:

http://127.0.0.1:5000

The application is intended for local development and learning.

If the database has not been initialised or upgraded, `python app.py` exits with instructions instead of starting.

---

## Secure Account Management

Account setup and password changes are handled by:

`setup_accounts.py`

### First-time administrator setup

Run:

```bash
python setup_accounts.py
```

When the user database is empty, the script creates the initial administrator account after requesting and confirming a password.

### Changing an existing password

Run the same command:

```bash
python setup_accounts.py
```

The program requests:

1. An existing username
2. The current password
3. A new password
4. Confirmation of the new password

The script checks the current password before updating the stored password hash.

### Password security

- Passwords are entered using Python's `getpass` module.
- New passwords must contain at least 16 characters.
- Werkzeug hashes passwords before they are stored in SQLite.
- The new password cannot match the existing password.
- SQL queries use parameter placeholders to reduce SQL injection risk.
- Password-changing functionality is available through the local command line, not through the web interface.

**Important:** Existing demonstration accounts from earlier project versions may still have their original passwords until they are changed. Removing the hardcoded passwords from source code does not automatically invalidate passwords stored in an existing database.

This local script is not a replacement for production-grade account administration.

`setup_accounts.py` requires an up-to-date database schema. Run `flask --app app init-db` first; the script no longer creates tables.

---

## Database Schema and Migrations

The database structure is defined in `migrations.py` as a numbered list of **migrations**. Each one moves the schema forward by exactly one version.

### Schema versions

| Version | Migration | What it adds |
|---|---|---|
| 0 | — | Empty database, or a database created before versioning existed |
| 1 | Core tables | `users`, `tickets`, `comments`; converts the original `ticket_comments` table |
| 2 | Ticket indexes | `idx_tickets_owner_date`, `idx_tickets_date` |
| 3 | Ticket history | Append-only `ticket_history`, its index and its two protective triggers |
| 4 | Response SLA | `ticket_sla` (current version) |

The version is stored inside the database file itself, using SQLite's built-in `PRAGMA user_version` header field. No extra table is needed.

### Commands

| Command | What it does | Changes the database? |
|---|---|---|
| `flask --app app db-status` | Shows the current version, the latest version and any pending migrations. Exits with status 0 only if the schema is up to date. | Never. The file is opened read-only, and a missing file is not created. |
| `flask --app app init-db` | Creates a new database, or applies pending migrations in order, then reports the final version. Exits with status 1 if anything fails. | Yes |
| `flask --app app init-db --backup-to FILE` | Writes a verified backup to a new `FILE` first. If the backup fails, no migration is attempted. | Yes |

Neither command creates user accounts or prints passwords, password hashes, ticket contents or `.env` values.

### Initialising a fresh database

```bash
flask --app app init-db
python setup_accounts.py
```

### Checking the current schema

```bash
flask --app app db-status
```

Example for a database created before versioning:

```text
Current schema version: 0 (database created before schema versioning)
Latest supported version: 4
Pending migrations:
  1: Create core users, tickets and comments tables
  2: Add ticket list indexes
  3: Add append-only ticket activity history
  4: Add response SLA deadlines
  (existing tables and data are kept; each step only adds what is missing)
```

### Upgrading an existing database

1. Stop the application so nothing writes during the upgrade.
2. Make and verify a backup (see below).
3. Check what will happen: `flask --app app db-status`
4. Upgrade: `flask --app app init-db --backup-to backups/helpdesk-before-v4.db`
5. Confirm: `flask --app app db-status` must exit with status 0.
6. Start the application again.

**Databases from before versioning.** A database at version 0 is never assumed to be empty. Each migration inspects the actual tables and only adds what is missing, so a database that already has some newer tables is upgraded without being rebuilt. Existing tickets, comments, accounts, password hashes, history events and SLA rows are kept exactly as they are.

- Tickets created before history tracking get **no** invented history events.
- Tickets created before SLA tracking get **no** SLA row and appear as **Not Tracked**. Their deadlines and responses were never recorded, so no outcome is made up for them.
- A database still using the original `ticket_comments` table has its notes copied into `comments` with the same ids and timestamps. The old table is kept, renamed to `ticket_comments_migrated`.

**What is refused.** The upgrade stops and changes nothing if the database:

- has a schema version newer than this application supports
- is not a helpdesk database (it has tables but no `tickets` table)
- has tables, indexes or triggers whose structure differs from what the application expects
- contains rows that reference records that do not exist (for example, comments on deleted tickets). Only the number of such rows per table is reported. Fix them deliberately, then retry.

### Why a verified backup is required

A migration changes the structure of the only copy of your data. The migration is transactional, but a backup still protects against things a transaction cannot cover: a mistake in a future migration, disk failure, or needing to return to the previous application version.

`--backup-to` uses SQLite's online backup API. It refuses to overwrite an existing file and runs `PRAGMA integrity_check` on the copy before the migration starts. A plain `cp` of a database that is in use can produce a corrupt copy.

Keep backups outside the project directory, with permissions that restrict access. They contain password hashes and ticket data, and they must never be committed (`*.db` is already in `.gitignore`).

### What happens if a migration fails

All pending migrations run inside **one** SQLite transaction, started with `BEGIN IMMEDIATE`:

- If any step fails, the whole transaction is rolled back, including the version number. The database is exactly as it was before the command.
- `init-db` prints the error and exits with status 1, so scripts and deployment tools can detect the failure.
- Running `init-db` again after fixing the cause is safe.
- Two `init-db` commands started at the same time cannot both migrate. The second waits for the first, up to 30 seconds, then finds nothing left to do.

### Local development compared with production

| | Local development | Production |
|---|---|---|
| Start command | `python app.py` (Flask development server) | A WSGI server such as Gunicorn, with several worker processes |
| Migrations | Run `flask --app app init-db` once, and again after pulling schema changes | Run once per deployment, by the operator, **before** starting the service |
| Missing or outdated schema | `python app.py` refuses to start and prints the command to run | Requests that need missing tables return **503 Service Unavailable**, not 500, and the log names the command to run |

Web workers never migrate the database. If several Gunicorn workers each tried to migrate at startup, they would compete for the same database and could start serving before the upgrade finished.

Deployment sequence:

```bash
sudo systemctl stop helpdesk
flask --app app init-db --backup-to /var/backups/helpdesk/helpdesk-$(date +%Y%m%d-%H%M%S).db
flask --app app db-status          # must exit 0
sudo systemctl start helpdesk
```

`db-status` can also guard the service itself, so it will not start against an outdated schema. Example systemd excerpt:

```ini
[Service]
ExecStartPre=/srv/helpdesk/.venv/bin/flask --app app db-status
ExecStart=/srv/helpdesk/.venv/bin/gunicorn --workers 3 app:app
```

Gunicorn is not yet in `requirements.txt`; production deployment is still on the roadmap.

### Preparing a rollback and recovery procedure

There are no "down" migrations. Recovery means restoring a backup. Before upgrading production:

1. **Back up and verify.** Use `init-db --backup-to`, or a separate backup made with the application stopped. Record the backup's file name and the application commit that was running.
2. **Rehearse.** Copy the backup to a test location and upgrade the copy first. One way is to run the tests or `migrations.migrate("copy.db")` from a Python shell against the copy.
3. **If the upgrade fails:** nothing was changed. Read the error, fix the cause (for example, orphaned rows), and run `init-db` again.
4. **If problems appear after a successful upgrade:**
   - stop the application
   - move the upgraded `helpdesk.db` aside (do not delete it)
   - copy the verified backup into place as `helpdesk.db`
   - check `PRAGMA integrity_check` on the restored file
   - deploy the previous application commit, then start the service

   Anything written after the upgrade exists only in the file you moved aside.

Migrations 1–4 only add tables, columns, indexes and triggers. Application versions from before this change ignore `user_version` and can still run on a version 4 database.

### Retired legacy migration script

`migrate.py` has been removed. It created `admin`, technician and user accounts with fixed default passwords whenever the `users` table was empty, made unverified file copies, and did not use a single transaction. Its comment-table conversion is now migration 1. Git history still contains the old script for reference, but it must not be run.

If a database was ever initialised with that script, make sure every account created by it has had its password changed with `python setup_accounts.py`.

### Adding a new migration

1. Write a function that takes the open connection and changes the schema. It must **never** call `commit()` or `executescript()`.
2. Append it to `MIGRATIONS` with the next version number. Never edit, reorder or remove a migration that has already been released.
3. Update the expected schema checked by `find_schema_problems()` and add upgrade tests.

---

## Project Structure

```text
it-helpdesk-ticketing-system/
├── app.py
├── database.py
├── sla.py
├── sla_storage.py
├── csv_export.py
├── setup_accounts.py
├── migrations.py
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
├── tests/
│   ├── test_account_setup.py
│   ├── test_authentication.py
│   ├── test_authorization.py
│   ├── test_csrf.py
│   ├── test_database_filters.py
│   ├── test_logout.py
│   ├── test_migrations.py
│   ├── test_migration_cli.py
│   ├── test_migration_upgrades.py
│   ├── test_pagination_db.py
│   ├── test_pagination_integration.py
│   ├── test_pagination_route.py
│   ├── test_rate_limit.py
│   ├── test_search_route.py
│   ├── test_sla_dashboard_db.py
│   ├── test_sla_dashboard_routes.py
│   ├── ...                      (SLA, CSV and history tests)
│   ├── test_ticket_ownership.py
│   └── test_ticket_search.py
├── templates/
│   ├── base.html
│   ├── login.html
│   ├── index.html
│   ├── new_ticket.html
│   └── ticket_detail.html
└── static/
    └── style.css
```

### Important Python files

| File | Purpose |
|---|---|
| `app.py` | Flask application, routes and login logic |
| `database.py` | SQLite queries and ticket functions, including the SLA dashboard queries; `init_db()` runs the migrations |
| `sla.py` | Response SLA policy: deadlines per priority and status rules |
| `sla_storage.py` | Stores SLA deadlines and first responses; reads stored UTC timestamps |
| `csv_export.py` | Builds safe CSV files from ticket rows |
| `setup_accounts.py` | Initial administrator setup and password changes |
| `migrations.py` | Versioned, transaction-safe schema migrations (replaces the retired `migrate.py`) |
| `tests/` | 429 automated tests covering auth, CSRF, ownership, search, pagination, CSV export, history, SLA and migrations |

The local `helpdesk.db` database and `.env` file are excluded from version control.

---

## How the Application Works

### Flask

Flask handles HTTP requests and connects URLs to Python functions.

For example, visiting `/tickets/3` calls the route responsible for displaying ticket number 3.

### SQLite

SQLite stores application information in a local database file, `helpdesk.db`.

The database contains these tables:

- `users` — usernames, password hashes and roles
- `tickets` — support requests and their status
- `comments` — notes associated with tickets
- `ticket_history` — append-only activity events, protected by triggers
- `ticket_sla` — response deadlines and first responses

The schema version is recorded in the file itself (`PRAGMA user_version`); see [Database Schema and Migrations](#database-schema-and-migrations).

### Jinja2

Jinja2 connects Python application data with HTML templates, allowing dynamic information to appear on webpages.

### Sessions

Flask uses signed session cookies to identify authenticated users between requests.

### Password Hashing

Werkzeug converts passwords into one-way hashes. When a user logs in, the submitted password is checked against the stored hash.

### Account Management

The `setup_accounts.py` script uses `getpass`, password verification and SQL updates to manage local account passwords without writing new passwords directly into source code.

---

## Ticket Search and Filtering

The dashboard provides a search box alongside the existing status and priority dropdowns. Searching matches text in either the ticket **title** or **description**.

### Using search

1. Type a term into the search box and press **Search**.
2. Optionally narrow the results further using the **Status** and **Priority** dropdowns.
3. The search term stays in the box so you can see what produced the current results.
4. Press **Clear** to reset the search and both filters at once.

If nothing matches, the dashboard shows a message naming the term that was searched for.

### How a search travels through the application

| Step | What happens |
|---|---|
| 1. Browser | The form submits `GET /?q=printer&status=Open&priority=All` |
| 2. Flask route | `index()` reads `q`, `status`, `priority` and `page` from the query string |
| 3. Role check | Staff search all tickets; regular users have their username attached |
| 4. Database | `count_tickets()` and `get_tickets_page()` apply the same parameterised `LIKE` filter, one to count matches and one to fetch a page |
| 5. Template | Results render in the ticket table, with the term kept in the input |

### Search and ticket ownership

Ownership is enforced in SQL, not filtered in Python afterwards.

When a regular user searches, the query functions receive their username, and the shared `_ticket_filters()` helper places `created_by = ?` as a condition in the `WHERE` clause. There is no URL a regular user can construct that returns another person's ticket, because the restriction is applied in the query itself rather than to the results.

Administrators and technicians search without that restriction, which is what lets them see the whole queue.

All search values are passed as bound SQL parameters, so a term such as `' OR 1=1 --` is treated as literal text to look for rather than as SQL. The search term is also capped at 100 characters by the route, independently of the `maxlength` set on the input.

---

## Dashboard Pagination

The dashboard shows **10 tickets per page**, newest first. Below the table it states which tickets are on screen, for example *Showing 11–20 of 47 tickets*.

### Using pagination

- **Previous** and **Next** move one page at a time, and are disabled on the first and last page.
- Page numbers jump straight to a page. The first page, the last page and two pages either side of the current one are shown, with `…` marking each gap: `1 … 4 5 6 7 8 … 11`.
- On small screens only **Previous**, the current page (for example *2 of 5*) and **Next** are shown.
- Every page link keeps the active search and filters, so page 2 of a printer search is still a printer search.
- Starting a new search or changing a filter returns to page 1.

### Page numbers in the URL

The page is part of the address, so any page can be bookmarked or shared:

```text
/?page=2
/?q=printer&status=Open&priority=High&page=2
```

| Requested page | Result |
|---|---|
| Missing | Page 1 |
| Zero, negative or not a number, e.g. `?page=abc` | Page 1 |
| Past the last page, e.g. `?page=99` | Redirects to the last page, keeping the search and filters |

### How pagination works

| Step | What happens |
|---|---|
| 1. Read the page | `parse_page()` turns `?page=` into a whole number of at least 1 |
| 2. Decide ownership | Staff see every ticket; regular users are limited to their own |
| 3. Count | `count_tickets()` runs `SELECT COUNT(*)` with the active filters |
| 4. Work out pages | `build_pagination()` calculates `ceil(total / 10)` pages |
| 5. Fetch one page | `get_tickets_page()` adds `LIMIT ? OFFSET ?`, binding 10 and `(page - 1) * 10` as parameters |
| 6. Render | The template builds every link with `url_for()`, carrying the filters |

Only the ten tickets being displayed are read from the database. The application never loads every ticket into Python and slices the list.

Tickets are ordered by `created_at DESC, id DESC`. The `id` breaks ties between tickets created in the same second, giving each ticket one fixed position, so moving between pages never repeats or skips a ticket.

An index on `(created_at DESC, id DESC)` lets SQLite read the newest tickets for staff directly in order, instead of sorting the whole table on every request. Regular users' pages already use the `(created_by, created_at DESC, id DESC)` index.

### Pagination and ticket ownership

The ownership restriction is applied **before** pagination, inside the SQL `WHERE` clause, and the count and page queries always receive identical filters. As a result:

- a regular user's pages contain only their own tickets
- the total in *Showing 1–10 of N* counts only their tickets, so it does not reveal how many tickets other users have
- a page number that would only exist for staff redirects to the user's own last page

Adding parameters such as `?username=bob`, `?role=admin` or `?per_page=1000` has no effect. The route reads only `q`, `status`, `priority`, `sla` and `page`. The user's identity and role come from the signed session cookie, which cannot be altered without the secret key, and never from the URL.

---

## SLA Monitoring Dashboard

The dashboard shows how every ticket you may see is doing against its **response SLA**: the time allowed for a technician or administrator to first reply.

### Response SLA policy

When a ticket is created, a response deadline is stored for it in the `ticket_sla` table:

| Priority | Response target |
|---|---|
| Critical | 1 hour |
| High | 4 hours |
| Medium | 24 hours |
| Low | 72 hours |

Targets are calendar hours (policy `calendar-hours-v1`). The first comment from a technician or administrator on **someone else's** ticket is recorded as the first response. A requester's own comments never stop the clock.

### SLA statuses

| Status | Meaning |
|---|---|
| Within SLA | No response yet, more than 30 minutes left |
| Due Soon | No response yet, 30 minutes or less left |
| Overdue | No response yet, deadline reached or passed |
| Met | First response at or before the deadline |
| Breached | First response after the deadline |
| Not Tracked | Ticket created before SLA tracking, or its SLA data cannot be read |

The boundaries are exact: a ticket with no response is **Overdue** at the deadline itself, and a response **at** the deadline is **Met**.

Not Tracked tickets are never counted as overdue. The status depends only on the first response, not on the ticket's Open/Closed status. This is the existing SLA policy, which the dashboard does not change.

### Using the SLA dashboard

- **Summary cards** count tickets in each status across everything you may see, not just the current page or search. They show the UTC time they were calculated at.
- **Click a card**, or use the **SLA** select next to the status and priority filters, to list only tickets in that status. Search, status, priority and pagination all keep working with it.
- The **Response SLA** column shows each ticket's status, deadline and first response time, in UTC. Overdue rows have a red edge and due-soon rows an amber one, and the status is always written out, so colour is never the only signal.
- **Export CSV** exports the tickets matching the current filters, including the SLA filter. The CSV columns are unchanged.

The SLA filter is in the URL, for example `/?sla=overdue&priority=High&page=2`. Accepted values are `within`, `due_soon`, `overdue`, `met`, `breached` and `not_tracked`. Any other value is ignored.

### How the SLA dashboard works

1. The route takes **one UTC timestamp** (`now`) per request and passes it to every query, so the cards, the filter, the page total and each row agree.
2. `database.py` turns the SLA rules into one SQL `CASE` expression, joined to the tickets with `LEFT JOIN ticket_sla`. Tickets without an SLA row become Not Tracked.
3. The SLA filter is part of the SQL `WHERE` clause, so it runs **before** `LIMIT`/`OFFSET`, and the count uses the same filter.
4. The summary cards come from one `GROUP BY` query. Only six numbers reach Python.
5. The ticket page still uses the original Python SLA code. Tests check that the SQL and Python give the same status for every stored timestamp format and boundary.

### Database performance

`EXPLAIN QUERY PLAN` shows the dashboard queries use the existing indexes:

- staff pages read tickets in order from `idx_tickets_date`; regular users' from `idx_tickets_owner_date`
- each ticket's SLA row is found by primary key (`ticket_sla.ticket_id`), so there is no extra query per ticket and no scan of `ticket_sla`
- the page order comes straight from the index, with no sorting step

No new index was added. An SLA status depends on the current time, so it cannot be stored in an index. Filtering by SLA status therefore checks each permitted ticket inside SQLite. That is fine at helpdesk scale.

### Stored timestamps

SQLite's `julianday()` reads stored times the same way `sla_storage.parse_stored_utc()` does: text without an offset (like SQLite's `CURRENT_TIMESTAMP`) is UTC, and text with `+05:30`, `-05:00` or `Z` is converted to UTC. Values that are not date-time text, such as `"2461000"` or `"now"`, are refused. The ticket page then shows no SLA and the dashboard shows Not Tracked.

### SLA and ticket ownership

The SLA cards, rows and filter use exactly the same ownership rule as the ticket list. Regular users get counts and deadlines for their own tickets only; technicians and administrators see every ticket. The `?sla=` value is checked against a fixed allow list before it reaches the database. Adding `?username=` or `?role=` still has no effect.

---

## Automated Testing

The project includes 429 automated tests using Python's built-in `unittest` framework.

To run the full suite:

```bash
python -m unittest discover -s tests -v
```

To run a single test file:

```bash
python -m unittest tests.test_search_route -v
```

### Test coverage by file

| Test file | Tests | Purpose |
|---|---|---|
| `test_authentication.py` | 4 | Protected pages redirect anonymous visitors |
| `test_account_setup.py` | 3 | Password hashing and password change rules |
| `test_authorization.py` | 3 | Only staff may update ticket status |
| `test_csrf.py` | 6 | State-changing forms reject missing or invalid tokens |
| `test_logout.py` | 4 | Logout requires POST with a valid CSRF token |
| `test_rate_limit.py` | 1 | Repeated login attempts are throttled |
| `test_ticket_ownership.py` | 6 | Users cannot view or comment on others' tickets |
| `test_database_filters.py` | 5 | Per-user and global ticket filtering and statistics |
| `test_ticket_search.py` | 5 | Search SQL behaviour and injection resistance |
| `test_search_route.py` | 11 | Search route, rendered HTML and role restrictions |
| `test_pagination_db.py` | 18 | `LIMIT`/`OFFSET` queries, ordering, page size limits and the index |
| `test_pagination_route.py` | 24 | Page parsing, page maths, page-number window and links |
| `test_pagination_integration.py` | 18 | Full requests with no mocks: navigation, filters and ownership |
| `test_csv_export.py` | 15 | CSV formatting and formula-injection protection |
| `test_csv_export_routes.py` | 15 | Export route: access, filters and response headers |
| `test_csv_export_integration.py` | 15 | Full export requests against a temporary database |
| `test_ticket_history.py` | 30 | Transactional, append-only history recording |
| `test_ticket_history_routes.py` | 16 | History shown only to users who may view the ticket |
| `test_ticket_history_view.py` | 6 | Timeline rendering |
| `test_sla.py` | 13 | Deadline calculation and SLA status rules |
| `test_sla_lifecycle.py` | 8 | Storing deadlines and first responses |
| `test_sla_status.py` | 6 | SLA status lookup and UTC parsing |
| `test_sla_integration.py` | 44 | SLA creation, first responses, rollback and the ticket page |
| `test_sla_dashboard_db.py` | 36 | SLA classification in SQL, boundaries, stored formats, counts, filters, pagination and query plans |
| `test_sla_dashboard_routes.py` | 37 | Full dashboard requests: cards, filters, pagination, ownership, malicious input and CSV export |
| `test_migrations.py` | 33 | Versioning, ordering, repeatability, rollback, validation, foreign keys, triggers, concurrency, backups, retired script |
| `test_migration_upgrades.py` | 23 | Synthetic copies of every historical schema upgraded with data preserved, and the app running on them |
| `test_migration_cli.py` | 24 | `init-db` and `db-status` output and exit codes, startup check, 503 readiness response, account setup guard |

Tests never touch the real `helpdesk.db`:

- **Database tests** redirect `DB_PATH` to a temporary SQLite file.
- **Route tests** mock the database functions, and point `DB_PATH` at a location that cannot be opened, so an accidental unmocked query fails loudly instead of reading real data.
- **Integration tests** mock nothing. Each request runs through the real route, real SQL against a temporary database, and the real template, and several tests follow the rendered **Next** links page by page.

Coverage is still not exhaustive. Concurrent migration attempts are tested, but general load testing, request-level concurrency testing and automated browser testing remain outstanding.

---

## Security Limitations

**This project is intended for local development and educational use. It is not production-ready.**

| Security area | Current status |
|---|---|
| Password hashing | Implemented |
| Hardcoded account creation | Removed; the legacy `migrate.py` seeding script has been deleted |
| Database migrations | Versioned, transactional, run explicitly by an operator; never seed accounts |
| Initial administrator setup | Implemented as a local script |
| Password changing | Implemented as a local script |
| CSRF protection | Implemented on all state-changing forms |
| Login rate limiting | Implemented (5 attempts per minute) |
| Ticket ownership authorization | Implemented and enforced in SQL |
| Search ownership restrictions | Implemented and tested |
| Pagination ownership restrictions | Applied before paging; counts and pages tested |
| SLA dashboard restrictions | SLA counts, rows and filters use the same ownership rule; tested |
| HTTPS | Not configured |
| Debug mode | Disabled (`debug=False`) |
| Web-based account administration | Not implemented |
| Automated security testing | 429 tests covering auth, CSRF, ownership, search, pagination, export, history, SLA and migrations |

Existing databases may still contain old demonstration credentials. Those passwords must be changed before relying on the updated authentication process.

Security improvements are ongoing.

---

## Current Limitations

- No email notifications
- No automatic SLA alerts; overdue tickets are visible only on the dashboard
- No file attachments
- No web-based password change form
- No administrative user management interface
- No end-to-end browser testing
- No load or concurrency testing
- Not configured for production deployment
- No "down" migrations; recovery from a bad upgrade means restoring a backup
- On databases first upgraded by the retired `migrate.py`, `tickets.updated_at` has no default, so new tickets show "Not recorded" until they are first updated. Fixing the default would require rebuilding the table.

---

## Future Development Roadmap

### Security

- [x] Password hashing
- [x] Remove hardcoded account initialization
- [x] Local administrator account setup
- [x] Command-line password changes
- [x] Basic authentication access tests
- [x] Add CSRF protection
- [x] Implement login rate limiting
- [x] Improve ticket ownership authorization
- [x] Expand authentication and authorization tests
- [x] Disable debug mode for deployment
- [ ] Add a secure web-based change-password feature
- [ ] Add administrator-controlled user creation
- [ ] Configure HTTPS for production

### Application Features

- [ ] Email notifications
- [ ] File attachments
- [x] Dashboard pagination
- [ ] Ticket export to CSV
- [x] Ticket search by title and description
- [ ] Advanced search (date ranges, assignee, multi-field)
- [ ] Ticket activity history
- [ ] Additional dashboard reports

### Deployment

- [ ] Prepare production configuration
- [ ] Configure Gunicorn
- [ ] Configure Nginx and HTTPS
- [ ] Perform security review
- [ ] Deploy to a suitable Linux hosting environment

---

## Development and Learning

This project is being developed incrementally to strengthen practical knowledge of:

- Python programming
- Flask web applications
- SQLite databases
- Authentication and authorization
- Secure coding principles
- Automated testing
- Linux development
- Git and GitHub workflows

Development has included AI-assisted guidance and code review, with local testing and incremental improvements.

---


## Project Status

**Active development — local educational prototype**

The application is being improved progressively, with an emphasis on learning software engineering, writing maintainable Python code and developing secure application features.
