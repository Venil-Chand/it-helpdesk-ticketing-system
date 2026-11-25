"""
Audit retention, step 1: archive and verify (audit_archive.py).

Events older than the retention period (provisionally 12 months) are
copied to a private JSON Lines archive with a checksummed manifest. The
database keeps every event and stays append-only; no deletion or purge
exists. Temporary databases and directories only.
"""

import json
import os
import sqlite3
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import accounts
import audit_archive
from app import app

from account_helpers import AccountTestCase

CUTOFF = "2026-01-01 00:00:00"


class ArchiveTestCase(AccountTestCase):

    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = temp.name
        self.archive = os.path.join(self.dir, "audit-2025.jsonl")

        # Two old events (one with every optional column) and one recent
        for created_at, values in (
            ("2025-03-01 08:00:00", dict(
                actor_user_id=self.ids["root"], actor="root",
                action="role_changed", target_user_id=self.ids["alice"],
                target_username="alice", old_value="user",
                new_value="technician", source_address=None)),
            ("2025-06-15 12:30:00", dict(
                actor_user_id=None, actor="anonymous",
                action="login_failed", target_user_id=self.ids["bob"],
                target_username="bob", old_value=None,
                new_value="wrong password", source_address="10.0.0.7")),
            ("2026-05-01 09:00:00", dict(
                actor_user_id=self.ids["root"], actor="root",
                action="password_reset", target_user_id=self.ids["bob"],
                target_username="bob", old_value=None, new_value=None,
                source_address=None)),
        ):
            columns = ", ".join(["created_at", *values])
            marks = ", ".join("?" for _ in range(len(values) + 1))
            self.sql(f"INSERT INTO audit_events ({columns}) VALUES ({marks})",
                     (created_at, *values.values()))

        self.events_before = self.sql("SELECT * FROM audit_events ORDER BY id")

    def export(self, **kwargs):
        return audit_archive.export_archive(
            self.archive, before=kwargs.pop("before", CUTOFF),
            operator="opsadmin", **kwargs
        )


# ---------------------------------------------------
# EXPORT
# ---------------------------------------------------

class TestExport(ArchiveTestCase):

    def test_old_events_are_copied_and_nothing_is_deleted(self):
        result = self.export()

        self.assertEqual(result.count, 2)
        with open(self.archive, encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle]
        self.assertEqual([e["created_at"] for e in lines],
                         ["2025-03-01 08:00:00", "2025-06-15 12:30:00"])
        self.assertEqual(lines[1]["source_address"], "10.0.0.7")

        # Every original event is still there, unchanged
        after = self.sql("SELECT * FROM audit_events ORDER BY id")
        self.assertEqual(after[:len(self.events_before)], self.events_before)

    def test_archiving_is_itself_audited(self):
        result = self.export()

        event = self.sql(
            "SELECT actor, action, new_value FROM audit_events "
            "ORDER BY id DESC LIMIT 1"
        )[0]
        self.assertEqual(event[:2], ("cli:opsadmin", "audit_archived"))
        self.assertIn(result.sha256, event[2])
        self.assertIn(f"{result.count} events before {CUTOFF}", event[2])

    def test_manifest_describes_the_archive(self):
        result = self.export()

        with open(result.manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)

        self.assertEqual(manifest["format"], audit_archive.ARCHIVE_FORMAT)
        self.assertEqual(manifest["count"], 2)
        self.assertEqual(manifest["sha256"], result.sha256)
        self.assertEqual(manifest["events_before"], CUTOFF)
        self.assertEqual(manifest["created_by"], "cli:opsadmin")
        self.assertEqual(manifest["columns"], list(audit_archive.COLUMNS))

    def test_files_are_private_to_their_owner(self):
        result = self.export()

        for path in (result.path, result.manifest_path):
            with self.subTest(path=os.path.basename(path)):
                mode = stat.S_IMODE(os.stat(path).st_mode)
                self.assertEqual(mode, 0o600)

    def test_existing_files_are_never_overwritten(self):
        with open(self.archive, "w") as handle:
            handle.write("keep me")

        with self.assertRaises(audit_archive.ArchiveError):
            self.export()

        with open(self.archive) as handle:
            self.assertEqual(handle.read(), "keep me")
        self.assertEqual(
            self.sql("SELECT * FROM audit_events ORDER BY id"),
            self.events_before
        )

    def test_nothing_to_archive_writes_nothing(self):
        self.assertIsNone(self.export(before="2000-01-01 00:00:00"))

        self.assertFalse(os.path.exists(self.archive))
        self.assertEqual(
            self.sql("SELECT * FROM audit_events ORDER BY id"),
            self.events_before
        )

    def test_failure_removes_partial_files_and_records_nothing(self):
        with patch.object(accounts, "record_audit_event",
                          side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.export()

        self.assertEqual(os.listdir(self.dir), [])
        self.assertEqual(
            self.sql("SELECT * FROM audit_events ORDER BY id"),
            self.events_before
        )

    def test_audit_events_remain_append_only(self):
        self.export()

        for statement in ("UPDATE audit_events SET actor = 'x'",
                          "DELETE FROM audit_events"):
            with self.subTest(statement=statement):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.sql(statement)


# ---------------------------------------------------
# VERIFICATION
# ---------------------------------------------------

class TestVerify(ArchiveTestCase):

    def setUp(self):
        super().setUp()
        self.result = self.export()

    def rewrite(self, path, change):
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(change(text))

    def test_untouched_archive_verifies(self):
        result = audit_archive.verify_archive(self.archive)

        self.assertTrue(result.ok, result.problems)
        self.assertEqual((result.count, result.in_database), (2, 2))

    def test_edited_archive_fails_its_checksum(self):
        self.rewrite(self.archive,
                     lambda text: text.replace("wrong password", "ok"))

        result = audit_archive.verify_archive(self.archive)

        self.assertFalse(result.ok)
        self.assertIn("archive checksum does not match manifest",
                      result.problems)

    def test_edit_with_recomputed_checksum_is_caught_by_the_database(self):
        import hashlib

        self.rewrite(self.archive,
                     lambda text: text.replace('"bob"', '"mallory"'))
        with open(self.archive, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        self.rewrite(self.result.manifest_path,
                     lambda text: text.replace(self.result.sha256, digest))

        result = audit_archive.verify_archive(self.archive)

        self.assertFalse(result.ok)
        self.assertTrue(any("differs from the database" in problem
                            for problem in result.problems))

    def test_removed_event_is_detected(self):
        self.rewrite(self.archive,
                     lambda text: text.splitlines(True)[1])

        problems = audit_archive.verify_archive(self.archive).problems

        self.assertIn("event count does not match manifest", problems)

    def test_malformed_lines_are_reported_not_crashed_on(self):
        with open(self.archive, encoding="utf-8") as handle:
            original = handle.read()

        for injected in ("{}", "[1, 2]", '"text"', '{"id": "7"}'):
            with self.subTest(injected=injected):
                self.rewrite(self.archive,
                             lambda text: text + injected + "\n")

                result = audit_archive.verify_archive(self.archive)

                self.assertFalse(result.ok)
                self.assertIn("archive contains malformed events",
                              result.problems)
                self.rewrite(self.archive, lambda _text: original)

    def test_missing_manifest_is_reported(self):
        os.remove(self.result.manifest_path)

        result = audit_archive.verify_archive(self.archive)

        self.assertFalse(result.ok)
        self.assertTrue(result.problems[0].startswith("cannot read archive"))

    def test_verification_never_writes_to_the_database(self):
        before = self.sql("SELECT * FROM audit_events ORDER BY id")

        audit_archive.verify_archive(self.archive)

        self.assertEqual(self.sql("SELECT * FROM audit_events ORDER BY id"),
                         before)


# ---------------------------------------------------
# RETENTION PERIOD
# ---------------------------------------------------

class TestRetentionPeriod(unittest.TestCase):

    def test_default_cutoff_is_twelve_months(self):
        now = datetime(2026, 11, 25, 6, 0, 0, tzinfo=timezone.utc)

        self.assertEqual(audit_archive.RETENTION_DAYS, 365)
        self.assertEqual(audit_archive.default_cutoff(now),
                         "2025-11-25 06:00:00")

    def test_cutoff_parsing(self):
        self.assertEqual(audit_archive.parse_cutoff("2025-01-31"),
                         "2025-01-31 00:00:00")
        self.assertEqual(audit_archive.parse_cutoff("2025-01-31 13:45:00"),
                         "2025-01-31 13:45:00")

        for bad in ("31/01/2025", "2025-13-01", "yesterday", "' OR 1=1"):
            with self.subTest(bad=bad):
                with self.assertRaises(audit_archive.ArchiveError):
                    audit_archive.parse_cutoff(bad)


# ---------------------------------------------------
# COMMAND LINE
# ---------------------------------------------------

class TestArchiveCommands(ArchiveTestCase):

    def setUp(self):
        super().setUp()
        self.runner = app.test_cli_runner()
        patcher = patch("helpdesk.cli._operator", return_value="opsadmin")
        patcher.start()
        self.addCleanup(patcher.stop)

    def invoke(self, *args):
        return self.runner.invoke(args=list(args))

    def test_dry_run_only_counts(self):
        result = self.invoke("audit-archive", "--before", "2026-01-01",
                             "--dry-run")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(": 2", result.output)
        self.assertEqual(os.listdir(self.dir), [])
        self.assertEqual(
            self.sql("SELECT * FROM audit_events ORDER BY id"),
            self.events_before
        )

    def test_archive_then_verify(self):
        archived = self.invoke("audit-archive", "--before", "2026-01-01",
                               "--to", self.archive)
        self.assertEqual(archived.exit_code, 0, archived.output)
        self.assertIn("nothing was deleted", archived.output)

        verified = self.invoke("audit-verify-archive", self.archive)
        self.assertEqual(verified.exit_code, 0, verified.output)
        self.assertIn("Archive verified.", verified.output)

    def test_verify_fails_with_exit_code_on_tampering(self):
        self.invoke("audit-archive", "--before", "2026-01-01",
                    "--to", self.archive)
        with open(self.archive, "a", encoding="utf-8") as handle:
            handle.write("{}\n")

        result = self.invoke("audit-verify-archive", self.archive)

        self.assertEqual(result.exit_code, 1)
        self.assertIn("Problem:", result.output)

    def test_destination_is_required_without_dry_run(self):
        result = self.invoke("audit-archive", "--before", "2026-01-01")

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(os.listdir(self.dir), [])

    def test_invalid_date_is_refused(self):
        result = self.invoke("audit-archive", "--before", "last year",
                             "--dry-run")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("YYYY-MM-DD", result.output)

    def test_no_deletion_command_exists(self):
        commands = set(app.cli.list_commands(None))

        self.assertFalse({name for name in commands
                          if "purge" in name or "delete" in name})


if __name__ == "__main__":
    unittest.main()
