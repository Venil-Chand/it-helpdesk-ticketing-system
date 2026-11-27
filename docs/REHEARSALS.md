# Release rehearsals

The unit tests, browser tests and Caddy check (in `python-ci.yml`) test
pieces of the application. The **rehearsals** run the real commands and
servers end to end, the way an operator would on the server, and break
things deliberately: corrupt backups, stop Redis, kill workers, weaken
security controls. They are in [`rehearsals/`](../rehearsals) and take
about 15 s to 15 minutes each.

They were first run by hand for the
[1.0.0-rc1 readiness report](RELEASE_READINESS_1.0.0-rc1.md) (sections
4–6) and are now committed, so anyone can repeat them.

## Safety

Every rehearsal works in a new temporary directory and cleans it up
afterwards. Inside it are:

* a **copy of the source code**: the files Git tracks (or would track).
  Ignored files are never copied, and neither are `.env`, `*.db`, key,
  backup or certificate files, even if one were tracked by mistake;
* its **own SQLite database**, created by `flask --app app init-db` and
  filled through the application's own functions (`rehearsals/seed.py`);
* **new random secrets** for that run: `SECRET_KEY`, account passwords,
  the backup key and the Redis password;
* where needed, its **own `redis-server`**, on a free port on 127.0.0.1,
  with a random password and no persistence. A rehearsal never connects to
  a Redis server it did not start.

Before any command starts, `rehearsals/common.py` (`guard_environment`)
refuses to run it unless `HELPDESK_DATABASE_PATH` and every other path
setting point inside the temporary directory, `APP_ENV` is `testing` or
`production` (neither reads `.env`) and `FLASK_SKIP_DOTENV=1`. Commands
start from a minimal environment: nothing from your shell is passed on
except `PATH`, `LD_LIBRARY_PATH`, `LANG` and `TZ`, so an exported
`SECRET_KEY` or Redis URL cannot leak in.

**The repository's `helpdesk.db` and `.env` are never opened.** Nothing
listens beyond 127.0.0.1 and no email can be sent: the only mail servers
used are loopback fakes that keep nothing but each message's Message-ID.

All output passes through a redaction step that replaces the run's
secrets with `[redacted]`, and several checks fail if a secret appears
in a log. These guarantees are tested by
[`rehearsals/tests/test_common.py`](../rehearsals/tests/test_common.py),
which runs in normal CI.

## Requirements

* **Linux.** The rehearsals use `/proc`, pseudo-terminals and POSIX
  signals. They were run on Ubuntu (GitHub Actions) and Ubuntu on WSL2.
* **Python 3.13 or later** with `pip install -r requirements.txt`
  (this includes Gunicorn).
* **`redis-server`** (Redis 7) for `production` and `reliability`.
  The mutation rehearsal uses it if it is there, and otherwise the shared
  rate-limit tests are skipped. Install it (`sudo apt install
  redis-server`; the system service can stay stopped) or point
  `HELPDESK_REHEARSAL_REDIS_SERVER` at the binary. Without root,
  `apt-get download redis-server liblzf1 libjemalloc2` and
  `dpkg-deb -x` work, with `LD_LIBRARY_PATH` set to the extracted
  `usr/lib/x86_64-linux-gnu`.
* **For `mutation --browser` only:** `pip install -r e2e/requirements.txt`
  and `python -m playwright install --with-deps --only-shell chromium`.

## Running

From the repository root, in the virtual environment:

```bash
python -m rehearsals backup                 # about 15 s, no Redis needed
python -m rehearsals production             # about 10 s
python -m rehearsals reliability            # about 1 minute
python -m rehearsals mutation               # about 2 minutes (unit tests)
python -m rehearsals mutation --browser     # about 10 minutes
python -m rehearsals all --browser          # everything
```

Each check prints `PASS`, `FAIL` (with the redacted reason) or `SKIP`.
Each rehearsal ends with a count, and the command exits 0 only if no check
failed. `--keep` keeps the temporary directories for inspection and prints
where they are. They hold that run's random secrets, so delete them
afterwards.

### In GitHub Actions

* `python-ci.yml` runs the quick harness checks on every push and pull
  request: the safety guard, and that every mutant still matches the code
  (see *Maintenance*).
* [`rehearsals.yml`](../.github/workflows/rehearsals.yml) runs the four
  rehearsals as parallel jobs, including `mutation --browser`. They are
  too slow for every push, so the workflow runs when a pull request
  changes `rehearsals/`, and on demand: **Actions → Release rehearsals →
  Run workflow**. Run it before tagging a release. (GitHub only offers
  the manual button once the workflow file is on `main`.)

## What each rehearsal checks

The counts are from the runs recorded at the end of this page.

### `backup` (32 checks)

Real `flask` backup commands against a database with 40 tickets and rows
in every table.

* `backup-generate-key` writes a 0600 key. `backup-create` writes one
  verified backup and its manifest, both 0600, with no readable database
  content in the backup.
* `backup-verify`, `backup-list`, `backup-transfer` (off-server copy,
  verified at the destination) and `backup-prune` (dry run keeps the
  backup).
* `backup-restore --to` a new file: the row counts of every table equal
  the original's, and `integrity_check` is ok. Restoring over the live
  database is refused, and the live database is unchanged.
* Corruption: a flipped byte, truncation and an empty file, each beside
  its genuine manifest. Each is detected ("does not match its manifest")
  and nothing is restored.
* Key problems: a missing key file, no key configured, the wrong key
  (refused, naming the key id), a key stored in the backup directory, and
  a key readable by others. Each is refused and nothing is restored.
* `backup-drill` passes and saves its report. `ops-check` reports the
  backup and the drill as OK and warns that there is no worker heartbeat.
* `backup-restore-live`, typed at a pseudo-terminal: refused without a
  terminal, refused while another process holds the database, and
  refused with the wrong phrase, with nothing changed each time. With
  the right phrase it restores the backup's data, writes a safety backup
  first, and leaves the database at schema version 10, passing its
  integrity and foreign-key checks.

### `production` (26 checks)

Gunicorn with `deploy/gunicorn.conf.py` and `wsgi:application`,
`APP_ENV=production`, two workers and the disposable Redis. Requests are
sent as Caddy sends them (`Host`, `X-Forwarded-Proto: https`,
`X-Forwarded-For`).

* Starts with two workers. `/healthz` and `/readyz` both return 200.
* Strict CSP (`default-src 'none'`, `frame-ancestors 'none'`,
  `object-src 'none'`), HSTS and `nosniff`.
* Sign-in works. The session cookie is `__Host-`, `Secure`, `HttpOnly`,
  `SameSite=Lax`, `Path=/`, with no `Domain`.
* An unknown `Host` gets 400, plain HTTP behind the proxy gets 400, and a
  body over 1 MiB gets 413.
* The sign-in limit holds against a spoofed left-most `X-Forwarded-For`:
  5 attempts, then 429.
* The access log shows the client address Caddy reported. It holds no
  query string and none of the run's secrets.
* **Refused at startup**, each before any worker boots and with a log
  line naming the reason (and no secret): an unmigrated database, a weak
  `SECRET_KEY`, no `TRUSTED_HOSTS`, `FLASK_DEBUG=1`, in-memory rate
  limits with several workers, `RATELIMIT_SINGLE_PROCESS=1` with two
  workers, a non-loopback bind address, Redis with the wrong password,
  and Redis unreachable.

### `reliability` (26 checks)

* **Redis outage** while Gunicorn runs: sign-in fails closed (503 with
  `Retry-After`), `/readyz` returns 503, `/healthz` returns 200, and the
  log names the problem without the password. When Redis returns,
  sign-in works again without a restart.
* **A Gunicorn worker killed** with SIGKILL is replaced, and 20 requests
  during the replacement all succeed. **SIGTERM** stops Gunicorn with
  exit code 0. After a **restart**, the restarted server becomes ready
  and a session from before the restart is still signed in.
* **Notification worker killed mid-delivery.** A loopback SMTP server
  holds the first message open at the end of DATA, so the kill always
  lands mid-delivery. The claimed row stays `processing` with its lease.
  A new worker does not take that row early but delivers all the others.
  After the 30 s lease expires, the attempt is recorded as `interrupted`
  and the message is retried. Every row ends up `sent`. Only the
  in-flight message arrived twice, both times with the same Message-ID
  (at-least-once delivery, as documented).
* **Notification worker stopped with SIGTERM** during a delivery: it
  finishes the message, exits 0, leaves nothing `processing`, and its
  heartbeat says `stopped`.
* **Mail server unreachable:** `notifications-send` pauses (exit 2) and
  nothing is lost.
* **SQLite concurrency:** 8 processes × 40 iterations, each a create, a
  comment, and an assignment with a status change (960 write
  transactions), finish with 0 errors. Every ticket is saved once with 4
  history rows, every first response is recorded, `integrity_check` is
  ok, and there are no foreign-key violations.

  **Known finding: this check can fail under heavy CPU load.** Once, while
  the machine was also running other test suites, 1 of the 960
  transactions failed with `database is locked`. That is SQLite's default
  5-second lock wait, used by `database.get_db()`. Its ticket was saved
  without its comment, so two follow-up checks failed too. In 8 repeats
  on an idle machine there were 0 errors.

  Production has at most two Gunicorn workers and one notification
  worker writing, far fewer than these 8 writers with no pause. The check
  stays strict on purpose: it is an application behaviour to know about,
  not a rehearsal fault. Changing the lock wait is an application change
  for a later package.

### `mutation` (15 mutants)

[`rehearsals/mutants.py`](../rehearsals/mutants.py) lists 15 deliberate
security regressions:

* CSRF protection removed
* sessions never expire
* session version ignored
* ticket ownership check removed
* everyone treated as an administrator
* login limit raised to 50 per minute
* rate limit fails open when Redis is down
* CSV formula neutralisation removed
* SMTP 5xx treated as temporary (two code paths)
* CSP `frame-ancestors` removed
* one proxy too many trusted
* first-login password change not enforced
* decrypted-backup checksum not compared
* live restore allowed while the database is in use

Each mutant is applied to the workspace copy, one at a time, and the test
modules named for it are run. A mutant is **killed** when at least one of
them fails. A survivor is reported as a failure. First, every named
module must pass on the unmodified copy. Otherwise the rehearsal stops,
because a failure would say nothing about the mutant.

Only the tests named for each mutant are run, not the whole suite (which
would take 15 × 80 s). A survivor therefore means "these tests would not
notice", not "no test would".

## Maintenance

* **A mutant no longer matches the code.** If a refactoring changes the
  exact source text of a mutant, the harness check in normal CI fails
  (`test_every_mutant_matches_the_current_code`). The mutation rehearsal
  would report it as `STALE`, never silently skip it. Update `original`
  and `replacement` in `rehearsals/mutants.py` to the new code.
* **Adding a mutant:** append a `Mutant(...)` with the exact text to
  replace, its weaker replacement, `count` (how often the text occurs,
  all replaced), and the unit modules or browser tests that should
  catch it.
* **Adding a check:** each rehearsal is a plain function that calls
  `report.check(name, passed, detail)`. Run every command through
  `Workspace.run`, `Workspace.flask`, `Workspace.popen` or `Gunicorn`, so
  the safety guard applies. Register any new secret with
  `Workspace.sensitive()` so that it is redacted.
* **Changed behaviour:** if a check fails because the documented
  behaviour changed on purpose, update the check and the operator
  documentation together.

## What is not covered, and why

* **Caddy and TLS** are not rehearsed here. The `caddy` job in normal CI
  already validates every Caddyfile with the real Caddy and runs 14
  proxy-boundary checks (`.github/scripts/caddy-proxy-check.sh`).
* **systemd units and timers, the Redis ACL file (`deploy/redis/`), the
  firewall, real certificates and file ownership on a server** need a
  real server and root access. They cannot be rehearsed safely on a
  development machine or a CI runner. They remain pilot conditions P1–P8
  in the readiness report.
* **Load and response times** (readiness report §6.3) are not committed as
  a rehearsal. The figures depend on the machine and are indicative only,
  so a script with pass/fail thresholds would either be fragile or prove
  nothing. Measure load on the pilot server itself.
* **Real SMTP delivery** is not exercised. Only loopback fakes are used,
  by design.
* The Redis outage is a stopped process on the same machine. It does not
  cover a network partition, a slow Redis, or a Redis with persistence or
  ACL users.

## Differences from the first, hand-run rehearsal

The scripts used for the readiness report were not kept. These were
rewritten from that report, so the check counts differ (the report says
27/27, 21/21 and 27/27). What changed:

* **Worker kill is now deterministic.** In the hand-run rehearsal the
  notification worker was killed at a random moment, and that run caught
  it mid-delivery in only 2 of 3 attempts. The holding SMTP server now
  makes it land mid-delivery every time.
* **Ownership mutant.** The report says removing the ticket ownership
  check was caught only by a browser test. The exact edit made then was
  not recorded. Both ways of removing the check tried now, in
  `can_view_ticket` (the committed mutant) and in `get_ticket_or_404`,
  are caught by `test_ticket_ownership.py`. The earlier finding could not
  be reproduced.
* **Out of scope here:** the load measurements and the scripted login
  abuse checks (readiness report §5.3). The abuse cases are covered by
  unit tests, browser tests, and the rate-limit and proxy checks above.

## Recorded runs

| Date | Where | Python | Result |
|---|---|---|---|
| 2026-11-27 | Ubuntu 24.04 on WSL2, Redis 7.0.15 | 3.14.3 | backup 32/32, production 26/26, reliability 26/26, mutation 15/15 killed (unit tests) |
| 2026-11-27 | Ubuntu 24.04 on WSL2, Redis 7.0.15 | 3.13.12 | backup 32/32, production 26/26, reliability 26/26 (one earlier run under extra load: 23/26, see the concurrency finding), mutation `--browser` 15/15 killed (36/36 checks; 9 mutants also caught by a browser test) |
