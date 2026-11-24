"""
Shared fixture for account administration and password-change tests.

Everything runs for real: the before_request account check, the SQL, the
triggers and the templates, on a temporary database. Only password
hashing is made fast. The real helpdesk.db is never opened.
"""

import itertools
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import accounts
import database as db
import migrations
from app import app

PASSWORD = "correct-horse-battery-staple"
NEW_PASSWORD = "a-brand-new-long-passphrase"
FAST_HASH = "pbkdf2:sha256:1000"

# A separate address per client keeps the in-memory rate limits apart
_client_numbers = itertools.count()


def unique_address():
    number = next(_client_numbers)
    return f"10.200.{number // 256 % 256}.{number % 256}"


def fast_hash(password):
    return generate_password_hash(password, FAST_HASH)


class AccountTestCase(unittest.TestCase):

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = os.path.join(temp_dir.name, "test.db")

        for patcher in (
            patch.object(db, "DB_PATH", self.db_path),
            patch.object(accounts, "hash_password", fast_hash),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        old_config = {
            key: app.config.get(key)
            for key in ("TESTING", "WTF_CSRF_ENABLED")
        }
        self.addCleanup(app.config.update, old_config)
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

        migrations.migrate(self.db_path)

        self.ids = {
            "alice": self.add_user("alice", "user"),
            "bob": self.add_user("bob", "user"),
            "tech": self.add_user("tech", "technician"),
            "root": self.add_user("root", "admin"),
        }

        self.bob_ticket = db.create_ticket(
            "Bob printer", "Paper jam", "Low", "bob"
        )

    # -----------------------------------------------
    # Helpers
    # -----------------------------------------------

    def add_user(self, username, role, active=1):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            return conn.execute(
                "INSERT INTO users (username, password_hash, role, "
                "is_active) VALUES (?, ?, ?, ?)",
                (username, fast_hash(PASSWORD), role, active)
            ).lastrowid

    def sql(self, statement, params=()):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            return conn.execute(statement, params).fetchall()

    def account(self, username):
        rows = self.sql(
            "SELECT id, username, role, is_active, session_version "
            "FROM users WHERE username = ?", (username,)
        )
        return rows[0] if rows else None

    def audit(self):
        return self.sql(
            "SELECT actor_user_id, actor, action, target_user_id, "
            "target_username, old_value, new_value "
            "FROM audit_events ORDER BY id"
        )

    def new_client(self):
        client = app.test_client()
        client.environ_base["REMOTE_ADDR"] = unique_address()
        return client

    def signed_in(self, username, password=PASSWORD):
        client = self.new_client()
        response = client.post(
            "/login", data={"username": username, "password": password}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")
        return client

    def can_log_in(self, username, password):
        response = self.new_client().post(
            "/login", data={"username": username, "password": password}
        )
        return response.headers.get("Location") == "/"

    def assert_signed_out(self, client):
        response = client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login")

    def assert_signed_in(self, client):
        self.assertEqual(client.get("/").status_code, 200)
