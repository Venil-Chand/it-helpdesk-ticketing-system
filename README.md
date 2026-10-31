
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
- **Ticket filtering** — filter tickets by status and priority
- **Ticket details** — view descriptions, updates and comments
- **Ticket management** — update status and technician assignment
- **Comments** — add notes to existing tickets
- **Account setup** — create the first administrator using a terminal script
- **Password changes** — verify the current password before changing it
- **Automated tests** — test access to login and protected pages

---

## User Roles

| Role | Permissions |
|---|---|
| Administrator | Manage support tickets and assignments |
| Technician | Update ticket status, manage assignments and add comments |
| Regular User | Create tickets, view permitted pages and add comments |

The existing application uses three roles. Further authorization improvements are planned, particularly around ticket ownership and access restrictions.

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
│   └── test_authentication.py
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
| `tests/test_authentication.py` | Automated authentication access tests |

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

## Automated Testing

The project includes automated authentication tests using Python's built-in `unittest` framework.

To run the tests:

```bash
python -m unittest discover -s tests -v
```

The initial four tests check:

| Test | Purpose |
|---|---|
| Login page | Confirm `/login` is accessible |
| Dashboard protection | Check that anonymous visitors are redirected |
| New ticket protection | Check that anonymous visitors cannot access the creation page |
| Ticket detail protection | Check that anonymous visitors are redirected |

These four tests have passed in local development.

The current tests do not provide complete security coverage. Further testing is planned for password changes, authorization rules, invalid logins and database operations.

---

## Security Limitations

**This project is intended for local development and educational use. It is not production-ready.**

| Security area | Current status |
|---|---|
| Password hashing | Implemented |
| Hardcoded account creation | Removed from database initialization |
| Initial administrator setup | Implemented as a local script |
| Password changing | Implemented as a local script |
| CSRF protection | Not implemented |
| Login rate limiting | Not implemented |
| Ticket ownership authorization | Requires further review |
| HTTPS | Not configured |
| Debug mode | Must be disabled before deployment |
| Web-based account administration | Not implemented |
| Automated security testing | Basic access tests implemented |

Existing databases may still contain old demonstration credentials. Those passwords must be changed before relying on the updated authentication process.

Security improvements are ongoing.

---

## Current Limitations

- No email notifications
- No file attachments
- No dashboard pagination
- No web-based password change form
- No administrative user management interface
- Limited automated test coverage
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
- [ ] Add CSRF protection
- [ ] Implement login rate limiting
- [ ] Improve ticket ownership authorization
- [ ] Add a secure web-based change-password feature
- [ ] Add administrator-controlled user creation
- [ ] Expand authentication and authorization tests
- [ ] Disable debug mode for deployment
- [ ] Configure HTTPS for production

### Application Features

- [ ] Email notifications
- [ ] File attachments
- [ ] Dashboard pagination
- [ ] Ticket export to CSV
- [ ] Advanced search
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
