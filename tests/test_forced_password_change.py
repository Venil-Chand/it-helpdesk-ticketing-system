"""
Mandatory password changes (migration 9).

An account whose password was chosen by someone else (created by an
administrator, reset by an administrator or by "flask reset-password")
may only change its password or log out until it chooses its own. The
flag is read from the database on every request.

Everything runs against temporary databases.
"""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

import accounts
import migrations
from app import app, limiter
from migrations import MIGRATIONS, SCHEMA_VERSION

from account_helpers import (
    CHANGE_PASSWORD_URL,
    NEW_PASSWORD,
    PASSWORD,
    AccountTestCase,
)

OWN_PASSWORD = "my-own-chosen-pass-phrase"


class ForcedChangeTestCase(AccountTestCase):

    def admin_reset(self, username, password=NEW_PASSWORD):
        accounts.reset_password(self.ids["root"], self.ids[username],
                                password)

    def flag(self, username):
        return self.sql(
            "SELECT must_change_password FROM users WHERE username = ?",
            (username,)
        )[0][0]

    def change(self, client, current=NEW_PASSWORD, new=OWN_PASSWORD):
        return client.post(CHANGE_PASSWORD_URL, data={
            "current_password": current,
            "new_password": new,
            "confirm_password": new,
        })


# ---------------------------------------------------
# WHO MUST CHANGE THEIR PASSWORD
# ---------------------------------------------------

class TestWhenTheFlagIsSet(ForcedChangeTestCase):

    def test_existing_accounts_are_not_disrupted(self):
        for username in ("alice", "bob", "tech", "root"):
            with self.subTest(username=username):
                self.assertEqual(self.flag(username), 0)
                self.assert_signed_in(self.signed_in(username))

    def test_admin_created_account_must_change(self):
        accounts.create_account(self.ids["root"], "carol", "user",
                                NEW_PASSWORD)
        self.assertEqual(self.flag("carol"), 1)

    def test_admin_reset_sets_the_flag(self):
        self.admin_reset("alice")
        self.assertEqual(self.flag("alice"), 1)

    def test_users_page_shows_who_must_change(self):
        self.admin_reset("alice")
        html = self.signed_in("root").get("/admin/users").get_data(
            as_text=True
        )
        self.assertEqual(html.count("Must change password"), 1)

    def test_flag_only_accepts_zero_or_one(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("UPDATE users SET must_change_password = 2 "
                     "WHERE username = 'alice'")


# ---------------------------------------------------
# WHAT A FLAGGED ACCOUNT CAN AND CANNOT DO
# ---------------------------------------------------

class TestRestrictedAccess(ForcedChangeTestCase):

    def setUp(self):
        super().setUp()
        self.admin_reset("bob")
        self.client = self.must_change_password_on_login("bob", NEW_PASSWORD)

    def test_every_ordinary_page_redirects_to_change_password(self):
        for path in ("/", "/tickets/new", f"/tickets/{self.bob_ticket}",
                     "/tickets/export", "/admin/users"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"],
                                 CHANGE_PASSWORD_URL)

    def test_posts_are_redirected_without_running(self):
        tickets = self.sql("SELECT COUNT(*) FROM tickets")
        comments = self.sql("SELECT COUNT(*) FROM comments")

        new = self.client.post("/tickets/new", data={
            "title": "Should not exist", "description": "x",
            "priority": "Low",
        })
        comment = self.client.post(
            f"/tickets/{self.bob_ticket}/comment",
            data={"content": "Should not exist"}
        )

        for response in (new, comment):
            self.assertEqual(response.headers["Location"],
                             CHANGE_PASSWORD_URL)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM tickets"), tickets)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM comments"), comments)

    def test_change_password_page_explains_why(self):
        html = self.client.get(CHANGE_PASSWORD_URL).get_data(as_text=True)

        self.assertIn("Your password was set by an administrator", html)
        # Password managers can tie the new password to this account
        self.assertIn('autocomplete="username"', html)
        self.assertIn('autocomplete="new-password"', html)

    def test_logout_still_works(self):
        response = self.client.post("/logout")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login")
        self.assert_signed_out(self.client)

    def test_choosing_a_password_restores_access(self):
        response = self.change(self.client)

        self.assertEqual(response.headers["Location"], "/")
        self.assertEqual(self.flag("bob"), 0)
        self.assert_signed_in(self.client)
        self.assertTrue(self.can_log_in("bob", OWN_PASSWORD))
        self.assertEqual(self.audit()[-1][2], "password_changed")

    def test_cannot_keep_the_administrator_password(self):
        response = self.change(self.client, new=NEW_PASSWORD)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.flag("bob"), 1)

    def test_wrong_temporary_password_changes_nothing(self):
        response = self.change(self.client, current="not-the-temporary-one")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.flag("bob"), 1)
        self.assertEqual(self.client.get("/").headers["Location"],
                         CHANGE_PASSWORD_URL)

    def test_flag_is_read_from_the_database_every_request(self):
        # Cleared by an administrator directly: access returns at once
        self.sql("UPDATE users SET must_change_password = 0 "
                 "WHERE username = 'bob'")
        self.assert_signed_in(self.client)

        # Set again: the next request is restricted again
        self.sql("UPDATE users SET must_change_password = 1 "
                 "WHERE username = 'bob'")
        self.assertEqual(self.client.get("/").headers["Location"],
                         CHANGE_PASSWORD_URL)


# ---------------------------------------------------
# SESSIONS, COOKIES, CSRF AND RATE LIMITS STILL APPLY
# ---------------------------------------------------

class TestSessionSecurityWithForcedChange(ForcedChangeTestCase):

    def test_reset_ends_sessions_opened_before_it(self):
        before = self.signed_in("alice")
        self.admin_reset("alice")

        self.assert_signed_out(before)

    def test_copied_cookie_stops_working_after_the_change(self):
        self.admin_reset("alice")
        here = self.must_change_password_on_login("alice", NEW_PASSWORD)
        copy = self.new_client()
        copy.set_cookie("session", here.get_cookie("session").value)

        self.change(here)

        self.assert_signed_in(here)
        self.assert_signed_out(copy)

    def test_deactivation_ends_a_restricted_session(self):
        self.admin_reset("alice")
        client = self.must_change_password_on_login("alice", NEW_PASSWORD)

        accounts.set_active(self.ids["root"], self.ids["alice"], False)

        self.assert_signed_out(client)
        response = self.change(client)
        self.assertEqual(response.headers["Location"], "/login")
        self.assertFalse(self.can_log_in("alice", NEW_PASSWORD))
        self.assertEqual(self.flag("alice"), 1)

    def test_expired_session_goes_to_login_not_to_change_password(self):
        self.admin_reset("alice")
        start = 1_900_000_000
        lifetime = int(app.permanent_session_lifetime.total_seconds())

        with patch("helpdesk.clock.current_time", return_value=start):
            client = self.must_change_password_on_login("alice",
                                                        NEW_PASSWORD)

        with patch("helpdesk.clock.current_time",
                   return_value=start + lifetime):
            response = client.get(CHANGE_PASSWORD_URL)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login")

    def test_change_without_csrf_token_is_refused(self):
        self.admin_reset("alice")
        client = self.must_change_password_on_login("alice", NEW_PASSWORD)
        app.config["WTF_CSRF_ENABLED"] = True

        response = self.change(client)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.flag("alice"), 1)

    def test_change_password_rate_limit_still_applies(self):
        limiter.reset()
        self.addCleanup(limiter.reset)
        self.admin_reset("alice")
        client = self.must_change_password_on_login("alice", NEW_PASSWORD)

        codes = [
            self.change(client, current=f"wrong-guess-{n}").status_code
            for n in range(6)
        ]

        self.assertEqual(codes[:5], [400] * 5)
        self.assertEqual(codes[5], 429)
        self.assertEqual(self.flag("alice"), 1)


# ---------------------------------------------------
# MIGRATION 9
# ---------------------------------------------------

class TestMigrationNine(unittest.TestCase):

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = os.path.join(temp.name, "v8.db")
        migrations.migrate(self.path, migrations=MIGRATIONS[:8])

        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, role) "
                "VALUES ('venil', 'hash-kept-as-is', 'admin')"
            )
            conn.execute(
                "INSERT INTO audit_events (actor, action, target_username) "
                "VALUES ('venil', 'account_created', 'venil')"
            )

    def sql(self, statement):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute(statement).fetchall()

    def test_upgrade_keeps_rows_and_forces_nobody(self):
        users = self.sql("SELECT * FROM users")
        events = self.sql("SELECT * FROM audit_events")

        result = migrations.migrate(self.path)

        # Migration 9, then migration 10 (users.email, NULL for everyone)
        self.assertEqual([m.version for m in result.applied], [9, 10])
        self.assertEqual(SCHEMA_VERSION, 10)
        self.assertEqual(
            self.sql("SELECT * FROM users"), [users[0] + (0, None)]
        )
        self.assertEqual(
            self.sql("SELECT * FROM audit_events"), [events[0] + (None,)]
        )
        self.assertTrue(migrations.schema_status(self.path).is_current)

    def test_audit_events_stay_append_only(self):
        migrations.migrate(self.path)

        with closing(sqlite3.connect(self.path)) as conn:
            for statement in ("UPDATE audit_events SET actor = 'x'",
                              "DELETE FROM audit_events"):
                with self.subTest(statement=statement):
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(statement)

    def test_failure_rolls_back_to_version_eight(self):
        def fail_after_first_column(conn):
            MIGRATIONS[8].apply(conn)
            raise RuntimeError("simulated failure")

        registry = MIGRATIONS[:8] + (
            migrations.Migration(9, "fails", fail_after_first_column),
        )
        schema = self.sql("SELECT sql FROM sqlite_master ORDER BY name")

        with self.assertRaises(RuntimeError):
            migrations.migrate(self.path, migrations=registry)

        self.assertEqual(self.sql("PRAGMA user_version"), [(8,)])
        self.assertEqual(
            self.sql("SELECT sql FROM sqlite_master ORDER BY name"), schema
        )

    def test_missing_check_constraint_is_detected(self):
        migrations.migrate(self.path, migrations=MIGRATIONS[:8])
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("ALTER TABLE users ADD COLUMN must_change_password "
                         "INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE audit_events "
                         "ADD COLUMN source_address TEXT")
            # Complete the later migrations, so the database claims to be
            # current and the full schema check runs
            for migration in MIGRATIONS[9:]:
                migration.apply(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        self.assertIn(
            "users.must_change_password is missing its CHECK",
            migrations.schema_status(self.path).problems
        )


if __name__ == "__main__":
    unittest.main()
