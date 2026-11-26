# Release-Readiness Report: IT Helpdesk 1.0.0-rc1

| | |
|---|---|
| Release | 1.0.0-rc1 (release candidate; **not tagged**) |
| Branch | `release/1.0.0-rc1` (pull request #16 into `main`) |
| Database schema | version 10 (no new migrations in this release) |
| Report date | 2026-11-27 |
| Prepared by | Automated verification run with Claude Code, for review by the project owner |

This report separates **what was actually verified** from **what can only be
verified on a real production server**. Passing automated tests are not
treated as proof of production readiness.

**No penetration test has been carried out.** The security checks below are
automated tests, mutation tests, dependency audits and scripted abuse
checks run by the developer. They do not replace an independent
assessment.

---

## 1. Summary verdicts

| Use | Verdict | Conditions |
|---|---|---|
| **Local demonstration** | **Suitable** | Development or testing mode on one machine, with demo accounts and demo data only. |
| **Controlled internal pilot** | **Suitable, with conditions** | Conditions P1–P8 in section 7 must be met: a prepared Linux server inside the organisation's network, HTTPS, Redis, backups to a second location, a named administrator, a small known group of users, and simulated (non-email) notifications until an SMTP provider is chosen. |
| **Public production deployment** | **Not ready** | Blocking items B1–B8 in section 8, including no real deployment rehearsal, no off-server backups, no SMTP provider, no independent security review, no MFA, and unresolved business decisions. |

---

## 2. Repository audit (Phase 1)

| Check | Result |
|---|---|
| Packages 1, 1b, 2, 3 merged into `main` | Yes: Package 1 = PR #12, 1b = PR #13, 2 = PR #14, 3 = PR #15 (merge commit `4c306d6`), after the earlier foundation PRs up to #11. |
| Release branch state at start | `release/1.0.0-rc1` = `main` (`4c306d6`), clean working tree. |
| Lost or unmerged work | One fix existed only on `feature/production-operations`: `36b0506` *Read the CSRF token across lines in the Caddy proxy check*, pushed after PR #15 was merged. **This was release-blocking:** `main` CI had been red since the merge (the Caddy job stopped after 5 of 14 checks). Cherry-picked as `cafef18`. No other branch has commits missing from `main`. |
| Dependabot | PR #5 (python-dotenv 1.2.2 → 1.2.4) and PR #6 (Flask-Limiter minimum ≥ 4.1.1). Both passed their Python tests and failed only on the Caddy script bug above. Both changes are adopted in this release (`3b4f444`); the two PRs can be closed after this one is merged. |
| Database migrations | Schema version 10, no pending migrations, no new migrations in this release. Fresh creation (`init-db`) produces version 10 (checked by every browser-test class). |
| Real `helpdesk.db` and `.env` | **Never opened, migrated or modified.** All test runs used rsync copies of the repository that exclude `*.db` and `.env`, plus temporary databases. `helpdesk.db` was last modified and last accessed on 2026-11-25, before this work began, and has no `-wal`, `-shm` or `-journal` file beside it. |

## 3. Automated tests

| Suite | Environment | Result |
|---|---|---|
| Unit and integration (`tests/`) | Python 3.13.12 and 3.14.3, local, with real Redis | **1,421 passed, 0 failed, 0 skipped** on each version |
| Unit and integration (`tests/`) | GitHub Actions, Python 3.13 | Passed |
| Browser end-to-end (`e2e/`, new) | Headless Chromium (Playwright 1.65.2), local | **17 passed** (run 4 times) |
| Browser end-to-end (`e2e/`) | GitHub Actions, new "Browser workflow tests" job | Passed |
| Caddy configuration | Real Caddy 2.11.9 (official image pinned by digest) in CI; the same version's checksum-verified binary locally | `caddy fmt`, `adapt --validate`, `validate`: passed |
| Caddy + Gunicorn proxy boundary (14 checks) | CI, and locally with the Caddy binary | **14/14 passed** |

The count rose from 1,420 to 1,421 because mutation testing found a gap
(section 5.4); a test was added. No test was removed, skipped or weakened.

**Observation.** In one local run with **two full suites running at the same time**,
`test_concurrent_requests_across_workers_allow_exactly_five` got 503
responses on Python 3.13. The application waits 1 second for Redis and then
deliberately refuses (fails closed). Run alone, the module passed 5 out of 5
times, and the full suite passed. This is CPU contention on the test
machine, not a defect, but it shows that a heavily loaded production host
would refuse sign-ins rather than slow them down. See [Known limitations](KNOWN_LIMITATIONS.md).

### 3.1 Browser tests (Phase 2)

Every page opened is also checked for **Content-Security-Policy violations
and JavaScript errors**; there were none.

| Workflow | Test | Result |
|---|---|---|
| Login, failed login, logout; signed-out redirect; sign-in audit | `test_login_and_logout` | Pass |
| Mandatory first-login password change; other pages locked until changed; server-side policy; old password rejected | `test_first_login_requires_a_new_password` | Pass |
| Password recovery by an administrator (web), session ended, forced change | `test_password_recovery_by_an_administrator` | Pass |
| Password recovery from the server command line (`reset-password` at a terminal), audit actor `cli:<user>` | `test_password_recovery_from_the_server_command_line` | Pass |
| Account administration: create, promote, deactivate (JavaScript confirmation), sign-in refused, reactivate, cannot deactivate self, audit page | `test_administrator_account_management` | Pass |
| Permissions: users see only their own tickets (404 for others), cannot update tickets even by direct POST, no admin pages (403) for users or technicians, export restricted to own tickets | `test_permissions_by_role` | Pass |
| Login rate limit (6th attempt 429, even with the right password) | `test_login_rate_limit_and_audit` | Pass |
| Ticket creation, assignment, comments by both sides, In Progress → Resolved → Closed, history timeline, notifications queued per event, simulated delivery | `test_ticket_lifecycle_history_and_notifications` | Pass |
| SLA dashboard: Within SLA → Due Soon → Overdue as the clock moves; filter; ticket page agrees; first response leaves Overdue | `test_sla_dashboard_follows_the_clock` | Pass |
| CSV export via the dashboard button, filter respected, formula injection neutralised | `test_csv_export_matches_the_dashboard_filter` | Pass |
| CSRF: missing token, forged token, and a cross-site auto-submitting form | `test_csrf_protection` | Pass |
| Session expiry at 8 hours | `test_session_expires_after_eight_hours` | Pass |
| Logout and password change end the other device's session | `test_logout_and_password_change_end_every_session` | Pass |
| Sessions survive an application restart | `test_sessions_survive_an_application_restart` | Pass |
| Security headers and CSP reach the browser; injected inline script is blocked | `test_security_headers_reach_the_browser` | Pass |
| Notification: mail server unreachable → deferred, worker exits 2 ("paused"), recovers when transport works | `test_unreachable_mail_server_defers_then_recovers` | Pass |
| Notification: temporary SMTP refusal → delayed retry → permanent failure → `notifications-status` → `notifications-requeue` → delivered | `test_temporary_then_permanent_failure_and_requeue` | Pass |

How the browser tests stay safe: each test class creates its own temporary
database with the real `init-db`, creates the first administrator with the
real `setup_accounts.py` typed at a pseudo-terminal, and runs the
application with `APP_ENV=testing` and `FLASK_SKIP_DOTENV=1`, so `.env` is
never read. Mail goes only to a local sink folder or to a fake SMTP server
on 127.0.0.1 that discards everything. **No email was sent.**

**What the browser tests do not cover:** the moved clock affects what the
application *displays*, but SQLite stamps stored times with the real
clock. A late first response ("Breached") is therefore covered by the
unit tests (`test_sla_dashboard_routes.py`, `test_sla_status.py`), not
by the browser test. The browser tests run the application in testing
mode over plain HTTP. Production cookies, HTTPS and the proxy are covered
by the Caddy proxy-boundary check.

## 4. Production configuration (Phase 3)

All checks used throwaway databases, random secrets and a disposable Redis
on 127.0.0.1. **Nothing was installed system-wide and no service was
configured.**

| Item | How verified | Result |
|---|---|---|
| Gunicorn starts with `deploy/gunicorn.conf.py` and `wsgi:application`, 2 workers | Local, production settings | Pass |
| Startup refused: unmigrated database, weak `SECRET_KEY`, no `TRUSTED_HOSTS`, in-memory rate limit with 2 workers, non-loopback bind, Redis wrong password, Redis unreachable | Local, 7 separate attempts | All refused (exit 1) with a clear log line |
| Caddyfile formatting, adaptation, validation with the real Caddy | CI (official image) and local binary | Pass |
| HTTPS, HTTP→HTTPS redirect, HSTS | Caddy proxy check | Pass |
| Session cookie `__Host-`, `Secure`, `HttpOnly`, `SameSite=Lax` | Caddy proxy check | Pass |
| Security headers, strict CSP (`default-src 'none'`, `frame-ancestors 'none'`, `object-src 'none'`) | Unit tests, browser test, proxy check | Pass |
| Trusted hosts: unknown `Host` refused (400) | Local Gunicorn | Pass |
| Trusted proxies: forged `X-Forwarded-For` ignored; Gunicorn logs the address Caddy saw | Caddy proxy check, local load script | Pass |
| Request body limit (413 over 1 MB) | Caddy proxy check | Pass |
| `/healthz` (process) and `/readyz` (database, schema, Redis) | Local Gunicorn; `/readyz` from a non-loopback address is 404 through Caddy | Pass |
| Gunicorn reachable only on loopback | Caddy proxy check | Pass |
| Logs contain no query strings and no Redis password | Proxy check, local log scan | Pass |
| Notification worker supervision: heartbeat written; `ops-check` warns when there is none; exit code 2 when delivery is paused; SIGTERM stops cleanly | Local | Pass |
| Backup and recovery configuration (key file permissions, key location, backup directory) | Local rehearsal, section 6 | Pass |
| systemd units, timers, Redis ACL, real certificates, firewall | **Not verifiable without a real server.** Reviewed as text only. | Not verified |

## 5. Security verification (Phase 5)

### 5.1 Dependency and code checks

| Check | Result |
|---|---|
| `pip-audit --strict -r requirements.txt` | No known vulnerabilities |
| `pip-audit --strict -r e2e/requirements.txt` | No known vulnerabilities |
| `pip check` (3.13 and 3.14) | All installed packages compatible |
| `compileall` (all application, test, e2e and deploy code) and `bash -n` on scripts | Pass |

### 5.2 Secret-exposure checks

| Check | Result |
|---|---|
| Any `.env`, `*.db`, `*.key`, `*.pem` or `instance/` file ever committed (all branches, all history) | None |
| Private keys, GitHub, AWS, Slack, Google or OpenAI token patterns in all history | None |
| Password-like assignments in history | Only documented test fixtures (for example `correct-horse-battery-staple`) |
| `.gitignore` covers `.env`, databases, WAL files, backups, manifests, keys, `credentials/`, `instance/` | Yes |
| Backup files contain no readable data; key and backup files are mode 0600 | Yes (rehearsal) |

### 5.3 Login and authorisation abuse

| Attack | Result |
|---|---|
| Password guessing: 6th attempt per minute from one address | 429, even with the correct password |
| Guessing while spoofing the left-most `X-Forwarded-For` | Still limited by the real client address (5 × 200 then 429) |
| Guessing during a Redis outage | Refused with 503 and `Retry-After: 30` (fails closed); recovers without restart |
| Reading another user's ticket by number | 404 (same as a missing ticket) |
| Changing a ticket as a user by direct POST | Ignored; ticket unchanged |
| Admin pages as user or technician | 403 |
| Administrator deactivating their own account by direct POST | Refused |
| Cross-site form post from another origin | No ticket created |
| Requests without or with a forged CSRF token | 400 |
| Replaying a session cookie after logout, password change or deactivation | Refused |
| Session older than 8 hours | Refused |
| Injected inline script | Blocked by CSP |
| CSV formula injection (`=HYPERLINK(...)`) | Neutralised |

### 5.4 Mutation testing

Fifteen deliberate security regressions were made one at a time in a scratch
copy. A mutant is "killed" when at least one test fails.

| Mutant | Killed by |
|---|---|
| CSRF protection removed | `test_csrf.py`, browser CSRF test |
| Sessions never expire | `test_session_revocation.py`, browser expiry test |
| Session version not compared (revocation ignored) | `test_session_revocation.py`, `test_logout.py`, browser revocation test |
| Ticket ownership check removed | **Only the new browser test** (`test_permissions_by_role`) |
| Every account treated as administrator | `test_account_administration.py`, browser permissions test |
| Login limit 5 → 50 per minute | `test_rate_limit.py`, `test_shared_rate_limit.py`, browser test |
| Rate limit fails open when Redis is down | `test_shared_rate_limit.py` |
| CSV formula neutralisation removed | `test_csv_export.py`, browser export test |
| SMTP 5xx treated as temporary (two code paths) | `test_notification_delivery.py` |
| CSP `frame-ancestors` removed | `test_security_headers_cookies.py`, browser headers test |
| Forwarded headers trusted from one proxy too many | `test_proxy_trust.py` |
| First-login password change not enforced | `test_forced_password_change.py`, browser test |
| Decrypted-backup checksum not compared | **Survived at first**; killed by the new `test_wrong_database_checksum_in_manifest_is_detected` |
| Live restore allowed while the database is in use | `test_backups.py` |

**Result: 15/15 killed** after one test was added. One finding is worth
knowing: removing the ticket ownership check was caught only by the
browser tests, not by any unit test.

## 6. Backup, restoration and reliability (Phase 4)

All checks ran against throwaway databases with real data made by the
application's own code (40–380 tickets), using the real `flask` commands.

### 6.1 Backup and restoration: 27/27 checks passed

| Check | Result |
|---|---|
| `backup-generate-key` writes a 0600 key | Pass |
| `backup-create` writes an encrypted 0600 backup; no plaintext inside | Pass |
| `backup-verify`, `backup-list` | Pass |
| `backup-restore --to` a new file; restored row counts identical for users, tickets, comments, history, audit events, notifications | Pass |
| Restore over the live database with `backup-restore` | Refused |
| Corruption: one flipped byte, truncation, empty file (each beside its genuine manifest) | Each detected ("does not match its manifest") and **not restored** |
| Key missing, key not configured, wrong key | Each refused clearly; wrong key named by key id; nothing decrypted |
| Key stored in the backup directory; key readable by others | Refused |
| `backup-drill` | Pass (decrypt, checksum, integrity, foreign keys, schema 10, row counts, application queries) |
| `ops-check` | Reports backup age and last drill; warned correctly that the worker had no heartbeat |
| `backup-restore-live` while the database is in use | Refused; live database unchanged |
| `backup-restore-live` after confirmation | Live database replaced; data back to the backup's state; **safety backup written first** |
| `backup-restore-live` with the wrong phrase | Nothing changed |
| Restored live database at the current schema | Pass (version 10) |

### 6.2 Reliability: 21/21 and 27/27 checks passed

| Scenario | Result |
|---|---|
| **Redis outage** while Gunicorn runs | Sign-in 503 + `Retry-After`; `/readyz` 503; `/healthz` 200; log names the cause without the password; **recovers automatically** when Redis returns |
| **Gunicorn worker killed** (SIGKILL) | Master starts a replacement; requests keep working |
| **Application restart** | SIGTERM exits 0; restarted server ready; browser sessions survive |
| **Notification worker killed** mid-delivery (SIGKILL) | Claimed row keeps its lease; a new worker does not take it early; after the lease expires the attempt is recorded as `interrupted` and the message is retried after the normal backoff; every row delivered. Duplicates are at most the rows in flight and keep the same `Message-ID` (at-least-once, as documented). Observed in 2 of 3 runs; in the third the kill fell between messages. |
| Notification worker stopped (SIGTERM) | Finishes the current message, exits 0, nothing left "processing"; heartbeat written |
| Failed and delayed notifications | Browser tests: deferred, retry, permanent failure, requeue (section 3.1) |
| **SQLite concurrency**: 8 processes × 40 (create + comment + assign + status) | 960 writes in about 5 s, **0 errors**, every ticket saved once, 4 history rows each, every first response recorded, `integrity_check` ok, no foreign-key violations |

### 6.3 Performance (indicative only)

Gunicorn with production settings, 2 sync workers, Redis rate limits, 380
tickets, 8 concurrent clients, on a 16-thread WSL2 development machine,
**without Caddy or TLS**:

| Page | Requests | Errors | Throughput | p50 | p95 | Max |
|---|---|---|---|---|---|---|
| Staff dashboard | 300 | 0 | 480/s | 16 ms | 19 ms | 46 ms |
| Staff dashboard, SLA filter | 300 | 0 | 349/s | 22 ms | 29 ms | 36 ms |
| Staff search | 300 | 0 | 504/s | 16 ms | 17 ms | 22 ms |
| Ticket page | 300 | 0 | 810/s | 9 ms | 12 ms | 21 ms |
| User dashboard | 300 | 0 | 563/s | 14 ms | 16 ms | 20 ms |
| CSV export (all 380 tickets) | 40 | 0 | 321/s | 24 ms | 25 ms | 26 ms |

These figures show that the application is not a bottleneck for a pilot of
tens of users. They are **not** a capacity guarantee: the target server's
hardware, TLS, network and larger data volumes were not tested.

The backup, production and reliability rehearsals were run with ad-hoc
scripts on the development machine. They are not committed to the
repository; the browser tests and unit tests are.

## 7. Conditions for a controlled internal pilot

| # | Condition |
|---|---|
| P1 | A dedicated Linux server (or VM) inside the organisation's network, prepared as in [PRODUCTION_OPERATIONS.md](PRODUCTION_OPERATIONS.md) sections 2–6, with a firewall that exposes only ports 80 and 443. |
| P2 | A real DNS name and a trusted certificate (Caddy automatic HTTPS, or the organisation's internal CA). |
| P3 | Redis on the same machine, password and ACL as in `deploy/redis/`. |
| P4 | Backup timer enabled, **plus at least one off-server copy** (`HELPDESK_BACKUP_OFFSITE_DIR`) and the backup key stored **separately** from the backups (for example in a password manager). Verify a drill on the server before users start. |
| P5 | `NOTIFICATION_DELIVERY=local` (simulated) until an SMTP provider is chosen. Users must be told that no emails are sent. |
| P6 | A named administrator who has run through the [UAT checklist](UAT_CHECKLIST.md) and the restore drill, and who watches `ops-check` alerts. |
| P7 | A small, known group of pilot users, with demo or non-sensitive ticket content until the organisation approves the data-protection position (retention, who can see what). |
| P8 | A first-deployment rehearsal on the pilot server itself, recorded, including a rollback. |

## 8. Blocking items for public production

| # | Item | Why it blocks |
|---|---|---|
| B1 | No real deployment has been carried out | systemd units, file permissions, certificate issuance, firewall and log rotation are verified only as text. |
| B2 | No off-server backup location and no key-escrow procedure | A lost server or lost key means total data loss. |
| B3 | No SMTP provider chosen; real email delivery never exercised | Delivery, SPF/DKIM/DMARC, bounces and provider limits are untested. |
| B4 | No independent security review or penetration test | Only developer-run automated checks exist. |
| B5 | No multi-factor authentication | Not acceptable for an internet-facing system holding staff data. |
| B6 | Single server, SQLite, no high availability | Acceptable for a pilot, not for a public service with availability promises. |
| B7 | Unresolved business decisions (section 9) | SLA policy, data retention and audit removal are not approved. |
| B8 | No monitoring of the server itself (disk, memory, certificate expiry) beyond `ops-check` | Outages may go unnoticed. |

## 9. Unresolved decisions (owner: project sponsor)

1. SLA policy: the targets (1/4/24/72 calendar hours) are examples, with no business hours or holidays. See the README section *Unresolved business decisions*.
2. SMTP provider and sender address; whether real email is needed for the pilot at all.
3. Backup retention periods and the off-server location; who holds the backup key.
4. Audit-log retention and whether archived events may ever be removed ([AUDIT_RETENTION.md](AUDIT_RETENTION.md), step 2).
5. Notification decisions listed in [NOTIFICATIONS.md](NOTIFICATIONS.md#decisions-for-review).
6. Account-policy decisions in the README (*Account policy decisions*).
7. Recovery objectives: how much data loss (RPO) and downtime (RTO) are acceptable ([DISASTER_RECOVERY.md](DISASTER_RECOVERY.md)).
8. Whether to tag `v1.0.0-rc1` after review (not done; awaiting approval).

## 10. Release-readiness checklist

| Item | Status |
|---|---|
| All approved packages merged | Done |
| `main` CI green | Fixed on this branch (green after merge) |
| Unit and integration tests pass (3.13 and 3.14) | Done: 1,421 |
| Browser tests automated in CI | Done: 17 |
| Real Caddy validation in CI | Done |
| Dependency audit clean | Done |
| No secrets in history | Done |
| Mutation tests for security controls | Done: 15/15 |
| Backup, corruption, key-loss and restore rehearsal | Done (disposable) |
| Redis outage, worker crash, restart rehearsal | Done (disposable) |
| Concurrency and load smoke test | Done (indicative) |
| Version 1.0 documentation | Done ([docs/README.md](README.md)) |
| Release notes | Done ([RELEASE_NOTES_1.0.0-rc1.md](RELEASE_NOTES_1.0.0-rc1.md)) |
| Owner review of PR #16 | **Pending** |
| Release tag `v1.0.0-rc1` | **Not created** (awaiting approval) |
| Pilot server prepared and rehearsed (P1–P8) | **Not started** (needs infrastructure) |
| Independent security review | **Not started** |
| SMTP provider | **Not chosen** |
