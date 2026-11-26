"""
Encrypted, verified SQLite backups and restoration.

Used by the "flask backup-*" commands in helpdesk/backup_cli.py. Nothing
in the web application calls this module.

How a backup is made (create_backup):

  1. snapshot()  SQLite's online backup API copies the live database,
                 opened read-only, into memory. The copy is consistent
                 even while the application is writing: it includes
                 every committed transaction, also those still in a WAL
                 file, and none that are half-done. The copy is marked as
                 an ordinary (rollback-journal) database so it is one
                 self-contained file. The plaintext never touches disk.
  2. check       integrity_check, foreign_key_check and the schema
                 version, on the in-memory copy.
  3. encrypt     AES-256-GCM with a random nonce. The 32-byte key comes
                 from a key file that is never stored with the backups.
                 GCM also authenticates: any change to the file, however
                 small, makes decryption fail instead of restoring
                 damaged data.
  4. write       to a temporary name, flushed to disk, then renamed, so a
                 crash never leaves a half-written file that looks like a
                 backup. A manifest (.manifest.json) records sizes,
                 SHA-256 checksums, the schema version and row counts.
  5. verify      the written file is read back, decrypted and checked
                 again before the backup is reported as done.

File format (.hdbk):

    8 bytes   magic "HDBKENC1"
    16 bytes  key id: first 16 bytes of SHA-256("helpdesk-backup-key-id"
              + key). Identifies which key is needed without revealing it.
    12 bytes  random nonce
    rest      AES-256-GCM ciphertext and 16-byte tag; the 36 header bytes
              are authenticated as associated data

What backups and manifests never contain in readable form: the database
contents (tickets, comments, accounts, password hashes, email addresses),
the secret key, SMTP or Redis passwords, or the encryption key. Manifests
contain only counts, checksums, dates and the schema version.

Restoring:

  * restore_to_new_file() writes a backup to a NEW file and verifies it.
    It refuses the live database path, so it is always safe.
  * restore_live() replaces the live database. It requires an explicit
    confirmation from the operator (checked by the command), refuses
    while the database is in use, and first saves an encrypted safety
    backup of the database it is about to replace.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import migrations

MAGIC = b"HDBKENC1"
KEY_BYTES = 32
KEY_ID_BYTES = 16
NONCE_BYTES = 12
HEADER_BYTES = len(MAGIC) + KEY_ID_BYTES + NONCE_BYTES

MANIFEST_FORMAT = "helpdesk-backup-1"
BACKUP_SUFFIX = ".hdbk"
MANIFEST_SUFFIX = ".manifest.json"

# helpdesk-20261125T093000Z.hdbk (UTC, so names sort by time)
NAME_PATTERN = re.compile(r"^helpdesk-(\d{8}T\d{6}Z)(?:-(\d+))?\.hdbk$")
TIME_FORMAT = "%Y%m%dT%H%M%SZ"

DRILL_DIRECTORY = "drills"
DRILL_PATTERN = re.compile(r"^drill-(\d{8}T\d{6}Z)(?:-(\d+))?\.json$")

# The whole database is held in memory while it is encrypted. The
# helpdesk's database is a few megabytes; refuse anything absurd.
MAX_DATABASE_BYTES = 1024 * 1024 * 1024

PROJECT_ROOT = Path(__file__).resolve().parent

# Database header bytes 18 and 19: 1 = rollback journal, 2 = WAL
_HEADER_WRITE_VERSION = 18
_HEADER_READ_VERSION = 19
_ROLLBACK_JOURNAL = 1


class BackupError(Exception):
    """A backup operation failed. The message is safe to print."""


# ---------------------------------------------------
# PATHS
# ---------------------------------------------------

def _inside(path, directory):
    path = os.path.realpath(path)
    directory = os.path.realpath(directory)
    return os.path.commonpath([path, directory]) == directory


def check_storage_directory(directory, *, create=True):
    """
    Make sure a backup directory is a safe place for backups.

    It must be outside the application's code (and so outside its Git
    repository), and no other account may read it. It is created with
    mode 0700 if it does not exist.
    """

    directory = os.path.abspath(directory)

    if _inside(directory, PROJECT_ROOT):
        raise BackupError(
            "backup directory must be outside the application folder "
            "(and its Git repository)"
        )

    if not os.path.exists(directory):
        if not create:
            raise BackupError("backup directory does not exist")
        os.makedirs(directory, mode=0o700)

    info = os.stat(directory)

    if not stat.S_ISDIR(info.st_mode):
        raise BackupError("backup directory is not a directory")

    if info.st_mode & 0o077:
        raise BackupError(
            "backup directory must not be accessible to other accounts "
            "(chmod 700)"
        )

    return directory


def _fsync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new_file(path, data):
    """
    Create `path` atomically with mode 0600, refusing to overwrite.

    Data goes to a temporary file in the same directory, is flushed to
    disk, and is then linked into place, so the name either does not
    exist or holds the complete content.
    """

    directory = os.path.dirname(os.path.abspath(path))
    descriptor, temporary = tempfile.mkstemp(
        prefix=".partial-", dir=directory
    )

    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        os.chmod(temporary, 0o600)

        # link() fails if the name exists, unlike rename()
        os.link(temporary, path)
    except FileExistsError:
        raise BackupError(f"{os.path.basename(path)} already exists") \
            from None
    finally:
        os.unlink(temporary)

    _fsync_directory(directory)


# ---------------------------------------------------
# KEYS
# ---------------------------------------------------

@dataclass(frozen=True)
class BackupKey:
    key: bytes = field(repr=False)

    @property
    def key_id(self):
        return key_id(self.key)


def key_id(key):
    return hashlib.sha256(b"helpdesk-backup-key-id" + key).digest()[
        :KEY_ID_BYTES].hex()


def write_new_key(path):
    """Create a new random key file (mode 0600). Returns its BackupKey."""

    path = os.path.abspath(path)

    if _inside(path, PROJECT_ROOT):
        raise BackupError(
            "the key file must be outside the application folder, so it "
            "can never be committed to Git"
        )

    key = secrets.token_bytes(KEY_BYTES)
    text = base64.urlsafe_b64encode(key) + b"\n"

    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             0o600)
    except FileExistsError:
        raise BackupError("key file already exists; choose a new path") \
            from None

    with os.fdopen(descriptor, "wb") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())

    return BackupKey(key)


def load_key(path, *, forbidden_directories=()):
    """
    Read a key file written by write_new_key().

    Refused when other accounts can read it, when it lies inside the
    application folder, or when it is stored inside a backup directory
    (a key kept next to the backups protects nothing).
    """

    if not path:
        raise BackupError(
            "no backup key: set HELPDESK_BACKUP_KEY_FILE or use --key-file"
        )

    path = os.path.abspath(path)

    for directory in (PROJECT_ROOT, *forbidden_directories):
        if directory and _inside(path, directory):
            raise BackupError(
                "the backup key must not be stored in the application "
                "folder or in a backup directory"
            )

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        raise BackupError("backup key file could not be read") from None

    try:
        info = os.fstat(descriptor)

        if not stat.S_ISREG(info.st_mode):
            raise BackupError("backup key file is not a regular file")

        if info.st_mode & 0o077:
            raise BackupError(
                "backup key file must not be readable by other accounts "
                "(chmod 600)"
            )

        text = os.read(descriptor, 256)
    finally:
        os.close(descriptor)

    try:
        key = base64.urlsafe_b64decode(text.strip())
    except ValueError:
        key = b""

    if len(key) != KEY_BYTES:
        raise BackupError("backup key file does not contain a valid key")

    return BackupKey(key)


# ---------------------------------------------------
# SNAPSHOT AND CHECKS (in memory)
# ---------------------------------------------------

def snapshot(db_path):
    """
    Return a consistent copy of the database as bytes.

    The live file is opened read-only and copied with SQLite's backup
    API, which takes a read snapshot: committed transactions (including
    those still in the WAL file) are copied, uncommitted ones are not,
    and writers are not blocked for long.
    """

    if not os.path.isfile(db_path):
        raise BackupError("the database file does not exist")

    if os.path.getsize(db_path) > MAX_DATABASE_BYTES:
        raise BackupError("the database is too large for this backup tool")

    source = migrations._connect_read_only(db_path)
    memory = sqlite3.connect(":memory:")

    try:
        source.backup(memory)
        image = bytearray(memory.serialize())
    except sqlite3.Error as error:
        raise BackupError(
            f"SQLite could not copy the database ({type(error).__name__})"
        ) from None
    finally:
        memory.close()
        source.close()

    # A copy of a WAL-mode database is still marked WAL, and would need
    # -wal and -shm files beside it. Mark it as a plain database, exactly
    # as "PRAGMA journal_mode=DELETE" would.
    if len(image) >= 100:
        image[_HEADER_WRITE_VERSION] = _ROLLBACK_JOURNAL
        image[_HEADER_READ_VERSION] = _ROLLBACK_JOURNAL

    return bytes(image)


@dataclass
class ImageCheck:
    integrity: str
    foreign_key_violations: int
    schema_version: int
    schema_state: str
    tables: dict

    @property
    def ok(self):
        return (self.integrity == "ok" and self.foreign_key_violations == 0
                and self.schema_state in ("current", "outdated"))


def _open_image(image):
    conn = sqlite3.connect(":memory:")
    try:
        conn.deserialize(image)
    except sqlite3.Error:
        conn.close()
        raise BackupError("the backup does not contain a SQLite database") \
            from None
    return conn


def check_connection(conn):
    """Integrity, foreign keys, schema and row counts of an open database."""

    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        version = conn.execute("PRAGMA user_version").fetchone()[0]

        target = migrations.SCHEMA_VERSION
        if version > target:
            state = "too_new"
        elif version == target:
            state = ("incompatible" if migrations.find_schema_problems(conn)
                     else "current")
        elif version == 0:
            state = "legacy"
        else:
            state = "outdated"

        tables = {
            name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        }
    except sqlite3.DatabaseError as error:
        raise BackupError(
            f"the database could not be checked ({type(error).__name__})"
        ) from None

    return ImageCheck(integrity, violations, version, state, tables)


def check_image(image):
    conn = _open_image(image)
    try:
        return check_connection(conn)
    finally:
        conn.close()


# ---------------------------------------------------
# ENCRYPTION
# ---------------------------------------------------

def encrypt(image, backup_key):
    nonce = secrets.token_bytes(NONCE_BYTES)
    header = MAGIC + bytes.fromhex(backup_key.key_id) + nonce
    return header + AESGCM(backup_key.key).encrypt(nonce, image, header)


def decrypt(blob, backup_key):
    if len(blob) < HEADER_BYTES + 16 or not blob.startswith(MAGIC):
        raise BackupError("not a helpdesk backup file")

    header = blob[:HEADER_BYTES]
    stored_id = header[len(MAGIC):len(MAGIC) + KEY_ID_BYTES].hex()

    if stored_id != backup_key.key_id:
        raise BackupError(
            f"this backup was encrypted with a different key "
            f"(key id {stored_id})"
        )

    nonce = header[len(MAGIC) + KEY_ID_BYTES:]

    try:
        return AESGCM(backup_key.key).decrypt(nonce, blob[HEADER_BYTES:],
                                              header)
    except InvalidTag:
        raise BackupError(
            "the backup is damaged or has been altered; it cannot be used"
        ) from None


# ---------------------------------------------------
# CREATE, VERIFY, LIST
# ---------------------------------------------------

def _now():
    return datetime.now(timezone.utc)


def manifest_path_for(backup_path):
    backup_path = str(backup_path)
    if not backup_path.endswith(BACKUP_SUFFIX):
        raise BackupError("backup files end in .hdbk")
    return backup_path[:-len(BACKUP_SUFFIX)] + MANIFEST_SUFFIX


@dataclass
class BackupResult:
    path: str
    manifest_path: str
    manifest: dict


def _new_backup_name(directory, now):
    stamp = now.strftime(TIME_FORMAT)
    name = f"helpdesk-{stamp}{BACKUP_SUFFIX}"
    number = 2

    while os.path.exists(os.path.join(directory, name)):
        name = f"helpdesk-{stamp}-{number}{BACKUP_SUFFIX}"
        number += 1

    return os.path.join(directory, name)


def create_backup(db_path, directory, backup_key, *, now=None):
    """Make, encrypt, write and verify one backup. Returns BackupResult."""

    directory = check_storage_directory(directory)
    now = now or _now()

    image = snapshot(db_path)
    check = check_image(image)

    if check.integrity != "ok":
        raise BackupError("the database failed its integrity check; "
                          "no backup was written")

    if check.foreign_key_violations:
        raise BackupError(
            f"the database has {check.foreign_key_violations} foreign-key "
            "violation(s); no backup was written"
        )

    blob = encrypt(image, backup_key)
    path = _new_backup_name(directory, now)

    manifest = {
        "format": MANIFEST_FORMAT,
        "created_at": now.isoformat(timespec="seconds"),
        "file": os.path.basename(path),
        "encryption": "AES-256-GCM",
        "key_id": backup_key.key_id,
        "size": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "database_size": len(image),
        "database_sha256": hashlib.sha256(image).hexdigest(),
        "schema_version": check.schema_version,
        "integrity_check": check.integrity,
        "foreign_key_violations": check.foreign_key_violations,
        "tables": check.tables,
    }

    manifest_path = manifest_path_for(path)
    _write_new_file(path, blob)
    _write_new_file(manifest_path,
                    json.dumps(manifest, indent=2, sort_keys=True).encode())

    # Read back what is actually on disk before calling it a backup. One
    # that fails is deleted, so nothing unverified looks like a backup.
    try:
        verify_backup(path, backup_key)
    except BackupError:
        remove_backup(path)
        raise

    return BackupResult(path, manifest_path, manifest)


def read_manifest(backup_path):
    try:
        with open(manifest_path_for(backup_path), "rb") as handle:
            manifest = json.loads(handle.read(1024 * 1024))
    except FileNotFoundError:
        raise BackupError("the backup's manifest file is missing") from None
    except (OSError, ValueError):
        raise BackupError("the backup's manifest cannot be read") from None

    if not isinstance(manifest, dict) or (
        manifest.get("format") != MANIFEST_FORMAT
    ):
        raise BackupError("the manifest is not a helpdesk backup manifest")

    return manifest


@dataclass
class VerifyResult:
    path: str
    manifest: dict
    check: ImageCheck
    image: bytes = field(repr=False)


def verify_backup(path, backup_key):
    """
    Check a backup completely: checksum, decryption, and database checks.

    Raises BackupError on any problem. Read-only.
    """

    manifest = read_manifest(path)

    try:
        with open(path, "rb") as handle:
            blob = handle.read(MAX_DATABASE_BYTES + HEADER_BYTES + 17)
    except OSError:
        raise BackupError("the backup file cannot be read") from None

    if len(blob) != manifest.get("size") or (
        hashlib.sha256(blob).hexdigest() != manifest.get("sha256")
    ):
        raise BackupError(
            "the backup file does not match its manifest (incomplete copy "
            "or altered file)"
        )

    image = decrypt(blob, backup_key)

    if hashlib.sha256(image).hexdigest() != manifest.get("database_sha256"):
        raise BackupError("the decrypted database does not match its "
                          "manifest")

    check = check_image(image)

    if check.integrity != "ok":
        raise BackupError("the backed-up database fails its integrity check")

    if check.foreign_key_violations:
        raise BackupError("the backed-up database has foreign-key "
                          "violations")

    if check.schema_version != manifest.get("schema_version") or (
        check.tables != manifest.get("tables")
    ):
        raise BackupError("the backed-up database does not match its "
                          "manifest")

    return VerifyResult(path, manifest, check, image)


def list_backups(directory):
    """[(created_at datetime, path), ...] oldest first, by file name."""

    if not os.path.isdir(directory):
        return []

    found = []

    for name in os.listdir(directory):
        match = NAME_PATTERN.match(name)
        if not match:
            continue

        created = datetime.strptime(match.group(1), TIME_FORMAT).replace(
            tzinfo=timezone.utc
        )
        found.append((created, int(match.group(2) or 1),
                      os.path.join(directory, name)))

    found.sort()
    return [(created, path) for created, _, path in found]


def latest_backup(directory):
    backups = list_backups(directory)
    return backups[-1] if backups else None


# ---------------------------------------------------
# OFF-SERVER COPY
# ---------------------------------------------------

def transfer(backup_path, destination, backup_key):
    """
    Copy a backup and its manifest to another directory and verify the
    copy by decrypting it there.

    `destination` is typically a mounted network share or removable disk
    on different hardware (HELPDESK_BACKUP_OFFSITE_DIR). The copy is
    trusted only after it has been read back from the destination.
    """

    destination = check_storage_directory(destination, create=False)

    source_dir = os.path.dirname(os.path.abspath(backup_path))
    if os.path.realpath(source_dir) == os.path.realpath(destination):
        raise BackupError("the destination is the backup's own directory")

    verify_backup(backup_path, backup_key)

    target = os.path.join(destination, os.path.basename(backup_path))

    with open(backup_path, "rb") as handle:
        _write_new_file(target, handle.read())
    with open(manifest_path_for(backup_path), "rb") as handle:
        _write_new_file(manifest_path_for(target), handle.read())

    return verify_backup(target, backup_key)


# ---------------------------------------------------
# RESTORE
# ---------------------------------------------------

def _same_file(first, second):
    if os.path.realpath(first) == os.path.realpath(second):
        return True
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def restore_to_new_file(backup_path, backup_key, target, *, live_db_path):
    """
    Decrypt a backup into a NEW database file and check the result.

    Always safe: refuses the live database (by path or by identity) and
    any file that already exists.
    """

    target = os.path.abspath(target)

    if _same_file(target, live_db_path) or any(
        _same_file(target, live_db_path + suffix)
        for suffix in SIDECAR_SUFFIXES
    ):
        raise BackupError(
            "refusing to restore over the live database; restore into a "
            "new file, or use the separate live-restore procedure"
        )

    if os.path.lexists(target):
        raise BackupError("the target file already exists; choose a new path")

    result = verify_backup(backup_path, backup_key)
    _write_new_file(target, result.image)

    conn = sqlite3.connect(Path(target).resolve().as_uri() + "?mode=ro",
                           uri=True)
    try:
        check = check_connection(conn)
    finally:
        conn.close()

    if not check.ok or check.tables != result.manifest["tables"]:
        raise BackupError("the restored file failed its checks")

    return check


def database_in_use(db_path):
    """
    Return a reason why the live database looks in use, or None.

    A -wal, -shm or -journal file beside it means a connection is open or
    was not closed cleanly; replacing the main file then would corrupt
    data. An exclusive lock that cannot be taken immediately means
    another process is using it right now.
    """

    for suffix in SIDECAR_SUFFIXES:
        if os.path.exists(db_path + suffix):
            return (f"a {suffix} file exists beside the database; stop the "
                    "web and notification services first")

    try:
        conn = sqlite3.connect(db_path, timeout=0, isolation_level=None)
        try:
            conn.execute("BEGIN EXCLUSIVE")
            conn.execute("ROLLBACK")
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return "another process is using the database right now"

    # Opening may itself create a -journal briefly; it is gone after close
    return None


def restore_live(backup_path, backup_key, *, live_db_path, safety_dir,
                 confirmed):
    """
    Replace the live database with a backup. Destructive.

    `confirmed` must be True, which the command sets only after the
    operator has typed the confirmation phrase. Before anything is
    replaced: the backup is fully verified, the live database must not
    be in use, and an encrypted safety backup of the current live
    database is written to `safety_dir`. Returns (check, safety backup).
    """

    if confirmed is not True:
        raise BackupError("live restore was not confirmed; nothing changed")

    result = verify_backup(backup_path, backup_key)

    if result.check.schema_state not in ("current", "outdated"):
        raise BackupError("the backup's schema cannot be used by this "
                          "version of the application")

    live_db_path = os.path.abspath(live_db_path)

    safety = None
    if os.path.exists(live_db_path):
        problem = database_in_use(live_db_path)
        if problem:
            raise BackupError(f"{problem}. Nothing was changed.")

        safety = create_backup(live_db_path, safety_dir, backup_key)

    directory = os.path.dirname(live_db_path)
    descriptor, temporary = tempfile.mkstemp(prefix=".restore-",
                                             dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(result.image)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)

        # Checked again right before the switch
        if os.path.exists(live_db_path):
            problem = database_in_use(live_db_path)
            if problem:
                raise BackupError(f"{problem}. Nothing was changed.")

        os.replace(temporary, live_db_path)
        temporary = None
        _fsync_directory(directory)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)

    conn = migrations._connect_read_only(live_db_path)
    try:
        check = check_connection(conn)
    finally:
        conn.close()

    return check, safety


# ---------------------------------------------------
# RESTORATION DRILL
# ---------------------------------------------------

@dataclass
class DrillReport:
    backup_file: str
    backup_created_at: str
    started_at: str
    passed: bool
    seconds: float
    checks: dict
    problems: list

    def as_dict(self):
        return {
            "format": "helpdesk-drill-1",
            "backup_file": self.backup_file,
            "backup_created_at": self.backup_created_at,
            "started_at": self.started_at,
            "passed": self.passed,
            "seconds": round(self.seconds, 3),
            "checks": self.checks,
            "problems": self.problems,
        }


def run_drill(backup_path, backup_key, *, work_dir=None, now=None):
    """
    Restore a backup into a disposable database and prove it is usable.

    The disposable copy lives in a new private temporary directory that
    is deleted afterwards, whatever happens. The live database is never
    opened. Returns a DrillReport (passed or not); never raises for a
    failed check, so the failure can be recorded.
    """

    now = now or _now()
    started = time.monotonic()
    checks, problems = {}, []
    manifest = {}

    try:
        manifest = read_manifest(backup_path)

        with tempfile.TemporaryDirectory(prefix="helpdesk-drill-",
                                         dir=work_dir) as scratch:
            os.chmod(scratch, 0o700)
            target = os.path.join(scratch, "restored.db")

            result = verify_backup(backup_path, backup_key)
            checks["decrypt_and_checksum"] = "ok"

            restore_to_new_file(backup_path, backup_key, target,
                                live_db_path=os.path.join(scratch, "none"))
            checks["restore"] = "ok"

            conn = sqlite3.connect(target)
            try:
                check = check_connection(conn)
                checks["integrity_check"] = check.integrity
                checks["foreign_key_violations"] = (
                    check.foreign_key_violations)
                checks["schema"] = (f"{check.schema_state} "
                                    f"(version {check.schema_version})")
                checks["row_counts_match"] = (
                    check.tables == result.manifest["tables"])

                # The restored database must work for the application's
                # own queries, not only for SQLite
                status = migrations.schema_status(target)
                checks["application_schema_status"] = status.state

                if status.is_current:
                    conn.execute("SELECT COUNT(*) FROM tickets").fetchone()
                    conn.execute(
                        "SELECT COUNT(*) FROM users WHERE is_active = 1"
                    ).fetchone()
                    conn.execute(
                        "SELECT status, COUNT(*) FROM notifications "
                        "GROUP BY status").fetchall()
                    checks["application_queries"] = "ok"

                # A write inside a transaction that is then rolled back:
                # the restored file is writable and its triggers work
                conn.execute("BEGIN")
                conn.execute("CREATE TABLE drill_write_test (x)")
                conn.execute("ROLLBACK")
                checks["writable"] = "ok"
            finally:
                conn.close()

            if check.integrity != "ok":
                problems.append("integrity check failed")
            if check.foreign_key_violations:
                problems.append("foreign-key violations")
            if not checks["row_counts_match"]:
                problems.append("row counts differ from the manifest")
            if status.state not in ("current", "outdated"):
                problems.append(f"schema state is {status.state}")

    except BackupError as error:
        problems.append(str(error))
    except (OSError, sqlite3.Error) as error:
        problems.append(f"drill failed ({type(error).__name__})")

    return DrillReport(
        backup_file=os.path.basename(backup_path),
        backup_created_at=manifest.get("created_at", ""),
        started_at=now.isoformat(timespec="seconds"),
        passed=not problems,
        seconds=time.monotonic() - started,
        checks=checks,
        problems=problems,
    )


def record_drill(report, backup_directory):
    """Save a drill report under <backup dir>/drills/ for monitoring."""

    directory = os.path.join(check_storage_directory(backup_directory),
                             DRILL_DIRECTORY)
    os.makedirs(directory, mode=0o700, exist_ok=True)

    stamp = datetime.fromisoformat(report.started_at).strftime(TIME_FORMAT)
    path = os.path.join(directory, f"drill-{stamp}.json")
    number = 2
    while os.path.exists(path):
        path = os.path.join(directory, f"drill-{stamp}-{number}.json")
        number += 1

    _write_new_file(path, json.dumps(report.as_dict(), indent=2,
                                     sort_keys=True).encode())
    return path


def latest_drill(backup_directory):
    """The most recent drill report as a dict, or None."""

    directory = os.path.join(backup_directory, DRILL_DIRECTORY)

    if not os.path.isdir(directory):
        return None

    # Ordered by time, then by the -2, -3... suffix of reports written
    # in the same second (plain text order would put "-2" first)
    found = []
    for name in os.listdir(directory):
        match = DRILL_PATTERN.match(name)
        if match:
            found.append((match.group(1), int(match.group(2) or 1), name))

    for _, _, name in sorted(found, reverse=True):
        try:
            with open(os.path.join(directory, name), "rb") as handle:
                return json.loads(handle.read(1024 * 1024))
        except (OSError, ValueError):
            continue

    return None


# ---------------------------------------------------
# RETENTION
# ---------------------------------------------------

def plan_retention(backups, *, keep_daily=7, keep_weekly=4, keep_monthly=6,
                   now=None):
    """
    Decide which backups to keep: the newest of each of the last
    `keep_daily` days, `keep_weekly` ISO weeks and `keep_monthly` months
    that have a backup. The newest backup is always kept.

    `backups` is [(created_at, path), ...]. Returns (keep, remove), both
    lists of paths. Nothing is deleted here.
    """

    if not backups:
        return [], []

    ordered = sorted(backups, reverse=True)
    keep = {ordered[0][1]}

    for count, key in (
        (keep_daily, lambda moment: moment.date()),
        (keep_weekly, lambda moment: moment.isocalendar()[:2]),
        (keep_monthly, lambda moment: (moment.year, moment.month)),
    ):
        seen = []
        for created, path in ordered:
            period = key(created)
            if period not in seen:
                if len(seen) >= count:
                    break
                seen.append(period)
                keep.add(path)

    keep_list = [path for _, path in ordered if path in keep]
    remove_list = [path for _, path in ordered if path not in keep]
    return keep_list, remove_list


def remove_backup(path):
    """Delete one backup and its manifest."""

    for target in (path, manifest_path_for(path)):
        try:
            os.unlink(target)
        except FileNotFoundError:
            pass


def backup_age(directory, now=None):
    """Seconds since the newest backup in `directory`, or None if none."""

    latest = latest_backup(directory)

    if latest is None:
        return None

    return ((now or _now()) - latest[0]).total_seconds()

