"""
flask --app app reset-password USERNAME: recovery of lost credentials.

The command is authorised by operating-system access to the server. It
only reads the password from an interactive hidden prompt, ends every
session of the account, forces a new password at the next sign-in,
records an audit event atomically with the change, and refuses
deactivated accounts.

Temporary databases only.
"""

import logging
import os
import pwd
import sqlite3
import unittest
from unittest.mock import patch

import accounts
from app import app

from account_helpers import (
    CHANGE_PASSWORD_URL,
    PASSWORD,
    AccountTestCase,
)

TEMPORARY = "temporary-recovery-phrase-91"
OWN = "chosen-by-owner-harbour-77"


class CaptureAllLogs(logging.Handler):
    """Collect every log record from every logger."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class ResetPasswordTestCase(AccountTestCase):

    def setUp(self):
        super().setUp()
        self.runner = app.test_cli_runner()

        for target, value in (("helpdesk.cli._interactive", True),
                              ("helpdesk.cli._operator", "opsadmin")):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.logs = CaptureAllLogs()
        root = logging.getLogger()
        old_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.logs)
        self.addCleanup(root.removeHandler, self.logs)
        self.addCleanup(root.setLevel, old_level)

    def run_reset(self, username, typed=(TEMPORARY, TEMPORARY), args=()):
        return self.runner.invoke(
            args=["reset-password", username, *args],
            input="".join(f"{line}\n" for line in typed)
        )

    def state(self, username):
        return self.sql(
            "SELECT password_hash, session_version, must_change_password, "
            "is_active FROM users WHERE username = ?", (username,)
        )[0]

    def assert_never_revealed(self, result, *secrets):
        everything = result.output + "\n".join(self.logs.messages) + " ".join(
            str(value) for row in self.sql("SELECT * FROM audit_events")
            for value in row
        )
        for secret in secrets:
            self.assertNotIn(secret, everything)


# ---------------------------------------------------
# SUCCESSFUL RESET
# ---------------------------------------------------

class TestSuccessfulReset(ResetPasswordTestCase):

    def test_reset_ends_sessions_forces_change_and_is_audited(self):
        existing = self.signed_in("alice")
        before = self.state("alice")

        result = self.run_reset("alice")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("reset", result.output)

        after = self.state("alice")
        self.assertNotEqual(after[0], before[0])
        self.assertGreater(after[1], before[1])
        self.assertEqual(after[2:], (1, 1))

        self.assert_signed_out(existing)
        self.assertFalse(self.can_log_in("alice", PASSWORD))
        self.must_change_password_on_login("alice", TEMPORARY)

        self.assertEqual(self.audit(), [(
            None, "cli:opsadmin", "password_reset", self.ids["alice"],
            "alice", None, None
        )])
        self.assert_never_revealed(result, TEMPORARY)

    def test_password_is_typed_at_a_hidden_prompt_twice(self):
        result = self.run_reset("alice")

        self.assertIn("New password:", result.output)
        self.assertIn("Repeat new password:", result.output)
        # Hidden input is not echoed back
        self.assertNotIn(TEMPORARY, result.output)

    def test_lost_last_administrator_is_recoverable(self):
        # root is the only administrator and its password is forgotten
        self.assertEqual(
            self.sql("SELECT COUNT(*) FROM users WHERE role = 'admin' "
                     "AND is_active = 1"), [(1,)]
        )
        self.assertFalse(self.can_log_in("root", "whatever-root-was-set-to"))

        result = self.run_reset("root")
        self.assertEqual(result.exit_code, 0, result.output)

        client = self.must_change_password_on_login("root", TEMPORARY)
        changed = client.post(CHANGE_PASSWORD_URL, data={
            "current_password": TEMPORARY,
            "new_password": OWN, "confirm_password": OWN,
        })
        self.assertEqual(changed.headers["Location"], "/")

        self.assertEqual(client.get("/admin/users").status_code, 200)
        self.assertEqual(self.state("root")[2], 0)
        self.assertEqual(self.state("root")[3], 1)
        # The role was not changed by the recovery
        self.assertEqual(self.account("root")[2], "admin")


# ---------------------------------------------------
# REFUSALS: NOTHING CHANGES
# ---------------------------------------------------

class TestRefusals(ResetPasswordTestCase):

    def assert_unchanged(self, username, before):
        self.assertEqual(self.state(username), before)
        self.assertEqual(self.audit(), [])

    def test_password_cannot_be_given_as_an_argument_or_option(self):
        before = self.state("alice")

        for args in (["--password", TEMPORARY], [TEMPORARY]):
            with self.subTest(args=args):
                result = self.run_reset("alice", args=args)
                self.assertEqual(result.exit_code, 2)

        self.assert_unchanged("alice", before)

    def test_non_interactive_input_is_refused_before_prompting(self):
        before = self.state("alice")

        with patch("helpdesk.cli._interactive", return_value=False):
            result = self.run_reset("alice")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("interactive terminal", result.output)
        self.assertNotIn("New password", result.output)
        self.assert_unchanged("alice", before)

    def test_deactivated_account_is_refused_before_prompting(self):
        self.sql("UPDATE users SET is_active = 0 WHERE username = 'bob'")
        before = self.state("bob")

        result = self.run_reset("bob")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("deactivated", result.output)
        self.assertNotIn("New password", result.output)
        self.assert_unchanged("bob", before)
        self.assertFalse(self.can_log_in("bob", TEMPORARY))

    def test_deactivated_account_is_refused_by_the_reset_itself(self):
        # Not only by the command's early check: the function refuses too,
        # with a message telling the operator what to do instead
        self.sql("UPDATE users SET is_active = 0 WHERE username = 'bob'")
        before = self.state("bob")

        with self.assertRaisesRegex(accounts.AccountError,
                                    "deactivated.*reactivate it first"):
            accounts.operator_reset_password("bob", TEMPORARY,
                                             operator="opsadmin")

        self.assert_unchanged("bob", before)

    def test_unknown_username_is_refused(self):
        result = self.run_reset("no-such-account")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("no account has that exact username", result.output)
        self.assertEqual(self.audit(), [])

    def test_username_must_match_exactly(self):
        result = self.run_reset("ALICE")

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.audit(), [])

    def test_weak_password_is_refused(self):
        before = self.state("alice")
        weak = "alice-alice-alice-01"

        result = self.run_reset("alice", typed=(weak, weak))

        self.assertEqual(result.exit_code, 1)
        self.assertIn("Nothing was changed", result.output)
        self.assert_unchanged("alice", before)
        self.assert_never_revealed(result, weak)

    def test_mismatched_confirmation_changes_nothing(self):
        before = self.state("alice")

        result = self.run_reset("alice", typed=(TEMPORARY, TEMPORARY + "x"))

        self.assertNotEqual(result.exit_code, 0)
        self.assert_unchanged("alice", before)

    def test_outdated_schema_is_refused(self):
        self.sql("PRAGMA user_version = 8")
        before = self.state("alice")

        result = self.run_reset("alice")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("init-db", result.output)
        self.assert_unchanged("alice", before)


# ---------------------------------------------------
# WHO RAN THE COMMAND
# ---------------------------------------------------

class TestOperatorName(unittest.TestCase):

    def operator(self, **environment):
        from helpdesk import cli

        with patch.dict(os.environ, environment, clear=False):
            return cli._operator()

    def real_name(self):
        return pwd.getpwuid(os.getuid()).pw_name

    def test_environment_cannot_change_the_recorded_account(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SUDO_USER", None)
            name = self.operator(USER="someone-else",
                                 LOGNAME="someone-else")

        self.assertEqual(name, self.real_name())

    def test_sudo_invoker_is_added(self):
        name = self.operator(SUDO_USER="maria")

        self.assertEqual(name, f"{self.real_name()} (sudo by maria)")

    def test_name_is_bounded(self):
        self.assertLessEqual(len(self.operator(SUDO_USER="m" * 500)), 64)


# ---------------------------------------------------
# ATOMICITY AND RACES
# ---------------------------------------------------

class TestAtomicity(ResetPasswordTestCase):

    def test_audit_failure_rolls_back_the_reset(self):
        existing = self.signed_in("alice")
        before = self.state("alice")

        with patch.object(accounts, "record_audit_event",
                          side_effect=sqlite3.OperationalError("disk full")):
            result = self.run_reset("alice")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("nothing was changed", result.output)
        self.assertEqual(self.state("alice"), before)
        self.assert_signed_in(existing)
        self.assertTrue(self.can_log_in("alice", PASSWORD))

    def test_deactivation_during_the_reset_is_detected(self):
        before = dict(self.sql(
            "SELECT username, password_hash FROM users"
        ))
        stale = {"id": self.ids["bob"], "username": "bob",
                 "role": "user", "is_active": 1}
        self.sql("UPDATE users SET is_active = 0 WHERE username = 'bob'")

        # The account looked active when first read, then was deactivated
        with patch.object(accounts, "get_account_by_username",
                          return_value=stale):
            with self.assertRaisesRegex(accounts.AccountError,
                                        "changed while"):
                accounts.operator_reset_password("bob", TEMPORARY,
                                                 operator="opsadmin")

        self.assertEqual(
            dict(self.sql("SELECT username, password_hash FROM users")),
            before
        )
        self.assertEqual(self.audit(), [])


if __name__ == "__main__":
    unittest.main()
