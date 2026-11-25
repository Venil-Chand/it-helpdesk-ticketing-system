
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
- **CSV export** — download every ticket matching the current search and filters, with the same SLA status, deadline and first response the dashboard shows, and spreadsheet formula injection neutralised
- **Ticket activity history** — an append-only timeline of creation, status, assignment and comment events
- **Ticket details** — view descriptions, updates and comments
- **Ticket management** — update status and assign tickets, only ever to active technicians or administrators (checked on the server)
- **Comments** — add notes to existing tickets
- **Account setup** — create the first administrator using a terminal script
- **Web account administration** — administrators create accounts, change roles, deactivate and reactivate accounts and reset passwords in the browser; accounts are never deleted
- **Self-service password change** — every user can change their own password after entering the current one; other sessions end
- **Administrative audit log** — an append-only record of every account change and sign-in attempt, who made it and when, with no passwords recorded; filterable by account changes or sign-ins
- **Password policy** — at least 16 characters, up to 1,024, any characters, with an offline check against 77,339 common and breached passwords; no complexity rules or expiry
- **Forced password changes** — a password set by an administrator must be replaced by its owner before anything else can be done
- **Administrator recovery** — `flask --app app reset-password USERNAME` recovers a lost password, including the last administrator's, from the server's command line
- **Audit archiving** — copy events older than the retention period to a verified, checksummed archive without deleting anything
- **Versioned database migrations** — explicit `flask --app app init-db` and read-only `db-status` commands; upgrades run in one transaction and never seed accounts
- **Automated tests** — 1,062 tests covering the application factory, password policy, forced password changes, sign-in auditing, administrator recovery, audit archiving, authentication, authorization, CSRF, account administration, password changes, audit events, search, pagination, CSV export, history, assignment rules, SLA policy and monitoring, and database migrations

---

## User Roles

| Role | Permissions |
|---|---|
| Administrator | Everything a technician can do, plus account administration and the audit log |
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

Passwords must contain at least 16 characters and must not be a common or breached password; see [Password policy](#password-policy).

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
- New passwords follow the [Password policy](#password-policy): 16 to 1,024 characters, not common or breached, not containing the username.
- Werkzeug hashes passwords before they are stored in SQLite.
- The new password cannot match the existing password.
- SQL queries use parameter placeholders to reduce SQL injection risk.
- Passwords can also be changed in the browser (**Password** in the navigation bar) and reset by an administrator; see [Web Account Administration](#web-account-administration).

**Important:** Existing demonstration accounts from earlier project versions may still have their original passwords until they are changed. Removing the hardcoded passwords from source code does not automatically invalidate passwords stored in an existing database.

Use this script for the first administrator; manage every other account from the **Users** page.

`setup_accounts.py` requires an up-to-date database schema. Run `flask --app app init-db` first; the script no longer creates tables.

---

## Web Account Administration

Administrators manage accounts at **Users** (`/admin/users`) in the navigation bar. Nobody else can open these pages: technicians and users get **403 Forbidden**, and signed-out visitors are sent to the login page.

| Task | How | Effect on sessions |
|---|---|---|
| Create an account | **New account**: username, role and an initial password ([Password policy](#password-policy), typed twice). The user must replace it at first sign-in. | — |
| Change a role | Choose the role in the account's row and press **Save** | Applies on the account's next request |
| Deactivate an account | **Deactivate** (asks for confirmation) | Signs the account out everywhere at once |
| Reactivate an account | **Reactivate** | Old sessions stay invalid; the user signs in again |
| Reset a forgotten password | **Reset password**, enter the new password twice. The user must replace it at next sign-in. | Signs the account out everywhere at once |
| Review changes | **Audit log** (`/admin/audit`): the 200 most recent events, or only **Account changes** or **Sign-ins** | — |

Every user can change their own password at **Password** (`/account/password`). The current password is required, the form is limited to 5 attempts per minute, and every other session for the account ends. The browser that made the change stays signed in.

### Rules enforced on the server

The rules live in `accounts.py` and are checked inside the same database transaction as the change. They hold whatever the form sends.

- The acting account is re-read inside the transaction and must still be an **active administrator**. An administrator demoted or deactivated a moment earlier changes nothing.
- Administrators **cannot change their own role, deactivate themselves or reset their own password** from the Users page; they use **Password** instead. Because the actor must be an active administrator and cannot act on itself, **the last active administrator can never be removed** through the application.
- **Accounts are never deleted.** Deactivation keeps every ticket, comment and history entry unchanged.
- **New usernames** must be 3 to 64 letters, digits, `.`, `_` or `-`, starting with a letter or digit. A name differing only in letter case from an existing account (`Admin` and `admin`) is refused, and so is a name that ticket records still use (migration 7). Existing usernames are unaffected.
- Demoting or deactivating a technician **does not unassign their tickets**: history is not rewritten. The page says how many open or in-progress tickets still need reassigning, and the ticket page flags each one as before.
- Passwords are hashed with Werkzeug before storage and are **never written to the audit log**.

### Audit events

`audit_events` (migration 8, extended by migration 9) records:

- **account changes:** `account_created`, `role_changed`, `account_deactivated`, `account_reactivated`, `password_reset` and `password_changed`
- **sign-ins:** `login_succeeded` and `login_failed`
- **archiving:** `audit_archived`

Each event holds the actor, the target account (id and username), the old and new value where relevant, the time in UTC and, for sign-ins, the client address.

Database triggers refuse any `UPDATE` or `DELETE` of an event. Changes made by `setup_accounts.py` are recorded with the actor `setup_accounts.py`, and those made by `flask reset-password` with `cli:<operating-system account>`. Changes made directly with SQL outside the application are not recorded.

### Forced password changes

A password chosen by someone else must be replaced by its owner. This covers an administrator creating an account, an administrator resetting a password, and `flask reset-password`. Migration 9 adds `users.must_change_password`; every existing account starts at 0, so nobody is disrupted.

While the flag is set, the account can only use **Change password** and **Logout**. Every other page, GET or POST, redirects to Change password without running. The flag is read from the database on every request, after the session check, so expiry, revocation, deactivation, CSRF and the rate limits all still apply. The new password must differ from the temporary one. The change-password form includes a hidden username field, so password managers save the new password against the right account.

### Password policy

The same rules apply everywhere a password is set: the web pages, account administration, `setup_accounts.py` and `flask reset-password` (`validation.new_password_problem`).

| Rule | Value |
|---|---|
| Minimum length | 16 characters (unchanged from earlier versions) |
| Maximum length | 1,024 characters. That is well over the 64 characters password managers usually generate, and short enough to hash safely. |
| Characters | Any, including spaces and non-English letters. Length counts characters, not bytes. |
| Common and breached passwords | Refused, using an offline list of 77,339 entries (see [`security_data/README.md`](security_data/README.md) for sources, checksums and limits). Case is ignored. |
| Username | A password containing the account's username (4 or more characters) is refused |
| Simple patterns | Short repeated blocks (`abcabcabc…`) and straight sequences (`abcdefgh…`) are refused |
| Complexity rules | None, deliberately. No required digits, symbols or capitals. |
| Expiry | None, deliberately. Change a password when it may have been exposed. |

Submitted passwords are never sent to any external service. If the blocklist file is missing, setting a password fails with an error instead of silently accepting everything.

### Sign-in auditing

Every attempt that reaches the login page is recorded as `login_succeeded` or `login_failed`, with the reason (`wrong password`, `account deactivated` or `unknown account`) and the client address. Client addresses come through the configured trusted proxies only; forged `X-Forwarded-For` headers are ignored.

- **Never stored:** passwords, session cookies, or a username that matches no account (it may be a password typed into the wrong box).
- **Rate limit first:** attempts over the limit (5 per minute) are refused before the login view runs, so they are neither processed nor recorded, and recording cannot be used to get round the limit.
- **Database failure:** if a *successful* sign-in cannot be recorded, nobody is signed in and the page returns 503. A *failed* attempt that cannot be recorded still gets the normal "Invalid username or password" answer, and only the error type is logged.
- **Same answer:** unknown usernames, wrong passwords and deactivated accounts get identical responses.

**Login timing.** An unknown username is now checked against a dummy password hash of the current method (scrypt), so it takes about as long as a wrong password and response time no longer reveals which usernames exist. Some smaller differences remain:

- an account whose stored hash uses an older, cheaper method answers at that method's speed until its password is next changed
- the database lookup itself is not constant-time
- oversized credentials are refused without hashing

These are much smaller than the gap that existed before, and they cannot be measured faster than the rate limit allows.

High volumes of failed sign-ins from many addresses each add a row to the audit table. The per-address rate limit bounds this, but the limit is held in memory per process until shared rate-limit storage arrives in Package 3.

### Administrator recovery

If a password is lost, including the only administrator's, recover the account from the server:

```bash
flask --app app reset-password USERNAME
```

- **Hidden prompt only:** the new password is typed twice at a hidden prompt. It is never accepted as an argument, option, environment variable or piped input, and the command refuses to run without an interactive terminal.
- **Effect:** every session of the account ends, the owner must choose a new password at next sign-in, and an audit event names the operating-system account that ran the command. The reset and its audit event are saved together or not at all.
- **What it never does:** it refuses deactivated accounts (reactivate first; it never unlocks anything), and it never changes roles.

Give the temporary password to its owner through a trusted channel, then make sure they sign in and replace it.

**Access controls this command relies on.** The command has no password check of its own: being able to run it on the server *is* the authorisation. Protect that access:

- **Ownership:** run the application as a dedicated, non-login service account (for example `helpdesk`) that owns `helpdesk.db`. The database file is `0600` and its directory `0700`, so no other account can read or write it.
- **Who can run it:** only named administrators, through `sudo -u helpdesk flask --app app reset-password …`. Use personal SSH accounts with key-only authentication; no shared logins and no direct root login.
- **The record:** the audit event records the real user id the command ran as and, under sudo, the invoking account. The system's sudo/auth log (`/var/log/auth.log` or `journalctl`) is the authoritative record of who ran the command; keep it, and review it alongside the audit log.
- **Backups:** anyone who can read a database backup can attack the password hashes offline, so restrict and encrypt backups the same way.

### Audit retention

The provisional policy is to keep audit events for **12 months**, pending the final privacy policy. The append-only triggers stay in place, so nothing is deleted. Instead, old events can be copied to a verified archive:

```bash
flask --app app audit-archive --dry-run                     # how many events are older than 12 months
flask --app app audit-archive --to /secure/audit-2025.jsonl # private archive + checksummed manifest
flask --app app audit-verify-archive /secure/audit-2025.jsonl
```

Removing archived events from the database is **designed but not implemented**; it needs an approved design first. The design, its safeguards and the open decisions are in [`docs/AUDIT_RETENTION.md`](docs/AUDIT_RETENTION.md).

### Account policy decisions

Decided for version 1.0 and implemented:

- forced password changes
- the password policy
- sign-in auditing
- 12-month provisional retention with archiving

Still open:

1. **Final retention and privacy policy:** the live and archive retention periods, and whether archived events are ever removed from the database ([`docs/AUDIT_RETENTION.md`](docs/AUDIT_RETENTION.md)).
2. **Online breached-password checks** (for example the Have I Been Pwned range API): not used, and would need approval because it contacts an external service.
3. **Account lock-out after repeated failures:** not implemented. The per-address rate limit applies instead, and a lock-out policy would let anyone lock out a known username.
4. **Multi-factor authentication:** not available.

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
| 4 | Response SLA | `ticket_sla` |
| 5 | Account activation | `users.is_active`; existing accounts stay active |
| 6 | Session revocation | `users.session_version` and the `users_revoke_sessions` trigger |
| 7 | Stable usernames | `users_username_immutable` and `users_no_historical_username_reuse` triggers; no rows changed |
| 8 | Audit events | Append-only `audit_events`, its index and its two protective triggers; no existing rows changed and no events invented for past changes |
| 9 | Account policy | `users.must_change_password` (0 for every existing account) and `audit_events.source_address` (NULL for older events); no rows changed, append-only triggers untouched (current version) |

The version is stored inside the database file itself, using SQLite's built-in `PRAGMA user_version` header field. No extra table is needed.

### Commands

| Command | What it does | Changes the database? |
|---|---|---|
| `flask --app app db-status` | Shows the current version, the latest version and any pending migrations. Exits with status 0 only if the schema is up to date. | Never. The file is opened read-only, and a missing file is not created. |
| `flask --app app init-db` | Creates a new database, or applies pending migrations in order, then reports the final version. Exits with status 1 if anything fails. | Yes |
| `flask --app app init-db --backup-to FILE` | Writes a verified backup to a new `FILE` first. If the backup fails, no migration is attempted. | Yes |
| `flask --app app assignment-audit` | Lists tickets assigned to an account that could not be chosen today (missing, deactivated or wrong role). Exits with status 1 if any are found. | Never. The file is opened read-only. |
| `flask --app app reset-password USERNAME` | Sets a temporary password typed at a hidden prompt, ends every session and forces a change at next sign-in. See [Administrator recovery](#administrator-recovery). | Yes (one account, audited) |
| `flask --app app audit-archive --to FILE` | Copies audit events older than the retention period to a private archive with a manifest. `--dry-run` only counts. | Adds one `audit_archived` event; deletes nothing |
| `flask --app app audit-verify-archive FILE` | Checks an archive against its manifest and the database. Exits with status 1 on any problem. | Never |

None of these commands creates user accounts or prints passwords, password hashes, ticket contents or `.env` values.

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

Migrations 1–6 only add tables, columns, indexes and triggers. Application versions from before versioning ignore `user_version` and can still run on a version 6 database, but they also ignore `is_active` and `session_version`, so **rolling the code back stops deactivated accounts and revoked cookies from being blocked**.

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
├── app.py                       (entry point: app = create_app())
├── helpdesk/                    (the web application package)
│   ├── __init__.py              (create_app(): the application factory)
│   ├── security.py              (HTTPS, CSRF, rate limit, proxy, headers)
│   ├── errors.py                (plain-text 400/403/413 and 503 responses)
│   ├── auth.py                  (blueprint "auth": login, logout, sessions)
│   ├── dashboard.py             (blueprint "dashboard": ticket list, SLA cards)
│   ├── tickets.py               (blueprint "tickets": create, view, update)
│   ├── reports.py               (blueprint "reports": CSV export)
│   ├── account.py               (blueprint "account": change your password)
│   ├── admin.py                 (blueprint "admin": accounts, audit log)
│   ├── ticket_filters.py        (filters shared by dashboard and export)
│   ├── cli.py                   (db-status, init-db, assignment-audit,
│   │                             reset-password, audit-archive, audit-verify-archive)
│   └── clock.py                 (the one clock every route reads)
├── database.py
├── accounts.py                  (account administration rules and audit events)
├── audit_archive.py             (audit archive export and verification)
├── sla.py
├── sla_storage.py
├── csv_export.py
├── setup_accounts.py
├── migrations.py
├── security_config.py
├── validation.py
├── proxy_trust.py
├── scripts/
│   └── build_password_blocklist.py
├── security_data/
│   ├── README.md                (blocklist sources, checksums and limits)
│   └── common_passwords.txt.gz
├── docs/
│   └── AUDIT_RETENTION.md
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
├── tests/
│   ├── test_account_setup.py
│   ├── test_account_administration.py
│   ├── test_password_change.py
│   ├── test_audit_migration.py
│   ├── test_password_policy.py
│   ├── test_forced_password_change.py
│   ├── test_login_audit.py
│   ├── test_reset_password_cli.py
│   ├── test_audit_archive.py
│   ├── account_helpers.py       (shared fixture for account tests)
│   ├── test_assignment_validation.py
│   ├── test_authentication.py
│   ├── test_authorization.py
│   ├── auth_helpers.py          (shared sign-in helper for route tests)
│   ├── security_helpers.py      (private app copies for security tests)
│   ├── test_application_factory.py
│   ├── test_csrf.py
│   ├── test_database_authentication.py
│   ├── test_database_filters.py
│   ├── test_input_limits.py
│   ├── test_logout.py
│   ├── test_migrations.py
│   ├── test_migration_cli.py
│   ├── test_migration_upgrades.py
│   ├── test_pagination_db.py
│   ├── test_pagination_integration.py
│   ├── test_pagination_route.py
│   ├── test_production_config.py
│   ├── test_proxy_trust.py
│   ├── test_rate_limit.py
│   ├── test_search_route.py
│   ├── test_security_headers_cookies.py
│   ├── test_session_revocation.py
│   ├── test_sla_dashboard_db.py
│   ├── test_sla_dashboard_routes.py
│   ├── test_sla_policy.py
│   ├── ...                      (SLA, CSV and history tests)
│   ├── test_ticket_ownership.py
│   └── test_ticket_search.py
├── templates/
│   ├── base.html
│   ├── admin_users.html
│   ├── admin_user_new.html
│   ├── admin_audit.html
│   ├── change_password.html
│   ├── login.html
│   ├── index.html
│   ├── new_ticket.html
│   └── ticket_detail.html
└── static/
    ├── app.js
    └── style.css
```

### Important Python files

| File | Purpose |
|---|---|
| `app.py` | Entry point: builds the application with `create_app()` for `python app.py`, `flask --app app` and WSGI servers (`app:app`) |
| `helpdesk/` | The Flask application: factory, security hooks, blueprints and CLI commands (see [Application Architecture](#application-architecture)) |
| `database.py` | SQLite queries and ticket functions, including the SLA dashboard queries; `init_db()` runs the migrations |
| `sla.py` | Response SLA policy: deadlines per priority and status rules |
| `sla_storage.py` | Stores SLA deadlines and first responses; reads stored UTC timestamps |
| `csv_export.py` | Builds safe CSV files from ticket rows |
| `setup_accounts.py` | Initial administrator setup and password changes from the terminal |
| `accounts.py` | Account administration rules, password changes and the audit log, used by the admin and account blueprints |
| `migrations.py` | Versioned, transaction-safe schema migrations (replaces the retired `migrate.py`) |
| `security_config.py` | Validates environment settings and defines cookies, headers and the CSP |
| `validation.py` | Server-side text and request size limits, and the password policy |
| `audit_archive.py` | Copies old audit events to a checksummed archive and verifies it; never deletes |
| `proxy_trust.py` | Believes forwarded headers only from configured proxies |
| `tests/` | 1,062 automated tests covering the application factory, account policy and recovery, auth, CSRF, ownership, search, pagination, CSV export, history, assignment, SLA, migrations and production security |

The local `helpdesk.db` database and `.env` file are excluded from version control.

---

## Application Architecture

The whole web application used to live in one 1,500-line `app.py`. It now uses two standard Flask patterns: an **application factory** and **blueprints**. Nothing a user can see changed: every URL, form, page, status code and security rule is the same.

### What is an application factory?

A *factory* is a function whose job is to build and return a new object. `create_app()` in `helpdesk/__init__.py` builds a complete, configured Flask application each time it is called:

```python
from helpdesk import create_app

app = create_app()          # reads APP_ENV, SECRET_KEY, ... from the environment
```

Before, the application was created at the top of `app.py` the moment the file was imported, so there could only ever be one, configured by whatever environment happened to be set at import time. With a factory:

- **Configuration happens in one place, in a fixed order.** Reading `create_app()` from top to bottom shows everything the application is made of.
- **Tests can build as many independent applications as they need.** A test can build a production application and a development application side by side, each with its own settings, secret key and rate-limit counters, without reloading modules:

  ```python
  app = create_app(environ={"APP_ENV": "testing", "SECRET_KEY": "..."},
                   config={"WTF_CSRF_ENABLED": False})
  ```

  Passing `environ` means `.env` is never read, so the test is isolated from your local settings.
- **Unsafe settings still stop the program before anything is served.** `create_app()` raises `ConfigurationError` exactly as the old import did.

`create_app()` does these steps, in this order:

1. Load `.env` (development only, and only when `environ` is not given).
2. Validate the security settings (`security_config.load_settings`).
3. Create the Flask object and apply cookie, CSRF, size-limit and session settings.
4. `register_security()`: proxy handling, HTTPS check, CSRF, rate limiter, security headers.
5. `register_error_handlers()`: plain-text 400 and 413, and the 503 shown while the database schema is not ready.
6. Register `load_current_user()`, which reads the signed-in account from the database before every view.
7. Register the four blueprints.
8. Attach the login rate limit and the CLI commands.

### What is a blueprint?

A *blueprint* is a group of related routes that can be plugged into an application. It is written like a small application (`@bp.route(...)` instead of `@app.route(...)`), and does nothing until `create_app()` registers it.

| Blueprint | File | URLs | Purpose |
|---|---|---|---|
| `auth` | `helpdesk/auth.py` | `/login`, `/logout` | Signing in and out. Also provides `login_required`, `staff_required` and `load_current_user()` for every other blueprint |
| `dashboard` | `helpdesk/dashboard.py` | `/` | Ticket list with search, filters, SLA monitoring cards and pagination |
| `tickets` | `helpdesk/tickets.py` | `/tickets/new`, `/tickets/<id>`, `/tickets/<id>/update`, `/tickets/<id>/comment` | Creating, viewing, updating, assigning and commenting on tickets, with the ownership rules |
| `reports` | `helpdesk/reports.py` | `/tickets/export` | CSV export of the tickets the user may see |

Account administration has its own blueprints, `admin` (`/admin/users`, `/admin/audit`) and `account` (`/account/password`). Server-side recovery and maintenance run through the command line (`flask --app app ...` and `setup_accounts.py`).

Blueprints keep each file focused on one job, so a change to the CSV export cannot accidentally break login, and each file is short enough to read in one sitting.

### Endpoint names

Flask gives every route an *endpoint* name, used by `url_for()` to build links. Inside a blueprint the name is prefixed with the blueprint's name. The URLs did not change, but the names did:

| Old endpoint | New endpoint | URL |
|---|---|---|
| `index` | `dashboard.index` | `/` |
| `login` | `auth.login` | `/login` |
| `logout` | `auth.logout` | `/logout` |
| `new_ticket` | `tickets.new_ticket` | `/tickets/new` |
| `ticket_detail` | `tickets.ticket_detail` | `/tickets/<id>` |
| `update_ticket` | `tickets.update_ticket` | `/tickets/<id>/update` |
| `add_comment` | `tickets.add_comment` | `/tickets/<id>/comment` |
| `export_tickets` | `reports.export_tickets` | `/tickets/export` |

Write `url_for("tickets.ticket_detail", ticket_id=3)`, never a hard-coded `"/tickets/3"`. The old names no longer exist, so a forgotten one fails immediately with a `BuildError` instead of producing a broken link. A test checks that every `url_for()` in the templates resolves.

### Security architecture

Every security hook is attached to the **application**, not to a blueprint, so it protects every route in every blueprint, including routes added later. Each runs once per request, in this order:

```text
request
  │
  ├─ TrustedProxyMiddleware   forwarded headers believed only from TRUSTED_PROXY_IPS
  ├─ Host check               only TRUSTED_HOSTS are served
  ├─ require_https            production refuses plain HTTP (400)
  ├─ CSRF protection          every POST needs a valid token (400)
  ├─ rate limiter             5 login attempts per minute per address (429)
  ├─ load_current_user        account, role, active flag and session version read from SQLite
  │
  ├─ view (blueprint route)   login_required / staff_required / ownership 404
  │
  └─ add_security_headers     CSP, X-Frame-Options, no-store, HSTS (production HTTPS) on every response
```

Each application gets its **own** CSRF and rate-limiter objects. A rate limiter keeps its counters on the object, so two applications sharing one would also share login attempt counts.

### Database connections and transactions

The factory changed nothing in the database layer. Every function in `database.py` opens its own short-lived connection and closes it in a `finally` block. Writes run inside `with conn:` (and `BEGIN IMMEDIATE` for ticket updates), so a ticket change and its history rows are saved together or not at all. Tests check that every connection opened during a request, including error paths and CLI commands, is closed.

`database.DB_PATH` is still one setting for the whole process. Tests point it at a temporary file. Two applications in one process always use the same database file.

### Development commands

```bash
python app.py                        # development server on http://127.0.0.1:5000
flask --app app routes               # list every URL, method and endpoint
```

`python app.py` refuses to start in production and when the schema is not up to date. `flask --app app run` also starts a development server, but skips both of those checks, so prefer `python app.py`.

### Database commands

```bash
flask --app app db-status            # read-only; exit code 0 only if the schema is current
flask --app app init-db              # create the database or apply pending migrations
flask --app app init-db --backup-to backup.db   # verified backup first, then migrate
flask --app app assignment-audit     # read-only; lists tickets with ineligible assignees
flask --app app reset-password NAME  # recover a lost password (hidden prompt; server access only)
flask --app app audit-archive --dry-run          # events older than 12 months
flask --app app audit-archive --to FILE          # archive them (nothing is deleted)
flask --app app audit-verify-archive FILE        # read-only; check an archive
python setup_accounts.py             # first administrator and password changes
```

### Testing commands

```bash
python -m unittest discover -s tests -v                                   # everything
python -m unittest discover -s tests -p "test_application_factory.py" -v  # factory and blueprints
```

To freeze time in a test, patch the single clock: `helpdesk.clock.current_time` (session ages) or `helpdesk.clock.datetime` (SLA statuses on the dashboard, ticket page and CSV export).

### Adding a new route

1. Pick the blueprint whose job the route belongs to (for example, a new report goes in `helpdesk/reports.py`).
2. Write the view with the blueprint's decorator, and protect it:

   ```python
   @bp.route("/tickets/summary")
   @login_required                     # or @staff_required for staff-only pages
   def ticket_summary():
       filters = current_ticket_filters()   # ownership comes from the account
       ...
   ```

3. Link to it with `url_for("reports.ticket_summary")`.
4. Write tests: signed-out visitors are redirected, other users' data is not shown, and POST forms are refused without a CSRF token.

You do **not** need to add CSRF, security headers, HTTPS checks or account loading to a new route. The application applies them automatically.

To add a completely new area, create `helpdesk/<name>.py` with `bp = Blueprint("<name>", __name__)` and add `<name>.bp` to `BLUEPRINTS` in `helpdesk/__init__.py`.

---

## How the Application Works

### Flask

Flask handles HTTP requests and connects URLs to Python functions.

For example, visiting `/tickets/3` calls `ticket_detail()` in `helpdesk/tickets.py`, which displays ticket number 3.

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

### Database-Backed Authentication

The session cookie stores a stable `user_id`, the account's `session_version` and the login time. It never stores a role. Before each view runs:

1. A `before_request` hook reads `user_id` from the session.
2. It loads the current account from SQLite.
3. If the account exists, is active, the session has not expired and its version matches, the account is stored in `flask.g` as `g.user`.
4. Otherwise the session is cleared and the visitor must log in again.
5. If the database cannot be read, the request gets a **503** and no access.

Older session formats are missing these values or contain a stored `username` and `role` that may be out of date. They are discarded, so **everyone must log in once after upgrading**.

Routes declare their access rule with a decorator:

| Decorator | Who may continue |
|---|---|
| `@login_required` | Any verified, active account |
| `@staff_required` | Active technicians and administrators |

To deactivate or reactivate an account until an admin screen exists, **back up the database first**, then run:

```bash
sqlite3 helpdesk.db "UPDATE users SET is_active = 0 WHERE username = 'example';"
sqlite3 helpdesk.db "UPDATE users SET is_active = 1 WHERE username = 'example';"
```

Prefer deactivating over deleting. Tickets store the creator's **username**, so a new account created later with a deleted user's name would gain access to that user's old tickets.

### Session Expiration and Revocation

Flask keeps the session in a signed browser cookie. The signature stops anyone from editing it, but a copy of the cookie remains valid unless the server has a way to refuse it. Each account therefore has a `session_version` number in SQLite:

- Login copies the current number into the cookie.
- Every request compares the cookie's number with the database.
- Revoking adds 1 to the database value, so every cookie holding the old number stops working.

| Event | Effect on sessions |
|---|---|
| Logout | Ends **all** sessions for that account, on every device |
| Password change | Ends all sessions for that account |
| Deactivation | Ends all sessions; reactivating does not restore them |
| Account deletion | The account lookup fails, so its sessions stop working |
| Role change | Applies on the next request; the user stays logged in with the new role |
| 8 hours after login | The session expires, even if the user is active |

**Logout is account-wide.** Logging out on a laptop also logs out the same user's phone, because they share one version number. Ending just one device would need a server-side `sessions` table with one row per login.

A database trigger handles password and activation changes, so they revoke sessions even when made directly with SQL. Logout uses a single atomic `UPDATE ... WHERE session_version = ?`: simultaneous logouts increment the version once, and an old replayed cookie cannot sign out a newer session.

If the database cannot be updated during logout, this browser is still logged out, and a warning explains that other sessions could not be ended. If a session cannot be verified, the request receives a 503 and no access.

### Password Hashing

Werkzeug converts passwords into one-way hashes. When a user logs in, the submitted password is checked against the stored hash.

### Account Management

Administrators manage accounts in the browser (see [Web Account Administration](#web-account-administration)). The `setup_accounts.py` script remains for creating the first administrator and for terminal password changes; it uses `getpass` so passwords never appear in source code or shell history.

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

## Staff Assignment Rules

A ticket can be assigned only to an account that can actually work on it. The rules are enforced on the server, in `database.update_ticket()`, inside the same database transaction that saves the change. The HTML form is a convenience, not the protection.

### Who can receive a ticket

| Account | Can be assigned? |
|---|---|
| Active technician | Yes |
| Active administrator | Yes |
| Regular user | No |
| Account with any other role (for example a typo such as `superuser`) | No |
| Deactivated account (`is_active = 0`) | No |
| Deleted or nonexistent account | No |

- **Only technicians and administrators can change an assignment.** Regular users, signed-out visitors and deactivated or demoted staff are refused before anything is read from the form.
- **Names must match exactly.** `TECH`, `tech ` (after trimming, spaces are removed like any other field) and look-alike Unicode letters are not the account `tech`.
- **Checked at save time.** If an account is deactivated after the page was loaded, saving still refuses it.
- **All or nothing.** A refused update saves nothing, not even a status change made in the same form, and records no history.
- **One message for every refusal.** The page says only that tickets can go to active technicians or administrators. It never says whether the name exists or is deactivated, so the form cannot be used to discover accounts. The exact reason is written to the server log.
- **Unassigning is always allowed.**
- **Assignment does not grant access.** Technicians and administrators can already see every ticket; regular users still see only tickets they created.

### Tickets already assigned to someone who no longer qualifies

Older versions accepted any name, and staff leave. These assignments are **kept exactly as stored**; nothing is deleted or rewritten automatically.

- The ticket page shows staff a warning naming the reason (for example "account is deactivated").
- The form keeps that name selected, labelled "current, no longer eligible", so saving a **status** change keeps the assignment instead of silently unassigning the ticket. (Before this change the browser fell back to "Unassigned" and saving removed the assignment.)
- Choosing a different, eligible person or "Unassigned" works normally, and the history records the old name.
- To list every such ticket without changing anything:

```bash
flask --app app assignment-audit
```

It opens the database read-only, prints each ticket number, assignee and reason, and exits with status 1 if any were found.

### Usernames are permanent (migration 7)

Tickets, comments and history refer to people by **username**. If an account were deleted and someone new were given the same name, the newcomer would see the old person's tickets and appear as the author of their comments. Migration 7 adds two database triggers so this cannot happen, even through manual SQL:

| Trigger | Effect |
|---|---|
| `users_username_immutable` | A username can never be changed. |
| `users_no_historical_username_reuse` | A new account cannot use a name that appears in any ticket's creator or assignee, any comment author, or any history entry. |

Deleting and deactivating accounts still work. A name that was never used in any ticket can be created again. If `setup_accounts.py` finds the name `admin` reserved this way, it explains why and creates nothing.

**Should assignments use user IDs instead?** This was investigated. Storing `users.id` would also make references stable, but with these triggers a username already cannot change or pass to someone else, which gives the same protection. Moving to IDs would mean rewriting the ownership queries, the `idx_tickets_owner_date` index, history and CSV export, and older tickets that name deleted accounts could not be converted because no ID exists for them. It is therefore proposed as a future migration (see [Proposed future migrations](#proposed-future-migrations)), not done now.

### Upgrading an existing database to version 7

Migration 7 adds only the two triggers. It does not change any existing ticket, comment, history event, SLA row or account. As with every migration, the application refuses to start until it has been applied:

```bash
flask --app app db-status
flask --app app init-db --backup-to backups/helpdesk-before-v7.db
flask --app app assignment-audit
```

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

### SLA definitions

Five different events happen to a ticket. Only one of them counts as the response:

| Event | Recorded where | Effect on the response SLA |
|---|---|---|
| **Creation** | `tickets.created_at` | Starts the clock. The deadline is creation time + the priority's target, stored once in `ticket_sla.response_due_at`. |
| **Assignment** to a staff member | `assigned`/`reassigned` history events | None. Assigning is routing, not a reply to the requester. |
| **First meaningful staff response** | `ticket_sla.first_response_at` | Stops the clock. It is the time of the first comment written by a technician or administrator on a ticket somebody else created. |
| **Status change** (Open, In Progress) | `status_changed` history events | None. |
| **Resolution or closure** (Resolved, Closed) | `status_changed` history events | None. Closing a ticket is not a reply. |

Rules that follow from this, all covered by tests:

- **Who can respond:** an account whose role is `technician` or `admin` at the moment it comments. Comments by the requester, by ordinary users, or by a technician on a ticket they raised themselves do not count. All comments are visible to the requester, so a staff comment is a reply they can read.
- **Recorded once:** later comments never replace the first response, and the stored time is the comment's own database time, not the time it was processed.
- **The deadline never moves:** changing status or assignment does not change it.
- **Reopened tickets:** a ticket moved from Resolved or Closed back to Open keeps its first response and its Met or Breached outcome; no new deadline is created. A reopened ticket that was never answered is still waiting for its first response.
- **Closed or resolved without any response:** the status stays **Overdue** once the deadline passes (or Within SLA / Due Soon before it). Closing does not hide a missed response. This is the existing, documented policy and has been kept.
- **Cancelled tickets:** there is no Cancelled status. A ticket closed by mistake follows the rule above.
- **Business hours:** none. Nights, weekends and holidays count. A Low ticket raised on Friday at 18:00 UTC is due Monday at 18:00 UTC.
- **Time zones:** every SLA time is stored and compared in UTC, and shown with a `UTC` label on the ticket page, the dashboard and the CSV export.
- **Historical tickets:** tickets created before SLA tracking stay Not Tracked. No deadline or response is calculated for them afterwards, and stored SLA rows are never recalculated.

### Unresolved business decisions

These need a decision from the service owner. They are deliberately **not** implemented, because they would change what existing reports mean:

1. **Business hours and holidays.** Should targets count only working hours? If so: which hours, which time zone, and which holiday calendar? This would be a new policy version (for example `business-hours-v1`) applied only to new tickets, so existing deadlines keep their meaning.
2. **Closed without a response.** Should these show as **Breached** instead of **Overdue**, so that Overdue means "still needs action"? Either way the missed response must remain visible.
3. **Resolution SLA.** Only a *response* target exists. Is a time-to-resolve target wanted, and should reopening pause or restart it?
4. **Pausing the clock** while waiting for the requester (an "On hold" or "Waiting for customer" status).
5. **What counts as meaningful.** Any non-empty staff comment counts today. Should very short or template replies be excluded? Software cannot judge this reliably.
6. **Priority changes.** Priority cannot be changed after creation today. If that is added, should the deadline be recalculated?
7. **Cancelled status.** Should there be one, and should cancelled tickets be left out of SLA counts?

### Proposed future migrations

None of these have been applied. Each needs a decision and explicit approval first, and a backup before it runs.

| Proposal | What it would do | Why it is not done now |
|---|---|---|
| `tickets.assigned_to_user_id` and `tickets.created_by_user_id` | Refer to accounts by permanent numeric ID, filled in from the current username where an account still exists | Usernames are already permanent (migration 7); tickets naming deleted accounts could not be filled in; ownership queries and indexes would all change |
| `ticket_sla.resolved_at` | Record when a ticket was first resolved | Depends on decision 3; would need a documented rule for existing tickets (left empty, not reconstructed from history) |
| New SLA policy version | Business-hours deadlines for new tickets only | Depends on decision 1 |

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

**The same answer everywhere.** The ticket page, the dashboard rows, the summary cards and the CSV export all use these definitions and are tested against each other at one fixed moment. The ticket page shows **Not Tracked** too, instead of showing nothing.

### Using the SLA dashboard

- **Summary cards** count tickets in each status across everything you may see, not just the current page or search. They show the UTC time they were calculated at.
- **Click a card**, or use the **SLA** select next to the status and priority filters, to list only tickets in that status. Search, status, priority and pagination all keep working with it.
- The **Response SLA** column shows each ticket's status, deadline and first response time, in UTC. Overdue rows have a red edge and due-soon rows an amber one, and the status is always written out, so colour is never the only signal.
- **Export CSV** exports the tickets matching the current filters, including the SLA filter. After the original nine columns (which keep their positions) it adds **SLA Status**, **Response Due (UTC)** and **First Response (UTC)**, calculated by the dashboard's own SQL at the moment of the export. Not Tracked tickets have empty SLA times.

The SLA filter is in the URL, for example `/?sla=overdue&priority=High&page=2`. Accepted values are `within`, `due_soon`, `overdue`, `met`, `breached` and `not_tracked`. Any other value is ignored.

### How the SLA dashboard works

1. The route takes **one UTC timestamp** (`now`) per request and passes it to every query, so the cards, the filter, the page total and each row agree.
2. `database.py` turns the SLA rules into one SQL `CASE` expression, joined to the tickets with `LEFT JOIN ticket_sla`. Tickets without an SLA row become Not Tracked.
3. The SLA filter is part of the SQL `WHERE` clause, so it runs **before** `LIMIT`/`OFFSET`, and the count uses the same filter.
4. The summary cards come from one `GROUP BY` query. Only six numbers reach Python.
5. The ticket page still uses the original Python SLA code, given the same clock as the dashboard. Tests check that the SQL and Python give the same status for every stored timestamp format and boundary, including a seeded fuzz test of damaged values.

### Database performance

`EXPLAIN QUERY PLAN` shows the dashboard queries use the existing indexes:

- staff pages read tickets in order from `idx_tickets_date`; regular users' from `idx_tickets_owner_date`
- each ticket's SLA row is found by primary key (`ticket_sla.ticket_id`), so there is no extra query per ticket and no scan of `ticket_sla`
- the page order comes straight from the index, with no sorting step

No new index was added. An SLA status depends on the current time, so it cannot be stored in an index. Filtering by SLA status therefore checks each permitted ticket inside SQLite. That is fine at helpdesk scale.

### Stored timestamps

Python and SQLite each read dates differently on their own. For example, Python accepted `20261117T090000` and `2026-W47-2`, while SQLite accepted `2026-02-30` (quietly moving it to 2 March), year `0000` and a trailing space. Each of those made the ticket page and the dashboard disagree about the same ticket. Both now accept exactly one format:

```text
YYYY-MM-DD HH:MM:SS     (a T or a space between date and time)
  optional .f / .ff / .fff         up to milliseconds
  optional Z or +HH:MM / -HH:MM    offset up to 14:59; none means UTC
```

This covers everything the application writes. Text without an offset (like SQLite's `CURRENT_TIMESTAMP`) is UTC; text with `+05:30`, `-05:00` or `Z` is converted to UTC. Anything else, such as `"2461000"`, `"now"`, a date without a time, or an impossible date, is unreadable: the ticket page, the dashboard and the CSV all show **Not Tracked**. The rule lives in `sla_storage.py` as a Python regular expression and a matching list of SQL `GLOB` patterns, and 62,700 generated values were checked to give identical results on both sides. Stored values are never rewritten; an unreadable row simply stays Not Tracked.

### SLA and ticket ownership

The SLA cards, rows and filter use exactly the same ownership rule as the ticket list. Regular users get counts and deadlines for their own tickets only; technicians and administrators see every ticket. The `?sla=` value is checked against a fixed allow list before it reaches the database. Adding `?username=` or `?role=` still has no effect.

---

## Production Security Configuration

The application chooses its security behaviour from `APP_ENV`. Local development needs no new settings. **This project has not been deployed publicly**, and the deployment steps below are requirements to meet first.

### Development compared with production

| Setting | `development` (default) | `testing` | `production` |
|---|---|---|---|
| Reads `.env` | Yes | No | No |
| `SECRET_KEY` | Required | Required | Required, 32+ characters, not a placeholder or repeated pattern |
| Debug mode | Allowed | Allowed | Startup is refused if `FLASK_DEBUG` is on |
| `python app.py` | Runs | Runs | Refused; use a production WSGI server |
| Accepted hosts | `localhost`, `127.0.0.1` | `localhost`, `127.0.0.1` | Only `TRUSTED_HOSTS` |
| Plain HTTP | Works | Works | Refused with 400 |
| Cookie name | `session` | `session` | `__Host-helpdesk_session` |
| Cookie `Secure` | Off, so HTTP login works | Off | On |
| HSTS | Never | Never | HTTPS responses only |

If production settings are missing or unsafe, the application stops before accepting traffic. Errors name the variable but never print its value.

### Production environment variables

Set these in the service environment. `.env` is deliberately ignored in production.

| Variable | Required | Example | Meaning |
|---|---|---|---|
| `APP_ENV` | Yes | `production` | Turns on every production check |
| `SECRET_KEY` | Yes | Output of `python3 -c "import secrets; print(secrets.token_hex(32))"` | Signs session cookies and CSRF tokens |
| `TRUSTED_HOSTS` | Yes | `helpdesk.example.org` | Comma-separated exact hostnames, with no scheme, port or wildcard |
| `TRUSTED_PROXY_COUNT` | Behind a proxy | `1` | Number of reverse proxies in front of the app (0 to 5) |
| `TRUSTED_PROXY_IPS` | Behind a proxy | `127.0.0.1` | Comma-separated proxy IPs or CIDR ranges |

### Secure cookies and sessions

- **HttpOnly** stops JavaScript from reading the session cookie, which limits what a script injection can steal.
- **SameSite=Lax** keeps the cookie off cross-site form posts while normal links to a ticket still work.
- **Secure** means browsers send the cookie only over HTTPS. It is off in development because `http://127.0.0.1:5000` would otherwise lose its login.
- **The `__Host-` prefix** makes browsers reject the cookie unless it is Secure, host-only and for path `/`, so a sibling subdomain cannot plant a session.
- The 8-hour limit, logout revocation and database account checks work the same in every environment.

### HTTP security headers

| Header | Value | Purpose |
|---|---|---|
| `Content-Security-Policy` | See below | Limits what the browser may load or run |
| `X-Content-Type-Options` | `nosniff` | Stops browsers guessing file types |
| `X-Frame-Options` | `DENY` | Blocks clickjacking in older browsers |
| `Referrer-Policy` | `same-origin` | Keeps URLs private from other sites while allowing the HTTPS CSRF check |
| `Permissions-Policy` | Camera, location, microphone, payment and USB off | Disables browser features the app never uses |
| `Cross-Origin-Opener-Policy` | `same-origin` | Isolates the page from windows opened by other sites |
| `Cross-Origin-Resource-Policy` | `same-origin` | Stops other sites embedding responses |
| `Cache-Control` | `no-store` on all non-static responses | Keeps tickets and CSRF tokens out of caches |
| `Strict-Transport-Security` | `max-age=31536000` | Production HTTPS only. Subdomains and preload are left out because they are hard to undo |

### Content Security Policy

The policy starts from `default-src 'none'` and allows only what the pages use. It contains no `unsafe-inline`, `unsafe-eval` or wildcards, so inline styles and event handlers were moved to `static/style.css` and `static/app.js`.

Documented exceptions:

- **Three exact CDN files and the icon font folder.** Bootstrap and Bootstrap Icons load from cdnjs. Whole-CDN access is avoided because cdnjs hosts old libraries an attacker could abuse, and each file also has a Subresource Integrity hash.
- **`img-src data:`.** Bootstrap's CSS draws its menu, close and select icons as `data:` SVG images. Images cannot run script here.

### Input limits

| Input | Limit | When exceeded |
|---|---|---|
| Whole request body | 1 MiB | 413 |
| Multipart form parts | 50 | 413 |
| Ticket title | 200 characters, single line | 400, form returned with your text |
| Ticket description | 10,000 characters | 400, form returned with your text |
| Comment | 10,000 characters | 400, page returned with your text |
| Assignee | 150 characters, single line, and an active technician or administrator | 400 for length or characters; refused with a message for an ineligible account |
| Login username / password | 150 / 1,024 characters | Normal "invalid" message, without hashing |

Text is **never silently shortened**. Limits count Unicode characters, so an emoji is one character on the server. Browsers count an emoji as two for `maxlength`, so the form can stop slightly earlier than the server would. Windows line endings count as one character, and control characters are rejected.

### Reverse proxy and forwarded headers

Any client can send `X-Forwarded-For: 1.2.3.4`. If the app believed it, an attacker could change "address" on each attempt and avoid the login rate limit. So:

- `X-Forwarded-For` and `X-Forwarded-Proto` are used **only** when the TCP connection comes from `TRUSTED_PROXY_IPS`. Otherwise they are deleted.
- `TRUSTED_PROXY_COUNT` selects the value written by your proxy, counting from the right. Values further left may be client-supplied.
- `X-Forwarded-Host`, `-Port`, `-Prefix` and `Forwarded` are always deleted. The proxy must pass the original `Host` header, which `TRUSTED_HOSTS` checks.

An example Caddy site on the same machine:

```caddyfile
helpdesk.example.org {
    reverse_proxy 127.0.0.1:8000
}
```

```bash
APP_ENV=production
TRUSTED_HOSTS=helpdesk.example.org
TRUSTED_PROXY_COUNT=1
TRUSTED_PROXY_IPS=127.0.0.1
```

### HTTPS deployment requirements

Before any real deployment:

1. Terminate HTTPS at a reverse proxy and redirect HTTP to HTTPS.
2. **Bind the app to a private address** such as `127.0.0.1:8000`, and firewall it so only the proxy can connect. With a loopback proxy address, every local process is trusted as a proxy.
3. Use a production WSGI server. None is included yet.
4. Run **one worker process**, or move rate limiting to shared storage such as Redis, because the current counters are per process.
5. Back up the database and run `flask --app app db-status` before starting.
6. Change any old demonstration passwords.

### Security testing

```bash
python -m unittest tests.test_production_config tests.test_security_headers_cookies tests.test_input_limits tests.test_proxy_trust -v
python -m pip_audit --strict -r requirements.txt
```

These tests build a separate application for each environment and use temporary databases only. Older tests load a private copy of `app.py` (`tests/security_helpers.py`); newer ones call `create_app(environ=...)` directly.

### Remaining limitations

- Not deployed or tested behind a live Caddy instance or production WSGI server.
- Login response time can reveal whether a username exists, because unknown users skip password hashing.
- Rate limits are in memory, so they reset on restart and are not shared between workers.
- `APP_ENV` defaults to development. A server started without it accepts only localhost hostnames, but it does not enforce HTTPS.
- `flask run` cannot be blocked in production by the app itself, so the deployment must use a WSGI server.
- Every `flask --app app ...` command (including `db-status` and `init-db`) loads `.env` through Flask's own CLI, even when `APP_ENV=production`. It never overrides a variable that is already set, but it can fill in missing ones. On production servers, set `FLASK_SKIP_DOTENV=1` or keep no `.env` file.
- Assignees must be active technicians or administrators; tickets assigned before this rule are kept and flagged rather than changed.
- Bidirectional text control characters are allowed, so ticket text can display in a misleading order.
- Pages depend on the cdnjs CDN. Integrity hashes stop altered files, but an outage would remove styling.
- SQLite suits a small, single-server deployment only.

---

## Automated Testing

The project includes 1,062 automated tests using Python's built-in `unittest` framework. CI runs them on Python 3.13.

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
| `test_account_administration.py` | 36 | Admin-only access (403 for other roles, nothing changed), CSRF, creating accounts and every refused input, role changes, deactivation and reactivation ending sessions, password resets, self-protection, actor re-checked in the transaction, rollback, audit events and the audit page |
| `test_password_change.py` | 14 | Current password required, this browser kept and other sessions ended, copied cookies refused, refused changes save nothing, concurrent changes detected, rate limit and CSRF |
| `test_audit_migration.py` | 8 | Migration 8 keeps every row (including the historical invalid assignee), append-only triggers, schema check, `setup_accounts.py` audit events |
| `test_password_policy.py` | 26 | 16 to 1,024 characters, any characters, no class rules, offline blocklist (checksum, normalisation, fails closed, case-insensitive), username and pattern rules, the deterministic list builder, and the policy applied by every way of setting a password |
| `test_forced_password_change.py` | 23 | Who must change, every other page and form blocked, logout allowed, change restores access, copied cookies, expiry, deactivation, CSRF and rate limit while restricted, migration 9 upgrade, rollback and schema check |
| `test_login_audit.py` | 16 | Success and failure records, no passwords, cookies or unknown usernames stored, forged forwarded headers ignored, identical responses and dummy hashing for unknown names, rate limit before recording, high-volume failures, audit filters, database failures |
| `test_reset_password_cli.py` | 17 | Reset ends sessions, forces a change and is audited; lost last administrator recovered; no password arguments or piped input; deactivated, unknown, weak and mismatched refused; atomic rollback; race detection; operator name not spoofable |
| `test_audit_archive.py` | 23 | Archive contents, private permissions, manifest and audit event, no overwrite, rollback and cleanup, triggers intact, verification of tampering and malformed lines, 12-month cutoff, CLI exit codes, no deletion command |
| `test_authorization.py` | 3 | Only staff may update ticket status |
| `test_session_revocation.py` | 106 | Copied-cookie replay, expiry, revocation, concurrency, legacy sessions, failures and migration 6 |
| `test_database_authentication.py` | 80 | Real account checks: deleted, deactivated and demoted users, legacy sessions, ownership, migration 5, database failures, CSRF and rate limiting |
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
| `test_production_config.py` | 32 | Unsafe secrets, debug restrictions, startup refusal, host and proxy settings |
| `test_security_headers_cookies.py` | 37 | Cookies, revocation, expiry, CSRF, headers, CSP, template compatibility and main workflows in production |
| `test_input_limits.py` | 47 | Body, field, Unicode, login, assignee and ticket-number limits |
| `test_proxy_trust.py` | 23 | Forwarded-header trust, rate-limit spoofing, HTTPS detection and Host validation |
| `test_assignment_validation.py` | 45 | Valid, missing, deleted, deactivated and wrong-role assignees; tampered requests; refusal saves nothing; former assignees kept; the audit command; username reuse and renaming; migration 7 |
| `test_sla_policy.py` | 30 | Deadlines; what is and is not a first response; reopened, closed and unanswered tickets; ticket page, dashboard and CSV agreeing; time zones; the shared timestamp format |
| `test_application_factory.py` | 70 | `create_app()` settings; independent applications (config, HTTPS, headers, secret keys, rate-limit counters); exact URL map and endpoint names; every template `url_for()`; security hooks registered once and in order; auth, ownership, sessions, CSRF and headers on every blueprint; tickets, assignment, SLA and CSV; CLI commands; `.env` rules; production failures; every SQLite connection closed |

Tests never touch the real `helpdesk.db`:

- **Database tests** redirect `DB_PATH` to a temporary SQLite file.
- **Route tests** mock the database functions, and point `DB_PATH` at a location that cannot be opened, so an accidental unmocked query fails loudly instead of reading real data.
- **Integration tests** mock nothing. Each request runs through the real route, real SQL against a temporary database, and the real template, and several tests follow the rendered **Next** links page by page.

Coverage is still not exhaustive. Concurrent migration attempts are tested, but general load testing, request-level concurrency testing and automated browser testing remain outstanding.

---

## Security Limitations

**This project is intended for local development and educational use. It has production security settings but has not been deployed.** See [Production Security Configuration](#production-security-configuration).

| Security area | Current status |
|---|---|
| Password hashing | Implemented |
| Hardcoded account creation | Removed; the legacy `migrate.py` seeding script has been deleted |
| Database migrations | Versioned, transactional, run explicitly by an operator; never seed accounts |
| Initial administrator setup | Implemented as a local script |
| Password changing | In the browser, by administrators, from `setup_accounts.py`, and `flask reset-password` for recovery |
| Password policy | 16–1,024 characters; offline common/breached-password check; no complexity rules or expiry |
| Forced password change | Required after an administrator sets a password; enforced on every request |
| Sign-in auditing | Successes and failures recorded without secrets; rate limit applies first |
| Username enumeration by timing | Mitigated with a dummy hash for unknown names; small residual differences documented |
| Lost administrator credentials | Recoverable from the server shell only, audited |
| CSRF protection | Implemented on all state-changing forms |
| Login rate limiting | Implemented (5 attempts per minute) |
| Database-backed authentication | Account, active status and role are checked in SQLite on every request |
| Deleted, deactivated and demoted users | Lose access on their next request |
| Session revocation | Logout, password changes and deactivation end all of an account's sessions |
| Session expiry | Absolute 8-hour limit enforced on the server |
| Ticket ownership authorization | Implemented and enforced in SQL |
| Search ownership restrictions | Implemented and tested |
| Pagination ownership restrictions | Applied before paging; counts and pages tested |
| SLA dashboard restrictions | SLA counts, rows and filters use the same ownership rule; tested |
| HTTPS | Required by production mode; must be provided by a reverse proxy, which is not yet deployed |
| Debug mode | Disabled, and production startup is refused if it is requested |
| Secret key validation | Unsafe or missing production keys stop startup |
| Secure cookies | HttpOnly and SameSite everywhere; Secure and `__Host-` in production |
| Security headers and CSP | Implemented with no `unsafe-inline` or `unsafe-eval` |
| Request and field limits | Enforced on the server |
| Reverse-proxy trust | Forwarded headers accepted only from configured proxies |
| Host validation | Implemented with `TRUSTED_HOSTS` |
| Web-based account administration | Administrators only; rules re-checked in the database transaction; audited; tested |
| Self-service password change | Current password required; rate limited; other sessions ended; tested |
| Administrative audit log | Append-only (enforced by triggers); no passwords recorded; archiving without deletion |
| Ticket assignment | Only active technicians and administrators; enforced in the database transaction; tested |
| Username reuse | Usernames cannot be renamed or given to a new account while ticket records use them (migration 7) |
| Automated security testing | 1,062 tests covering password policy, forced changes, sign-in auditing, recovery, audit archiving, account administration, password changes, audit events, the application factory, auth, CSRF, ownership, search, pagination, export, history, assignment, SLA, migrations and production security |

**Session limits:** a stolen cookie remains usable until the user logs out, changes their password, is deactivated, or the 8-hour limit passes. Logout cannot target a single device. A request already being processed when a session is revoked may finish. Local development uses plain HTTP, so it must not be exposed to an untrusted network.

Existing databases may still contain old demonstration credentials. Those passwords must be changed before relying on the updated authentication process.

Security improvements are ongoing.

---

## Current Limitations

- No email notifications
- No automatic SLA alerts; overdue tickets are visible only on the dashboard
- No file attachments
- No end-to-end browser testing
- No load or concurrency testing
- Not deployed; production mode still needs a reverse proxy and WSGI server
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
- [x] Verify accounts, active status and roles against the database on every request
- [x] Add session expiry and account-wide session revocation
- [x] Disable debug mode for deployment
- [x] Add a secure web-based change-password feature
- [x] Add administrator-controlled user creation
- [x] Record account administration in an append-only audit log
- [x] Require a password change after an administrator sets a password
- [x] Reject common and breached passwords (offline)
- [x] Record successful and failed sign-ins
- [x] Server-side recovery of lost administrator credentials
- [x] Archive and verify old audit events
- [ ] Approved design for removing archived audit events
- [ ] Multi-factor authentication
- [x] Add production security configuration, secure cookies and security headers
- [x] Enforce server-side request and field limits
- [ ] Configure HTTPS for production

### Application Features

- [ ] Email notifications
- [ ] File attachments
- [x] Dashboard pagination
- [x] Ticket export to CSV
- [x] Ticket search by title and description
- [ ] Advanced search (date ranges, assignee, multi-field)
- [x] Ticket activity history
- [ ] Additional dashboard reports

### Deployment

- [x] Prepare production configuration
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
