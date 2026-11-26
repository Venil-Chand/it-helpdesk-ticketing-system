"""
Encrypted SQLite backups, verification, off-server copies, restoration
into disposable databases, the restoration drill, retention, and the
guarded live restore.

Every database here is a temporary file. The "live" database is a
temporary file too (database.DB_PATH is patched), and each test checks
that the real helpdesk.db next to the code was not created or changed.
"""

import base64
import json
import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import backups
import database as db
import migrations
from app import app

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Values that must never be readable in a backup file or manifest
SECRET_TITLE = "Payroll laptop for Ms Unique-Zebra-7731"
SECRET_EMAIL = "zebra7731@example.org"
SECRET_COMMENT = "Her home address is 12 Unique-Zebra Lane"


def stat_or_none(path):
    try:
        info = os.stat(path)
        return info.st_size, info.st_mtime_ns
    except FileNotFoundError:
        return None


class BackupTestCase(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = directory.name

        self.db_path = os.path.join(self.root, "live", "helpdesk.db")
        os.makedirs(os.path.dirname(self.db_path), mode=0o700)
        self.backup_dir = os.path.join(self.root, "backups")
        self.offsite_dir = os.path.join(self.root, "offsite")
        os.makedirs(self.offsite_dir, mode=0o700)
        self.key_path = os.path.join(self.root, "keys", "backup-key")
        os.makedirs(os.path.dirname(self.key_path), mode=0o700)

        patcher = patch.object(db, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        # The real database must be exactly as it was after every test
        real = db.DEFAULT_DB_PATH
        before = stat_or_none(real)
        self.addCleanup(lambda: self.assertEqual(
            stat_or_none(real), before, "the real helpdesk.db was touched"))

        migrations.migrate(self.db_path)
        self.populate()
        self.key = backups.write_new_key(self.key_path)

    def populate(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO users (username, password_hash, role, email) "
            "VALUES ('zebra', 'scrypt:32768:8:1$salt$hashvalue', 'user', ?)",
            (SECRET_EMAIL,))
        conn.commit()
        conn.close()

        ticket_id = db.create_ticket(SECRET_TITLE, "Details", "High", "zebra")
        db.add_comment(ticket_id, "zebra", SECRET_COMMENT)

    def row_count(self, path, table):
        conn = sqlite3.connect(path)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    def make_backup(self, **kwargs):
        return backups.create_backup(self.db_path, self.backup_dir, self.key,
                                     **kwargs)


# ---------------------------------------------------
# SNAPSHOT
# ---------------------------------------------------

class TestSnapshot(BackupTestCase):

    def test_snapshot_does_not_change_the_live_file(self):
        before = Path(self.db_path).read_bytes()
        backups.snapshot(self.db_path)
        self.assertEqual(Path(self.db_path).read_bytes(), before)
        self.assertFalse(os.path.exists(self.db_path + "-journal"))

    def test_wal_mode_changes_not_yet_checkpointed_are_included(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.executemany(
            "INSERT INTO tickets (title, description, priority, created_by)"
            " VALUES (?, 'x', 'Low', 'zebra')",
            [(f"wal ticket {n}",) for n in range(50)])
        conn.commit()
        self.assertGreater(os.path.getsize(self.db_path + "-wal"), 0)

        try:
            image = backups.snapshot(self.db_path)
        finally:
            conn.close()

        # Standalone file: marked as a rollback-journal database
        self.assertEqual((image[18], image[19]), (1, 1))
        check = backups.check_image(image)
        self.assertEqual(check.tables["tickets"], 51)
        self.assertEqual(check.integrity, "ok")

    def test_snapshot_during_concurrent_writes_is_consistent(self):
        stop = threading.Event()
        written = []

        def writer():
            conn = sqlite3.connect(self.db_path, timeout=10)
            while not stop.is_set():
                # Two rows per transaction: a snapshot never has one alone
                conn.execute("BEGIN IMMEDIATE")
                for _ in range(2):
                    conn.execute(
                        "INSERT INTO tickets (title, description, priority, "
                        "created_by) VALUES ('pair', 'x', 'Low', 'zebra')")
                conn.execute("COMMIT")
                written.append(1)
            conn.close()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            images = [backups.snapshot(self.db_path) for _ in range(5)]
        finally:
            stop.set()
            thread.join(10)

        self.assertTrue(written)
        for image in images:
            check = backups.check_image(image)
            self.assertEqual(check.integrity, "ok")
            self.assertEqual((check.tables["tickets"] - 1) % 2, 0)

    def test_missing_database(self):
        with self.assertRaisesRegex(backups.BackupError, "does not exist"):
            backups.snapshot(os.path.join(self.root, "missing.db"))


# ---------------------------------------------------
# ENCRYPTION AND KEYS
# ---------------------------------------------------

class TestEncryption(BackupTestCase):

    def test_round_trip(self):
        image = backups.snapshot(self.db_path)
        self.assertEqual(backups.decrypt(backups.encrypt(image, self.key),
                                         self.key), image)

    def test_each_encryption_uses_a_new_nonce(self):
        image = backups.snapshot(self.db_path)
        self.assertNotEqual(backups.encrypt(image, self.key),
                            backups.encrypt(image, self.key))

    def test_wrong_key_is_named_not_guessed(self):
        blob = backups.encrypt(backups.snapshot(self.db_path), self.key)
        other = backups.BackupKey(os.urandom(32))

        with self.assertRaisesRegex(backups.BackupError, "different key"):
            backups.decrypt(blob, other)

    def test_any_altered_byte_is_detected(self):
        blob = bytearray(backups.encrypt(backups.snapshot(self.db_path),
                                         self.key))
        # Nonce, ciphertext start, middle and the authentication tag
        for position in (backups.HEADER_BYTES - 1, backups.HEADER_BYTES,
                         len(blob) // 2, len(blob) - 1):
            with self.subTest(position=position):
                damaged = bytearray(blob)
                damaged[position] ^= 0x01
                with self.assertRaisesRegex(backups.BackupError,
                                            "damaged or has been altered"):
                    backups.decrypt(bytes(damaged), self.key)

    def test_not_a_backup(self):
        with self.assertRaisesRegex(backups.BackupError, "not a helpdesk"):
            backups.decrypt(b"SQLite format 3\x00" + b"\x00" * 100, self.key)

    def test_key_repr_hides_the_key(self):
        self.assertNotIn(base64.urlsafe_b64encode(self.key.key).decode(),
                         repr(self.key))
        self.assertNotIn(self.key.key.hex(), repr(self.key))


class TestKeyFiles(BackupTestCase):

    def test_generated_key_is_private_and_loads(self):
        mode = stat.S_IMODE(os.stat(self.key_path).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(backups.load_key(self.key_path).key, self.key.key)

    def test_existing_key_file_is_never_overwritten(self):
        before = Path(self.key_path).read_bytes()
        with self.assertRaisesRegex(backups.BackupError, "already exists"):
            backups.write_new_key(self.key_path)
        self.assertEqual(Path(self.key_path).read_bytes(), before)

    def test_readable_key_file_is_refused(self):
        os.chmod(self.key_path, 0o644)
        with self.assertRaisesRegex(backups.BackupError, "chmod 600"):
            backups.load_key(self.key_path)

    def test_key_inside_the_application_folder_is_refused(self):
        with self.assertRaisesRegex(backups.BackupError, "application folder"):
            backups.write_new_key(str(PROJECT_ROOT / "backup-test-key"))
        self.assertFalse((PROJECT_ROOT / "backup-test-key").exists())

        with self.assertRaisesRegex(backups.BackupError, "application folder"):
            backups.load_key(str(PROJECT_ROOT / "app.py"))

    def test_key_stored_with_the_backups_is_refused(self):
        os.makedirs(self.backup_dir, mode=0o700)
        inside = os.path.join(self.backup_dir, "backup-key")
        backups.write_new_key(inside)

        with self.assertRaisesRegex(backups.BackupError, "backup directory"):
            backups.load_key(inside, forbidden_directories=[self.backup_dir])

    def test_invalid_key_content(self):
        bad = os.path.join(self.root, "keys", "bad")
        for content in (b"", b"short", base64.urlsafe_b64encode(b"x" * 16)):
            with self.subTest(content=content):
                Path(bad).write_bytes(content)
                os.chmod(bad, 0o600)
                with self.assertRaisesRegex(backups.BackupError,
                                            "valid key"):
                    backups.load_key(bad)

    def test_no_key_configured(self):
        with self.assertRaisesRegex(backups.BackupError,
                                    "HELPDESK_BACKUP_KEY_FILE"):
            backups.load_key("")


# ---------------------------------------------------
# CREATE AND VERIFY
# ---------------------------------------------------

class TestCreateBackup(BackupTestCase):

    def test_backup_files_are_private_and_described_by_the_manifest(self):
        result = self.make_backup()

        self.assertEqual(stat.S_IMODE(os.stat(self.backup_dir).st_mode),
                         0o700)
        for path in (result.path, result.manifest_path):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

        manifest = json.loads(Path(result.manifest_path).read_text())
        self.assertEqual(manifest["schema_version"], migrations.SCHEMA_VERSION)
        self.assertEqual(manifest["integrity_check"], "ok")
        self.assertEqual(manifest["foreign_key_violations"], 0)
        self.assertEqual(manifest["tables"]["tickets"], 1)
        self.assertEqual(manifest["key_id"], self.key.key_id)
        self.assertEqual(manifest["encryption"], "AES-256-GCM")

        # No half-written files are left behind
        self.assertEqual(
            [name for name in os.listdir(self.backup_dir)
             if name.startswith(".")], [])

    def test_no_personal_data_or_secrets_are_readable(self):
        result = self.make_backup()

        for path in (result.path, result.manifest_path):
            data = Path(path).read_bytes()
            for secret in (SECRET_TITLE, SECRET_EMAIL, SECRET_COMMENT,
                           "zebra", "scrypt:", "SQLite format",
                           base64.urlsafe_b64encode(self.key.key).decode()):
                with self.subTest(path=os.path.basename(path), secret=secret):
                    self.assertNotIn(secret.encode(), data)

    def test_backups_inside_the_repository_are_refused(self):
        target = PROJECT_ROOT / "backup-test-output"

        with self.assertRaisesRegex(backups.BackupError,
                                    "outside the application folder"):
            backups.create_backup(self.db_path, str(target), self.key)

        self.assertFalse(target.exists())

    def test_a_directory_other_accounts_can_read_is_refused(self):
        os.makedirs(self.backup_dir, mode=0o755)
        os.chmod(self.backup_dir, 0o755)

        with self.assertRaisesRegex(backups.BackupError, "chmod 700"):
            self.make_backup()

    def test_corrupt_database_is_not_backed_up(self):
        # Damage the tickets table's root page (its b-tree page type
        # byte). The file still opens, but the table cannot be read.
        conn = sqlite3.connect(self.db_path)
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        root = conn.execute("SELECT rootpage FROM sqlite_master "
                            "WHERE name = 'tickets'").fetchone()[0]
        conn.close()

        with open(self.db_path, "r+b") as handle:
            handle.seek((root - 1) * page_size)
            handle.write(b"\x00")

        with self.assertRaises(backups.BackupError):
            self.make_backup()

        self.assertEqual(backups.list_backups(self.backup_dir), [])

    def test_foreign_key_violations_are_not_backed_up(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("INSERT INTO comments (ticket_id, author, content) "
                     "VALUES (9999, 'zebra', 'orphan')")
        conn.commit()
        conn.close()

        with self.assertRaisesRegex(backups.BackupError,
                                    "foreign-key violation.*no backup was "
                                    "written"):
            self.make_backup()

        self.assertEqual(backups.list_backups(self.backup_dir), [])

    def test_two_backups_in_the_same_second_get_distinct_names(self):
        moment = datetime(2026, 11, 25, 9, 30, tzinfo=timezone.utc)
        first = self.make_backup(now=moment)
        second = self.make_backup(now=moment)

        self.assertNotEqual(first.path, second.path)
        self.assertTrue(second.path.endswith("-2.hdbk"))
        self.assertEqual(len(backups.list_backups(self.backup_dir)), 2)

    def test_failed_verification_after_writing_is_reported(self):
        with patch.object(backups, "verify_backup",
                          side_effect=backups.BackupError("read-back failed")):
            with self.assertRaisesRegex(backups.BackupError,
                                        "read-back failed"):
                self.make_backup()

        # The unverified file and its manifest are not left behind
        self.assertEqual(os.listdir(self.backup_dir), [])


class TestVerifyBackup(BackupTestCase):

    def setUp(self):
        super().setUp()
        self.result = self.make_backup()

    def test_good_backup_verifies(self):
        verified = backups.verify_backup(self.result.path, self.key)
        self.assertEqual(verified.check.tables["comments"], 1)

    def test_truncated_copy_is_detected(self):
        data = Path(self.result.path).read_bytes()
        Path(self.result.path).write_bytes(data[:-10])

        with self.assertRaisesRegex(backups.BackupError,
                                    "does not match its manifest"):
            backups.verify_backup(self.result.path, self.key)

    def test_missing_manifest_is_detected(self):
        os.unlink(self.result.manifest_path)
        with self.assertRaisesRegex(backups.BackupError, "manifest"):
            backups.verify_backup(self.result.path, self.key)

    def test_edited_manifest_is_detected(self):
        manifest = json.loads(Path(self.result.manifest_path).read_text())
        manifest["tables"]["tickets"] = 99
        Path(self.result.manifest_path).write_text(json.dumps(manifest))

        with self.assertRaisesRegex(backups.BackupError,
                                    "does not match its manifest"):
            backups.verify_backup(self.result.path, self.key)


# ---------------------------------------------------
# OFF-SERVER COPY
# ---------------------------------------------------

class TestTransfer(BackupTestCase):

    def test_copy_is_verified_at_the_destination(self):
        result = self.make_backup()
        copied = backups.transfer(result.path, self.offsite_dir, self.key)

        self.assertEqual(os.path.dirname(copied.path), self.offsite_dir)
        self.assertEqual(Path(copied.path).read_bytes(),
                         Path(result.path).read_bytes())
        self.assertTrue(os.path.exists(
            backups.manifest_path_for(copied.path)))

    def test_damaged_copy_is_detected(self):
        result = self.make_backup()
        real_write = backups._write_new_file

        def flaky_write(path, data):
            if path.startswith(self.offsite_dir) and path.endswith(".hdbk"):
                data = data[:100] + bytes([data[100] ^ 1]) + data[101:]
            real_write(path, data)

        with patch.object(backups, "_write_new_file", flaky_write):
            with self.assertRaisesRegex(backups.BackupError,
                                        "does not match its manifest"):
                backups.transfer(result.path, self.offsite_dir, self.key)

    def test_destination_must_exist_and_differ(self):
        result = self.make_backup()

        with self.assertRaisesRegex(backups.BackupError, "does not exist"):
            backups.transfer(result.path, os.path.join(self.root, "nope"),
                             self.key)

        with self.assertRaisesRegex(backups.BackupError, "own directory"):
            backups.transfer(result.path, self.backup_dir, self.key)

    def test_existing_copy_is_not_overwritten(self):
        result = self.make_backup()
        backups.transfer(result.path, self.offsite_dir, self.key)

        with self.assertRaisesRegex(backups.BackupError, "already exists"):
            backups.transfer(result.path, self.offsite_dir, self.key)


# ---------------------------------------------------
# RESTORE INTO A NEW FILE
# ---------------------------------------------------

class TestRestoreToNewFile(BackupTestCase):

    def setUp(self):
        super().setUp()
        self.result = self.make_backup()

    def test_restores_an_identical_usable_database(self):
        target = os.path.join(self.root, "restored.db")
        check = backups.restore_to_new_file(self.result.path, self.key,
                                            target, live_db_path=self.db_path)

        self.assertEqual(check.schema_state, "current")
        self.assertEqual(migrations.schema_status(target).state, "current")
        self.assertEqual(self.row_count(target, "tickets"), 1)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)

        conn = sqlite3.connect(target)
        title = conn.execute("SELECT title FROM tickets").fetchone()[0]
        conn.close()
        self.assertEqual(title, SECRET_TITLE)

    def test_the_live_database_is_always_refused(self):
        before = Path(self.db_path).read_bytes()
        link = os.path.join(self.root, "link-to-live.db")
        os.symlink(self.db_path, link)

        for target in (self.db_path, link, self.db_path + "-wal",
                       os.path.join(os.path.dirname(self.db_path), ".",
                                    "helpdesk.db")):
            with self.subTest(target=target):
                with self.assertRaisesRegex(backups.BackupError,
                                            "live database"):
                    backups.restore_to_new_file(
                        self.result.path, self.key, target,
                        live_db_path=self.db_path)

        self.assertEqual(Path(self.db_path).read_bytes(), before)

    def test_existing_target_is_refused(self):
        target = os.path.join(self.root, "existing.db")
        Path(target).write_bytes(b"keep me")

        with self.assertRaisesRegex(backups.BackupError, "already exists"):
            backups.restore_to_new_file(self.result.path, self.key, target,
                                        live_db_path=self.db_path)
        self.assertEqual(Path(target).read_bytes(), b"keep me")


# ---------------------------------------------------
# LIVE RESTORE (the "live" database is a temporary file)
# ---------------------------------------------------

class TestRestoreLive(BackupTestCase):

    def setUp(self):
        super().setUp()
        self.result = self.make_backup()

        # Work done after the backup, which a restore discards
        db.create_ticket("After the backup", "x", "Low", "zebra")
        self.safety_dir = os.path.join(self.root, "safety")

    def restore(self, confirmed=True):
        return backups.restore_live(
            self.result.path, self.key, live_db_path=self.db_path,
            safety_dir=self.safety_dir, confirmed=confirmed)

    def test_requires_explicit_confirmation(self):
        for value in (False, None, "yes", 1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(backups.BackupError,
                                            "not confirmed"):
                    self.restore(confirmed=value)

        self.assertEqual(self.row_count(self.db_path, "tickets"), 2)
        self.assertFalse(os.path.exists(self.safety_dir))

    def test_refused_while_the_database_is_in_use(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("BEGIN IMMEDIATE")
        self.addCleanup(conn.close)

        with self.assertRaisesRegex(backups.BackupError,
                                    "using the database"):
            self.restore()

        conn.rollback()
        self.assertEqual(self.row_count(self.db_path, "tickets"), 2)

    def test_refused_when_a_wal_file_is_present(self):
        Path(self.db_path + "-wal").write_bytes(b"")

        with self.assertRaisesRegex(backups.BackupError, "-wal"):
            self.restore()

        self.assertEqual(self.row_count(self.db_path, "tickets"), 2)

    def test_replaces_the_database_after_saving_a_safety_backup(self):
        check, safety = self.restore()

        self.assertEqual(check.schema_state, "current")
        self.assertEqual(self.row_count(self.db_path, "tickets"), 1)

        # The replaced database (2 tickets) can itself be restored
        verified = backups.verify_backup(safety.path, self.key)
        self.assertEqual(verified.check.tables["tickets"], 2)

    def test_a_damaged_backup_never_replaces_anything(self):
        data = bytearray(Path(self.result.path).read_bytes())
        data[-1] ^= 1
        Path(self.result.path).write_bytes(bytes(data))

        with self.assertRaises(backups.BackupError):
            self.restore()

        self.assertEqual(self.row_count(self.db_path, "tickets"), 2)
        self.assertFalse(os.path.exists(self.safety_dir))


# ---------------------------------------------------
# DRILL
# ---------------------------------------------------

class TestRestorationDrill(BackupTestCase):

    def test_drill_passes_and_leaves_nothing_behind(self):
        result = self.make_backup()
        work = os.path.join(self.root, "drill-work")
        os.makedirs(work, mode=0o700)
        live_before = Path(self.db_path).read_bytes()

        report = backups.run_drill(result.path, self.key, work_dir=work)

        self.assertTrue(report.passed, report.problems)
        self.assertEqual(report.checks["integrity_check"], "ok")
        self.assertEqual(report.checks["foreign_key_violations"], 0)
        self.assertTrue(report.checks["row_counts_match"])
        self.assertEqual(report.checks["application_schema_status"],
                         "current")
        self.assertEqual(report.checks["application_queries"], "ok")
        self.assertEqual(report.checks["writable"], "ok")
        self.assertEqual(os.listdir(work), [])
        self.assertEqual(Path(self.db_path).read_bytes(), live_before)

    def test_drill_of_a_damaged_backup_fails_and_says_why(self):
        result = self.make_backup()
        data = bytearray(Path(result.path).read_bytes())
        data[200] ^= 1
        Path(result.path).write_bytes(bytes(data))

        report = backups.run_drill(result.path, self.key)

        self.assertFalse(report.passed)
        self.assertIn("does not match its manifest", report.problems[0])

    def test_drill_reports_are_recorded_for_monitoring(self):
        result = self.make_backup()
        report = backups.run_drill(result.path, self.key)
        saved = backups.record_drill(report, self.backup_dir)

        self.assertEqual(stat.S_IMODE(os.stat(saved).st_mode), 0o600)
        latest = backups.latest_drill(self.backup_dir)
        self.assertTrue(latest["passed"])
        self.assertEqual(latest["backup_file"], os.path.basename(result.path))
        self.assertNotIn(SECRET_TITLE, Path(saved).read_text())

    def test_latest_drill_in_the_same_second_is_the_newest(self):
        moment = "2026-11-25T04:00:00+00:00"
        for passed in (True, False, True, False):
            backups.record_drill(backups.DrillReport(
                "x.hdbk", "", moment, passed, 0.1, {}, []), self.backup_dir)

        names = sorted(os.listdir(os.path.join(self.backup_dir,
                                               backups.DRILL_DIRECTORY)))
        self.assertIn("drill-20261125T040000Z-4.json", names)
        self.assertFalse(backups.latest_drill(self.backup_dir)["passed"])


# ---------------------------------------------------
# RETENTION
# ---------------------------------------------------

class TestRetention(unittest.TestCase):

    def backups_every(self, hours, count, start):
        return [(start - timedelta(hours=hours * n), f"/b/{n}")
                for n in range(count)]

    def test_daily_weekly_monthly(self):
        start = datetime(2026, 11, 25, 2, 0, tzinfo=timezone.utc)
        # One backup a day for a year
        found = self.backups_every(24, 365, start)

        keep, remove = backups.plan_retention(found, keep_daily=7,
                                              keep_weekly=4, keep_monthly=6)

        self.assertEqual(len(keep) + len(remove), 365)
        # 7 days, plus older weekly/monthly ones not already counted
        self.assertLessEqual(len(keep), 7 + 4 + 6)
        self.assertGreaterEqual(len(keep), 7)
        self.assertIn("/b/0", keep)
        for n in range(7):
            self.assertIn(f"/b/{n}", keep)
        # Nothing older than about six months survives
        self.assertNotIn("/b/300", keep)

    def test_newest_backup_is_always_kept(self):
        start = datetime(2026, 11, 25, tzinfo=timezone.utc)
        keep, remove = backups.plan_retention(
            [(start, "/b/new")], keep_daily=1, keep_weekly=0, keep_monthly=0)
        self.assertEqual((keep, remove), (["/b/new"], []))

    def test_several_backups_a_day_keep_the_newest_of_each_day(self):
        start = datetime(2026, 11, 25, 23, 0, tzinfo=timezone.utc)
        found = self.backups_every(6, 8, start)  # two days, 4 per day

        keep, _ = backups.plan_retention(found, keep_daily=7, keep_weekly=0,
                                         keep_monthly=0)

        self.assertEqual(keep, ["/b/0", "/b/4"])


# ---------------------------------------------------
# COMMANDS
# ---------------------------------------------------

class TestBackupCommands(BackupTestCase):

    def setUp(self):
        super().setUp()
        self.runner = app.test_cli_runner()
        env = patch.dict(os.environ, {
            "HELPDESK_BACKUP_DIR": self.backup_dir,
            "HELPDESK_BACKUP_OFFSITE_DIR": self.offsite_dir,
            "HELPDESK_BACKUP_KEY_FILE": self.key_path,
        })
        env.start()
        self.addCleanup(env.stop)

        operator = patch("helpdesk.cli._operator", return_value="opsadmin")
        operator.start()
        self.addCleanup(operator.stop)

    def run_command(self, *args, interactive=False, input=None):
        with patch("helpdesk.cli._interactive", return_value=interactive):
            return self.runner.invoke(args=list(args), input=input)

    def test_create_verify_transfer_drill(self):
        result = self.run_command("backup-create")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Backup written and verified", result.output)
        self.assertNotIn(SECRET_TITLE, result.output)

        path = backups.latest_backup(self.backup_dir)[1]

        result = self.run_command("backup-verify", path)
        self.assertEqual(result.exit_code, 0, result.output)

        result = self.run_command("backup-transfer")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(os.path.exists(
            os.path.join(self.offsite_dir, os.path.basename(path))))

        result = self.run_command("backup-drill")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Restoration drill passed", result.output)
        self.assertTrue(backups.latest_drill(self.backup_dir)["passed"])

        result = self.run_command("backup-list")
        self.assertIn("1 backup(s)", result.output)

    def test_missing_settings_are_explained(self):
        with patch.dict(os.environ, {"HELPDESK_BACKUP_DIR": "",
                                     "HELPDESK_BACKUP_KEY_FILE": ""}):
            result = self.run_command("backup-create")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("HELPDESK_BACKUP_DIR", result.output)

    def test_failed_drill_exits_1(self):
        self.run_command("backup-create")
        path = backups.latest_backup(self.backup_dir)[1]
        data = bytearray(Path(path).read_bytes())
        data[-5] ^= 1
        Path(path).write_bytes(bytes(data))

        result = self.run_command("backup-drill")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("FAILED", result.output)
        self.assertFalse(backups.latest_drill(self.backup_dir)["passed"])

    def test_restore_command_refuses_the_live_database(self):
        self.run_command("backup-create")
        path = backups.latest_backup(self.backup_dir)[1]

        result = self.run_command("backup-restore", path, "--to",
                                  self.db_path)

        self.assertEqual(result.exit_code, 1)
        self.assertIn("refusing to restore over the live database",
                      result.output)

    def test_prune_is_a_dry_run_unless_applied(self):
        for day in range(10):
            self.make_backup(now=datetime(2026, 11, 1 + day, 2, 0,
                                          tzinfo=timezone.utc))

        result = self.run_command("backup-prune", "--keep-daily", "3",
                                  "--keep-weekly", "0", "--keep-monthly", "0")
        self.assertIn("would delete", result.output)
        self.assertEqual(len(backups.list_backups(self.backup_dir)), 10)

        result = self.run_command("backup-prune", "--keep-daily", "3",
                                  "--keep-weekly", "0", "--keep-monthly", "0",
                                  "--apply")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(len(backups.list_backups(self.backup_dir)), 3)

    def test_live_restore_refuses_without_a_terminal(self):
        self.run_command("backup-create")
        path = backups.latest_backup(self.backup_dir)[1]

        result = self.run_command("backup-restore-live", path)

        self.assertEqual(result.exit_code, 1)
        self.assertIn("interactive terminal", result.output)

    def test_live_restore_refuses_a_wrong_phrase(self):
        self.run_command("backup-create")
        path = backups.latest_backup(self.backup_dir)[1]
        db.create_ticket("After the backup", "x", "Low", "zebra")

        for typed in ("yes", "replace", "REPLACE HELPDESK.DB", ""):
            with self.subTest(typed=typed):
                result = self.run_command("backup-restore-live", path,
                                          interactive=True,
                                          input=typed + "\n")
                self.assertEqual(result.exit_code, 1)
                self.assertIn("did not match", result.output)

        self.assertEqual(self.row_count(self.db_path, "tickets"), 2)

    def test_live_restore_with_the_phrase(self):
        self.run_command("backup-create")
        path = backups.latest_backup(self.backup_dir)[1]
        db.create_ticket("After the backup", "x", "Low", "zebra")

        result = self.run_command("backup-restore-live", path,
                                  interactive=True,
                                  input="replace helpdesk.db\n")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Safety backup of the replaced database", result.output)
        self.assertEqual(self.row_count(self.db_path, "tickets"), 1)
        self.assertEqual(len(backups.list_backups(self.backup_dir)), 2)

    def test_generate_key_command(self):
        new_key = os.path.join(self.root, "keys", "second-key")

        result = self.run_command("backup-generate-key", new_key)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("key id", result.output)
        self.assertNotIn(Path(new_key).read_text().strip(), result.output)


if __name__ == "__main__":
    unittest.main()
