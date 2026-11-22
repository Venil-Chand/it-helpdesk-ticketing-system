"""
Security regression tests for database-backed authentication.

Nothing about the account is stubbed unless a test simulates a database
failure: requests run through the real before_request hook, real SQL on
a temporary database, and the real templates. The real helpdesk.db is
never opened.
"""

import itertools
import os
import re
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import database as db
import migrations
from app import app
from migrations import MIGRATIONS, SCHEMA_VERSION


PASSWORD = "correct-horse-battery-staple"
FAST_HASH = "pbkdf2:sha256:1000"

# Each client gets its own address so the login rate limit, which is
# stored in memory for the whole test run, never leaks between tests
_client_numbers = itertools.count()


def unique_address():
    number = next(_client_numbers)
    return f"10.{number // 65536 % 256}.{number // 256 % 256}.{number % 256}"


class AuthTestCase(unittest.TestCase):

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = os.path.join(temp_dir.name, "test.db")

        db_patch = patch.object(db, "DB_PATH", self.db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)

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

        self.alice_ticket = db.create_ticket(
            "Alice laptop", "Screen flickers", "High", "alice"
        )
        self.bob_ticket = db.create_ticket(
            "Bob printer", "Paper jam", "Low", "bob"
        )
        self.tech_ticket = db.create_ticket(
            "Tech own request", "New keyboard", "Low", "tech"
        )

        self.client = self.new_client()

    # -----------------------------------------------
    # Helpers
    # -----------------------------------------------

    def new_client(self):
        client = app.test_client()
        client.environ_base["REMOTE_ADDR"] = unique_address()
        return client

    def add_user(self, username, role):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO users (username, password_hash, role) "
                "VALUES (?, ?, ?)",
                (username, generate_password_hash(PASSWORD, FAST_HASH), role)
            )
            return cursor.lastrowid

    def sql(self, statement, params=()):
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                return conn.execute(statement, params).fetchall()
        finally:
            conn.close()

    def set_active(self, username, active):
        self.sql("UPDATE users SET is_active = ? WHERE username = ?",
                 (int(active), username))

    def set_role(self, username, role):
        self.sql("UPDATE users SET role = ? WHERE username = ?",
                 (role, username))

    def delete_user(self, username):
        self.sql("DELETE FROM users WHERE username = ?", (username,))

    def login(self, username, password=PASSWORD, client=None):
        return (client or self.client).post(
            "/login",
            data={"username": username, "password": password}
        )

    def session_of(self, client=None):
        with (client or self.client).session_transaction() as sess:
            return dict(sess)

    def set_session(self, **values):
        # A session naming a user gets the current format unless a test
        # overrides part of it
        if "user_id" in values:
            values.setdefault("session_version", 1)
            values.setdefault("issued_at", int(time.time()))

        with self.client.session_transaction() as sess:
            sess.clear()
            sess.update(values)

    def assert_sent_to_login(self, response):
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))

    def protected_requests(self):
        """Every protected action, as (method, url, form data)."""

        ticket = self.bob_ticket
        return [
            ("GET", "/", None),
            ("GET", "/?sla=overdue", None),
            ("GET", "/tickets/export", None),
            ("GET", "/tickets/export?sla=within", None),
            ("GET", "/tickets/new", None),
            ("POST", "/tickets/new",
             {"title": "x", "description": "y", "priority": "Low"}),
            ("GET", f"/tickets/{ticket}", None),
            ("POST", f"/tickets/{ticket}/comment", {"content": "intrusion"}),
            ("POST", f"/tickets/{ticket}/update",
             {"status": "Closed", "assigned_to": "alice"}),
        ]

    def send(self, method, url, data):
        return self.client.open(url, method=method, data=data)

    def ticket_row(self, ticket_id):
        return self.sql(
            "SELECT status, assigned_to, "
            "(SELECT COUNT(*) FROM comments WHERE ticket_id = t.id), "
            "(SELECT COUNT(*) FROM ticket_history WHERE ticket_id = t.id) "
            "FROM tickets AS t WHERE id = ?",
            (ticket_id,)
        )

    def assert_every_action_refused(self):
        """Each protected action redirects to login and changes nothing."""

        tickets_before = self.sql("SELECT COUNT(*) FROM tickets")
        bob_before = self.ticket_row(self.bob_ticket)

        for method, url, data in self.protected_requests():
            with self.subTest(method=method, url=url):
                self.assert_sent_to_login(self.send(method, url, data))

        self.assertEqual(self.sql("SELECT COUNT(*) FROM tickets"),
                         tickets_before)
        self.assertEqual(self.ticket_row(self.bob_ticket), bob_before)


# ---------------------------------------------------
# 1 AND 2: LOGIN
# ---------------------------------------------------

class TestLogin(AuthTestCase):

    def test_active_account_can_log_in(self):
        response = self.login("alice")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_session_stores_only_the_stable_user_id(self):
        self.login("tech")
        stored = self.session_of()

        self.assertEqual(stored["user_id"], self.ids["tech"])
        self.assertNotIn("username", stored)
        self.assertNotIn("role", stored)
        self.assertNotIn("is_active", stored)

    def test_page_shows_identity_from_the_database(self):
        self.login("tech")
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn("tech", html)
        self.assertIn("technician", html)

    def test_inactive_account_cannot_log_in(self):
        self.set_active("alice", False)

        response = self.login("alice")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("user_id", self.session_of())
        self.assert_sent_to_login(self.client.get("/"))

    def test_inactive_login_looks_like_a_wrong_password(self):
        self.set_active("alice", False)

        inactive = self.login("alice").get_data(as_text=True)
        wrong = self.login("bob", "wrong-password").get_data(as_text=True)

        self.assertIn("Invalid username or password.", inactive)
        self.assertIn("Invalid username or password.", wrong)
        self.assertNotIn("deactivated", inactive.lower())
        self.assertNotIn("inactive", inactive.lower())

    def test_wrong_password_is_rejected(self):
        self.login("alice", "wrong-password")
        self.assertNotIn("user_id", self.session_of())

    def test_unknown_user_is_rejected(self):
        self.login("nobody")
        self.assertNotIn("user_id", self.session_of())

    def test_reactivated_account_can_log_in_again(self):
        self.set_active("alice", False)
        self.login("alice")
        self.set_active("alice", True)

        self.login("alice")

        self.assertEqual(self.session_of()["user_id"], self.ids["alice"])


# ---------------------------------------------------
# 3 AND 4: DELETED AND DEACTIVATED SESSIONS
# ---------------------------------------------------

class TestRevokedAccounts(AuthTestCase):

    def test_deleted_account_session_is_rejected(self):
        self.login("root")
        self.assertEqual(self.client.get("/").status_code, 200)

        self.delete_user("root")

        self.assert_sent_to_login(self.client.get("/"))
        self.assertEqual(self.session_of().get("user_id"), None)

    def test_deleted_account_cannot_perform_any_action(self):
        for number, (method, url, data) in enumerate(
            self.protected_requests()
        ):
            with self.subTest(method=method, url=url):
                username = f"removed{number}"
                user_id = self.add_user(username, "admin")
                self.set_session(user_id=user_id)
                self.delete_user(username)

                self.assert_sent_to_login(self.send(method, url, data))

    def test_deactivated_account_session_is_rejected(self):
        self.login("root")
        self.assertEqual(self.client.get("/").status_code, 200)

        self.set_active("root", False)

        self.assert_sent_to_login(self.client.get("/"))
        self.assertNotIn("user_id", self.session_of())

    def test_deactivated_admin_cannot_perform_any_action(self):
        before = self.ticket_row(self.bob_ticket)

        for method, url, data in self.protected_requests():
            with self.subTest(method=method, url=url):
                self.set_session(user_id=self.ids["root"])
                self.set_active("root", False)
                self.assert_sent_to_login(self.send(method, url, data))

        self.assertEqual(self.ticket_row(self.bob_ticket), before)

    def test_rejected_session_stays_rejected_after_reactivation(self):
        self.login("alice")
        self.set_active("alice", False)
        self.client.get("/")

        self.set_active("alice", True)

        self.assert_sent_to_login(self.client.get("/"))

    def test_rejection_explains_why(self):
        self.login("alice")
        self.delete_user("alice")

        response = self.client.get("/", follow_redirects=True)

        self.assertIn("no longer valid", response.get_data(as_text=True))

    def test_rejected_user_is_shown_the_login_form(self):
        self.login("alice")
        self.set_active("alice", False)

        response = self.client.get("/login")

        self.assertEqual(response.status_code, 200)
        self.assertIn('name="password"', response.get_data(as_text=True))

    def test_other_users_are_not_affected(self):
        other = self.new_client()
        self.login("bob", client=other)
        self.login("alice")

        self.set_active("alice", False)

        self.assert_sent_to_login(self.client.get("/"))
        self.assertEqual(other.get("/").status_code, 200)


# ---------------------------------------------------
# 5: ROLE CHANGES
# ---------------------------------------------------

class TestRoleChanges(AuthTestCase):

    def setUp(self):
        super().setUp()
        self.login("tech")
        self.assertEqual(
            self.client.get(f"/tickets/{self.bob_ticket}").status_code, 200
        )
        self.set_role("tech", "user")

    def test_demoted_technician_loses_other_tickets_immediately(self):
        response = self.client.get(f"/tickets/{self.bob_ticket}")
        self.assertEqual(response.status_code, 404)

    def test_demoted_technician_still_sees_own_ticket(self):
        response = self.client.get(f"/tickets/{self.tech_ticket}")
        self.assertEqual(response.status_code, 200)

    def test_demoted_technician_sees_no_staff_controls(self):
        html = self.client.get(
            f"/tickets/{self.tech_ticket}"
        ).get_data(as_text=True)

        self.assertNotIn('name="assigned_to"', html)

    def test_demoted_technician_dashboard_is_limited(self):
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn("Tech own request", html)
        self.assertNotIn("Bob printer", html)
        self.assertNotIn("Alice laptop", html)

    def test_demoted_technician_sla_filter_is_limited(self):
        html = self.client.get("/?sla=within").get_data(as_text=True)

        self.assertNotIn("Bob printer", html)
        self.assertNotIn("Alice laptop", html)

    def test_demoted_technician_export_is_limited(self):
        for url in ("/tickets/export", "/tickets/export?sla=within"):
            with self.subTest(url=url):
                text = self.client.get(url).get_data(as_text=True)

                self.assertIn("Tech own request", text)
                self.assertNotIn("Bob printer", text)

    def test_demoted_technician_cannot_update_tickets(self):
        before = self.ticket_row(self.bob_ticket)

        response = self.client.post(
            f"/tickets/{self.bob_ticket}/update",
            data={"status": "Closed", "assigned_to": "tech"}
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.ticket_row(self.bob_ticket), before)

    def test_demoted_technician_cannot_update_own_ticket(self):
        before = self.ticket_row(self.tech_ticket)

        self.client.post(
            f"/tickets/{self.tech_ticket}/update",
            data={"status": "Closed", "assigned_to": ""}
        )

        self.assertEqual(self.ticket_row(self.tech_ticket), before)

    def test_demoted_technician_cannot_comment_on_other_tickets(self):
        before = self.ticket_row(self.bob_ticket)

        response = self.client.post(
            f"/tickets/{self.bob_ticket}/comment",
            data={"content": "still here"}
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.ticket_row(self.bob_ticket), before)

    def test_promotion_also_applies_immediately(self):
        self.set_role("tech", "admin")

        response = self.client.get(f"/tickets/{self.bob_ticket}")

        self.assertEqual(response.status_code, 200)

    def test_unknown_database_role_has_no_staff_access(self):
        self.set_role("tech", "superuser")

        response = self.client.get(f"/tickets/{self.bob_ticket}")

        self.assertEqual(response.status_code, 404)

    def test_demoted_admin_loses_access(self):
        admin = self.new_client()
        self.login("root", client=admin)
        self.set_role("root", "user")

        response = admin.get(f"/tickets/{self.bob_ticket}")

        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------
# 6: UNAUTHORIZED TICKET CHANGES
# ---------------------------------------------------

class TestUnauthorizedModification(AuthTestCase):

    def setUp(self):
        super().setUp()
        self.login("alice")

    def test_user_cannot_update_own_ticket_status(self):
        before = self.ticket_row(self.alice_ticket)

        response = self.client.post(
            f"/tickets/{self.alice_ticket}/update",
            data={"status": "Closed", "assigned_to": ""}
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.ticket_row(self.alice_ticket), before)

    def test_user_cannot_update_another_users_ticket(self):
        before = self.ticket_row(self.bob_ticket)

        self.client.post(
            f"/tickets/{self.bob_ticket}/update",
            data={"status": "Closed", "assigned_to": "alice"}
        )

        self.assertEqual(self.ticket_row(self.bob_ticket), before)

    def test_form_role_and_actor_fields_are_ignored(self):
        before = self.ticket_row(self.bob_ticket)

        self.client.post(
            f"/tickets/{self.bob_ticket}/update?role=admin",
            data={"status": "Closed", "role": "admin",
                  "username": "root", "actor": "root"}
        )

        self.assertEqual(self.ticket_row(self.bob_ticket), before)

    def test_user_cannot_comment_on_another_users_ticket(self):
        before = self.ticket_row(self.bob_ticket)

        response = self.client.post(
            f"/tickets/{self.bob_ticket}/comment",
            data={"content": "intrusion"}
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.ticket_row(self.bob_ticket), before)

    def test_comment_author_comes_from_the_account(self):
        self.client.post(
            f"/tickets/{self.alice_ticket}/comment",
            data={"content": "mine", "author": "root", "username": "root"}
        )

        rows = self.sql("SELECT author FROM comments WHERE ticket_id = ?",
                        (self.alice_ticket,))
        self.assertEqual(rows, [("alice",)])

    def test_ticket_creator_comes_from_the_account(self):
        self.client.post(
            "/tickets/new",
            data={"title": "Spoof", "description": "d", "priority": "Low",
                  "created_by": "bob", "username": "bob"}
        )

        rows = self.sql("SELECT created_by FROM tickets WHERE title = 'Spoof'")
        self.assertEqual(rows, [("alice",)])

    def test_staff_change_records_the_verified_actor(self):
        staff = self.new_client()
        self.login("tech", client=staff)

        staff.post(
            f"/tickets/{self.bob_ticket}/update",
            data={"status": "In Progress", "assigned_to": "tech",
                  "actor": "root"}
        )

        rows = self.sql(
            "SELECT DISTINCT actor FROM ticket_history "
            "WHERE ticket_id = ? AND action != 'created'",
            (self.bob_ticket,)
        )
        self.assertEqual(rows, [("tech",)])


# ---------------------------------------------------
# 7: TICKET OWNERSHIP
# ---------------------------------------------------

class TestOwnership(AuthTestCase):

    def setUp(self):
        super().setUp()
        self.login("alice")

    def test_user_can_open_own_ticket(self):
        response = self.client.get(f"/tickets/{self.alice_ticket}")
        self.assertEqual(response.status_code, 200)

    def test_other_ticket_and_missing_ticket_look_the_same(self):
        other = self.client.get(f"/tickets/{self.bob_ticket}")
        missing = self.client.get("/tickets/999999")

        self.assertEqual(other.status_code, 404)
        self.assertEqual(missing.status_code, 404)

    def test_dashboard_shows_only_own_tickets(self):
        html = self.client.get("/").get_data(as_text=True)

        self.assertIn("Alice laptop", html)
        self.assertNotIn("Bob printer", html)

    def test_search_cannot_reach_other_tickets(self):
        html = self.client.get("/?q=printer").get_data(as_text=True)
        self.assertNotIn("Bob printer", html)

    def test_url_parameters_cannot_widen_access(self):
        html = self.client.get(
            "/?username=bob&role=admin"
        ).get_data(as_text=True)

        self.assertNotIn("Bob printer", html)

    def test_export_contains_only_own_tickets(self):
        text = self.client.get(
            "/tickets/export?username=bob&role=admin"
        ).get_data(as_text=True)

        self.assertIn("Alice laptop", text)
        self.assertNotIn("Bob printer", text)

    def test_sla_filters_contain_only_own_tickets(self):
        for key in ("within", "due_soon", "overdue", "met", "breached",
                    "not_tracked"):
            with self.subTest(sla=key):
                html = self.client.get(f"/?sla={key}").get_data(as_text=True)
                self.assertNotIn("Bob printer", html)

    def test_staff_can_see_every_ticket(self):
        for username in ("tech", "root"):
            with self.subTest(username=username):
                staff = self.new_client()
                self.login(username, client=staff)

                html = staff.get("/").get_data(as_text=True)

                self.assertIn("Alice laptop", html)
                self.assertIn("Bob printer", html)


class TestEveryRouteIsProtected(AuthTestCase):

    PUBLIC_ENDPOINTS = {"login", "logout", "static"}

    def test_every_other_route_requires_a_login(self):
        for rule in app.url_map.iter_rules():
            if rule.endpoint in self.PUBLIC_ENDPOINTS:
                continue

            url = rule.rule.replace("<int:ticket_id>",
                                    str(self.alice_ticket))

            for method in rule.methods - {"HEAD", "OPTIONS"}:
                with self.subTest(endpoint=rule.endpoint, method=method):
                    response = self.new_client().open(url, method=method)
                    self.assert_sent_to_login(response)


# ---------------------------------------------------
# 8: LEGACY AND MALFORMED SESSIONS
# ---------------------------------------------------

class TestLegacySessions(AuthTestCase):

    def test_legacy_admin_session_gets_no_access(self):
        self.set_session(username="root", role="admin")
        self.assert_every_action_refused()

    def test_legacy_session_is_cleared(self):
        self.set_session(username="root", role="admin")

        self.client.get("/")
        stored = self.session_of()

        self.assertNotIn("username", stored)
        self.assertNotIn("role", stored)

    def test_legacy_session_is_not_upgraded_for_a_real_user(self):
        self.set_session(username="alice", role="user")

        self.assert_sent_to_login(self.client.get("/"))
        self.assertNotIn("user_id", self.session_of())

    def test_outdated_role_beside_a_user_id_is_not_trusted(self):
        self.set_session(user_id=self.ids["alice"], role="admin")

        self.assert_sent_to_login(
            self.client.get(f"/tickets/{self.bob_ticket}")
        )
        self.assertNotIn("user_id", self.session_of())

    def test_username_only_session_is_rejected(self):
        self.set_session(username="root")
        self.assert_sent_to_login(self.client.get("/"))

    def test_role_only_session_is_rejected(self):
        self.set_session(role="admin")
        self.assert_sent_to_login(self.client.get("/"))

    def test_malformed_user_ids_are_rejected(self):
        real_id = self.ids["root"]

        for bad in (str(real_id), True, 0, -1, real_id + 0.5, [real_id],
                    {"id": real_id}, "1 OR 1=1"):
            with self.subTest(user_id=bad):
                self.set_session(user_id=bad)

                self.assert_sent_to_login(self.client.get("/"))
                self.assertNotIn("user_id", self.session_of())

    def test_unknown_user_id_is_rejected(self):
        self.set_session(user_id=999999)

        self.assert_sent_to_login(self.client.get("/"))
        self.assertNotIn("user_id", self.session_of())

    def test_legacy_user_can_log_in_normally_afterwards(self):
        self.set_session(username="alice", role="user")
        self.client.get("/")

        self.login("alice")

        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.session_of()["user_id"], self.ids["alice"])


# ---------------------------------------------------
# 9: DATABASE MIGRATION
# ---------------------------------------------------

class TestActivationMigration(unittest.TestCase):

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.path = os.path.join(temp_dir.name, "upgrade.db")

        migrations.migrate(self.path, migrations=MIGRATIONS[:4])

        self.users = [
            (3, "alice", "hash-alice", "user"),
            (7, "tech", "hash-tech", "technician"),
            (9, "root", "hash-root", "admin"),
        ]
        conn = sqlite3.connect(self.path)
        with conn:
            conn.executemany(
                "INSERT INTO users (id, username, password_hash, role) "
                "VALUES (?, ?, ?, ?)",
                self.users
            )
            conn.execute(
                "INSERT INTO tickets (id, title, description, created_by) "
                "VALUES (5, 'Old', 'Ticket', 'alice')"
            )
        conn.close()

    def rows(self, statement, params=()):
        conn = sqlite3.connect(self.path)
        try:
            with conn:
                return conn.execute(statement, params).fetchall()
        finally:
            conn.close()

    def version(self):
        return self.rows("PRAGMA user_version")[0][0]

    def test_version_four_upgrades_to_latest(self):
        self.assertEqual(self.version(), 4)

        result = migrations.migrate(self.path)

        self.assertEqual([m.version for m in result.applied], [5, 6])
        self.assertEqual(self.version(), SCHEMA_VERSION)
        self.assertTrue(migrations.schema_status(self.path).is_current)

    def test_accounts_are_preserved_exactly(self):
        migrations.migrate(self.path)

        self.assertEqual(
            self.rows("SELECT id, username, password_hash, role "
                      "FROM users ORDER BY id"),
            self.users
        )

    def test_tickets_are_preserved(self):
        before = self.rows("SELECT * FROM tickets")
        migrations.migrate(self.path)
        self.assertEqual(self.rows("SELECT * FROM tickets"), before)

    def test_existing_accounts_stay_active(self):
        migrations.migrate(self.path)

        self.assertEqual(
            self.rows("SELECT DISTINCT is_active FROM users"), [(1,)]
        )

    def test_new_accounts_default_to_active(self):
        migrations.migrate(self.path)
        self.rows("INSERT INTO users (username, password_hash) "
                  "VALUES ('new', 'hash')")

        self.assertEqual(
            self.rows("SELECT is_active FROM users WHERE username = 'new'"),
            [(1,)]
        )

    def test_migration_is_repeatable(self):
        migrations.migrate(self.path)
        self.rows("UPDATE users SET is_active = 0 WHERE id = 3")
        before = self.rows("SELECT * FROM users ORDER BY id")

        result = migrations.migrate(self.path)

        self.assertEqual(result.applied, [])
        self.assertEqual(self.rows("SELECT * FROM users ORDER BY id"), before)

    def test_invalid_activation_values_are_refused(self):
        migrations.migrate(self.path)

        for bad in (2, -1, None):
            with self.subTest(value=bad):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.rows("UPDATE users SET is_active = ? WHERE id = 3",
                              (bad,))

    def test_failed_later_step_rolls_the_column_back(self):
        def fail(conn):
            raise RuntimeError("simulated failure")

        registry = MIGRATIONS + (
            migrations.Migration(SCHEMA_VERSION + 1, "fails", fail),
        )

        with self.assertRaises(RuntimeError):
            migrations.migrate(self.path, migrations=registry)

        self.assertEqual(self.version(), 4)
        columns = [row[1] for row in self.rows("PRAGMA table_info(users)")]
        self.assertNotIn("is_active", columns)

    def test_existing_column_without_constraints_is_refused(self):
        self.rows("ALTER TABLE users ADD COLUMN is_active INTEGER")

        with self.assertRaises(migrations.IncompatibleSchemaError):
            migrations.migrate(self.path)

        self.assertEqual(self.version(), 4)

    def test_status_reports_the_pending_migration(self):
        status = migrations.schema_status(self.path)

        self.assertEqual(status.state, "outdated")
        self.assertEqual([m.version for m in status.pending], [5, 6])

    def test_upgraded_account_can_authenticate(self):
        migrations.migrate(self.path)
        self.rows("UPDATE users SET password_hash = ? WHERE id = 3",
                  (generate_password_hash(PASSWORD, FAST_HASH),))

        with patch.object(db, "DB_PATH", self.path):
            old = app.config.get("WTF_CSRF_ENABLED")
            self.addCleanup(app.config.update, {"WTF_CSRF_ENABLED": old})
            app.config["WTF_CSRF_ENABLED"] = False

            client = app.test_client()
            client.environ_base["REMOTE_ADDR"] = unique_address()
            client.post("/login",
                        data={"username": "alice", "password": PASSWORD})

            self.assertEqual(client.get("/").status_code, 200)


# ---------------------------------------------------
# 10: DATABASE ERRORS FAIL SECURELY
# ---------------------------------------------------

class TestDatabaseFailures(AuthTestCase):

    ERRORS = (
        sqlite3.OperationalError("database is locked at /secret/path"),
        sqlite3.DatabaseError("database disk image is malformed"),
        sqlite3.InterfaceError("bad parameter"),
    )

    def test_account_lookup_failure_blocks_every_action(self):
        self.login("root")
        before = self.ticket_row(self.bob_ticket)

        for error in self.ERRORS:
            with patch("app.db.get_user_by_id", side_effect=error):
                for method, url, data in self.protected_requests():
                    with self.subTest(error=type(error), url=url):
                        response = self.send(method, url, data)
                        self.assertEqual(response.status_code, 503)

        self.assertEqual(self.ticket_row(self.bob_ticket), before)

    def test_error_details_are_not_shown(self):
        self.login("root")

        with patch("app.db.get_user_by_id", side_effect=self.ERRORS[0]):
            response = self.client.get("/")

        body = response.get_data(as_text=True)
        self.assertNotIn("/secret/path", body)
        self.assertNotIn("locked", body)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_transient_failure_keeps_the_session(self):
        self.login("alice")

        with patch("app.db.get_user_by_id", side_effect=self.ERRORS[0]):
            self.client.get("/")

        self.assertEqual(self.client.get("/").status_code, 200)

    def test_failure_does_not_override_a_later_deactivation(self):
        self.login("alice")

        with patch("app.db.get_user_by_id", side_effect=self.ERRORS[0]):
            self.client.get("/")

        self.set_active("alice", False)
        self.assert_sent_to_login(self.client.get("/"))

    def test_login_database_failure_grants_nothing(self):
        for error in self.ERRORS:
            with self.subTest(error=type(error)):
                with patch("app.db.get_user", side_effect=error):
                    response = self.login("alice")

                self.assertEqual(response.status_code, 503)
                self.assertNotIn("user_id", self.session_of())

    def test_schema_without_is_active_fails_closed(self):
        path = os.path.join(os.path.dirname(self.db_path), "old.db")
        migrations.migrate(path, migrations=MIGRATIONS[:4])
        conn = sqlite3.connect(path)
        with conn:
            conn.execute(
                "INSERT INTO users (id, username, password_hash, role) "
                "VALUES (1, 'alice', ?, 'admin')",
                (generate_password_hash(PASSWORD, FAST_HASH),)
            )
        conn.close()

        self.set_session(user_id=1)

        with patch.object(db, "DB_PATH", path):
            page = self.client.get("/")
            login = self.new_client().post(
                "/login", data={"username": "alice", "password": PASSWORD}
            )

        self.assertEqual(page.status_code, 503)
        self.assertEqual(login.status_code, 503)

    def test_logout_works_during_a_database_failure(self):
        self.login("alice")

        with patch("app.db.get_user_by_id", side_effect=self.ERRORS[0]):
            response = self.client.post("/logout")

        self.assert_sent_to_login(response)
        self.assertNotIn("user_id", self.session_of())

    def test_static_files_need_no_database(self):
        self.login("alice")

        with patch("app.db.get_user_by_id",
                   side_effect=self.ERRORS[0]) as lookup:
            response = self.client.get("/static/style.css")

        self.assertEqual(response.status_code, 200)
        response.close()
        lookup.assert_not_called()


# ---------------------------------------------------
# 11: CSRF AND RATE LIMITING STILL APPLY
# ---------------------------------------------------

class TestExistingProtections(AuthTestCase):

    def token_from(self, url):
        html = self.client.get(url).get_data(as_text=True)
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
        self.assertIsNotNone(match)
        return match.group(1)

    def enable_csrf_and_log_in(self, username="tech"):
        app.config["WTF_CSRF_ENABLED"] = True
        token = self.token_from("/login")
        self.client.post("/login", data={
            "username": username, "password": PASSWORD, "csrf_token": token
        })

    def test_login_requires_a_csrf_token(self):
        app.config["WTF_CSRF_ENABLED"] = True

        response = self.login("alice")

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("user_id", self.session_of())

    def test_state_changes_require_a_csrf_token(self):
        self.enable_csrf_and_log_in()
        before = self.ticket_row(self.bob_ticket)

        for url, data in (
            (f"/tickets/{self.bob_ticket}/update", {"status": "Closed"}),
            (f"/tickets/{self.bob_ticket}/comment", {"content": "no token"}),
            ("/tickets/new", {"title": "t", "description": "d"}),
            ("/logout", {}),
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    self.client.post(url, data=data).status_code, 400
                )

        self.assertEqual(self.ticket_row(self.bob_ticket), before)
        self.assertEqual(self.session_of()["user_id"], self.ids["tech"])

    def test_valid_token_works_with_a_verified_account(self):
        self.enable_csrf_and_log_in()
        token = self.token_from(f"/tickets/{self.bob_ticket}")

        response = self.client.post(
            f"/tickets/{self.bob_ticket}/comment",
            data={"content": "looking now", "csrf_token": token}
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.ticket_row(self.bob_ticket)[0][2], 1)

    def test_valid_token_does_not_help_a_deactivated_account(self):
        self.enable_csrf_and_log_in()
        token = self.token_from(f"/tickets/{self.bob_ticket}")
        self.set_active("tech", False)

        response = self.client.post(
            f"/tickets/{self.bob_ticket}/comment",
            data={"content": "after removal", "csrf_token": token}
        )

        self.assert_sent_to_login(response)
        self.assertEqual(self.ticket_row(self.bob_ticket)[0][2], 0)

    def test_login_rate_limit_applies_to_inactive_accounts(self):
        self.set_active("alice", False)

        for _ in range(5):
            self.assertEqual(self.login("alice").status_code, 200)

        self.assertEqual(self.login("alice").status_code, 429)

    def test_rate_limit_blocks_even_the_correct_password(self):
        for _ in range(5):
            self.login("alice", "wrong-password")

        response = self.login("alice")

        self.assertEqual(response.status_code, 429)
        self.assertNotIn("user_id", self.session_of())


# ---------------------------------------------------
# DOCUMENTED BOUNDARY (DAY 32)
# ---------------------------------------------------

class TestKnownSessionBoundary(AuthTestCase):

    def test_copied_cookie_still_works_after_ordinary_logout(self):
        """
        Records a KNOWN LIMITATION rather than desired behaviour.

        Flask sessions live in the signed cookie, so logout clears only
        the browser that sent it. Full revocation is planned for Day 32;
        this test should then be changed to expect a rejection.
        """

        self.login("alice")
        copied = self.client.get_cookie("session").value

        self.client.post("/logout")
        self.assert_sent_to_login(self.client.get("/"))

        attacker = self.new_client()
        attacker.set_cookie("session", copied)

        self.assertEqual(attacker.get("/").status_code, 200)

    def test_copied_cookie_stops_working_once_deactivated(self):
        self.login("alice")
        copied = self.client.get_cookie("session").value
        self.client.post("/logout")
        self.set_active("alice", False)

        attacker = self.new_client()
        attacker.set_cookie("session", copied)

        self.assert_sent_to_login(attacker.get("/"))


if __name__ == "__main__":
    unittest.main()
