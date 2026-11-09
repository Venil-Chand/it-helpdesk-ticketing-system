
# IT Helpdesk Ticketing System

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
- **Ticket details** — view descriptions, updates and comments
- **Ticket management** — update status and technician assignment
- **Comments** — add notes to existing tickets
- **Account setup** — create the first administrator using a terminal script
- **Password changes** — verify the current password before changing it
- **Automated tests** — 108 tests covering authentication, authorization, CSRF, search and pagination

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

### 5. Set up the administrator account

For a new installation, run:

```bash
python setup_accounts.py
```

If no accounts exist, the program creates the initial `admin` account after prompting for a password.

Passwords must contain at least 16 characters.

No fixed default password is created during database initialization.

### 6. Start the application

```bash
python app.py
```

Open your browser at:

http://127.0.0.1:5000

The application is intended for local development and learning.

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

---

## Project Structure

```text
it-helpdesk-ticketing-system/
├── app.py
├── database.py
├── setup_accounts.py
├── migrate.py
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
│   ├── test_pagination_db.py
│   ├── test_pagination_integration.py
│   ├── test_pagination_route.py
│   ├── test_rate_limit.py
│   ├── test_search_route.py
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
| `database.py` | SQLite tables, queries and ticket functions |
| `setup_accounts.py` | Initial administrator setup and password changes |
| `migrate.py` | Migration from an earlier database structure |
| `tests/` | 108 automated tests covering auth, CSRF, ownership, search and pagination |

The local `helpdesk.db` database and `.env` file are excluded from version control.

---

## How the Application Works

### Flask

Flask handles HTTP requests and connects URLs to Python functions.

For example, visiting `/tickets/3` calls the route responsible for displaying ticket number 3.

### SQLite

SQLite stores application information in a local database file, `helpdesk.db`.

The database contains three main tables:

- `users` — usernames, password hashes and roles
- `tickets` — support requests and their status
- `comments` — notes associated with tickets

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

Adding parameters such as `?username=bob`, `?role=admin` or `?per_page=1000` has no effect. The route reads only `q`, `status`, `priority` and `page`. The user's identity and role come from the signed session cookie, which cannot be altered without the secret key, and never from the URL.

---

## Automated Testing

The project includes 108 automated tests using Python's built-in `unittest` framework.

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

Tests never touch the real `helpdesk.db`:

- **Database tests** redirect `DB_PATH` to a temporary SQLite file.
- **Route tests** mock the database functions, and point `DB_PATH` at a location that cannot be opened, so an accidental unmocked query fails loudly instead of reading real data.
- **Integration tests** mock nothing. Each request runs through the real route, real SQL against a temporary database, and the real template, and several tests follow the rendered **Next** links page by page.

Coverage is still not exhaustive. Load testing, concurrency testing and automated browser testing remain outstanding.

---

## Security Limitations

**This project is intended for local development and educational use. It is not production-ready.**

| Security area | Current status |
|---|---|
| Password hashing | Implemented |
| Hardcoded account creation | Removed from database initialization |
| Initial administrator setup | Implemented as a local script |
| Password changing | Implemented as a local script |
| CSRF protection | Implemented on all state-changing forms |
| Login rate limiting | Implemented (5 attempts per minute) |
| Ticket ownership authorization | Implemented and enforced in SQL |
| Search ownership restrictions | Implemented and tested |
| Pagination ownership restrictions | Applied before paging; counts and pages tested |
| HTTPS | Not configured |
| Debug mode | Disabled (`debug=False`) |
| Web-based account administration | Not implemented |
| Automated security testing | 108 tests covering auth, CSRF, ownership, search and pagination |

Existing databases may still contain old demonstration credentials. Those passwords must be changed before relying on the updated authentication process.

Security improvements are ongoing.

---

## Current Limitations

- No email notifications
- No file attachments
- No web-based password change form
- No administrative user management interface
- No end-to-end browser testing
- No load or concurrency testing
- Not configured for production deployment

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
