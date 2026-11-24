"""
Security regression tests for session expiry and revocation.

Replay tests copy the real signed cookie out of one test client and send
it from a second client, as an attacker holding a stolen cookie would.
Deleting the first browser's cookie would prove nothing. All data lives
in temporary databases.
"""

import contextlib
import email.utils
import io
import itertools
import os
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import database as db
import migrations
from app import SESSION_MAX_LIFETIME, app
from migrations import MIGRATIONS, SCHEMA_VERSION
from setup_accounts import setup_account
from tests.test_database_authentication import (
    FAST_HASH,
    PASSWORD,
    AuthTestCase,
)


NEW_PASSWORD = "a-different-long-test-passphrase"
MAX_AGE = int(SESSION_MAX_LIFETIME.total_seconds())
START = 2_000_000_000

_client_numbers = itertools.count()


class SessionTestCase(AuthTestCase):

    def new_client(self):
        # A separate address range from test_database_authentication, as
        # the in-memory login rate limit is shared by the whole test run
        number = next(_client_numbers)
        client = app.test_client()
        client.environ_base["REMOTE_ADDR"] = (
            f"172.16.{number // 256 % 256}.{number % 256}"
        )
        return client

    def cookie_of(self, client=None):
        cookie = (client or self.client).get_cookie("session")
        self.assertIsNotNone(cookie)
        return cookie.value

    def client_with_cookie(self, value):
        """A different browser holding a copy of a session cookie."""

        client = self.new_client()
        client.set_cookie("session", value)
        return client

    def version_of(self, username):
        return self.sql(
            "SELECT session_version FROM users WHERE username = ?",
            (username,)
        )[0][0]

    def change_password_with_script(self, username, old, new):
        output = io.StringIO()

        with patch("builtins.input", return_value=username), \
                patch("setup_accounts.getpass",
                      side_effect=[old, new, new]), \
                contextlib.redirect_stdout(output):
            setup_account()

        return output.getvalue()

    def csrf_token(self, client, url):
        html = client.get(url).get_data(as_text=True)
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
        self.assertIsNotNone(match)
        return match.group(1)

    def login_with_csrf(self, username, client=None):
        client = client or self.client
        app.config["WTF_CSRF_ENABLED"] = True
        token = self.csrf_token(client, "/login")
        return client.post("/login", data={
            "username": username,
            "password": PASSWORD,
            "csrf_token": token,
        })


# ---------------------------------------------------
# 1: SUCCESSFUL LOGIN
# ---------------------------------------------------

class TestLoginSession(SessionTestCase):

    def test_login_works(self):
        self.assertEqual(self.login("alice").status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_session_holds_only_identity_version_and_time(self):
        before = int(time.time())
        self.login("tech")
        stored = self.session_of()

        self.assertEqual(stored["user_id"], self.ids["tech"])
        self.assertEqual(stored["session_version"], 1)
        self.assertGreaterEqual(stored["issued_at"], before)

        for key in ("username", "role", "is_active", "password_hash"):
            self.assertNotIn(key, stored)

    def test_login_uses_the_current_database_version(self):
        self.sql("UPDATE users SET session_version = 7 WHERE username = ?",
                 ("alice",))

        self.login("alice")

        self.assertEqual(self.session_of()["session_version"], 7)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_login_does_not_revoke_other_devices(self):
        other = self.new_client()
        self.login("alice", client=other)

        self.login("alice")

        self.assertEqual(other.get("/").status_code, 200)
        self.assertEqual(self.version_of("alice"), 1)

    def test_cookie_has_security_flags_and_lifetime(self):
        before = time.time()
        response = self.login("alice")
        header = response.headers["Set-Cookie"]

        self.assertIn("HttpOnly", header)
        self.assertIn("SameSite=Lax", header)

        # Flask sends the lifetime as an Expires date
        expires = re.search(r"Expires=([^;]+)", header).group(1)
        remaining = (
            email.utils.parsedate_to_datetime(expires).timestamp() - before
        )
        self.assertAlmostEqual(remaining, MAX_AGE, delta=5)

    def test_ordinary_requests_do_not_refresh_the_cookie(self):
        self.login("alice")

        # The first page reads the "welcome" message, which saves the session
        self.client.get("/")
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Set-Cookie", response.headers)

    def test_failed_login_issues_no_session(self):
        self.login("alice", "wrong-password")

        self.assertNotIn("user_id", self.session_of())
        self.assertEqual(self.version_of("alice"), 1)


# ---------------------------------------------------
# 2 AND 3: LOGOUT AND COOKIE REPLAY
# ---------------------------------------------------

class TestLogoutRevocation(SessionTestCase):

    def test_logout_increments_the_version_once(self):
        self.login("alice")
        self.client.post("/logout")
        self.assertEqual(self.version_of("alice"), 2)

    def test_logout_invalidates_the_previous_session(self):
        self.login("alice")
        self.client.post("/logout")

        self.assertNotIn("user_id", self.session_of())
        self.assert_sent_to_login(self.client.get("/"))

    def test_copied_cookie_cannot_be_replayed_after_logout(self):
        self.login("alice")
        copied = self.cookie_of()

        # The stolen copy works before logout, proving the test is real
        self.assertEqual(
            self.client_with_cookie(copied).get("/").status_code, 200
        )

        self.client.post("/logout")

        self.assert_sent_to_login(self.client_with_cookie(copied).get("/"))

    def test_replay_cannot_read_any_protected_page(self):
        self.login("root")
        copied = self.cookie_of()
        self.client.post("/logout")

        for url in ("/", "/?sla=overdue", "/tickets/export",
                    "/tickets/export?sla=within", "/tickets/new",
                    f"/tickets/{self.bob_ticket}"):
            with self.subTest(url=url):
                response = self.client_with_cookie(copied).get(url)
                self.assert_sent_to_login(response)

    def test_replay_cannot_change_tickets(self):
        self.login("root")
        copied = self.cookie_of()
        self.client.post("/logout")
        before = self.ticket_row(self.bob_ticket)

        for url, data in (
            (f"/tickets/{self.bob_ticket}/comment", {"content": "replay"}),
            (f"/tickets/{self.bob_ticket}/update",
             {"status": "Closed", "assigned_to": "root"}),
            ("/tickets/new",
             {"title": "Replay", "description": "d", "priority": "Low"}),
        ):
            with self.subTest(url=url):
                response = self.client_with_cookie(copied).post(url, data=data)
                self.assert_sent_to_login(response)

        self.assertEqual(self.ticket_row(self.bob_ticket), before)
        self.assertEqual(
            self.sql("SELECT COUNT(*) FROM tickets WHERE title = 'Replay'"),
            [(0,)]
        )

    def test_rejected_replay_receives_no_usable_session(self):
        self.login("alice")
        copied = self.cookie_of()
        self.client.post("/logout")

        attacker = self.client_with_cookie(copied)
        attacker.get("/")

        self.assertNotIn("user_id", self.session_of(attacker))
        self.assert_sent_to_login(attacker.get("/"))

    def test_logout_ends_every_device_for_the_account(self):
        laptop = self.new_client()
        phone = self.new_client()
        self.login("alice", client=laptop)
        self.login("alice", client=phone)

        laptop.post("/logout")

        self.assert_sent_to_login(phone.get("/"))

    def test_logout_does_not_affect_other_accounts(self):
        other = self.new_client()
        self.login("bob", client=other)
        self.login("alice")

        self.client.post("/logout")

        self.assertEqual(other.get("/").status_code, 200)
        self.assertEqual(self.version_of("bob"), 1)

    def test_old_cookie_stays_dead_after_logging_in_again(self):
        self.login("alice")
        copied = self.cookie_of()
        self.client.post("/logout")

        self.login("alice")

        self.assertEqual(self.client.get("/").status_code, 200)
        self.assert_sent_to_login(self.client_with_cookie(copied).get("/"))

    def test_replayed_logout_cannot_sign_out_a_newer_session(self):
        self.login("alice")
        copied = self.cookie_of()
        self.client.post("/logout")
        self.login("alice")

        self.client_with_cookie(copied).post("/logout")

        self.assertEqual(self.version_of("alice"), 2)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_anonymous_logout_changes_nothing(self):
        response = self.client.post("/logout")

        self.assert_sent_to_login(response)
        self.assertEqual(self.version_of("alice"), 1)

    def test_logout_message_says_all_devices(self):
        self.login("alice")

        response = self.client.post("/logout", follow_redirects=True)

        self.assertIn("logged out on all devices",
                      response.get_data(as_text=True))


# ---------------------------------------------------
# 4: SESSION EXPIRY
# ---------------------------------------------------

class TestSessionExpiry(SessionTestCase):

    def login_at(self, moment, username="alice"):
        with patch("app.current_time", return_value=moment):
            self.login(username)

    def get_at(self, moment, url="/", client=None):
        with patch("app.current_time", return_value=moment):
            return (client or self.client).get(url)

    def test_maximum_lifetime_is_eight_hours(self):
        self.assertEqual(MAX_AGE, 8 * 60 * 60)
        self.assertEqual(app.permanent_session_lifetime, SESSION_MAX_LIFETIME)

    def test_session_valid_one_second_before_expiry(self):
        self.login_at(START)
        response = self.get_at(START + MAX_AGE - 1)
        self.assertEqual(response.status_code, 200)

    def test_session_expires_exactly_at_the_limit(self):
        self.login_at(START)
        self.assert_sent_to_login(self.get_at(START + MAX_AGE))

    def test_expired_session_is_cleared(self):
        self.login_at(START)
        self.get_at(START + MAX_AGE + 1)
        self.assertNotIn("user_id", self.session_of())

    def test_activity_does_not_extend_the_session(self):
        self.login_at(START)

        for hours in range(1, 8):
            self.assertEqual(
                self.get_at(START + hours * 3600).status_code, 200
            )

        self.assert_sent_to_login(self.get_at(START + MAX_AGE))

    def test_saving_session_data_does_not_extend_it(self):
        self.login_at(START)

        # Rendering a form writes a CSRF token, which re-signs the cookie
        app.config["WTF_CSRF_ENABLED"] = True
        self.assertEqual(
            self.get_at(START + MAX_AGE - 10, "/tickets/new").status_code,
            200
        )

        self.assert_sent_to_login(self.get_at(START + MAX_AGE))

    def test_copied_cookie_also_expires(self):
        self.login_at(START)
        copied = self.client_with_cookie(self.cookie_of())

        self.assert_sent_to_login(
            self.get_at(START + MAX_AGE, client=copied)
        )

    def test_expired_admin_cannot_perform_any_action(self):
        before = self.ticket_row(self.bob_ticket)

        for method, url, data in self.protected_requests():
            with self.subTest(method=method, url=url):
                self.set_session(user_id=self.ids["root"],
                                 issued_at=START - MAX_AGE)

                with patch("app.current_time", return_value=START):
                    response = self.send(method, url, data)

                self.assert_sent_to_login(response)

        self.assertEqual(self.ticket_row(self.bob_ticket), before)

    def test_signature_age_is_also_enforced(self):
        self.login("alice")
        now = time.time()

        with patch("time.time", return_value=now + MAX_AGE + 5):
            response = self.client.get("/")

        self.assert_sent_to_login(response)

    def test_future_login_time_is_rejected(self):
        self.set_session(user_id=self.ids["alice"], issued_at=START + 61)
        self.assert_sent_to_login(self.get_at(START))

    def test_small_clock_correction_is_tolerated(self):
        self.set_session(user_id=self.ids["alice"], issued_at=START + 60)
        self.assertEqual(self.get_at(START).status_code, 200)

    def test_expired_cookie_cannot_revoke_newer_sessions(self):
        self.login_at(START)
        expired = self.client_with_cookie(self.cookie_of())

        with patch("app.current_time", return_value=START + MAX_AGE):
            expired.post("/logout")

        self.assertEqual(self.version_of("alice"), 1)


# ---------------------------------------------------
# 5: PASSWORD CHANGES
# ---------------------------------------------------

class TestPasswordChange(SessionTestCase):

    def test_password_change_invalidates_older_sessions(self):
        self.login("alice")
        copied = self.cookie_of()

        output = self.change_password_with_script(
            "alice", PASSWORD, NEW_PASSWORD
        )

        self.assertIn("sessions", output)
        self.assert_sent_to_login(self.client.get("/"))
        self.assert_sent_to_login(self.client_with_cookie(copied).get("/"))

    def test_new_password_can_log_in_afterwards(self):
        self.login("alice")
        self.change_password_with_script("alice", PASSWORD, NEW_PASSWORD)
        self.client.get("/")

        self.login("alice", NEW_PASSWORD)

        self.assertEqual(self.client.get("/").status_code, 200)

    def test_old_password_no_longer_logs_in(self):
        self.change_password_with_script("alice", PASSWORD, NEW_PASSWORD)

        self.login("alice", PASSWORD)

        self.assertNotIn("user_id", self.session_of())

    def test_failed_password_change_keeps_sessions(self):
        self.login("alice")

        self.change_password_with_script(
            "alice", "not-the-password", NEW_PASSWORD
        )

        self.assertEqual(self.version_of("alice"), 1)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_direct_sql_password_change_also_revokes(self):
        self.login("alice")

        self.sql("UPDATE users SET password_hash = ? WHERE username = ?",
                 (generate_password_hash(NEW_PASSWORD, FAST_HASH), "alice"))

        self.assertEqual(self.version_of("alice"), 2)
        self.assert_sent_to_login(self.client.get("/"))

    def test_rolled_back_password_change_does_not_revoke(self):
        self.login("alice")
        conn = sqlite3.connect(self.db_path)

        try:
            conn.execute("UPDATE users SET password_hash = 'x' "
                         "WHERE username = 'alice'")
            conn.rollback()
        finally:
            conn.close()

        self.assertEqual(self.version_of("alice"), 1)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_saving_an_identical_hash_does_not_revoke(self):
        self.login("alice")

        self.sql("UPDATE users SET password_hash = password_hash "
                 "WHERE username = 'alice'")

        self.assertEqual(self.version_of("alice"), 1)

    def test_password_change_affects_only_that_account(self):
        other = self.new_client()
        self.login("bob", client=other)

        self.change_password_with_script("alice", PASSWORD, NEW_PASSWORD)

        self.assertEqual(other.get("/").status_code, 200)


# ---------------------------------------------------
# 6 AND 7: DEACTIVATION AND DELETION
# ---------------------------------------------------

class TestDeactivationAndDeletion(SessionTestCase):

    def test_deactivation_rejects_copied_cookie(self):
        self.login("alice")
        copied = self.cookie_of()

        self.set_active("alice", False)

        self.assert_sent_to_login(self.client_with_cookie(copied).get("/"))

    def test_reactivation_does_not_restore_old_cookies(self):
        self.login("alice")
        copied = self.cookie_of()

        self.set_active("alice", False)
        self.set_active("alice", True)

        self.assert_sent_to_login(self.client_with_cookie(copied).get("/"))

    def test_inactive_status_is_enforced_without_the_version_change(self):
        self.login("alice")

        # Even if the version were restored, is_active must still block it
        self.set_active("alice", False)
        self.sql("UPDATE users SET session_version = 1 WHERE username = ?",
                 ("alice",))

        self.assert_sent_to_login(self.client.get("/"))

    def test_deactivation_increments_the_version(self):
        self.set_active("alice", False)
        self.assertEqual(self.version_of("alice"), 2)

    def test_repeated_deactivation_does_not_increment_again(self):
        self.set_active("alice", False)
        self.set_active("alice", False)
        self.assertEqual(self.version_of("alice"), 2)

    def test_deactivated_account_can_log_out_safely(self):
        self.login("alice")
        self.set_active("alice", False)

        response = self.client.post("/logout")

        self.assert_sent_to_login(response)
        self.assertNotIn("user_id", self.session_of())
        self.assertEqual(self.version_of("alice"), 2)

    def test_deleted_account_session_is_rejected(self):
        self.login("alice")
        copied = self.cookie_of()

        self.delete_user("alice")

        self.assert_sent_to_login(self.client_with_cookie(copied).get("/"))

    def test_deleted_account_can_log_out_safely(self):
        self.login("alice")
        self.delete_user("alice")

        response = self.client.post("/logout")

        self.assert_sent_to_login(response)
        self.assertNotIn("user_id", self.session_of())

    def test_new_account_never_reuses_a_deleted_id(self):
        self.login("root")
        copied = self.cookie_of()
        old_id = self.ids["root"]

        self.delete_user("root")
        new_id = self.add_user("replacement", "admin")

        self.assertNotEqual(new_id, old_id)
        self.assert_sent_to_login(self.client_with_cookie(copied).get("/"))


# ---------------------------------------------------
# 8: ROLE DEMOTION
# ---------------------------------------------------

class TestTechnicianDemotion(SessionTestCase):

    def setUp(self):
        super().setUp()
        self.login("tech")
        self.copied = self.cookie_of()
        self.set_role("tech", "user")

    def test_demotion_applies_without_a_new_login(self):
        self.assertEqual(
            self.client.get(f"/tickets/{self.bob_ticket}").status_code, 404
        )

    def test_copied_cookie_gets_only_the_new_role(self):
        attacker = self.client_with_cookie(self.copied)

        self.assertEqual(
            attacker.get(f"/tickets/{self.bob_ticket}").status_code, 404
        )

    def test_demoted_user_stays_logged_in_with_own_tickets(self):
        self.assertEqual(self.version_of("tech"), 1)
        self.assertEqual(
            self.client.get(f"/tickets/{self.tech_ticket}").status_code, 200
        )

    def test_demoted_user_cannot_change_tickets(self):
        before = self.ticket_row(self.bob_ticket)

        self.client.post(f"/tickets/{self.bob_ticket}/update",
                         data={"status": "Closed", "assigned_to": "tech"})

        self.assertEqual(self.ticket_row(self.bob_ticket), before)

    def test_role_placed_in_a_cookie_is_refused(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "admin"

        self.assert_sent_to_login(
            self.client.get(f"/tickets/{self.bob_ticket}")
        )


# ---------------------------------------------------
# 9: INVALID SESSION VERSIONS
# ---------------------------------------------------

class TestInvalidSessions(SessionTestCase):

    BAD_INTEGERS = (0, -1, "1", True, False, 1.0, None, [1], {"v": 1},
                    2 ** 63, 10 ** 30, "1 OR 1=1")

    def assert_rejected(self):
        with patch("app.db.revoke_user_sessions") as revoke:
            self.assert_sent_to_login(self.client.get("/"))
            revoke.assert_not_called()

        self.assertNotIn("user_id", self.session_of())

    def test_malformed_versions_are_rejected(self):
        for bad in self.BAD_INTEGERS:
            with self.subTest(session_version=bad):
                self.set_session(user_id=self.ids["alice"],
                                 session_version=bad)
                self.assert_rejected()

    def test_malformed_user_ids_are_rejected(self):
        for bad in self.BAD_INTEGERS:
            with self.subTest(user_id=bad):
                self.set_session(user_id=bad)
                self.assert_rejected()

    def test_malformed_login_times_are_rejected(self):
        for bad in self.BAD_INTEGERS:
            with self.subTest(issued_at=bad):
                self.set_session(user_id=self.ids["alice"], issued_at=bad)
                self.assert_rejected()

    def test_user_id_zero_is_rejected_even_if_a_row_exists(self):
        self.sql("INSERT INTO users (id, username, password_hash, role) "
                 "VALUES (0, 'zero', 'unused', 'admin')")
        self.set_session(user_id=0)

        self.assert_rejected()

    def test_older_version_is_rejected(self):
        self.sql("UPDATE users SET session_version = 5 WHERE username = ?",
                 ("alice",))
        self.set_session(user_id=self.ids["alice"], session_version=4)
        self.assert_rejected()

    def test_newer_version_is_rejected(self):
        self.set_session(user_id=self.ids["alice"], session_version=2)
        self.assert_rejected()

    def test_each_missing_claim_is_rejected(self):
        full = {
            "user_id": self.ids["alice"],
            "session_version": 1,
            "issued_at": int(time.time()),
        }

        for missing in full:
            with self.subTest(missing=missing):
                with self.client.session_transaction() as sess:
                    sess.clear()
                    sess.update({key: value for key, value in full.items()
                                 if key != missing})

                self.assert_sent_to_login(self.client.get("/"))
                self.assertFalse(
                    set(full) & set(self.session_of())
                )

    def test_tampered_cookie_is_ignored(self):
        self.login("root")
        value = self.cookie_of()
        tampered = value[:-2] + ("aa" if value[-2:] != "aa" else "bb")

        self.assert_sent_to_login(self.client_with_cookie(tampered).get("/"))

    def test_malformed_session_cannot_revoke(self):
        self.set_session(user_id=self.ids["alice"], session_version="1")
        self.client.post("/logout")
        self.assertEqual(self.version_of("alice"), 1)


# ---------------------------------------------------
# 10: CONCURRENT REVOCATION
# ---------------------------------------------------

class TestConcurrentRevocation(SessionTestCase):

    WORKERS = 8

    def run_together(self, work):
        """Start `work(index)` in every thread at the same moment."""

        barrier = threading.Barrier(self.WORKERS)
        results = [None] * self.WORKERS
        errors = []

        def runner(index):
            try:
                barrier.wait(timeout=10)
                results[index] = work(index)
            except Exception as error:  # reported by the assertion below
                errors.append(error)

        threads = [
            threading.Thread(target=runner, args=(index,))
            for index in range(self.WORKERS)
        ]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        return results

    def test_only_one_concurrent_revocation_wins(self):
        user_id = self.ids["alice"]

        results = self.run_together(
            lambda _: db.revoke_user_sessions(user_id, 1)
        )

        self.assertEqual(results.count(True), 1)
        self.assertEqual(self.version_of("alice"), 2)

    def test_concurrent_logouts_with_one_cookie(self):
        self.login("alice")
        copied = self.cookie_of()
        clients = [self.client_with_cookie(copied)
                   for _ in range(self.WORKERS)]

        results = self.run_together(
            lambda index: clients[index].post("/logout").status_code
        )

        self.assertEqual(results, [302] * self.WORKERS)
        self.assertEqual(self.version_of("alice"), 2)
        self.assert_sent_to_login(self.client_with_cookie(copied).get("/"))

    def test_logout_racing_a_password_change_still_revokes(self):
        self.login("alice")
        copied = self.cookie_of()
        user_id = self.ids["alice"]

        def work(index):
            if index % 2:
                return db.revoke_user_sessions(user_id, 1)

            conn = sqlite3.connect(self.db_path, timeout=10)
            try:
                with conn:
                    conn.execute(
                        "UPDATE users SET password_hash = ? WHERE id = ?",
                        (f"hash-{index}", user_id)
                    )
            finally:
                conn.close()

        self.run_together(work)

        self.assertGreaterEqual(self.version_of("alice"), 2)
        self.assert_sent_to_login(self.client_with_cookie(copied).get("/"))

    def test_login_racing_a_revocation_never_outlives_it(self):
        self.login("alice")
        old = self.cookie_of()

        self.assertTrue(db.revoke_user_sessions(self.ids["alice"], 1))

        # A login that read version 1 just before the revocation committed
        self.set_session(user_id=self.ids["alice"], session_version=1)

        self.assert_sent_to_login(self.client.get("/"))
        self.assert_sent_to_login(self.client_with_cookie(old).get("/"))


# ---------------------------------------------------
# 11: LEGACY SESSIONS
# ---------------------------------------------------

class TestLegacySessionFormats(SessionTestCase):

    def assert_format_rejected(self, values):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess.update(values)

        self.assert_sent_to_login(self.client.get("/"))
        self.assertFalse(
            {"user_id", "username", "role"} & set(self.session_of())
        )

    def test_day_31_session_format_is_rejected(self):
        self.assert_format_rejected({"user_id": self.ids["root"]})

    def test_original_session_format_is_rejected(self):
        self.assert_format_rejected({"username": "root", "role": "admin"})

    def test_mixed_format_is_rejected(self):
        self.assert_format_rejected({
            "user_id": self.ids["alice"],
            "session_version": 1,
            "issued_at": int(time.time()),
            "role": "admin",
        })

    def test_legacy_cookie_cannot_revoke_sessions(self):
        with self.client.session_transaction() as sess:
            sess.update({"username": "alice", "role": "user",
                         "user_id": self.ids["alice"],
                         "session_version": 1,
                         "issued_at": int(time.time())})

        self.client.post("/logout")

        self.assertEqual(self.version_of("alice"), 1)

    def test_legacy_user_can_log_in_again(self):
        self.assert_format_rejected({"username": "alice", "role": "user"})

        self.login("alice")

        self.assertEqual(self.client.get("/").status_code, 200)


# ---------------------------------------------------
# 12: CSRF PROTECTION
# ---------------------------------------------------

class TestCsrfWithRevocation(SessionTestCase):

    def test_logout_without_token_revokes_nothing(self):
        self.login_with_csrf("alice")

        response = self.client.post("/logout")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.version_of("alice"), 1)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_logout_with_wrong_token_revokes_nothing(self):
        self.login_with_csrf("alice")

        response = self.client.post("/logout",
                                    data={"csrf_token": "not-a-token"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.version_of("alice"), 1)

    def test_logout_with_valid_token_revokes(self):
        self.login_with_csrf("alice")
        token = self.csrf_token(self.client, "/tickets/new")

        response = self.client.post("/logout", data={"csrf_token": token})

        self.assert_sent_to_login(response)
        self.assertEqual(self.version_of("alice"), 2)

    def test_valid_token_in_a_replayed_cookie_changes_nothing(self):
        self.login_with_csrf("root")
        token = self.csrf_token(self.client, f"/tickets/{self.bob_ticket}")
        copied = self.cookie_of()
        self.client.post("/logout", data={"csrf_token": token})
        before = self.ticket_row(self.bob_ticket)

        attacker = self.client_with_cookie(copied)
        response = attacker.post(
            f"/tickets/{self.bob_ticket}/comment",
            data={"content": "replayed", "csrf_token": token}
        )

        self.assert_sent_to_login(response)
        self.assertEqual(self.ticket_row(self.bob_ticket), before)

    def test_login_still_requires_a_token(self):
        app.config["WTF_CSRF_ENABLED"] = True

        response = self.login("alice")

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("user_id", self.session_of())

    def test_ticket_changes_still_require_a_token(self):
        self.login_with_csrf("tech")
        before = self.ticket_row(self.bob_ticket)

        response = self.client.post(
            f"/tickets/{self.bob_ticket}/update",
            data={"status": "Closed"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.ticket_row(self.bob_ticket), before)


# ---------------------------------------------------
# 13 AND 14: OWNERSHIP, CSV AND SLA
# ---------------------------------------------------

class TestAuthorizationWithVersionedSessions(SessionTestCase):

    SLA_KEYS = ("within", "due_soon", "overdue", "met", "breached",
                "not_tracked")

    def test_user_cannot_open_another_users_ticket(self):
        self.login("alice")
        self.assertEqual(
            self.client.get(f"/tickets/{self.bob_ticket}").status_code, 404
        )

    def test_user_cannot_comment_on_another_users_ticket(self):
        self.login("alice")
        before = self.ticket_row(self.bob_ticket)

        response = self.client.post(f"/tickets/{self.bob_ticket}/comment",
                                    data={"content": "intrusion"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.ticket_row(self.bob_ticket), before)

    def test_user_cannot_update_own_ticket(self):
        self.login("alice")
        before = self.ticket_row(self.alice_ticket)

        self.client.post(f"/tickets/{self.alice_ticket}/update",
                         data={"status": "Closed"})

        self.assertEqual(self.ticket_row(self.alice_ticket), before)

    def test_csv_contains_only_own_tickets(self):
        self.login("alice")

        text = self.client.get(
            "/tickets/export?username=bob&role=admin"
        ).get_data(as_text=True)

        self.assertIn("Alice laptop", text)
        self.assertNotIn("Bob printer", text)

    def test_every_csv_sla_filter_is_limited_to_own_tickets(self):
        self.login("alice")

        for key in self.SLA_KEYS:
            with self.subTest(sla=key):
                text = self.client.get(
                    f"/tickets/export?sla={key}"
                ).get_data(as_text=True)

                self.assertNotIn("Bob printer", text)

    def test_every_sla_dashboard_filter_is_limited_to_own_tickets(self):
        self.login("alice")

        for key in self.SLA_KEYS:
            with self.subTest(sla=key):
                html = self.client.get(f"/?sla={key}").get_data(as_text=True)
                self.assertNotIn("Bob printer", html)

    def test_staff_can_export_every_ticket(self):
        self.login("tech")

        text = self.client.get("/tickets/export").get_data(as_text=True)

        self.assertIn("Alice laptop", text)
        self.assertIn("Bob printer", text)

    def test_revoked_staff_cookie_cannot_export(self):
        self.login("tech")
        copied = self.cookie_of()
        self.client.post("/logout")

        response = self.client_with_cookie(copied).get("/tickets/export")

        self.assert_sent_to_login(response)
        self.assertNotIn("Bob printer", response.get_data(as_text=True))

    def test_revoked_staff_cookie_cannot_read_sla_dashboard(self):
        self.login("tech")
        copied = self.cookie_of()
        self.client.post("/logout")

        for key in self.SLA_KEYS:
            with self.subTest(sla=key):
                response = self.client_with_cookie(copied).get(f"/?sla={key}")
                self.assert_sent_to_login(response)


# ---------------------------------------------------
# 15: DATABASE FAILURES
# ---------------------------------------------------

class TestFailedDatabaseOperations(SessionTestCase):

    ERRORS = (
        sqlite3.OperationalError("database is locked at /secret/path"),
        sqlite3.DatabaseError("database disk image is malformed"),
        sqlite3.IntegrityError("constraint failed"),
    )

    def test_failed_verification_blocks_every_action(self):
        self.login("root")
        before = self.ticket_row(self.bob_ticket)

        for error in self.ERRORS:
            with patch("app.db.get_user_by_id", side_effect=error):
                for method, url, data in self.protected_requests():
                    with self.subTest(error=type(error), url=url):
                        self.assertEqual(
                            self.send(method, url, data).status_code, 503
                        )

        self.assertEqual(self.ticket_row(self.bob_ticket), before)

    def test_unreadable_database_file_grants_nothing(self):
        self.login("root")
        directory = os.path.dirname(self.db_path)

        with patch.object(db, "DB_PATH", directory):
            page = self.client.get("/")
            login = self.new_client().post(
                "/login", data={"username": "root", "password": PASSWORD}
            )

        self.assertEqual(page.status_code, 503)
        self.assertEqual(login.status_code, 503)

    def test_failed_login_lookup_issues_no_session(self):
        for error in self.ERRORS:
            with self.subTest(error=type(error)):
                with patch("app.db.get_user", side_effect=error):
                    response = self.login("alice")

                self.assertEqual(response.status_code, 503)
                self.assertNotIn("user_id", self.session_of())

    def test_database_without_session_version_fails_closed(self):
        path = os.path.join(os.path.dirname(self.db_path), "version5.db")
        migrations.migrate(path, migrations=MIGRATIONS[:5])
        conn = sqlite3.connect(path)

        with conn:
            conn.execute(
                "INSERT INTO users (id, username, password_hash, role) "
                "VALUES (1, 'root', ?, 'admin')",
                (generate_password_hash(PASSWORD, FAST_HASH),)
            )

        conn.close()
        self.set_session(user_id=1)

        with patch.object(db, "DB_PATH", path):
            page = self.client.get("/")
            login = self.new_client().post(
                "/login", data={"username": "root", "password": PASSWORD}
            )

        self.assertEqual(page.status_code, 503)
        self.assertEqual(login.status_code, 503)

    def test_failed_revocation_still_clears_this_browser(self):
        self.login("alice")

        for error in self.ERRORS:
            with self.subTest(error=type(error)):
                self.login("alice")

                with patch("app.db.revoke_user_sessions", side_effect=error):
                    response = self.client.post("/logout")

                self.assert_sent_to_login(response)
                self.assertNotIn("user_id", self.session_of())

    def test_failed_revocation_warns_honestly(self):
        self.login("alice")

        with patch("app.db.revoke_user_sessions",
                   side_effect=self.ERRORS[0]):
            response = self.client.post("/logout", follow_redirects=True)

        body = response.get_data(as_text=True)
        self.assertIn("could not be ended", body)
        self.assertNotIn("on all devices", body)
        self.assertNotIn("/secret/path", body)

    def test_failed_verification_cannot_bypass_revocation(self):
        self.login("alice")
        copied = self.cookie_of()
        self.client.post("/logout")
        attacker = self.client_with_cookie(copied)

        with patch("app.db.get_user_by_id", side_effect=self.ERRORS[0]):
            self.assertEqual(attacker.get("/").status_code, 503)

        self.assert_sent_to_login(attacker.get("/"))

    def test_rolled_back_revocation_keeps_the_old_version(self):
        conn = sqlite3.connect(self.db_path)

        try:
            conn.execute("UPDATE users SET session_version = 2 "
                         "WHERE username = 'alice'")
            conn.rollback()
        finally:
            conn.close()

        self.assertEqual(self.version_of("alice"), 1)


# ---------------------------------------------------
# MIGRATION 6
# ---------------------------------------------------

class TestSessionVersionMigration(unittest.TestCase):

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.path = os.path.join(temp_dir.name, "upgrade.db")

    def build_version_five(self):
        migrations.migrate(self.path, migrations=MIGRATIONS[:5])
        conn = sqlite3.connect(self.path)

        with conn:
            conn.executemany(
                "INSERT INTO users "
                "(id, username, password_hash, role, is_active) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (3, "alice", "stored-a", "user", 1),
                    (7, "tech", "stored-b", "technician", 0),
                    (9, "root", "stored-c", "admin", 1),
                ]
            )
            conn.execute(
                "INSERT INTO tickets (id, title, description, created_by) "
                "VALUES (5, 'Old', 'Ticket', 'alice')"
            )
            conn.execute(
                "INSERT INTO comments (id, ticket_id, author, content) "
                "VALUES (2, 5, 'tech', 'Looking')"
            )
            conn.execute(
                "INSERT INTO ticket_history (ticket_id, actor, action) "
                "VALUES (5, 'alice', 'created')"
            )
            conn.execute(
                "INSERT INTO ticket_sla (ticket_id, response_due_at) "
                "VALUES (5, '2026-10-01T13:00:00+00:00')"
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

    def test_upgrade_from_version_five(self):
        self.build_version_five()

        result = migrations.migrate(self.path)

        self.assertEqual([m.version for m in result.applied], [6, 7])
        self.assertEqual(self.version(), SCHEMA_VERSION)
        self.assertTrue(migrations.schema_status(self.path).is_current)

    def test_accounts_are_preserved_exactly(self):
        self.build_version_five()
        query = ("SELECT id, username, password_hash, role, is_active "
                 "FROM users ORDER BY id")
        before = self.rows(query)

        migrations.migrate(self.path)

        self.assertEqual(self.rows(query), before)

    def test_all_ticket_data_is_preserved(self):
        self.build_version_five()
        tables = ("tickets", "comments", "ticket_history", "ticket_sla")
        before = {t: self.rows(f"SELECT * FROM {t}") for t in tables}

        migrations.migrate(self.path)

        for table in tables:
            with self.subTest(table=table):
                self.assertEqual(self.rows(f"SELECT * FROM {table}"),
                                 before[table])

    def test_existing_accounts_start_at_version_one(self):
        self.build_version_five()
        migrations.migrate(self.path)

        self.assertEqual(
            self.rows("SELECT DISTINCT session_version FROM users"), [(1,)]
        )

    def test_migration_is_repeatable(self):
        self.build_version_five()
        migrations.migrate(self.path)
        self.rows("UPDATE users SET session_version = 4 WHERE id = 3")
        before = self.rows("SELECT * FROM users ORDER BY id")

        result = migrations.migrate(self.path)

        self.assertEqual(result.applied, [])
        self.assertEqual(self.rows("SELECT * FROM users ORDER BY id"), before)

    def test_invalid_versions_are_refused_by_the_database(self):
        self.build_version_five()
        migrations.migrate(self.path)

        for bad in (0, -1, None, 1.5, "abc"):
            with self.subTest(value=bad):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.rows(
                        "UPDATE users SET session_version = ? WHERE id = 3",
                        (bad,)
                    )

    def test_failed_later_step_rolls_everything_back(self):
        self.build_version_five()

        def fail(conn):
            raise RuntimeError("simulated failure")

        registry = MIGRATIONS + (
            migrations.Migration(SCHEMA_VERSION + 1, "fails", fail),
        )

        with self.assertRaises(RuntimeError):
            migrations.migrate(self.path, migrations=registry)

        self.assertEqual(self.version(), 5)
        columns = [row[1] for row in self.rows("PRAGMA table_info(users)")]
        self.assertNotIn("session_version", columns)
        self.assertEqual(
            self.rows("SELECT name FROM sqlite_master "
                      "WHERE name = 'users_revoke_sessions'"),
            []
        )

    def test_existing_column_without_check_is_refused(self):
        self.build_version_five()
        self.rows("ALTER TABLE users ADD COLUMN session_version "
                  "INTEGER NOT NULL DEFAULT 1")

        with self.assertRaises(migrations.IncompatibleSchemaError):
            migrations.migrate(self.path)

        self.assertEqual(self.version(), 5)

    def test_existing_column_with_bad_values_is_refused(self):
        self.build_version_five()

        self.rows("ALTER TABLE users ADD COLUMN session_version "
                  "INTEGER NOT NULL DEFAULT 0")

        with self.assertRaisesRegex(
            migrations.IncompatibleSchemaError, "invalid values"
        ):
            migrations.migrate(self.path)

        self.assertEqual(self.version(), 5)

    def test_users_table_without_autoincrement_is_refused(self):
        conn = sqlite3.connect(self.path)

        with conn:
            conn.execute("""
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user'
                )
            """)
            conn.execute("""
                CREATE TABLE tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    priority TEXT NOT NULL DEFAULT 'Medium',
                    status TEXT NOT NULL DEFAULT 'Open',
                    assigned_to TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

        conn.close()

        with self.assertRaises(migrations.IncompatibleSchemaError):
            migrations.migrate(self.path)

        self.assertEqual(self.version(), 0)

    def test_missing_trigger_is_reported(self):
        migrations.migrate(self.path)
        self.rows("DROP TRIGGER users_revoke_sessions")

        status = migrations.schema_status(self.path)

        self.assertEqual(status.state, "incompatible")

    def test_weakened_trigger_is_reported(self):
        migrations.migrate(self.path)
        self.rows("DROP TRIGGER users_revoke_sessions")
        self.rows("""
            CREATE TRIGGER users_revoke_sessions
            AFTER UPDATE OF password_hash ON users
            BEGIN
                SELECT 1;
            END
        """)

        status = migrations.schema_status(self.path)

        self.assertEqual(status.state, "incompatible")


if __name__ == "__main__":
    unittest.main()
