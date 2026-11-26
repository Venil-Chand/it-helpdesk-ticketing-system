# Disaster Recovery Plan

**Status: draft for the pilot.** The recovery objectives below follow from
the current backup schedule and **need the sponsor's approval**. The
procedures were rehearsed on disposable files, **not yet on the pilot
server**.

## 1. What must be protected

| Asset | Where | Recovered from |
|---|---|---|
| Ticket, account and audit data | `/var/lib/helpdesk/helpdesk.db` | Encrypted backups (local and off-server) |
| Backup encryption key | `/etc/helpdesk/credentials/backup.key` | **Offline copy only**; without it every backup is unreadable |
| Secrets (session key, Redis, SMTP) | `/etc/helpdesk/credentials/` | Regenerate: a new session key signs everyone out; Redis and SMTP passwords are reset at their source |
| Configuration | `/etc/helpdesk/helpdesk.env`, Caddyfile, units | Repository `deploy/` folder, plus a record of local changes |
| Code | `/opt/helpdesk/releases/<version>` | The Git tag or commit |

Redis holds only rate-limit counters and needs no backup.

## 2. Recovery objectives (proposed)

| Objective | Proposed value | Based on |
|---|---|---|
| RPO (data that may be lost) | **24 hours** | Nightly backup. Shorten it by running `helpdesk-backup.timer` more often. |
| RTO (time to restore service) | **4 hours** on a prepared spare machine; longer if a server must be built | Restore itself takes minutes; most of the time is rebuilding the server. |

**Decision needed:** are 24 hours of lost tickets and 4 hours of downtime
acceptable for the pilot?

## 3. Scenarios

| # | Scenario | Response |
|---|---|---|
| D1 | Application bug or bad release | Roll back the release ([PRODUCTION_OPERATIONS.md §10](PRODUCTION_OPERATIONS.md#10-deployment-and-rollback)). If a migration ran, restore the pre-upgrade backup with `backup-restore-live`. |
| D2 | Database damaged (integrity check fails, disk error) | Stop services; `backup-verify` the newest backup; `backup-restore-live` ([BACKUP_AND_RESTORE.md §5](BACKUP_AND_RESTORE.md#5-replace-the-live-database-destructive)). |
| D3 | Data deleted or changed by mistake | Restore the backup from before the mistake into a **new** file (`backup-restore --to`), compare, and decide with the sponsor whether to replace the live database or copy records across by hand. |
| D4 | Server lost (hardware, VM deleted) | Build a new server (operations guide §2–6), restore the key from its offline copy, fetch the newest backup **and its manifest** from the **off-server** location, `backup-verify` it, then `backup-restore FILE --to /var/lib/helpdesk/restored.db` (the command always refuses the configured live path, even on an empty server). Check it with `db-status` (pointing `HELPDESK_DATABASE_PATH` at it), move it to `/var/lib/helpdesk/helpdesk.db` as the service account with the services stopped, start the services, and change DNS if the address changed. |
| D5 | Server compromised | Take it offline. Preserve logs. Build a new server from the repository (not from the old disk), rotate **every** secret (session key, Redis, SMTP, backup key for future backups), restore the newest backup made **before** the compromise, and reset user passwords. Follow §12 of the operations guide. |
| D6 | Backup key lost | Existing backups cannot be read. If the live database is intact: generate a new key, make and verify a new backup immediately, and store the new key offline. If both the database and the key are lost, **the data is unrecoverable**. |
| D7 | Redis down | Sign-in and password changes are refused (503), everything else works. Restart Redis; the application recovers without a restart (verified). |
| D8 | Notification worker stopped or SMTP down | Tickets keep working; messages queue up and are delivered when the worker or SMTP returns. Messages older than `NOTIFICATION_MAX_AGE_HOURS` are expired. `ops-check` alerts on both. |
| D9 | Certificate expired or DNS broken | Users cannot connect. Check Caddy logs and renewal; Caddy renews automatically while ports 80 and 443 are reachable. |

## 4. After any recovery

1. `flask --app app db-status` and `ops-check` both report OK.
2. Sign in as an administrator and as a user; open, comment on and
   export a ticket (the [UAT checklist](UAT_CHECKLIST.md) smoke-test rows).
3. Tell users which period of data was lost, if any.
4. Record what happened, how long it took, and what to change.

## 5. Testing this plan

| Test | Frequency | Status |
|---|---|---|
| Automated restoration drill (`backup-drill`) | Weekly (timer) | Rehearsed on disposable data; **enable on the server** |
| Manual live restore on a copy of the server | Before the pilot, then every 6 months | **Not done on the server** |
| Full rebuild from the off-server backup (D4) | Before the pilot | **Not done**: needs the off-server location |
| Key recovery from the offline copy (D6) | Before the pilot | **Not done** |
