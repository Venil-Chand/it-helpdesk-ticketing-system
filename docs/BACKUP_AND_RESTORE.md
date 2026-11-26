# Backup and Restoration Guide

Step-by-step procedures for operators. How backups work (snapshot,
encryption, manifest, schedule, retention) is explained in
[PRODUCTION_OPERATIONS.md §7](PRODUCTION_OPERATIONS.md#7-sqlite-backup-and-recovery).

Run every command **as the service account**, with the service's
environment (`HELPDESK_DATABASE_PATH`, `HELPDESK_BACKUP_DIR`,
`HELPDESK_BACKUP_KEY_FILE`, optionally `HELPDESK_BACKUP_OFFSITE_DIR`), from
the current release folder, as `flask --app app <command>`.

## Every procedure here was rehearsed

On 2026-11-27 each step below was run against throwaway databases with
real data. All 27 checks passed, including corruption, a missing key, a
wrong key and live replacement (see the
[readiness report §6.1](RELEASE_READINESS_1.0.0-rc1.md#61-backup-and-restoration-2727-checks-passed)).
**They have not yet been run on the production server**; do that before
the pilot.

## 1. One-time: create and protect the key

```bash
flask --app app backup-generate-key /etc/helpdesk/credentials/backup.key
```

* The key file is created with mode 0600 and is never overwritten.
* It must **not** live in the application folder or a backup folder (the
  commands refuse it there) and must not be readable by other accounts.
* **Make an offline copy now** (password manager or sealed envelope) and
  write down who can reach it. Backups cannot be decrypted without this
  key. A copy stored next to the backups protects nothing.

## 2. Make and check a backup

```bash
flask --app app backup-create        # encrypted, verified, 0600
flask --app app backup-list          # newest first, with its age
flask --app app backup-verify FILE   # checksum, decrypt, integrity, manifest
flask --app app backup-transfer      # copy the newest off-server and check the copy
```

Each backup is two files: `helpdesk-<UTC time>.hdbk` (encrypted database)
and `helpdesk-<UTC time>.manifest.json` (sizes, checksums, schema, row
counts, key id). **Always copy both.** Without the manifest a backup is
refused.

The nightly timer (`helpdesk-backup.timer`) does create + transfer +
prune automatically.

## 3. Weekly drill (automatic, and before the pilot by hand)

```bash
flask --app app backup-drill
```

It restores the newest backup into a disposable database, checks it, runs
the application's own queries against it, and deletes it. The live
database is never opened. The result is saved for `ops-check`, which
warns if the last drill failed or is too old.

## 4. Look at old data without touching the live database

```bash
flask --app app backup-restore FILE --to /tmp/inspect/helpdesk.db
```

The target must be a **new** file. The live database is always refused,
whether given by path, symlink or WAL name.

## 5. Replace the live database (destructive)

Use this only when the live database is lost or damaged, or a release
must be undone to before a migration.

1. Decide which backup to restore (`backup-list`) and verify it
   (`backup-verify FILE`). Note what will be lost: everything since that
   backup.
2. Stop the services:
   ```bash
   sudo systemctl stop helpdesk-web helpdesk-notifications
   ```
3. Run, in an interactive terminal:
   ```bash
   flask --app app backup-restore-live FILE
   ```
   It refuses while the database is in use (open connection or a
   `-wal`/`-journal` file beside it), verifies the backup completely,
   writes an **encrypted safety backup of the current database first**,
   and asks you to type `replace helpdesk.db`. Anything else cancels.
4. Check and start:
   ```bash
   flask --app app db-status            # schema up to date
   sudo systemctl start helpdesk-web helpdesk-notifications
   curl -fsS https://<host>/healthz
   ```
5. Tell users what period of data was lost. Notifications queued after
   the backup are gone with it.

## 6. What the commands refuse, and what to do

| Message | Meaning | Action |
|---|---|---|
| `the backup file does not match its manifest (incomplete copy or altered file)` | The `.hdbk` file is truncated, damaged or altered | Use another backup (or the off-server copy); find out why the copy is damaged. |
| `the backup's manifest file is missing` | Only the `.hdbk` was copied | Copy the matching `.manifest.json` too. |
| `this backup was encrypted with a different key (key id ...)` | Wrong key file | Find the key with that id (key ids are in each manifest). |
| `backup key file could not be read` / `no backup key` | Key missing or not configured | Restore the key from its offline copy. **Without it the backups are unreadable**; see [disaster recovery](DISASTER_RECOVERY.md). |
| `backup key file must not be readable by other accounts (chmod 600)` | Permissions too open | `chmod 600` the key and check who could have read it. |
| `the backup key must not be stored in the application folder or in a backup directory` | Key next to the data it protects | Move it to `/etc/helpdesk/credentials/`. |
| `another process is using the database right now` | Services still running | Stop both services (step 5.2). |
| `confirmation did not match` | Phrase not typed exactly | Nothing changed; run again. |
