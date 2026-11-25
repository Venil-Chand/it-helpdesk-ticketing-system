"""
Sign-in audit events (Package 1b).

Every login attempt that reaches the login view is recorded:
login_succeeded or login_failed, the account (only if it exists), the
reason for a failure, and the client address. Never recorded: passwords,
session cookies, or usernames that match no account. Attempts over the
rate limit are refused before the view runs, so they are not recorded and
recording cannot be used to get round the limit.

Temporary databases only.
"""

import logging
import re
import sqlite3
import unittest
from unittest.mock import patch

from werkzeug.security import check_password_hash

import accounts
from app import app, limiter
from helpdesk import auth

from account_helpers import (
    PASSWORD,
    AccountTestCase,
    fast_hash,
)

WRONG = "definitely-not-the-password"

# The real function, saved before setUp swaps in a fast stand-in
REAL_DUMMY_PASSWORD_HASH = auth._dummy_password_hash

# Each response carries its own random CSRF token; nothing else may differ
CSRF_VALUE = re.compile(r'(name="csrf_token"[^>]*value=")[^"]*"', re.S)


class LoginAuditTestCase(AccountTestCase):

    def setUp(self):
        super().setUp()
        limiter.reset()
        self.addCleanup(limiter.reset)

        # An unknown username is checked against a dummy hash; make that
        # one fast too, like every other hash in these tests
        dummy = fast_hash("dummy-password-nobody-knows")
        patcher = patch.object(auth, "_dummy_password_hash",
                               lambda: dummy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def attempt(self, username, password, client=None):
        client = client or self.new_client()
        return client, client.post(
            "/login", data={"username": username, "password": password}
        )

    def all_audit_text(self):
        """Every stored audit value, as one string, for leak checks."""

        return " ".join(
            str(value)
            for row in self.sql("SELECT * FROM audit_events")
            for value in row
        )


# ---------------------------------------------------
# WHAT IS RECORDED
# ---------------------------------------------------

class TestRecordedEvents(LoginAuditTestCase):

    def test_successful_login_is_recorded_with_address(self):
        client, _ = self.attempt("alice", PASSWORD)
        address = client.environ_base["REMOTE_ADDR"]

        self.assertEqual(self.login_events(), [(
            self.ids["alice"], "alice", "login_succeeded",
            self.ids["alice"], "alice", None, address
        )])

    def test_wrong_password_is_recorded_against_the_account(self):
        client, _ = self.attempt("bob", WRONG)

        self.assertEqual(self.login_events(), [(
            None, "anonymous", "login_failed", self.ids["bob"], "bob",
            "wrong password", client.environ_base["REMOTE_ADDR"]
        )])

    def test_deactivated_account_is_recorded_with_reason(self):
        self.sql("UPDATE users SET is_active = 0 WHERE username = 'bob'")

        self.attempt("bob", PASSWORD)

        self.assertEqual(self.login_events()[0][2:6], (
            "login_failed", self.ids["bob"], "bob", "account deactivated"
        ))

    def test_unknown_username_is_not_stored(self):
        # It could be a password typed into the wrong box
        typed = "Tr0ub4dor&3-typed-in-wrong-box"

        self.attempt(typed, WRONG)

        self.assertEqual(self.login_events(), [(
            None, "anonymous", "login_failed", None, None,
            "unknown account", self.login_events()[0][6]
        )])
        self.assertNotIn(typed, self.all_audit_text())

    def test_passwords_and_cookies_are_never_stored(self):
        client, _ = self.attempt("alice", PASSWORD)
        self.attempt("alice", WRONG)
        cookie = client.get_cookie("session").value

        text = self.all_audit_text()
        for secret in (PASSWORD, WRONG, cookie):
            with self.subTest(secret=secret[:6]):
                self.assertNotIn(secret, text)

    def test_forged_forwarded_header_is_not_trusted(self):
        # No trusted proxy is configured, so X-Forwarded-For is ignored
        client = self.new_client()
        client.post("/login",
                    data={"username": "alice", "password": WRONG},
                    headers={"X-Forwarded-For": "203.0.113.99"})

        self.assertEqual(self.login_events()[0][6],
                         client.environ_base["REMOTE_ADDR"])

    def test_overlong_address_is_truncated(self):
        long_address = "a" * 500
        conn = sqlite3.connect(self.db_path)
        self.addCleanup(conn.close)
        with conn:
            accounts.record_audit_event(
                conn, actor="anonymous", action="login_failed",
                source_address=long_address
            )

        stored = self.sql("SELECT source_address FROM audit_events")[0][0]
        self.assertEqual(len(stored), accounts.MAX_SOURCE_ADDRESS_LENGTH)


# ---------------------------------------------------
# NO USERNAME ENUMERATION
# ---------------------------------------------------

class TestNoUsernameEnumeration(LoginAuditTestCase):

    def test_unknown_wrong_and_deactivated_look_identical(self):
        self.sql("UPDATE users SET is_active = 0 WHERE username = 'bob'")

        responses = [
            self.attempt(username, password)[1]
            for username, password in (
                ("nobody-by-this-name", WRONG),
                ("alice", WRONG),
                ("bob", PASSWORD),
            )
        ]

        bodies = {
            CSRF_VALUE.sub(r'\1TOKEN"', r.get_data(as_text=True))
            for r in responses
        }
        self.assertEqual({r.status_code for r in responses}, {200})
        self.assertEqual(len(bodies), 1)
        self.assertIn("Invalid username or password.", bodies.pop())

    def test_unknown_username_still_runs_a_password_hash(self):
        # Without this, unknown names answer faster than wrong passwords
        with patch("helpdesk.auth.check_password_hash",
                   wraps=check_password_hash) as checked:
            self.attempt("nobody-by-this-name", WRONG)

        checked.assert_called_once()
        self.assertEqual(checked.call_args.args[0],
                         auth._dummy_password_hash())

    def test_real_dummy_hash_uses_the_current_method_and_matches_nothing(
            self):
        # The real function, not the fast stand-in used in these tests
        dummy = REAL_DUMMY_PASSWORD_HASH.__wrapped__()
        self.assertTrue(dummy.startswith("scrypt:"))
        self.assertFalse(check_password_hash(dummy, ""))
        self.assertFalse(check_password_hash(dummy, WRONG))


# ---------------------------------------------------
# RATE LIMIT AND VOLUME
# ---------------------------------------------------

class TestRateLimitAndVolume(LoginAuditTestCase):

    def test_attempts_over_the_limit_are_refused_and_not_recorded(self):
        client = self.new_client()
        codes = [
            self.attempt("alice", f"{WRONG}-{n}", client)[1].status_code
            for n in range(8)
        ]

        self.assertEqual(codes[:5], [200] * 5)
        self.assertEqual(set(codes[5:]), {429})
        self.assertEqual(len(self.login_events()), 5)

        # The correct password is refused too while limited
        _, response = self.attempt("alice", PASSWORD, client)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(self.login_events()), 5)

    def test_high_volume_of_failures_from_many_addresses(self):
        addresses = 60
        for _ in range(addresses):
            client = self.new_client()
            for attempt in range(6):
                _, response = self.attempt(
                    "nobody" if attempt % 2 else "alice", WRONG, client
                )
                self.assertEqual(response.status_code,
                                 429 if attempt == 5 else 200)

        events = self.login_events()
        self.assertEqual(len(events), addresses * 5)
        self.assertEqual({e[2] for e in events}, {"login_failed"})
        self.assertEqual(len({e[6] for e in events}), addresses)

        # The account itself is unaffected: no lock-out was invented
        self.assertTrue(self.can_log_in("alice", PASSWORD))

    def test_audit_page_filters_sign_ins(self):
        self.attempt("alice", WRONG)
        admin = self.signed_in("root")
        accounts.set_active(self.ids["root"], self.ids["bob"], False)

        logins = admin.get("/admin/audit?kind=logins").get_data(as_text=True)
        changes = admin.get("/admin/audit?kind=accounts").get_data(
            as_text=True
        )
        bogus = admin.get("/admin/audit?kind=' OR 1=1 --").get_data(
            as_text=True
        )

        self.assertIn("Sign-in failed", logins)
        self.assertNotIn("Account deactivated", logins)
        self.assertIn("Account deactivated", changes)
        self.assertNotIn("Sign-in failed", changes)
        # An unknown filter is ignored, not passed to SQL
        self.assertIn("Sign-in failed", bogus)
        self.assertIn("Account deactivated", bogus)

    def test_non_admins_cannot_read_sign_in_records(self):
        for username in ("alice", "tech"):
            with self.subTest(username=username):
                response = self.signed_in(username).get(
                    "/admin/audit?kind=logins"
                )
                self.assertEqual(response.status_code, 403)


# ---------------------------------------------------
# DATABASE FAILURES
# ---------------------------------------------------

class TestDatabaseFailures(LoginAuditTestCase):

    def test_no_sign_in_without_its_audit_record(self):
        with patch.object(accounts, "record_login_event",
                          side_effect=sqlite3.OperationalError("disk full")):
            client, response = self.attempt("alice", PASSWORD)

        self.assertEqual(response.status_code, 503)
        self.assert_signed_out(client)

    def test_failed_attempt_still_gets_the_normal_answer(self):
        with patch.object(accounts, "record_login_event",
                          side_effect=sqlite3.OperationalError("disk full")), \
                self.assertLogs(app.logger, logging.ERROR) as logs:
            _, response = self.attempt("alice", WRONG)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Invalid username or password.",
                      response.get_data(as_text=True))
        output = "\n".join(logs.output)
        self.assertIn("OperationalError", output)
        self.assertNotIn(WRONG, output)
        self.assertNotIn("alice", output)


if __name__ == "__main__":
    unittest.main()
