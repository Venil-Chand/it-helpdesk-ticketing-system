# Installation Guide

This guide installs the helpdesk on one machine for **development,
evaluation or a local demonstration**. To install it on a server for real
users, follow the [production deployment guide](PRODUCTION_OPERATIONS.md)
instead.

## 1. Requirements

| Item | Version |
|---|---|
| Operating system | Linux, macOS, or Windows with WSL2 (developed on Ubuntu under WSL2) |
| Python | 3.13 (CI) or 3.14 (also tested) |
| Git | any recent version |
| Redis | only for the shared rate-limit tests or a production-like setup; not needed for development |
| A web browser | any current browser |

The page styles and icons load from `cdnjs.cloudflare.com` (allowed by the
Content Security Policy), so the browser needs internet access to show the
pages styled.

## 2. Get the code and create a virtual environment

```bash
git clone https://github.com/Venil-Chand/it-helpdesk-ticketing-system.git
cd it-helpdesk-ticketing-system
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 3. Configure the secret key

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Put the printed value in `.env` as `SECRET_KEY=...`. `.env` is ignored by
Git; never commit it. Production never reads `.env` (see the
[security configuration](SECURITY_CONFIGURATION.md)).

## 4. Create the database

```bash
flask --app app init-db       # creates helpdesk.db at schema version 10
flask --app app db-status     # "Current schema version: 10 (up to date)"
```

`init-db` never creates accounts. To keep the database somewhere else, set
`HELPDESK_DATABASE_PATH=/path/to/helpdesk.db` for every command.

## 5. Create the first administrator

```bash
python setup_accounts.py
```

The first account is always called `admin`. The password must be at least
16 characters, must not contain the username, and must not be a common or
breached password. Run the script again later to change a password from
the command line.

All other accounts are created by the administrator in the web interface
(see the [administrator guide](ADMINISTRATOR_GUIDE.md)).

## 6. Start the application

```bash
python app.py
```

Open <http://127.0.0.1:5000> and sign in as `admin`. The built-in server
is for development only; `python app.py` refuses to run in production
mode.

Notifications are **simulated** in development: run
`flask --app app notifications-send` and the messages are written to
`instance/notification-sink/` instead of being emailed.

## 7. Run the tests

```bash
# Unit and integration tests (about 2 minutes)
SECRET_KEY=ci-testing-key-not-for-production python -m unittest discover -s tests

# Optional: the shared rate-limit tests need a throwaway Redis database,
# which they EMPTY before each test
HELPDESK_TEST_REDIS_URL=redis://127.0.0.1:6379/15 python -m unittest discover -s tests
```

Browser tests (headless Chromium, about 70 seconds):

```bash
python -m pip install -r e2e/requirements.txt
python -m playwright install --with-deps --only-shell chromium
python -m unittest discover -s e2e -v
```

The browser tests never use your `helpdesk.db` or `.env`: each test class
builds its own temporary database and runs the application with
`APP_ENV=testing`. No email is sent.

## 8. Common installation problems

| Symptom | Cause and fix |
|---|---|
| `Unsafe ... configuration: SECRET_KEY ...` at startup | `.env` is missing or the key is weak. Repeat step 3. |
| The page says the database schema is not ready (503) | Run `flask --app app init-db`. |
| `setup_accounts.py` says the schema is not up to date | Run `init-db` first. |
| A password is refused | It is shorter than 16 characters, contains the username, or is on the blocklist of common passwords. |
| Pages have no styling | The browser cannot reach `cdnjs.cloudflare.com`. |
| Playwright cannot start Chromium (`libnss3`, `libasound2` missing) | Run `playwright install --with-deps` (needs `sudo`), or install those system libraries. |
| `reset-password` says to run it in an interactive terminal | Intended: passwords are never read from pipes or arguments. |
