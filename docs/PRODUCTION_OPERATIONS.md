# Production Operations Guide

How to run the IT Helpdesk on a Linux server, keep it running, back it
up, recover it, and know when something is wrong.

> **Status.** Everything here is prepared and tested on a development
> machine. The helpdesk **has not been deployed**, no service has been
> installed, and the Caddy configuration has not been run through
> `caddy validate` (no Caddy binary was available while it was written).
> Passing tests do not make the system production-ready; see
> [Release-readiness risks](#15-release-readiness-risks) and
> [Remaining infrastructure requirements](#14-remaining-infrastructure-requirements).

Contents

1. [The deployment in plain words](#1-the-deployment-in-plain-words)
2. [Recommended infrastructure](#2-recommended-infrastructure)
3. [Server configuration](#3-server-configuration)
4. [Environment variables and secrets](#4-environment-variables-and-secrets)
5. [Gunicorn and Caddy](#5-gunicorn-and-caddy)
6. [Redis (shared rate limits)](#6-redis-shared-rate-limits)
7. [SQLite backup and recovery](#7-sqlite-backup-and-recovery)
8. [Notification processing](#8-notification-processing)
9. [Monitoring](#9-monitoring)
10. [Deployment and rollback](#10-deployment-and-rollback)
11. [Security maintenance](#11-security-maintenance)
12. [Incident response](#12-incident-response)
13. [Troubleshooting](#13-troubleshooting)
14. [Remaining infrastructure requirements](#14-remaining-infrastructure-requirements)
15. [Release-readiness risks](#15-release-readiness-risks)

---

## 1. The deployment in plain words

```
  Browser ──HTTPS──▶ Caddy ──HTTP on 127.0.0.1:8000──▶ Gunicorn ──▶ Flask app ──▶ SQLite file
                     (port 443)                        (2+ workers)     │              /var/lib/helpdesk
                                                                         │
                                                                         └──▶ Redis (Unix socket)
                                                                              sign-in attempt counters

  Notification worker (separate process) ──reads queue──▶ SQLite ──▶ SMTP server (when enabled)
  Timers: nightly backup · weekly restore drill · ops-check every 5 minutes
```

- **Caddy** is the front door. It is the only program the internet can
  reach. It holds the HTTPS certificate (and renews it automatically),
  turns `http://` into `https://`, and passes each request to Gunicorn,
  adding two notes: who the visitor really is (`X-Forwarded-For`) and that
  they used HTTPS (`X-Forwarded-Proto`).
- **Gunicorn** runs the Python application. Flask's own server
  (`python app.py`) handles one request at a time and is meant only for
  development; Gunicorn runs several copies ("workers") so one slow
  request does not block everybody, restarts a worker that gets stuck, and
  stops cleanly. It listens on `127.0.0.1` only, so it cannot be reached
  from another machine.
- **The application** believes Caddy's two notes only because the
  connection comes from `127.0.0.1` (`TRUSTED_PROXY_IPS`). A visitor who
  writes those headers themselves gets them deleted.
- **Redis** is a small in-memory store. The application uses it for one
  thing: counting sign-in and password-change attempts per visitor address
  ("5 per minute"). Each Gunicorn worker is a separate process with its
  own memory; without a shared counter, three workers would allow three
  times as many password guesses. Redis is reached through a Unix socket
  file, not the network.
- **SQLite** is the database: one file, `/var/lib/helpdesk/helpdesk.db`,
  outside the code folder so that replacing the code never touches data.
- **The notification worker** is a separate program that delivers queued
  notifications. The web application never sends email itself; it only
  writes a "please notify" row in the same transaction as the ticket
  change (the *transactional outbox*), so a crash can never lose or
  invent a notification.
- **systemd** (Linux's service manager) starts all of this at boot,
  restarts what crashes, and runs the timers.

## 2. Recommended infrastructure

| Item | Recommendation |
|---|---|
| Server | One Linux VM, Ubuntu 24.04 LTS, 2 vCPU, 2 GB RAM, 20 GB disk. SQLite and a few hundred users need nothing bigger. |
| Name and certificate | A DNS name (for example `helpdesk.example.org`) pointing at the server; Caddy obtains a certificate from Let's Encrypt automatically. An internal-only server can use Caddy's `tls internal` or your organisation's CA. |
| Firewall | Inbound 443 (and 80 for certificate issuance/redirects) from users, 22 from administrators only. Nothing else. |
| Redis | The distribution package (`redis-server`) on the same machine, Unix socket only. |
| Off-server backups | A second location on **different hardware**: a mounted network share, a removable disk, or an object-storage bucket synchronised by your own tooling. Required before go-live. |
| Mail | An SMTP relay or provider (decision still open; see docs/NOTIFICATIONS.md). |
| Alerting | Something that receives the alert hook (email via the server's relay, a chat webhook or a pager). |
| Time | NTP enabled (`timedatectl`); sessions, SLAs and backups all rely on the clock. |

## 3. Server configuration

Everything below is **to be done on the server, by an administrator**.
None of it has been done; it is the checklist for doing it.

```bash
# Service account: owns the data, cannot log in
sudo adduser --system --group --home /var/lib/helpdesk --shell /usr/sbin/nologin helpdesk

# Code (one folder per release, "current" points at the active one)
sudo mkdir -p /opt/helpdesk/releases

# Settings and secrets
sudo install -d -m 0750 -o root -g helpdesk /etc/helpdesk
sudo install -d -m 0700 -o root -g root /etc/helpdesk/credentials

# Backups (local copy) - private to the service account
sudo install -d -m 0700 -o helpdesk -g helpdesk /var/backups/helpdesk

# Packages
sudo apt install python3.13 python3.13-venv redis-server caddy sqlite3
```

File layout:

| Path | Owner, mode | Holds |
|---|---|---|
| `/opt/helpdesk/releases/<commit>/` | root, 0755 | One checked-out release with its own `.venv` |
| `/opt/helpdesk/current` | symlink | The active release |
| `/etc/helpdesk/helpdesk.env` | root:helpdesk, 0640 | Non-secret settings (`deploy/helpdesk.env.example`) |
| `/etc/helpdesk/credentials/*` | root, 0600 | Secrets, one per file |
| `/var/lib/helpdesk/helpdesk.db` | helpdesk, 0600 (dir 0700) | The database |
| `/var/lib/helpdesk/state/` | helpdesk, 0700 | Worker heartbeat |
| `/var/backups/helpdesk/` | helpdesk, 0700 | Encrypted backups and drill reports |
| `/etc/systemd/system/helpdesk-*` | root, 0644 | Units copied from `deploy/systemd/` |
| `/etc/caddy/Caddyfile` | root, 0644 | From `deploy/Caddyfile` |

The application code is owned by root and is read-only for the service:
a compromised application cannot rewrite itself. The units use systemd
sandboxing (`ProtectSystem=strict`, `NoNewPrivileges`, no capabilities,
private `/tmp`), so the service can write only its state directory and,
for the backup job, the backup directory.

## 4. Environment variables and secrets

Non-secret settings live in `/etc/helpdesk/helpdesk.env` (template:
`deploy/helpdesk.env.example`). Secrets never go there.

| Variable | Needed by | Example | Meaning |
|---|---|---|---|
| `APP_ENV` | all | `production` | Turns on every production check |
| `TRUSTED_HOSTS` | all | `helpdesk.example.org` | Accepted `Host` names |
| `TRUSTED_PROXY_COUNT` / `TRUSTED_PROXY_IPS` | web | `1` / `127.0.0.1` | Caddy on this machine |
| `HELPDESK_DATABASE_PATH` | all | `/var/lib/helpdesk/helpdesk.db` | Absolute path of the database; keeps data out of the code folder |
| `RATELIMIT_STORAGE_URI` (`_FILE`) | all | `redis+unix://helpdesk:…@/run/redis/redis-server.sock` | **Required in production.** See [Redis](#6-redis-shared-rate-limits) |
| `RATELIMIT_SINGLE_PROCESS` | — | `1` | Only with `RATELIMIT_STORAGE_URI=memory://` and exactly one worker |
| `SECRET_KEY` (`_FILE`) | all | 64 hex characters | Signs sessions and CSRF tokens |
| `HELPDESK_BIND` | web | `127.0.0.1:8000` | Gunicorn address; must be loopback |
| `WEB_CONCURRENCY` | web | `2` | Gunicorn workers (1–8) |
| `GUNICORN_TIMEOUT` | web | `30` | Seconds before a stuck worker is replaced |
| `NOTIFICATION_*` | worker | see docs/NOTIFICATIONS.md | Delivery; `local` = simulated |
| `NOTIFICATION_SMTP_PASSWORD` (`_FILE`) | worker | | SMTP password |
| `HELPDESK_BACKUP_DIR` | backup, ops-check | `/var/backups/helpdesk` | Local backup folder (0700, outside the code) |
| `HELPDESK_BACKUP_OFFSITE_DIR` | backup, ops-check | `/mnt/helpdesk-offsite` | Off-server copy (mounted) |
| `HELPDESK_BACKUP_KEY_FILE` | backup | `%d/backup-key` | Backup encryption key file |
| `HELPDESK_STATE_DIR` | worker, ops-check | `/var/lib/helpdesk/state` | Where the worker heartbeat is written |

**Secrets as files.** Every secret can be given as `NAME_FILE`, the path
of a file containing it, instead of `NAME`. The units use systemd's
`LoadCredential=`: systemd copies `/etc/helpdesk/credentials/secret-key`
into a private, read-only directory visible only to that one service and
points `SECRET_KEY_FILE` at it (`%d` is that directory). The secret is
then not in the process environment, where `systemctl show`, child
processes or crash reports could expose it. In production a secret file
readable by other accounts is refused, as is setting both `NAME` and
`NAME_FILE`. Errors name the variable, never the value or the path.

Create the credentials (as root):

```bash
umask 077
python3 -c "import secrets; print(secrets.token_hex(32))" > /etc/helpdesk/credentials/secret-key
printf 'redis+unix://helpdesk:%s@/run/redis/redis-server.sock\n' "$REDIS_PASSWORD" > /etc/helpdesk/credentials/redis-uri
# Backup key: generated by the application, then moved into place
sudo -u helpdesk /opt/helpdesk/current/.venv/bin/flask --app app backup-generate-key /var/lib/helpdesk/new-backup-key
mv /var/lib/helpdesk/new-backup-key /etc/helpdesk/credentials/backup-key && chown root:root /etc/helpdesk/credentials/backup-key
```

**Keep an offline copy of the backup key** (password manager, sealed
envelope in a safe). Without it no backup can ever be restored; with it,
anyone holding a backup file can read it. Never store it next to the
backups; the backup commands refuse a key inside the backup folders or
the code folder.

## 5. Gunicorn and Caddy

### Gunicorn (`deploy/gunicorn.conf.py`, unit `helpdesk-web.service`)

```bash
gunicorn --config deploy/gunicorn.conf.py wsgi:application
```

`wsgi.py` is the production entry point. Compared with `app:app` it
switches logs to one JSON object per line and runs the **startup checks**
before Gunicorn accepts a connection: the database exists, its schema is
current, and Redis answers. If one fails, the service does not start and
the journal says why (`journalctl -u helpdesk-web`).

| Setting | Value | Why |
|---|---|---|
| `bind` | `127.0.0.1:8000` | Loopback only; a non-loopback value is refused at startup |
| `workers` | 2 (`WEB_CONCURRENCY`) | More than one requires Redis rate limits; refused with `RATELIMIT_SINGLE_PROCESS=1` |
| `worker_class` | `sync` | Simple and predictable with SQLite |
| `preload_app` | on | Config/schema/Redis are checked once, in the master, before any worker exists |
| `timeout` | 30 s | A stuck worker is killed and replaced |
| `graceful_timeout` | 30 s | On stop/restart, requests in progress may finish |
| `max_requests` | 1000 ± 100 | Workers are recycled, limiting any slow leak |
| `forwarded_allow_ips`, `secure_scheme_headers`, `forwarder_headers` | empty | Gunicorn trusts no forwarded header; the application's `TrustedProxyMiddleware` alone decides (Gunicorn's default would trust `X-Forwarded-Proto` from 127.0.0.1) |
| `limit_request_*` | 4094 / 100 / 8190 | Oversized request lines and headers refused |
| `control_socket_disable` | on | Gunicorn 26's control socket is not needed |
| access log | no query strings | Search text can be personal; `rid=` matches `X-Request-ID` |

systemd: `Type=notify` (Gunicorn reports when it is ready), `KillMode=mixed`
with `TimeoutStopSec=40` (SIGTERM, then 30 s for requests to finish),
`Restart=on-failure`, and at most 5 starts in 5 minutes so a broken
configuration is not retried forever. `ExecStartPre=flask db-status`
refuses to start on an unmigrated database.

**Application errors.** An unexpected exception returns a plain-text 500
that says only "Something went wrong … quote reference `<request id>`".
The full traceback goes to the journal, tagged with the same request ID,
so a user's report can be matched to the log line without them sending
screenshots of ticket data. Every response carries `X-Request-ID`; IDs
are generated by the server, never taken from the client.

### Caddy (`deploy/Caddyfile`)

- Automatic HTTPS and HTTP→HTTPS redirect.
- `reverse_proxy 127.0.0.1:8000`; Caddy sets `X-Forwarded-For` and
  `X-Forwarded-Proto` itself and, with no `trusted_proxies`, discards
  values sent by visitors. `X-Forwarded-Host` is removed.
- Request bodies over 1 MB refused before reaching Python.
- Timeouts against slow clients; a 35 s response timeout to Gunicorn.
- `/readyz` answers 404 to anything but the server itself.
- Access log with query strings and `Referer` removed, rotated after
  50 MiB and kept 14 days.

Validate on the server before every reload:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

What was tested instead of running Caddy: real Gunicorn processes were
started with `deploy/gunicorn.conf.py` and sent exactly the headers Caddy
sends (`tests/test_production_server.py`): HTTPS pages and `__Host-`
Secure cookies through the proxy, plain HTTP refused, unknown hosts
refused, forwarded headers ignored from an untrusted address (including
Gunicorn's own `X-Forwarded-Ssl`/`-Protocol`), the rightmost
`X-Forwarded-For` used for rate limits, no query strings in the access
log, graceful SIGTERM, and refusal to start on a bad configuration.

## 6. Redis (shared rate limits)

**Why.** "5 sign-in attempts per minute per address" must be counted in
one place. In-memory counters exist once per Gunicorn worker, so N workers
would allow N × 5 attempts.

**Rules enforced by the application:**

- Production refuses to start without `RATELIMIT_STORAGE_URI`. There is
  **no silent fallback** to memory.
- `memory://` in production only with `RATELIMIT_SINGLE_PROCESS=1`, and
  Gunicorn then refuses more than one worker.
- `redis://` (unencrypted) is accepted only for `127.0.0.1`, `::1` or
  `localhost`; another machine needs `rediss://` (TLS). `redis+unix://`
  (a socket file) is recommended.
- If Redis stops answering, sign-in and password changes return **503**
  ("temporarily unavailable") within about a second. The attempt is
  **not processed**: no password is checked, nothing is recorded, nobody
  is signed in. This fails closed: an outage can lock people out
  briefly, but it can never allow unlimited guessing. Other pages keep
  working.
- Every key starts with `LIMITS:LIMITER/helpdesk/`; the Redis password and
  address never appear in errors or logs.

**Server setup.** Merge `deploy/redis/redis-helpdesk.conf` into
`/etc/redis/redis.conf` (no TCP listener at all: `port 0`; socket
`/run/redis/redis-server.sock` with mode 770; no persistence; 64 MB
limit), install `deploy/redis/users.acl` as `/etc/redis/users.acl` (0600
redis:redis) with a real password, add the service account to the
`redis` group, restart Redis.

The ACL disables the default user and gives `helpdesk` only the commands
the limiter uses, on its own keys. This exact rule set was checked
against the application on Redis 7.0: limiting and the readiness ping
work; `GET` of other keys, `SET`, `KEYS`, `FLUSHALL` and `CONFIG` are
refused. Redis 7.2+ also needs `+client|setinfo`.

Redis holds only counters that expire after a minute: it needs no backup.
A Redis restart resets the counters (a visitor gets a fresh 5 attempts),
which is acceptable.

**Local development** needs nothing: `memory://` is the default outside
production.

## 7. SQLite backup and recovery

### How a backup is made

```bash
flask --app app backup-create          # helpdesk-backup.service runs this nightly
```

1. **Consistent copy while running.** SQLite's online backup API copies
   the live database (opened read-only) into memory. It includes every
   committed transaction, also those still in a WAL file if WAL mode is
   ever enabled, and nothing half-done. The copy is marked as a plain
   rollback-journal database so it is one self-contained file. The
   unencrypted copy never touches the disk.
2. **Checked** with `PRAGMA integrity_check`, `foreign_key_check` and the
   schema version. A damaged database is **not** backed up (the job fails
   and alerts) rather than silently overwriting good backups with bad.
3. **Encrypted** with AES-256-GCM and a random nonce, using the key file.
   GCM also authenticates: one changed byte anywhere makes decryption fail.
4. **Written atomically** (temporary file, flushed, linked into place,
   mode 0600) with a manifest `.manifest.json` holding sizes, SHA-256
   checksums, schema version and row counts.
5. **Read back and verified** before success is reported.

Backups are refused inside the application folder (and so inside the Git
repository), and in a directory other accounts can read.

**What a backup file reveals without the key:** nothing but its size. The
manifest holds only dates, checksums, the key id, the schema version and
per-table row counts: no names, addresses, ticket text, password hashes
or secrets (tested by searching the files for known values).

### Commands

| Command | Does | Changes data? |
|---|---|---|
| `backup-generate-key PATH` | New random key, mode 0600, refuses to overwrite | — |
| `backup-create` | Encrypted, verified backup | No (reads the DB) |
| `backup-verify FILE` | Checksum, decrypt, integrity, FK, manifest | No |
| `backup-list` | Backups and the newest one's age | No |
| `backup-transfer [FILE]` | Copy the newest (or FILE) to `HELPDESK_BACKUP_OFFSITE_DIR`, then decrypt and check **the copy** | No |
| `backup-restore FILE --to NEW` | Restore into a **new** file; the live database (by path, symlink or `-wal`/`-shm` name) is always refused | No |
| `backup-drill [FILE]` | Automated restoration drill (below) | No |
| `backup-prune [--apply]` | Retention policy; a dry run without `--apply` | Deletes old backups only |
| `backup-restore-live FILE` | Replace the live database (see below) | **Yes, after confirmation** |

### Schedule and retention

| What | When | Unit |
|---|---|---|
| Backup + off-server copy + prune | Nightly ≈02:15 | `helpdesk-backup.timer` |
| Restoration drill | Sunday ≈04:00 | `helpdesk-backup-drill.timer` |
| Check backups are fresh | Every 5 min | `helpdesk-ops-check.timer` |

Retention (`backup-prune --apply`): the newest backup of each of the last
**7 days**, **4 weeks** and **6 months**, and always the newest backup.
That is at most 17 files locally. The off-server location keeps its own
copies; apply your organisation's retention there (it is not pruned by
the application). Old backups contain old personal data: keeping them
longer than six months needs a reason and a decision, like the audit
retention in docs/AUDIT_RETENTION.md.

Recovery objectives with this schedule: at most **24 hours** of data can
be lost (RPO); restoring takes **minutes** (the drill reports the time).
If 24 hours is too much, run the backup more often (the timer's
`OnCalendar=`), since a backup of a small database takes seconds.

### The restoration drill

`flask --app app backup-drill` (weekly by timer):

1. picks the newest backup (or the FILE given),
2. verifies its checksum and decrypts it,
3. restores it into a new private temporary directory,
4. checks integrity, foreign keys, row counts against the manifest, the
   application's own schema check, a few application queries, and that
   the file is writable,
5. deletes the temporary copy, whatever happened,
6. saves a report in `<backup dir>/drills/` (no personal data) that
   `ops-check` reads, and exits 1 if anything failed.

The live database is never opened.

### Restoring the live database

Only for a real recovery. The command:

- needs an interactive terminal and the typed phrase
  `replace helpdesk.db`;
- fully verifies the backup first;
- refuses while the database is in use (a `-wal`, `-shm` or `-journal`
  file beside it, or a lock it cannot take);
- saves an encrypted **safety backup** of the database it is replacing;
- writes the restored file next to the live one and swaps it in with one
  atomic rename.

```bash
sudo systemctl stop helpdesk-web helpdesk-notifications helpdesk-backup.timer
sudo -u helpdesk -E /opt/helpdesk/current/.venv/bin/flask --app app backup-verify /var/backups/helpdesk/helpdesk-<time>.hdbk
sudo -u helpdesk -E /opt/helpdesk/current/.venv/bin/flask --app app backup-restore-live /var/backups/helpdesk/helpdesk-<time>.hdbk
sudo -u helpdesk -E /opt/helpdesk/current/.venv/bin/flask --app app db-status   # init-db if it says outdated
sudo systemctl start helpdesk-web helpdesk-notifications helpdesk-backup.timer
curl -fsS https://helpdesk.example.org/healthz
sudo -u helpdesk -E /opt/helpdesk/current/.venv/bin/flask --app app ops-check
```

(`-E` keeps the environment from `/etc/helpdesk/helpdesk.env`; load it
first with `set -a; . /etc/helpdesk/helpdesk.env; set +a`, and point
`HELPDESK_BACKUP_KEY_FILE`, `SECRET_KEY_FILE` and
`RATELIMIT_STORAGE_URI_FILE` at the credential files.)

Everything written after the backup was taken is lost; it remains only in
the safety backup.

### Off-server storage

`backup-transfer` copies to any mounted directory and **verifies the copy
by decrypting it at the destination**. Typical choices: an NFS/SMB share
on another server, a USB disk rotated off-site, or a local folder that
your own tooling (for example `rclone sync`, `restic`) uploads to object
storage. Backups are already encrypted, so the remote side never sees
plaintext; still restrict who can read it. If you upload with another
tool, periodically download one file and run `backup-verify` on it.

## 8. Notification processing

The design (transactional outbox, retries, recipient checks) is in
docs/NOTIFICATIONS.md. Operating it:

- **Service:** `helpdesk-notifications.service` runs
  `flask notifications-send --loop`. `ExecStartPre` validates the delivery
  settings without connecting anywhere. Development and the default
  production template use `NOTIFICATION_DELIVERY=local` (simulated:
  messages are written to a folder, never sent).
- **Crashes:** `Restart=on-failure` after 30 s; at most 5 starts in 10
  minutes, then systemd gives up and the alert unit runs. A message being
  delivered when the worker died stays "processing" until its lease
  expires (5 min), is then recovered with the attempt counted ("it may
  have been sent"), and is retried after the normal backoff, not at once.
  Tested by killing a real worker process mid-delivery.
- **No retry storms:** at most `NOTIFICATION_MAX_ATTEMPTS` (5) deliveries
  per message, exponential backoff (60 s doubling to 1 h), give up after
  24 h, and the whole run pauses when the mail server is down (waiting up
  to 15 min between tries) instead of burning every message's attempts.
- **Stopping:** SIGTERM is honoured between messages
  (`TimeoutStopSec=150` covers a slow delivery).
- **Several workers** may run at once (tested with three processes: each
  message delivered exactly once), but one is enough.
- **Heartbeat:** the worker rewrites
  `$HELPDESK_STATE_DIR/notification-worker.json` (state running, paused
  or stopped; counts only) at least every few minutes. `ops-check` treats
  a missing or stale heartbeat as critical.
- **Monitoring:** `ops-check` reports the due backlog and its age,
  interrupted deliveries, permanent failures in the last 24 h, and
  queueing failures. `flask notifications-status` lists recent failures
  (no addresses).
- **Queueing failures:** if notifications for a ticket or account change
  cannot be queued (a bug), the change is still saved and an audit event
  `notification_queue_failed` ("new ticket 12 (ValueError)") is written
  **in the same transaction**, so the record exists exactly when the
  change does. It appears in the admin audit log under *Operations* and
  in `ops-check`.
- **Manual requeue** after fixing a cause (for example a wrong SMTP
  password):

  ```bash
  flask --app app notifications-status                      # find the ids
  flask --app app notifications-requeue 41 42 --reason "SMTP password fixed (INC-12)"
  ```

  Only *failed* notifications; the failed row and its history are kept
  and a new copy is queued with fresh attempts; each original at most 3
  times; every requeue is an audit event naming the operating-system
  account and the reason. Asks for confirmation unless `--yes`.

## 9. Monitoring

### Health endpoints

| Endpoint | Answers | Use |
|---|---|---|
| `GET /healthz` | `200 ok` if the process answers. Touches nothing else. | External uptime monitor; Caddy |
| `GET /readyz` | `200 {"status":"ready","checks":{"schema":"ok","database":"ok","rate_limit_storage":"ok"}}` or `503 … "fail"` | This server's monitoring only (Caddy returns 404 to others) |

They need no sign-in, set no cookie, and contain only check names and
ok/fail: no versions, paths, hostnames, counts, exception text or account
data (tested). The reason for a failure is in the journal. They obey the
production rules like any page: use `https://` and the real host name.

### ops-check

```bash
flask --app app ops-check          # text
flask --app app ops-check --json   # for a monitoring system
```

Exit 0 OK, 1 WARNING, 2 CRITICAL. The timer runs it every 5 minutes;
CRITICAL fails the unit, which starts the alert unit; warnings are kept
in the journal.

| Check | WARNING | CRITICAL |
|---|---|---|
| configuration, notification settings | simulated delivery in production | invalid notification settings |
| schema / database | DB inside the code folder | missing, unreadable, outdated, integrity damage, FK violations |
| rate_limit_storage | | Redis not answering (sign-in is being refused) |
| notification_backlog | oldest due ≥ 15 min | ≥ 60 min |
| notification_claims / failures / queueing | interrupted deliveries; failures or queueing failures in 24 h | |
| worker | paused (mail server down) | no heartbeat, stopped, or heartbeat > 20 min old |
| backups | | none, or newest > 26 h old (nightly job failing) |
| backups_offsite | not configured, missing, or > 26 h old | |
| restore_drill | none recorded, or last success > 8 days ago | last drill failed |
| disk | < 10 % free | < 3 % free |

### Alerts

`OnFailure=helpdesk-alert@%n.service` on the backup, drill, worker and
ops-check units writes a `daemon.crit` journal entry and runs
`/etc/helpdesk/alert-hook <unit>` if it exists. **Write that hook** for
your alerting (it is site-specific); it must not print secrets.

### Logs

Everything goes to the journal:

```bash
journalctl -u helpdesk-web -f                 # JSON lines from the app, Gunicorn access/error lines
journalctl -u helpdesk-web -o cat | jq 'select(.level=="ERROR")'
journalctl -u helpdesk-web | grep <request id>
journalctl -u helpdesk-notifications --since today
journalctl -t helpdesk-alert
```

Application log lines are one JSON object each (time, level, logger,
message, request_id, method, path, exception). A message containing a
line break cannot forge a second entry. Query strings, form data,
cookies and headers are never logged.

## 10. Deployment and rollback

### First deployment

1. Server configuration (section 3), Redis (6), credentials (4).
2. Release: `git clone` the reviewed tag into
   `/opt/helpdesk/releases/<commit>`, create `.venv` with Python 3.13,
   `pip install -r requirements.txt`, then `pip-audit -r requirements.txt`.
3. `ln -sfn /opt/helpdesk/releases/<commit> /opt/helpdesk/current`
4. Copy the units and the Caddyfile, adjust names, `systemctl daemon-reload`.
5. Database: `flask --app app init-db` (new) or copy the existing database
   to `/var/lib/helpdesk/helpdesk.db` and run
   `flask --app app init-db --backup-to /var/backups/helpdesk/pre-v<N>.db`.
   Create the first administrator with `setup_accounts.py`.
6. `flask --app app backup-create` and `backup-drill` once by hand.
7. Start: `systemctl enable --now helpdesk-web helpdesk-notifications
   helpdesk-backup.timer helpdesk-backup-drill.timer helpdesk-ops-check.timer`,
   `caddy validate` then `systemctl reload caddy`.
8. Verify: `curl -fsS https://helpdesk.example.org/healthz`,
   `ops-check` (expect only known warnings), sign in, create a test ticket.

### Updating (each release)

```bash
NEW=/opt/helpdesk/releases/<new-commit>
# 1. prepare the new release next to the running one (no downtime yet)
git clone --branch <tag> <repo> $NEW && python3.13 -m venv $NEW/.venv
$NEW/.venv/bin/pip install -r $NEW/requirements.txt
# 2. back up, verified
sudo systemctl start helpdesk-backup && journalctl -u helpdesk-backup -n 20
# 3. stop, migrate if needed, switch, start
sudo systemctl stop helpdesk-web helpdesk-notifications
(cd $NEW && sudo -u helpdesk -E .venv/bin/flask --app app db-status) \
  || (cd $NEW && sudo -u helpdesk -E .venv/bin/flask --app app init-db --backup-to /var/backups/helpdesk/pre-upgrade-$(date -u +%Y%m%dT%H%M%SZ).db)
sudo ln -sfn $NEW /opt/helpdesk/current
sudo systemctl start helpdesk-web helpdesk-notifications
# 4. verify
curl -fsS https://helpdesk.example.org/healthz && sudo -u helpdesk -E /opt/helpdesk/current/.venv/bin/flask --app app ops-check
```

Keep the previous release folder until the new one has run for a while.

### Rolling back

- **No schema change in the release:** point `current` back at the
  previous release and restart both services. Seconds; no data lost.
- **The release migrated the schema:** the old code refuses the newer
  schema ("too_new"), by design. Either fix forward, or restore the
  pre-upgrade backup made by `init-db --backup-to` (an unencrypted,
  verified SQLite copy: protect and delete it like any backup) or the
  encrypted backup from step 2 with `backup-restore-live`, then switch
  `current` back. Anything written since the upgrade is lost.
- Rehearse a rollback on a copy before the first real upgrade.

## 11. Security maintenance

| Task | How often |
|---|---|
| OS updates (`unattended-upgrades` for security updates) | Automatic; review monthly |
| `pip-audit -r requirements.txt`; review Dependabot PRs | Weekly (CI runs it on every PR) |
| Rebuild the virtualenv with updated pins, deploy as a release | When an advisory affects a dependency |
| Review the admin audit log (Operations, Sign-ins) and `/var/log/auth.log` | Weekly |
| Check certificate renewal (`journalctl -u caddy | grep -i certificate`) | Monthly (Caddy renews automatically) |
| Restore a backup by hand into a test file and look at it | Quarterly, in addition to the weekly drill |
| Rotate `SECRET_KEY` (signs everyone out) | After any suspected exposure; otherwise yearly |
| Rotate the Redis password | Yearly, or when an administrator leaves |
| Rotate the backup key | Yearly: generate a new key, keep the old one (offline) until the last backup made with it has expired |
| Remove accounts of people who left | When they leave |

## 12. Incident response

1. **Contain.** Suspected account compromise: deactivate the account
   (admin page) or `reset-password`; both end its sessions. Suspected
   server compromise: take the server off the network (firewall) but do
   not power it off before preserving logs.
2. **Preserve evidence.** `journalctl --since … > incident.log`, the audit
   log (`audit-archive` copies it without deleting), `/var/log/auth.log`,
   Caddy access logs. Note times in UTC.
3. **Rotate secrets** that might be exposed: `SECRET_KEY` (signs everyone
   out), Redis password, SMTP password, and the backup key if a backup
   *and* the key may both be exposed.
4. **Recover.** If data was damaged or altered: stop services, verify a
   backup from before the incident, `backup-restore-live`, start, verify.
   The safety backup keeps the damaged state for investigation.
5. **Notify.** Personal data is involved (names, email addresses, ticket
   text). Your organisation's data-protection obligations may require
   notifying people or a regulator within a fixed time; that decision
   belongs to the data owner, not to this guide.
6. **Learn.** Write down what happened and what will change.

Quick reference:

| Symptom | First step |
|---|---|
| Site down | `systemctl status helpdesk-web caddy`; `journalctl -u helpdesk-web -n 50` |
| "Signing in … temporarily unavailable" | Redis: `systemctl status redis-server`; `/readyz` shows `rate_limit_storage: fail` |
| 503 "database has not been initialised or upgraded" | `flask --app app db-status`, then `init-db` (back up first) |
| No notifications arriving | `ops-check` (worker, backlog); `notifications-status`; is delivery still `local`? |
| Backup alert | `journalctl -u helpdesk-backup -n 50`; disk space; key file permissions |
| Drill alert | `journalctl -u helpdesk-backup-drill`; run `backup-verify` on the newest backup |

## 13. Troubleshooting

| Message (journal) | Meaning |
|---|---|
| `RATELIMIT_STORAGE_URI is required in production` | Set it (Redis), or memory with `RATELIMIT_SINGLE_PROCESS=1` and `WEB_CONCURRENCY=1` |
| `RATELIMIT_SINGLE_PROCESS=1 allows exactly one worker` | Gunicorn config: set `WEB_CONCURRENCY=1` or use Redis |
| `HELPDESK_BIND must be a loopback address` | Gunicorn must stay on 127.0.0.1 |
| `Startup check failed: rate_limit_storage …` | Redis down, wrong socket path, or the service account is not in the `redis` group |
| `Startup check failed: database file does not exist` | `HELPDESK_DATABASE_PATH` wrong, or `init-db` not run |
| `SECRET_KEY_FILE must not be readable by other accounts` | `chmod 600` the credential file |
| `This site must be accessed over HTTPS` (400) | The request did not come through Caddy, or `TRUSTED_PROXY_IPS` does not match Caddy's address |
| `backup directory must not be accessible to other accounts` | `chmod 700 /var/backups/helpdesk` |
| `this backup was encrypted with a different key (key id …)` | Use the matching (older) key with `--key-file` |
| `the backup is damaged or has been altered` | Do not use it; try the previous backup or the off-server copy |
| `another process is using the database right now` | Stop the web and worker services before a live restore |
| A user quotes "reference 3f9c…" | `journalctl -u helpdesk-web | grep 3f9c…` |

## 14. Remaining infrastructure requirements

Not provided by this repository; each must exist before go-live:

1. A Linux server, DNS name, and firewall as in section 2.
2. Redis installed and configured as in section 6 (socket only, ACL).
3. An **off-server backup location** on separate hardware, mounted at
   `HELPDESK_BACKUP_OFFSITE_DIR`, with its own access control and
   retention.
4. An **alert hook** (`/etc/helpdesk/alert-hook`) connected to something a
   person reads; and an external uptime check of `/healthz`.
5. A decision and account for **SMTP delivery** (until then notifications
   stay simulated).
6. A secure, **offline copy of the backup key**, and a named person who
   can reach it.
7. Log retention for the journal (`SystemMaxUse=` in journald.conf) and
   for Caddy logs consistent with your data-protection policy.
8. `caddy validate` of the Caddyfile on the real server, and an HTTPS
   test from outside (for example SSL Labs) after the first deployment.
9. Named administrators with personal SSH accounts and sudo, and a
   tested way to reach the server when the site is down.

## 15. Release-readiness risks

The automated tests pass. That shows the code does what it was designed
to do on a development machine; it does **not** show the system is ready
for production. Known risks:

- **Never deployed.** The systemd units and Redis settings are reviewed
  examples, syntax-checked (`systemd-analyze verify`) but never run on a
  server. The Caddyfile is formatted, adapted and validated by the real
  Caddy (2.11.9) in CI, and real Caddy in front of real Gunicorn passes 14
  proxy-boundary checks there, but never with a real certificate or DNS
  name.
- **Single server, single database file.** No high availability; a
  server failure means restoring from backup onto another machine
  (minutes to hours, plus up to 24 h of lost data with nightly backups).
- **SQLite write concurrency.** Writes are serialised. Fine for a small
  helpdesk: 8 processes writing at once completed 960 writes without an
  error, and a load smoke test served 350–800 pages per second (see
  docs/RELEASE_READINESS_1.0.0-rc1.md). Neither was run on the target
  server. Readers and writers in rollback-journal mode can briefly block
  each other.
- **Browser tests cover the main workflows (17, in CI); no penetration
  test** has been carried out.
- **Redis outage locks out sign-in** (by design: fail closed). Users
  already signed in are unaffected.
- **The backup key is a single point of failure** for recovery, and its
  offline copy is a manual procedure.
- **Off-server copy depends on a mounted directory**; if the mount
  disappears, `ops-check` warns but the local backup continues.
- **Operational events share the audit log.** Queueing failures and
  requeues are written to `audit_events` (no schema change was needed);
  they are filtered into their own "Operations" tab but are archived with
  the account audit trail. A dedicated table would need a migration.
- **SMTP delivery has never been run against a real provider** in this
  project (by rule: no real email during development).
- **Business decisions still open:** SMTP provider, notification
  policies (docs/NOTIFICATIONS.md), SLA policy (README), data-retention
  periods for backups and logs.
