"""
Password policy for version 1.0 (Package 1b).

The agreed rules: at least 16 characters (kept from setup_accounts.py),
at most 1,024, any characters including spaces, typed twice, and not a
common or breached password, the username or a simple pattern. No
character-class rules and no expiry. The blocklist check is offline.

All checks run against temporary databases; the real helpdesk.db is
never opened.
"""

import ast
import gzip
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import accounts
import setup_accounts
import validation

from account_helpers import (
    PASSWORD,
    AccountTestCase,
)

PROJECT = Path(__file__).resolve().parent.parent

# Published in security_data/README.md; the bundled file must match it
DOCUMENTED_BLOCKLIST_SHA256 = (
    "fbf9baa7f264c2414f35949a9eee29316ac56167668f364d3e910f5593c904ff"
)

GOOD = "quiet-orbit-lantern-58"


def problem(password, username=None, confirmation=None):
    return validation.new_password_problem(
        password,
        password if confirmation is None else confirmation,
        username=username
    )


def a_blocked_password():
    """A real entry from the bundled list (the first, for determinism)."""

    return min(validation.load_blocklist())


# ---------------------------------------------------
# LENGTH AND CHARACTERS
# ---------------------------------------------------

class TestLengthRules(unittest.TestCase):

    def test_minimum_is_sixteen_characters(self):
        self.assertEqual(validation.MIN_PASSWORD_LENGTH, 16)
        self.assertIsNotNone(problem("mauve-kettle-71"))       # 15
        self.assertIsNone(problem("mauve-kettle-71x"))         # 16

    def test_at_least_sixty_four_characters_are_allowed(self):
        long_password = ("Tq8#vL2m!pW9zR4k" * 4)[:64]
        self.assertEqual(len(long_password), 64)
        self.assertIsNone(problem(long_password))

    def test_maximum_is_bounded_for_safe_hashing(self):
        self.assertEqual(validation.MAX_PASSWORD_LENGTH, 1024)

        # A pass phrase made of different words, so no pattern rule fires
        words = [f"word{n:04d}" for n in range(200)]
        at_limit = " ".join(words)[:1024]
        self.assertEqual(len(at_limit), 1024)
        self.assertIsNone(problem(at_limit))
        self.assertIsNotNone(problem(at_limit + "z"))

    def test_length_counts_characters_not_bytes(self):
        # 16 characters, 48 bytes in UTF-8
        cjk = "密码安全长句子心山水" \
              "风雨日月星辰"
        self.assertEqual(len(cjk), 16)
        self.assertIsNone(problem(cjk))

    def test_spaces_and_any_characters_are_accepted(self):
        for password in (
            "correct horse battery stapler",
            "émigré café naïve façade",
            "😀 sunlit harbour at dawn 🌊",
            "Tq8#vL2m!pW9zR4k~^[]{}",
        ):
            with self.subTest(password=password):
                self.assertIsNone(problem(password))

    def test_no_character_class_rules(self):
        # Lower-case letters only, no digit or symbol: acceptable
        self.assertIsNone(problem("plumquietharbourmeadow"))

    def test_confirmation_must_match(self):
        self.assertEqual(
            problem(GOOD, confirmation=GOOD + "!"), "Passwords do not match."
        )


# ---------------------------------------------------
# COMMON AND BREACHED PASSWORDS
# ---------------------------------------------------

class TestBlocklist(unittest.TestCase):

    def test_listed_password_is_refused(self):
        blocked = a_blocked_password()
        self.assertGreaterEqual(len(blocked), 16)
        self.assertIn("common or breached", problem(blocked))

    def test_matching_ignores_letter_case(self):
        blocked = a_blocked_password()
        self.assertIsNotNone(problem(blocked.upper()))

    def test_well_known_breached_long_password_is_refused(self):
        # Long enough for the length rule, and in breach lists
        found = [
            candidate for candidate in (
                "passwordpassword", "1qaz2wsx3edc4rfv", "qwertyuiopasdfgh",
                "iloveyouiloveyou",
            )
            if validation.normalize_for_blocklist(candidate)
            in validation.load_blocklist()
        ]
        self.assertTrue(found)
        for password in found:
            with self.subTest(password=password):
                self.assertIsNotNone(problem(password))

    def test_bundled_file_matches_documented_checksum(self):
        data = validation.BLOCKLIST_PATH.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         DOCUMENTED_BLOCKLIST_SHA256)

    def test_entries_are_normalised_and_within_the_length_limits(self):
        with gzip.open(validation.BLOCKLIST_PATH, "rt",
                       encoding="utf-8") as data:
            lines = data.read().split("\n")

        self.assertEqual(lines[-1], "")
        entries = lines[:-1]
        self.assertEqual(entries, sorted(set(entries)))
        self.assertGreater(len(entries), 50_000)

        for entry in entries:
            self.assertEqual(entry, validation.normalize_for_blocklist(entry))
            self.assertGreaterEqual(len(entry), validation.MIN_PASSWORD_LENGTH)
            self.assertLessEqual(len(entry), validation.MAX_PASSWORD_LENGTH)

    def test_missing_blocklist_fails_closed(self):
        # An installation without the file must not accept every password
        validation.load_blocklist.cache_clear()
        self.addCleanup(validation.load_blocklist.cache_clear)

        with patch.object(validation, "BLOCKLIST_PATH",
                          Path("/nonexistent/common_passwords.txt.gz")):
            with self.assertRaises(OSError):
                problem(GOOD)

    def test_check_is_offline(self):
        # validation.py must not be able to send a password anywhere
        tree = ast.parse((PROJECT / "validation.py").read_text())
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in (node.names if isinstance(node, ast.Import)
                          else [ast.alias(node.module or "")])
        }

        for module in ("socket", "urllib", "http", "requests", "ssl"):
            self.assertNotIn(module, imported)


class TestBlocklistBuilder(unittest.TestCase):

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "build_password_blocklist",
            PROJECT / "scripts" / "build_password_blocklist.py"
        )
        self.builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.builder)

    def test_build_is_deterministic_and_filters_entries(self):
        source = self.dir / "source.txt"
        source.write_bytes(
            b"short\n"
            b"LongEnoughPassword1\r\n"
            b"longenoughpassword1\n"
            b"\xff\xfe not utf-8 at all here\n"
            b"another-long-entry-xyz\n"
            + b"x" * 2000 + b"\n"
        )

        first, second = self.dir / "a.gz", self.dir / "b.gz"
        self.builder.build([source], output=first)
        self.builder.build([source], output=second)

        self.assertEqual(first.read_bytes(), second.read_bytes())

        with gzip.open(first, "rt", encoding="utf-8") as data:
            self.assertEqual(
                data.read(),
                "another-long-entry-xyz\nlongenoughpassword1\n"
            )


# ---------------------------------------------------
# USERNAME AND PATTERNS
# ---------------------------------------------------

class TestUsernameAndPatterns(unittest.TestCase):

    def test_password_containing_the_username_is_refused(self):
        self.assertEqual(
            problem("hello-Margaret-2026!", username="margaret"),
            "Password must not contain the username."
        )

    def test_very_short_usernames_are_not_searched_for(self):
        # "bob" occurs in many ordinary words
        self.assertIsNone(problem("kebob-harbour-lantern", username="bob"))

    def test_repeated_blocks_are_refused(self):
        # Some of these are also in the breach list; refused either way
        for password in ("a" * 20, "ab" * 10, "xyz" * 6, "abcd" * 5):
            with self.subTest(password=password):
                self.assertIsNotNone(problem(password))

    def test_pattern_rule_catches_what_the_list_does_not(self):
        password = "Zq7%" * 5
        self.assertNotIn(validation.normalize_for_blocklist(password),
                         validation.load_blocklist())
        self.assertEqual(
            problem(password),
            "Password must not be a repeated or sequential pattern."
        )

    def test_sequences_are_refused(self):
        for password in ("abcdefghijklmnopq", "9876543210zyxwvu"[:10] * 2,
                         "qponmlkjihgfedcba"):
            with self.subTest(password=password):
                self.assertIsNotNone(problem(password))

    def test_longer_repeated_block_is_allowed(self):
        # Five characters repeated is beyond MAX_REPEATED_BLOCK
        self.assertIsNone(problem("tulip" * 4))


# ---------------------------------------------------
# EVERY WAY OF SETTING A PASSWORD USES THE POLICY
# ---------------------------------------------------

class TestPolicyIsAppliedEverywhere(AccountTestCase):

    def test_admin_cannot_create_account_with_blocked_password(self):
        blocked = a_blocked_password()
        response = self.signed_in("root").post("/admin/users/new", data={
            "username": "carol", "role": "user",
            "password": blocked, "confirm_password": blocked,
        })

        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.account("carol"))
        self.assertNotIn(blocked, response.get_data(as_text=True))

    def test_admin_reset_refuses_password_with_username(self):
        with self.assertRaisesRegex(accounts.AccountError, "username"):
            accounts.reset_password(self.ids["root"], self.ids["alice"],
                                    "alice-spring-garden-2026")

        self.assertTrue(self.can_log_in("alice", PASSWORD))

    def test_own_change_refuses_blocked_password(self):
        blocked = a_blocked_password()
        client = self.signed_in("alice")

        response = client.post("/account/password", data={
            "current_password": PASSWORD,
            "new_password": blocked, "confirm_password": blocked,
        })

        self.assertEqual(response.status_code, 400)
        self.assertTrue(self.can_log_in("alice", PASSWORD))

    def test_setup_script_refuses_blocked_password(self):
        self.sql("DELETE FROM users")
        blocked = a_blocked_password()

        with patch("setup_accounts.getpass", return_value=blocked), \
                patch("builtins.print") as printed:
            setup_accounts.setup_account()

        self.assertEqual(self.sql("SELECT COUNT(*) FROM users"), [(0,)])
        self.assertIn("common or breached",
                      " ".join(str(c) for c in printed.call_args_list))

    def test_policy_messages_never_echo_the_password(self):
        for password in (a_blocked_password(), "abcd" * 5,
                         "alice-spring-garden-2026"):
            with self.subTest(password=password):
                message = problem(password, username="alice") or ""
                self.assertNotIn(password, message)


if __name__ == "__main__":
    unittest.main()
