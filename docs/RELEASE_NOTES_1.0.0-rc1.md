# Release Notes: IT Helpdesk 1.0.0-rc1

**Release candidate.** For local demonstration and, with the conditions in
the [readiness report](RELEASE_READINESS_1.0.0-rc1.md#7-conditions-for-a-controlled-internal-pilot),
a controlled internal pilot. **Not approved for public production.** The
Git tag `v1.0.0-rc1` has not been created; it awaits the owner's approval.

Database schema: **version 10**. Upgrading from an earlier 0.x database:
take a verified backup, then `flask --app app init-db --backup-to FILE`.
There are no new migrations since Package 3.

## What 1.0 contains

**Tickets.** Create, comment, assign, Open → In Progress → Resolved →
Closed. Full activity history. Search, status, priority and SLA filters,
pagination. CSV export of exactly the current view, with formula injection
neutralised.

**SLA monitoring.** First-response targets by priority, dashboard cards
(Within SLA, Due Soon, Overdue, Met, Breached, Not Tracked), the same
verdict on every page and in exports.

**Accounts and security.** Database-backed sign-in with roles checked on
every request. Administrator account management with a forced password
change. Server-side password recovery. Password policy with an offline
breached-password blocklist. Append-only audit log of account changes and
sign-ins. CSRF protection. Strict CSP and security headers. Secure
`__Host-` cookies. 8-hour sessions with account-wide revocation.
Per-address rate limiting shared through Redis, failing closed.

**Notifications.** A transactional outbox queue, a supervised worker,
simulated local delivery or SMTP with verified TLS, bounded retries and a
requeue command. Delivery is at-least-once.

**Operations.** Gunicorn behind Caddy (HTTPS), startup validation,
`/healthz` and `/readyz`, JSON logs, encrypted verified backups with
off-server copy, retention and automated restoration drills, `ops-check`
monitoring with alert hooks, systemd units and timers.

## Changes in this release candidate (since Package 3)

* **Fixed:** the CI job "Validate Caddy configuration" stopped after 5 of
  its 14 proxy checks because the script could not read a CSRF token
  split across lines. `main` had been red since Package 3 was merged. Only
  the CI script changed; the application did not (`cafef18`, cherry-picked
  from `36b0506`).
* **New:** 17 end-to-end browser tests in headless Chromium (Playwright),
  run by a new CI job. They cover sign-in, first-login password change,
  password recovery, account administration, permissions, rate limiting,
  the ticket lifecycle, the SLA dashboard, CSV export, CSRF, session
  expiry and revocation, restart, security headers and CSP, and
  notification deferral, retry, failure and requeue.
* **New test:** backup verification now has a test for a manifest whose
  decrypted-database checksum is wrong (found by mutation testing).
* **Dependencies:** python-dotenv 1.2.4 (was 1.2.2); Flask-Limiter ≥ 4.1.1.
  These are Dependabot PRs #5 and #6.
* **Documentation:** the version 1.0 set in [docs/](README.md): installation,
  administrator and user guides, security configuration, backup and
  restoration, disaster recovery, known limitations, UAT checklist, and
  the release-readiness report.

## Verification summary

1,421 unit and integration tests (Python 3.13 and 3.14). 17 browser tests.
Real Caddy validation and 14 proxy-boundary checks. 15/15 security mutants
killed. `pip-audit` clean. No secrets in Git history. Backup, corruption,
key-loss and live-restore rehearsal 27/27. Redis outage, worker crash and
restart rehearsal 27/27. Concurrency, worker interruption and load
rehearsal 21/21. Details and limits:
[RELEASE_READINESS_1.0.0-rc1.md](RELEASE_READINESS_1.0.0-rc1.md).

**No penetration test has been performed.**

## Known limitations

See [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md). Most important: no MFA,
no SMTP provider (email is simulated), never deployed, single server, no
off-server backup location yet.
