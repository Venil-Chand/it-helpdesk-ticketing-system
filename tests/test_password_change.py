"""
Self-service password change: the current password is required, every
other session ends, this browser stays signed in, and the change is
audited without recording the password.
"""

import re
import unittest
from unittest.mock import patch

import accounts
from app import app, limiter

from account_helpers import (
    NEW_PASSWORD,
    PASSWORD,
    AccountTestCase,
)


class TestChangeOwnPassword(AccountTestCase):

    def change(self, client, current=PASSWORD, new=NEW_PASSWORD,
               confirmation=None):
        return client.post("/account/password", data={
            "current_password": current,
            "new_password": new,
            "confirm_password": new if confirmation is None else confirmation,
        })

    def test_form_is_available_to_every_role(self):
        for username in ("alice", "tech", "root"):
            with self.subTest(username=username):
                response = self.signed_in(username).get("/account/password")
                self.assertEqual(response.status_code, 200)
                self.assertIn('name="current_password"',
                              response.get_data(as_text=True))

    def test_signed_out_visitors_are_sent_to_login(self):
        response = self.new_client().post("/account/password", data={})

        self.assertEqual(response.headers["Location"], "/login")

    def test_success_keeps_this_browser_and_ends_other_sessions(self):
        here = self.signed_in("alice")
        elsewhere = self.signed_in("alice")
        version_before = self.account("alice")[4]

        response = self.change(here)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")
        self.assert_signed_in(here)
        self.assert_signed_out(elsewhere)

        self.assertGreater(self.account("alice")[4], version_before)
        self.assertFalse(self.can_log_in("alice", PASSWORD))
        self.assertTrue(self.can_log_in("alice", NEW_PASSWORD))

    def test_old_cookie_copy_stops_working(self):
        here = self.signed_in("alice")

        with here.session_transaction() as sess:
            stolen = dict(sess)

        self.change(here)

        thief = self.new_client()
        with thief.session_transaction() as sess:
            sess.update(stolen)

        self.assert_signed_out(thief)

    def test_change_is_audited_without_the_password(self):
        self.change(self.signed_in("alice"))

        self.assertEqual(self.audit(), [(
            self.ids["alice"], "alice", "password_changed",
            self.ids["alice"], "alice", None, None
        )])

        for row in self.sql("SELECT * FROM audit_events"):
            for value in row:
                self.assertNotIn(NEW_PASSWORD, str(value))
                self.assertNotIn(PASSWORD, str(value))

    def test_refused_changes_keep_the_password_and_sessions(self):
        cases = {
            "wrong current": {"current": "not-my-password-at-all"},
            "empty current": {"current": ""},
            "oversized current": {"current": "x" * 2000},
            "too short": {"new": "short-pass"},
            "mismatch": {"confirmation": NEW_PASSWORD + "!"},
            "same as old": {"new": PASSWORD},
        }

        client = self.signed_in("alice")
        before = self.account("alice")
        self.addCleanup(limiter.reset)

        for label, form in cases.items():
            with self.subTest(label):
                # More attempts than the rate limit allows in one minute
                limiter.reset()
                response = self.change(client, **form)
                self.assertEqual(response.status_code, 400)

        self.assertEqual(self.account("alice"), before)
        self.assert_signed_in(client)
        self.assertTrue(self.can_log_in("alice", PASSWORD))
        self.assertEqual(self.audit(), [])

    def test_wrong_current_password_message(self):
        html = self.change(self.signed_in("alice"),
                           current="wrong-password-entirely"
                           ).get_data(as_text=True)

        self.assertIn("Current password is incorrect.", html)
        self.assertNotIn(NEW_PASSWORD, html)

    def test_deactivated_account_cannot_change_password(self):
        client = self.signed_in("alice")
        self.sql("UPDATE users SET is_active = 0 WHERE username = 'alice'")

        response = self.change(client)

        self.assertEqual(response.headers["Location"], "/login")
        self.assertFalse(self.can_log_in("alice", NEW_PASSWORD))

    def test_stale_session_version_changes_nothing(self):
        version = self.account("alice")[4]

        with self.assertRaises(accounts.AccountError):
            accounts.change_own_password(self.ids["alice"], version + 1,
                                         PASSWORD, NEW_PASSWORD)

        self.assertTrue(self.can_log_in("alice", PASSWORD))

    def test_concurrent_password_change_is_detected(self):
        # Another change lands between the password check and the UPDATE
        version = self.account("alice")[4]
        real_hash = accounts.hash_password

        def hash_then_race(password):
            self.sql("UPDATE users SET password_hash = 'changed-elsewhere' "
                     "WHERE username = 'alice'")
            return real_hash(password)

        with patch.object(accounts, "hash_password", hash_then_race):
            with self.assertRaises(accounts.AccountError):
                accounts.change_own_password(self.ids["alice"], version,
                                             PASSWORD, NEW_PASSWORD)

        self.assertEqual(
            self.sql("SELECT password_hash FROM users WHERE username = "
                     "'alice'")[0][0],
            "changed-elsewhere"
        )
        self.assertEqual(self.audit(), [])


class TestPasswordChangeRateLimit(AccountTestCase):

    def test_sixth_attempt_in_a_minute_is_refused(self):
        client = self.signed_in("alice")
        limiter.reset()
        self.addCleanup(limiter.reset)

        statuses = [
            client.post("/account/password", data={
                "current_password": "wrong-password-guess",
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            }).status_code
            for _ in range(6)
        ]

        self.assertEqual(statuses, [400] * 5 + [429])

    def test_viewing_the_form_is_not_limited(self):
        client = self.signed_in("alice")
        limiter.reset()
        self.addCleanup(limiter.reset)

        for _ in range(8):
            self.assertEqual(client.get("/account/password").status_code,
                             200)


class TestPasswordChangeCsrf(AccountTestCase):

    def test_post_without_token_is_refused(self):
        client = self.signed_in("alice")
        app.config["WTF_CSRF_ENABLED"] = True

        response = client.post("/account/password", data={
            "current_password": PASSWORD,
            "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        })

        self.assertEqual(response.status_code, 400)

        app.config["WTF_CSRF_ENABLED"] = False
        self.assertTrue(self.can_log_in("alice", PASSWORD))

    def test_form_contains_a_token(self):
        client = self.signed_in("alice")
        app.config["WTF_CSRF_ENABLED"] = True

        html = client.get("/account/password").get_data(as_text=True)

        self.assertRegex(html, re.compile(r'name="csrf_token" value="[^"]+"'))


if __name__ == "__main__":
    unittest.main()
