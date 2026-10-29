# IT Helpdesk Ticketing System

A Python and Flask web application for managing IT support requests.
Built as a portfolio project by Venil Chand (computing student, Unitec Auckland).

---

## Technologies Used

| Layer | Technology |
|---|---|
| Language | Python 3 |
| Web framework | Flask 3.1 |
| Database | SQLite (via Python's built-in `sqlite3`) |
| Password security | Werkzeug `generate_password_hash` / `check_password_hash` |
| Templates | Jinja2 (Flask's built-in templating engine) |
| Frontend | Bootstrap 5.3 (CSS framework) + Bootstrap Icons 1.11 |
| Environment | Ubuntu (WSL2 on Windows 11) |

---

## Features

- **User authentication** — login and logout with secure password hashing
- **Role-based access** — three roles with different permissions:
  - `admin` — full access to all features
  - `technician` — can update ticket status and assignment
  - `user` — can raise tickets and add notes
- **Ticket creation** — raise a ticket with a title, description and priority level
- **Dashboard** — view all tickets with summary statistics (total / open / in-progress / critical)
- **Ticket filtering** — filter tickets by status and priority
- **Ticket detail** — full description, comment history and update panel
- **Status updates** — technicians and admins can update ticket status
- **Technician assignment** — assign tickets to specific team members
- **Notes and comments** — any logged-in user can add notes to a ticket
- **Clickable rows** — click any row in the dashboard to open that ticket

---

## Installation (Ubuntu / WSL2)

### 1. Clone the repository

```bash
git clone https://github.com/Venil-Chand/it-helpdesk-ticketing-system.git
cd it-helpdesk-ticketing-system
```

### 2. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Set up the environment file

```bash
cp .env.example .env
```

Open `.env` and replace the placeholder with a real secret key:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Paste the output as the value of `SECRET_KEY` in `.env`.

### 5. Run the application

```bash
python app.py
```

Open your browser at **http://127.0.0.1:5000**

---

## Default Login Accounts

| Username | Password | Role |
|---|---|---|
| `admin` | `admin123` | Admin — full access |
| `venil` | `venil123` | Technician |
| `staff1` | `staff123` | Regular user |

> **Note:** Change these passwords before using the application in any shared environment.

---

## Project Structure

```
it-helpdesk-ticketing-system/
├── app.py              Flask application and routes
├── database.py         SQLite database helpers
├── migrate.py          One-time migration script (old schema → new schema)
├── requirements.txt    Python dependencies
├── .env.example        Template for environment variables
├── .gitignore          Files excluded from Git
├── templates/
│   ├── base.html       Shared page layout (navbar, flash messages, footer)
│   ├── login.html      Login page
│   ├── index.html      Dashboard with stats cards and ticket table
│   ├── new_ticket.html Raise a new ticket
│   └── ticket_detail.html  View, update and comment on a ticket
└── static/
    └── style.css       Custom styles
```

---

## How the Application Works (Beginner Explanation)

**Flask** is the web server. When you visit a URL like `/tickets/3`, Flask calls the matching Python function (called a *route*) and returns an HTML page.

**SQLite** stores all the data in a single file (`helpdesk.db`). It has three tables:
- `users` — usernames, hashed passwords and roles
- `tickets` — every support request
- `comments` — notes added to each ticket

**Jinja2** fills in the HTML templates. Variables like `{{ ticket['title'] }}` are replaced with real values before the page is sent to the browser.

**Sessions** remember who is logged in. When you log in successfully, Flask stores your username in a signed cookie. When you log out, the cookie is cleared.

**Password hashing** — passwords are never stored as plain text. Werkzeug converts them into a one-way hash. When you log in, it checks that the entered password matches the stored hash.

---

## Security Limitations

> **This application is for local learning and portfolio purposes only.**
> It is not hardened for public deployment.

The following known security issues exist and must be addressed before the application is shared
or deployed publicly:

| Issue | Risk | Status |
|---|---|---|
| **No CSRF protection** | A malicious page could submit forms on behalf of a logged-in user | Not yet implemented |
| **Weak default passwords** | `admin123`, `venil123`, `staff123` are trivially guessable | Change before sharing |
| **No rate limiting on login** | An attacker could try thousands of passwords | Not yet implemented |
| **No HTTPS** | Passwords and session cookies travel in plaintext on a network | Use only on localhost |
| **Debug mode enabled** | Flask debug mode leaks internal errors and enables a code-execution pin | Must be disabled before deployment |
| **No input length validation in templates** | Very long titles or descriptions will display but may cause layout issues | Minor |
| **No account management** | No way to change a password or create new users through the UI | Planned |

The `SECRET_KEY` is now **required** — if the `.env` file is missing or empty, the application
will refuse to start rather than fall back to an insecure default.

---

## Current Limitations

- No email notifications when tickets are updated
- No file attachments on tickets
- No pagination on the dashboard
- No account self-service (change password, create users)
- Not tested with multiple simultaneous users

---

## Future Development Roadmap

- [ ] CSRF tokens on all forms (use Flask-WTF)
- [ ] HTTPS support (use a reverse proxy such as Nginx)
- [ ] Rate limiting on the login route (use Flask-Limiter)
- [ ] Disable debug mode for any non-local environment
- [ ] Change-password feature
- [ ] User management page (admin only)
- [ ] Email notifications (Flask-Mail)
- [ ] File attachments on tickets
- [ ] Pagination on the dashboard
- [ ] Export tickets to CSV
- [ ] Deploy to a Linux server with Gunicorn + Nginx

---

## Acknowledgement

> **AI-assisted project — work in progress — not production-ready**

This project was built as a hands-on learning exercise by Venil Chand.
Development was assisted by [Claude](https://claude.ai) (Anthropic AI) acting as a coding tutor.

All code has been reviewed and tested by the author.
The purpose of this repository is to demonstrate continuous, genuine development progress
as part of a computing portfolio at Unitec Auckland.
