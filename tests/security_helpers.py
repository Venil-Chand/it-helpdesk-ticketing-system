"""
Shared helpers for the production security tests.

Configuration is read once, when app.py is imported, so each test loads its
own private copy of the application under the environment it wants to
check. Every copy uses a temporary database, and .env is never read.
"""

import importlib.util
import itertools
import os
import re
import secrets
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import database
import migrations

PROJECT_ROOT = Path(__file__).resolve().parent.parent

HTTPS_URL = "https://helpdesk.test"
HTTP_URL = "http://helpdesk.test"
LOCAL_URL = "http://localhost"

PASSWORD = "correct-horse-battery-staple"

# Generated for each test run, so no key is stored in the repository
TEST_SECRET_KEY = secrets.token_hex(32)

# Every variable that changes security behaviour
SECURITY_VARIABLES = (
    "APP_ENV",
    "SECRET_KEY",
    "FLASK_DEBUG",
    "TRUSTED_HOSTS",
    "TRUSTED_PROXY_COUNT",
    "TRUSTED_PROXY_IPS",
)

PRODUCTION_ENV = {
    "APP_ENV": "production",
    "SECRET_KEY": TEST_SECRET_KEY,
    "TRUSTED_HOSTS": "helpdesk.test",
}

PROXIED_PRODUCTION_ENV = {
    **PRODUCTION_ENV,
    "TRUSTED_PROXY_COUNT": "1",
    "TRUSTED_PROXY_IPS": "10.0.0.2",
}

DEVELOPMENT_ENV = {
    "APP_ENV": "development",
    "SECRET_KEY": TEST_SECRET_KEY,
}

TESTING_ENV = {
    "APP_ENV": "testing",
    "SECRET_KEY": TEST_SECRET_KEY,
}

_module_numbers = itertools.count(1)


def clean_environment(settings):
    """A copy of the environment holding only the given security settings."""

    environment = {
        key: value for key, value in os.environ.items()
        if key not in SECURITY_VARIABLES
    }
    environment.update(settings)

    return environment


def load_app(settings):
    """Import a private copy of app.py under the given environment."""

    name = f"security_test_app_{next(_module_numbers)}"
    spec = importlib.util.spec_from_file_location(
        name, PROJECT_ROOT / "app.py"
    )
    module = importlib.util.module_from_spec(spec)

    # Flask finds the templates folder through sys.modules
    sys.modules[name] = module

    try:
        with patch.dict(os.environ, clean_environment(settings), clear=True):
            with patch("dotenv.load_dotenv"):
                spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise

    return module


class SecurityTestCase(unittest.TestCase):
    """Runs each test against a private app and a temporary database."""

    settings = PRODUCTION_ENV
    base_url = HTTPS_URL

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)

        self.db_path = os.path.join(directory.name, "security_test.db")

        patcher = patch.object(database, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        migrations.migrate(self.db_path)

        self.module = load_app(self.settings)
        self.addCleanup(sys.modules.pop, self.module.__name__, None)
        self.app = self.module.app

        password_hash = generate_password_hash(
            PASSWORD, method="pbkdf2:sha256:1000"
        )

        conn = database.get_db()
        conn.executemany(
            "INSERT INTO users (username, password_hash, role) "
            "VALUES (?, ?, ?)",
            [
                ("admin", password_hash, "admin"),
                ("tech", password_hash, "technician"),
                ("alice", password_hash, "user"),
                ("bob", password_hash, "user"),
            ]
        )
        conn.commit()
        conn.close()

    # -----------------------------------------------
    # Requests
    # -----------------------------------------------

    def referer_headers(self):
        """HTTPS form posts must carry a same-site Referer."""

        return {"Referer": f"{self.base_url}/"}

    def get(self, client, path, **kwargs):
        kwargs.setdefault("base_url", self.base_url)
        return client.get(path, **kwargs)

    def csrf_token(self, client, page):
        response = self.get(client, page)
        match = re.search(
            r'name="csrf_token"\s+value="([^"]+)"', response.text
        )
        self.assertIsNotNone(match, f"no CSRF token on {page}")
        return match.group(1)

    def post_form(self, client, path, data, token_page="/tickets/new"):
        """Submit a form the way a browser does, with a real CSRF token."""

        form = {"csrf_token": self.csrf_token(client, token_page), **data}

        return client.post(
            path,
            data=form,
            base_url=self.base_url,
            headers=self.referer_headers()
        )

    def login(self, username, client=None):
        client = client or self.app.test_client()

        response = self.post_form(
            client,
            "/login",
            {"username": username, "password": PASSWORD},
            token_page="/login"
        )
        self.assertEqual(response.status_code, 302)

        return client

    # -----------------------------------------------
    # Database checks
    # -----------------------------------------------

    def count(self, table):
        conn = database.get_db()
        try:
            return conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
        finally:
            conn.close()

    def make_ticket(self, owner="alice", title="Printer offline"):
        return database.create_ticket(title, "Details", "Medium", owner)
