"""
Backup and restore rehearsal: the real "flask" backup commands against a
disposable database filled by rehearsals/seed.py.

Covers key creation, encrypted backups, verification, listing, off-server
transfer, restoring into a new file, corruption, key problems, the
restoration drill, ops-check, and the interactive live restore (typed at
a pseudo-terminal exactly as an operator would). Needs no Redis.
"""

import os
import shutil
import sqlite3
import sys
from contextlib import closing

from .common import (Report, Workspace, new_password, private_dir, query,
                     row_counts, sha256)

TICKETS = 40


def _mode(path):
    return os.stat(path).st_mode & 0o777


def _backups(directory):
    return sorted(directory.glob("helpdesk-*.hdbk"))


def _output(result):
    return result.stdout + result.stderr


def run(keep=False):
    ws = Workspace("rehearsal-backup", keep=keep)
    report = Report("Backup and restore rehearsal", ws)
    try:
        _rehearse(ws, report)
    except Exception as error:  # report, never hide, an unexpected stop
        report.check("rehearsal finished without an unexpected error", False,
                     f"{type(error).__name__}: {error}")
    finally:
        ws.close()
    return report.finish()


def _rehearse(ws, report):
    check = report.check
    backup_dir = private_dir(ws.root / "backups")
    offsite_dir = private_dir(ws.root / "offsite")
    key_dir = private_dir(ws.root / "keys")
    key = key_dir / "backup.key"
    env = ws.env(HELPDESK_BACKUP_DIR=backup_dir,
                 HELPDESK_BACKUP_OFFSITE_DIR=offsite_dir,
                 HELPDESK_BACKUP_KEY_FILE=key)

    result = ws.flask("init-db", env=env)
    if not check("init-db creates the disposable database",
                 result.returncode == 0, _output(result)):
        return
    ws.seed(env, tickets=TICKETS, password=new_password())
    original = row_counts(ws.db_path)
    check(f"seeded data has rows in every table ({TICKETS} tickets)",
          original["tickets"] == TICKETS
          and all(original[t] for t in ("comments", "ticket_history",
                                        "audit_events", "notifications")),
          str(original))

    # --- key ------------------------------------------------------------
    result = ws.flask("backup-generate-key", str(key), env=env)
    check("backup-generate-key writes a key file with mode 0600",
          result.returncode == 0 and key.exists() and _mode(key) == 0o600,
          _output(result))
    ws.sensitive(key.read_text().strip())

    # --- create, verify, list -------------------------------------------
    result = ws.flask("backup-create", env=env)
    found = _backups(backup_dir)
    check("backup-create writes one verified backup",
          result.returncode == 0 and len(found) == 1, _output(result))
    if not found:
        return
    backup = found[0]
    manifest = backup.with_name(backup.name[:-len(".hdbk")]
                                + ".manifest.json")
    check("backup and manifest files have mode 0600",
          _mode(backup) == 0o600 and manifest.exists()
          and _mode(manifest) == 0o600)

    data = backup.read_bytes()
    check("the backup file contains no readable database content",
          b"SQLite format 3" not in data and b"Rehearsal ticket" not in data
          and b"example.invalid" not in data)

    result = ws.flask("backup-verify", str(backup), env=env)
    check("backup-verify accepts the backup", result.returncode == 0,
          _output(result))

    result = ws.flask("backup-list", env=env)
    check("backup-list shows the backup",
          result.returncode == 0 and backup.name in result.stdout,
          _output(result))

    result = ws.flask("backup-transfer", env=env)
    check("backup-transfer copies the newest backup off-server and "
          "verifies the copy",
          result.returncode == 0 and (offsite_dir / backup.name).exists(),
          _output(result))

    result = ws.flask("backup-prune", env=env)
    check("backup-prune is a dry run by default and keeps the backup",
          result.returncode == 0 and backup.exists(), _output(result))

    # --- restore into a new file ----------------------------------------
    restored = ws.data / "restored.db"
    result = ws.flask("backup-restore", str(backup), "--to", str(restored),
                      env=env)
    check("backup-restore --to restores into a new file",
          result.returncode == 0 and restored.exists(), _output(result))
    if restored.exists():
        check("restored row counts equal the original for every table",
              row_counts(restored) == original,
              f"{row_counts(restored)} != {original}")
        integrity = query(restored, "PRAGMA integrity_check")[0][0]
        check("restored copy passes integrity_check", integrity == "ok",
              integrity)

    live_hash = sha256(ws.db_path)
    result = ws.flask("backup-restore", str(backup), "--to",
                      str(ws.db_path), env=env)
    check("backup-restore refuses to write over the live database",
          result.returncode != 0 and sha256(ws.db_path) == live_hash,
          _output(result))

    # --- corruption -------------------------------------------------------
    corrupt_dir = private_dir(ws.root / "corrupt")

    def corrupted(name, change):
        damaged = corrupt_dir / backup.name
        shutil.copy2(backup, damaged)
        shutil.copy2(manifest, corrupt_dir / manifest.name)
        damaged.write_bytes(change(damaged.read_bytes()))
        target = ws.data / f"from-{name}.db"
        verify = ws.flask("backup-verify", str(damaged), env=env)
        restore = ws.flask("backup-restore", str(damaged), "--to",
                           str(target), env=env)
        check(f"corruption ({name}) is detected and not restored",
              verify.returncode != 0 and "does not match its manifest"
              in _output(verify) and restore.returncode != 0
              and not target.exists(),
              _output(verify) + _output(restore))

    middle = len(data) // 2
    corrupted("one flipped byte",
              lambda b: b[:middle] + bytes([b[middle] ^ 0x01]) + b[middle + 1:])
    corrupted("truncated", lambda b: b[:len(b) // 2])
    corrupted("empty file", lambda b: b"")

    # --- key problems -----------------------------------------------------
    def refused(name, extra_env, args=(), expect=""):
        target = ws.data / f"key-{len(report.results)}.db"
        result = ws.flask("backup-restore", str(backup), "--to", str(target),
                          *args, env={**env, **extra_env})
        check(name, result.returncode != 0 and not target.exists()
              and expect in _output(result), _output(result))

    refused("missing key file is refused",
            {"HELPDESK_BACKUP_KEY_FILE": str(key_dir / "missing.key")})
    refused("no key configured is refused",
            {"HELPDESK_BACKUP_KEY_FILE": ""}, expect="no backup key")

    other_key = key_dir / "other.key"
    ws.flask("backup-generate-key", str(other_key), env=env)
    ws.sensitive(other_key.read_text().strip())
    result = ws.flask("backup-verify", str(backup), "--key-file",
                      str(other_key), env=env)
    check("wrong key is refused and the backup's key id is named",
          result.returncode != 0 and "key id" in _output(result).lower(),
          _output(result))

    stored_with_backups = backup_dir / "kept-here.key"
    shutil.copy2(key, stored_with_backups)
    refused("key stored in the backup directory is refused",
            {}, ("--key-file", str(stored_with_backups)),
            expect="must not be stored")
    stored_with_backups.unlink()

    readable = key_dir / "readable.key"
    shutil.copy2(key, readable)
    os.chmod(readable, 0o644)
    refused("key readable by other accounts is refused",
            {}, ("--key-file", str(readable)), expect="chmod 600")

    # --- drill and monitoring ---------------------------------------------
    result = ws.flask("backup-drill", env=env)
    drills = list((backup_dir / "drills").glob("drill-*.json"))
    check("backup-drill passes and saves its report",
          result.returncode == 0 and "Restoration drill passed" in
          result.stdout and len(drills) == 1, _output(result))

    result = ws.flask("ops-check", "--json", env=env)
    import json
    try:
        checks = {item["name"]: item for item in
                  json.loads(result.stdout)["checks"]}
    except (ValueError, KeyError):
        checks = {}
    check("ops-check reports the backup age and the last drill as OK",
          checks.get("backups", {}).get("level") == "OK"
          and checks.get("restore_drill", {}).get("level") == "OK",
          _output(result))
    check("ops-check warns that no notification worker heartbeat exists",
          checks.get("worker", {}).get("level") in ("WARNING", "CRITICAL"),
          _output(result))

    # --- live restore -----------------------------------------------------
    _live_restore(ws, report, env, backup, original)


def _live_restore(ws, report, env, backup, original):
    check = report.check
    flask = [sys.executable, "-m", "flask", "--app", "app"]
    phrase = f"replace {ws.db_path.name}"
    safety_dir = ws.root / "backups"

    # Changes made after the backup, which the restore must undo
    with closing(sqlite3.connect(ws.db_path)) as conn:
        with conn:
            conn.execute("DELETE FROM comments WHERE id IN "
                         "(SELECT id FROM comments LIMIT 5)")
    changed = row_counts(ws.db_path)

    result = ws.flask("backup-restore-live", str(backup), env=env)
    check("backup-restore-live refuses without an interactive terminal",
          result.returncode != 0 and row_counts(ws.db_path) == changed,
          _output(result))

    holder = sqlite3.connect(ws.db_path, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        code, text = ws.at_terminal(
            flask + ["backup-restore-live", str(backup)], env,
            [('Type "', phrase)])
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    check("backup-restore-live refuses while the database is in use",
          code != 0 and "was not changed" in text
          and row_counts(ws.db_path) == changed, text)

    before = len(_backups(safety_dir))
    code, text = ws.at_terminal(
        flask + ["backup-restore-live", str(backup)], env,
        [('Type "', "replace something-else")])
    check("backup-restore-live with the wrong phrase changes nothing",
          code != 0 and "did not match" in text
          and row_counts(ws.db_path) == changed
          and len(_backups(safety_dir)) == before, text)

    code, text = ws.at_terminal(
        flask + ["backup-restore-live", str(backup)], env,
        [('Type "', phrase)])
    check("backup-restore-live after confirmation restores the backup",
          code == 0 and row_counts(ws.db_path) == original, text)
    check("a safety backup of the replaced database was written first",
          len(_backups(safety_dir)) == before + 1
          and "Safety backup" in text, text)

    result = ws.flask("db-status", env=env)
    check("restored live database is at the current schema (version 10)",
          result.returncode == 0
          and "Current schema version: 10 (up to date)" in result.stdout,
          _output(result))
    integrity = query(ws.db_path, "PRAGMA integrity_check")[0][0]
    violations = query(ws.db_path, "PRAGMA foreign_key_check")
    check("restored live database passes integrity and foreign-key checks",
          integrity == "ok" and not violations, f"{integrity} {violations}")
