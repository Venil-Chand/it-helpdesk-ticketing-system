"""
Fast checks of the rehearsal harness itself (no servers, a few seconds):

    python -m unittest discover -s rehearsals/tests -t .

They prove the safety guard refuses anything that could reach the real
helpdesk.db or .env, that private files are never copied, that secrets
are redacted, and that every mutant in rehearsals/mutants.py still
matches the code (so a refactoring cannot silently make the mutation
rehearsal test nothing).
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rehearsals import common
from rehearsals.common import (REPO_ROOT, UnsafeRehearsal, Workspace,
                               copy_source, guard_environment,
                               is_private_path, source_files)
from rehearsals.mutants import MUTANTS


class PrivateFileTests(unittest.TestCase):

    def test_private_paths(self):
        for path in (".env", ".env.local", "helpdesk.db", "x/helpdesk.db-wal",
                     "helpdesk_backup_20261029_184425.db", "keys/backup.key",
                     "backups/helpdesk-20261125T093000Z.hdbk",
                     "instance/state/x.json", "credentials/secret_key",
                     ".git/config", ".venv/bin/python"):
            with self.subTest(path=path):
                self.assertTrue(is_private_path(path))

    def test_ordinary_paths(self):
        for path in (".env.example", "deploy/helpdesk.env.example",
                     "database.py", "docs/BACKUP_AND_RESTORE.md",
                     "rehearsals/common.py"):
            with self.subTest(path=path):
                self.assertFalse(is_private_path(path))

    def test_repository_listing_has_no_private_files(self):
        listed = source_files()
        self.assertIn("database.py", listed)
        self.assertIn("rehearsals/common.py", listed)
        self.assertEqual([p for p in listed if is_private_path(p)], [])
        self.assertNotIn(".env", listed)
        self.assertNotIn("helpdesk.db", listed)

    def test_copy_without_git_skips_private_files(self):
        with tempfile.TemporaryDirectory() as root, \
                tempfile.TemporaryDirectory() as out:
            root = Path(root)
            (root / "app.py").write_text("print('hi')\n")
            (root / ".env").write_text("SECRET_KEY=real\n")
            (root / "helpdesk.db").write_bytes(b"SQLite format 3\0")
            with patch.object(common.subprocess, "run",
                              side_effect=OSError("no git")):
                copy_source(out, root=root)
            self.assertEqual(sorted(os.listdir(out)), ["app.py"])


class GuardTests(unittest.TestCase):

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="guard-test-"))
        self.addCleanup(os.rmdir, self.workspace)
        self.env = {
            "APP_ENV": "testing",
            "FLASK_SKIP_DOTENV": "1",
            "HELPDESK_DATABASE_PATH": str(self.workspace / "helpdesk.db"),
            "HOME": str(self.workspace),
        }

    def refused(self, **changes):
        env = {**self.env, **changes}
        env = {k: v for k, v in env.items() if v is not None}
        with self.assertRaises(UnsafeRehearsal):
            guard_environment(env, self.workspace)

    def test_accepts_a_workspace_environment(self):
        guard_environment(self.env, self.workspace)
        guard_environment({**self.env, "APP_ENV": "production"},
                          self.workspace)

    def test_refuses_the_default_database(self):
        self.refused(HELPDESK_DATABASE_PATH=None)
        self.refused(HELPDESK_DATABASE_PATH="")

    def test_refuses_the_real_database_and_env_file(self):
        self.refused(HELPDESK_DATABASE_PATH=str(REPO_ROOT / "helpdesk.db"))
        self.refused(SECRET_KEY_FILE=str(REPO_ROOT / ".env"))

    def test_refuses_any_path_outside_the_workspace(self):
        for name in common.PATH_VARIABLES:
            with self.subTest(name=name):
                self.refused(**{name: "/var/lib/helpdesk/x"})

    def test_refuses_a_traversal_out_of_the_workspace(self):
        self.refused(HELPDESK_DATABASE_PATH=str(
            self.workspace / ".." / "helpdesk.db"))

    def test_refuses_environments_that_read_dotenv(self):
        self.refused(APP_ENV="development")
        self.refused(APP_ENV=None)
        self.refused(FLASK_SKIP_DOTENV=None)
        self.refused(FLASK_SKIP_DOTENV="0")

    def test_refuses_a_workspace_inside_the_repository(self):
        with self.assertRaises(UnsafeRehearsal):
            guard_environment(
                {**self.env, "HELPDESK_DATABASE_PATH":
                 str(REPO_ROOT / "tmp" / "x.db"), "HOME": str(REPO_ROOT)},
                REPO_ROOT / "tmp")


class WorkspaceTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.ws = Workspace("rehearsal-selftest")

    @classmethod
    def tearDownClass(cls):
        cls.ws.close()

    def test_copy_has_code_and_no_private_files(self):
        self.assertTrue((self.ws.src / "wsgi.py").is_file())
        self.assertFalse((self.ws.src / ".env").exists())
        self.assertFalse((self.ws.src / "helpdesk.db").exists())
        self.assertEqual(oct(self.ws.root.stat().st_mode & 0o777), "0o700")

    def test_environment_inherits_no_secrets_from_the_shell(self):
        shell = {"SECRET_KEY": "from-the-shell",
                 "RATELIMIT_STORAGE_URI": "redis://:pw@prod:6379/0",
                 "HELPDESK_DATABASE_PATH": str(REPO_ROOT / "helpdesk.db"),
                 "PATH": os.environ.get("PATH", "")}
        with patch.dict(os.environ, shell, clear=True):
            env = self.ws.env()
        self.assertNotEqual(env["SECRET_KEY"], "from-the-shell")
        self.assertNotIn("RATELIMIT_STORAGE_URI", env)
        self.assertEqual(env["HELPDESK_DATABASE_PATH"], str(self.ws.db_path))
        guard_environment(env, self.ws.root)

    def test_secret_keys_are_random_per_workspace(self):
        other = Workspace("rehearsal-selftest")
        self.addCleanup(other.close)
        self.assertNotEqual(other.secret_key, self.ws.secret_key)
        self.assertEqual(len(self.ws.secret_key), 64)

    def test_redaction(self):
        secret = self.ws.sensitive("a-very-private-value")
        text = f"key={self.ws.secret_key} pw={secret}"
        cleaned = self.ws.redact(text)
        self.assertNotIn(secret, cleaned)
        self.assertNotIn(self.ws.secret_key, cleaned)
        self.assertEqual(len(self.ws.leaks(text)), 2)
        self.assertEqual(self.ws.leaks(cleaned), [])

    def test_unsafe_commands_are_not_started(self):
        env = {**self.ws.env(), "HELPDESK_DATABASE_PATH":
               str(REPO_ROOT / "helpdesk.db")}
        with patch.object(common.subprocess, "run") as run, \
                patch.object(common.subprocess, "Popen") as popen:
            with self.assertRaises(UnsafeRehearsal):
                self.ws.flask("db-status", env=env)
            with self.assertRaises(UnsafeRehearsal):
                self.ws.popen(["true"], env, "x.log")
        run.assert_not_called()
        popen.assert_not_called()


class MutantTests(unittest.TestCase):

    def test_every_mutant_matches_the_current_code(self):
        for mutant in MUTANTS:
            with self.subTest(mutant=mutant.name):
                text = (REPO_ROOT / mutant.path).read_text()
                self.assertEqual(text.count(mutant.original), mutant.count,
                                 "update rehearsals/mutants.py")
                self.assertNotEqual(mutant.original, mutant.replacement)
                self.assertTrue(mutant.unit or mutant.browser)

    def test_named_tests_exist(self):
        browser = (REPO_ROOT / "e2e" / "test_browser_workflows.py").read_text()
        for mutant in MUTANTS:
            for module in mutant.unit:
                with self.subTest(module=module):
                    self.assertTrue((REPO_ROOT / "tests" / module).is_file())
            for test in mutant.browser:
                with self.subTest(test=test):
                    self.assertIn(f"def {test.rsplit('.', 1)[1]}(", browser)


if __name__ == "__main__":
    unittest.main()
