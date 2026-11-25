# Audit retention and archiving

**Status:** step 1 (archive and verify) is implemented. Step 2 (removing archived events from the live database) is **designed but not implemented**, and needs explicit approval before any code is written.

**Provisional policy:** keep audit events for **12 months** (`audit_archive.RETENTION_DAYS = 365`), pending the final privacy policy.

## What is in the audit log

`audit_events` (migration 8, extended by migration 9) records:

| Action | Recorded by |
|---|---|
| `account_created`, `role_changed`, `account_deactivated`, `account_reactivated`, `password_reset`, `password_changed` | Account administration, password changes, `setup_accounts.py`, `flask reset-password` |
| `login_succeeded`, `login_failed` | The login page |
| `audit_archived` | `flask audit-archive` |

Each event holds the time in UTC, the actor and target (id and username), old and new values where relevant, and, for sign-ins, the client address (`source_address`).

It never holds passwords, password hashes or session cookies. A username that matches no account is not stored, because it may be a password typed into the wrong box.

Personal data in the log: usernames, client IP addresses, and the times people signed in. That is why retention is limited at all.

## Why the log cannot simply be trimmed

Migration 8 makes `audit_events` **append-only**. The triggers `audit_events_no_update` and `audit_events_no_delete` refuse every `UPDATE` and `DELETE`, including from the application itself. That is the property that makes the log worth having: nobody who can use the application can erase what they did.

A "delete events older than 12 months" job would need those triggers removed or bypassed. Anyone able to do that could also delete *recent* events, so the guarantee would be gone. This design therefore keeps the triggers and separates **archiving** (safe, implemented) from **removal** (needs a controlled, approved procedure).

## Step 1: archive and verify (implemented)

```bash
# How many events are older than 12 months? (read-only)
flask --app app audit-archive --dry-run

# Copy them to a new private file, with a manifest
flask --app app audit-archive --to /secure/archive/audit-2025.jsonl

# Prove the archive is intact and still matches the database (read-only)
flask --app app audit-verify-archive /secure/archive/audit-2025.jsonl
```

`audit-archive`:

- reads every event created before the cutoff (default: 12 months ago; `--before YYYY-MM-DD` to choose) inside one `BEGIN IMMEDIATE` transaction, so the archive is exactly what the database held at that moment
- writes them as canonical JSON Lines (one event per line, stable key order) plus `<file>.manifest.json`, which records the format, cutoff, count, first and last id, SHA-256, schema version and operator
- creates both files with mode `0600` and never overwrites an existing file
- records an `audit_archived` event naming the operator, the count, the id range and the SHA-256, in the same transaction
- **deletes nothing**. On any failure the transaction rolls back and partly written files are removed.

`audit-verify-archive` checks:

- the archive's SHA-256 against the manifest
- that every line is a well-formed event with exactly the archived columns
- that the count, id range and ordering match the manifest
- that no event is newer than the cutoff
- that every event still in the database is **identical** to its archived copy

It exits with status 1 on any problem and never writes to the database. An event that is in the archive but no longer in the database is counted separately; after step 2 that is expected.

### Storing archives

Archives contain personal data. Treat them like database backups:

- Encrypt them before they leave the server (for example `age` or `gpg` to a key held off the server). Package 3 adds encrypted off-server backups, and archives should use the same mechanism.
- Keep the manifest with the archive. Also record the SHA-256 somewhere separate (for example the change ticket), so an attacker who replaces both files can still be detected.
- Restrict who can read them, and delete them when the retention period for archives (still to be decided) ends.

## Step 2: controlled removal (designed, NOT implemented)

The proposed design, for approval:

1. **Only verified, archived events can be removed.** A new command, `flask audit-purge --archive FILE`, first runs the full verification and refuses unless every event in the archive is identical in the database. It removes exactly those ids and nothing else.
2. **The append-only triggers stay in place.** Removal goes through one narrowly scoped mechanism: a dedicated migration that replaces `audit_events_no_delete` with a trigger allowing a `DELETE` only when a matching row exists in a new `audit_purge_authorizations` table (archive SHA-256, id range, operator, time). That table is itself append-only. The command inserts the authorization and deletes the rows in **one transaction**, so the database itself records each removal.
3. **The removal is audited.** An `audit_purged` event records the archive SHA-256, the id range, the count and the operator. Purge events and authorizations are never purged themselves.
4. **Server access only.** Like `reset-password`, the command runs only from a shell on the server, never from the web application.
5. **A backup comes first.** The command requires a verified backup (`init-db --backup-to` style) written immediately before.

### Decisions needed before step 2

| Decision | Options |
|---|---|
| Final retention period for live events | 12 months (provisional), or as the privacy policy requires |
| Retention period for archives | For example, a further N years, or the same as the live log |
| Different periods per event type? | For example, keep account changes longer than sign-ins |
| Who may authorize a purge | A named role, and whether two people must approve |
| Is purging needed at all? | At pilot scale the table stays small, so archiving alone may be enough for now |

Until these are decided, **events stay in the database for ever**, and archiving is used only for off-server copies and evidence.

## Tests

`tests/test_audit_archive.py` uses temporary databases. It covers:

- export contents and the private file permissions
- the manifest and the `audit_archived` event
- refusal to overwrite, the nothing-to-archive case, and rollback with file cleanup on failure
- the append-only triggers after archiving
- verification: success, edited files, an edit with a recomputed checksum (caught by the database comparison), removed events, malformed or injected lines, and a missing manifest
- the 12-month cutoff and date parsing
- the command-line exit codes, and a check that no deletion or purge command exists
