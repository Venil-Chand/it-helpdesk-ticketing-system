"""
Audit retention, step 1: archive and verify. Nothing is ever deleted here.

The provisional retention policy keeps audit events for 12 months
(RETENTION_DAYS), pending the final privacy policy. audit_events is
append-only (migration 8 triggers), and this module keeps it that way:

    export_archive()   copies every event older than the cutoff into a
                       JSON Lines file plus a manifest holding its SHA-256,
                       and records an "audit_archived" event saying who
                       archived what. The database is not changed
                       otherwise.
    verify_archive()   proves an archive is intact and that every event in
                       it still matches the database row with the same id.

Removing archived rows from the database is deliberately NOT implemented.
The proposed design (an authorised, recorded purge that only removes rows
matching a verified archive) is described in docs/AUDIT_RETENTION.md and
needs explicit approval first.

Archives contain usernames and client addresses: they are created readable
only by their owner (mode 0600) and must be stored encrypted.
"""

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import accounts
import database as db
import migrations

ARCHIVE_FORMAT = "helpdesk-audit-archive-v1"

# Provisional: 12 months, pending the final privacy policy
RETENTION_DAYS = 365

COLUMNS = (
    "id", "created_at", "actor_user_id", "actor", "action",
    "target_user_id", "target_username", "old_value", "new_value",
    "source_address",
)

# The format CURRENT_TIMESTAMP stores, which compares correctly as text
STORED_FORMAT = "%Y-%m-%d %H:%M:%S"


class ArchiveError(Exception):
    """The archive could not be written or is not trustworthy."""


@dataclass
class ArchiveResult:
    path: str
    manifest_path: str
    count: int
    first_id: int
    last_id: int
    sha256: str


@dataclass
class VerifyResult:
    count: int = 0
    in_database: int = 0
    no_longer_in_database: int = 0
    problems: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.problems


def manifest_path_for(path):
    return f"{path}.manifest.json"


def default_cutoff(now=None):
    """The moment before which events are older than the retention period."""

    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=RETENTION_DAYS)).strftime(STORED_FORMAT)


def parse_cutoff(text):
    """Accept YYYY-MM-DD or YYYY-MM-DD HH:MM:SS (UTC); return stored form."""

    for pattern in (STORED_FORMAT, "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).strftime(STORED_FORMAT)
        except ValueError:
            continue

    raise ArchiveError(
        "Use a UTC date as YYYY-MM-DD or YYYY-MM-DD HH:MM:SS."
    )


def _encode(event):
    """One event as a canonical JSON line (stable key order)."""

    return json.dumps(
        {name: event[name] for name in COLUMNS},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":")
    ) + "\n"


def _create_private(path):
    """Open a new file readable only by its owner; never overwrite."""

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")


def count_archivable(before):
    """(number of events older than `before`, oldest created_at or None)."""

    conn = db.get_db()

    try:
        row = conn.execute(
            "SELECT COUNT(*), MIN(created_at) FROM audit_events "
            "WHERE created_at < ?",
            (before,)
        ).fetchone()
        return row[0], row[1]

    finally:
        conn.close()


def export_archive(path, *, before, operator, now=None):
    """
    Write every event created before `before` to `path` and a manifest.

    Returns an ArchiveResult, or None if there is nothing to archive (no
    file is written then). The rows are read and the "audit_archived"
    event recorded inside one write transaction, so the archive is exactly
    what the database held at that moment. On any failure the transaction
    is rolled back and partly written files are removed.
    """

    manifest_path = manifest_path_for(path)

    for target in (path, manifest_path):
        if os.path.exists(target):
            raise ArchiveError(f"{target} already exists; choose a new name.")

    created = []
    conn = db.get_db()

    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")

            events = conn.execute(
                f"""
                SELECT {", ".join(COLUMNS)} FROM audit_events
                WHERE created_at < ?
                ORDER BY id
                """,
                (before,)
            ).fetchall()

            if not events:
                return None

            digest = hashlib.sha256()

            with _create_private(path) as archive:
                created.append(path)

                for event in events:
                    line = _encode(event)
                    archive.write(line)
                    digest.update(line.encode("utf-8"))

            result = ArchiveResult(
                path=path,
                manifest_path=manifest_path,
                count=len(events),
                first_id=events[0]["id"],
                last_id=events[-1]["id"],
                sha256=digest.hexdigest()
            )

            manifest = {
                "format": ARCHIVE_FORMAT,
                "created_at": (now or datetime.now(timezone.utc)).strftime(
                    STORED_FORMAT
                ),
                "created_by": f"cli:{operator}",
                "events_before": before,
                "count": result.count,
                "first_id": result.first_id,
                "last_id": result.last_id,
                "sha256": result.sha256,
                "schema_version": migrations.SCHEMA_VERSION,
                "columns": list(COLUMNS),
            }

            with _create_private(manifest_path) as handle:
                created.append(manifest_path)
                json.dump(manifest, handle, indent=2, sort_keys=True)
                handle.write("\n")

            accounts.record_audit_event(
                conn,
                actor=f"cli:{operator}",
                action=accounts.AUDIT_ARCHIVED,
                new_value=(
                    f"{result.count} events before {before} "
                    f"(ids {result.first_id}-{result.last_id}, "
                    f"sha256 {result.sha256})"
                )
            )

        return result

    except BaseException:
        for leftover in created:
            try:
                os.remove(leftover)
            except OSError:
                pass
        raise

    finally:
        conn.close()


def verify_archive(path):
    """
    Check an archive against its manifest and against the database.

    Problems are reported, never fixed. An event that is in the archive
    but no longer in the database is counted, not treated as a problem:
    that is what a future, approved purge would produce.
    """

    result = VerifyResult()

    try:
        with open(manifest_path_for(path), encoding="utf-8") as handle:
            manifest = json.load(handle)

        with open(path, "rb") as handle:
            data = handle.read()

    except (OSError, ValueError) as error:
        result.problems.append(f"cannot read archive: {error}")
        return result

    if manifest.get("format") != ARCHIVE_FORMAT:
        result.problems.append("unknown archive format")
        return result

    if hashlib.sha256(data).hexdigest() != manifest.get("sha256"):
        result.problems.append("archive checksum does not match manifest")

    try:
        events = [json.loads(line) for line in data.decode().splitlines()]
    except (UnicodeDecodeError, ValueError):
        result.problems.append("archive is not valid JSON Lines")
        return result

    result.count = len(events)

    # Every line must be one event with exactly the archived columns and
    # an integer id; anything else (an edited or injected line) is
    # reported, never compared or sorted
    if any(
        not isinstance(event, dict)
        or set(event) != set(COLUMNS)
        or type(event["id"]) is not int
        for event in events
    ):
        result.problems.append("archive contains malformed events")
        return result

    ids = [event["id"] for event in events]

    if result.count != manifest.get("count"):
        result.problems.append("event count does not match manifest")

    if ids != sorted(set(ids)) or (ids and (
        ids[0] != manifest.get("first_id")
        or ids[-1] != manifest.get("last_id")
    )):
        result.problems.append("event ids are missing, repeated or "
                               "out of order")

    cutoff = manifest.get("events_before", "")

    if any(str(event.get("created_at")) >= cutoff for event in events):
        result.problems.append("archive holds events newer than its cutoff")

    conn = migrations._connect_read_only(db.DB_PATH)

    try:
        for event in events:
            row = conn.execute(
                f"SELECT {', '.join(COLUMNS)} FROM audit_events "
                "WHERE id = ?",
                (event.get("id"),)
            ).fetchone()

            if row is None:
                result.no_longer_in_database += 1
            elif dict(zip(COLUMNS, row)) != event:
                result.problems.append(
                    f"event {event.get('id')} differs from the database"
                )
            else:
                result.in_database += 1

    finally:
        conn.close()

    return result
