# Email Notifications

This document describes the notification engine added in schema version 10:
how it works, which events notify whom, how to run and configure delivery,
and what it cannot guarantee. The **recipient rules** and the **open
decisions** at the end should be reviewed before the pilot.

## Contents

1. [Architecture](#architecture)
2. [Supported events and recipient rules](#supported-events-and-recipient-rules)
3. [What a message contains](#what-a-message-contains)
4. [Email addresses](#email-addresses)
5. [Delivery modes](#delivery-modes)
6. [SMTP configuration](#smtp-configuration)
7. [Queue-processing commands](#queue-processing-commands)
8. [Retries, failures and duplicates](#retries-failures-and-duplicates)
9. [Security considerations](#security-considerations)
10. [Deployment requirements](#deployment-requirements)
11. [Known limitations](#known-limitations)
12. [Decisions for review](#decisions-for-review)

---

## Architecture

The engine is a **transactional outbox**. A web request never sends email;
it only writes rows to a queue table in the same SQLite transaction as the
change that caused them. A separate worker process delivers them later.

```text
 web request (e.g. add a comment)                 worker (flask notifications-send)
 ──────────────────────────────────              ─────────────────────────────────
 BEGIN                                            BEGIN IMMEDIATE
   INSERT comment                                   recover expired claims
   INSERT ticket_history event                      expire rows older than max age
   record SLA first response                        claim ONE due row (token + lease)
   SAVEPOINT                                      COMMIT              ← lock released
     INSERT OR IGNORE notifications …             re-check recipient (read only)
   RELEASE (or ROLLBACK TO on error)              build the message
 COMMIT          ← comment + queue rows saved     send it — no transaction open
                   together, or neither           BEGIN IMMEDIATE
                                                    record outcome if the claim token
                                                    still matches; add attempt row
                                                  COMMIT
```

Guarantees that follow from this design:

| Requirement | How it is met |
|---|---|
| Queue consistent with the ticket or account change | Rows are inserted on the caller's connection, inside its transaction. If the ticket change rolls back, so do its notifications. |
| No email before the transaction commits | Nothing is sent during the request at all. The worker can only see rows that have been committed. |
| Notification failures never undo a valid ticket operation | The queueing code runs inside a `SAVEPOINT`. Any error in it rolls back only the savepoint and is logged; the ticket, comment or account change is still committed. Delivery failures happen later, in another process, and never touch tickets. |
| No SQLite transaction held open during SMTP | Claiming and recording outcomes are two short transactions. The SMTP conversation happens between them with no transaction open, so the web application can keep writing. |
| Safe with several workers | `BEGIN IMMEDIATE` serialises claims; a claimed row leaves `pending` in the same transaction; results are recorded only under the worker's random claim token. |
| Recovery from interrupted processing | A claim has a lease (`claimed_until`). If a worker dies, the next claim transaction finds the expired lease, records an `interrupted` attempt and puts the row back in the queue with backoff. |
| Duplicates avoided where practical | `notifications.dedupe_key` is `UNIQUE` and inserted with `INSERT OR IGNORE`, so one event can never queue the same message twice. Delivery-side duplicates are bounded (see below). |
| Timezone-aware UTC timestamps | Every queue timestamp is written from an aware UTC `datetime` as `YYYY-MM-DDTHH:MM:SS.mmm+00:00`. Naive datetimes are refused. All values have the same width, so they compare correctly as text and can use the indexes. |
| Works without an SMTP provider | The default development mode writes messages to a private local folder and marks them `simulated`. |
| No background thread in the web server | Delivery is a separate CLI process meant for a process supervisor. |

### Code layout

| File | Role |
|---|---|
| `notifications/events.py` | Event types, recipient rules, payload allow-list; the hooks called by `database.py` and `accounts.py` |
| `notifications/queue.py` | Claim, finish, lease recovery, expiry, delivery-time recipient check, status counts |
| `notifications/worker.py` | One run (`run_once`) and the supervised loop (`run_forever`) |
| `notifications/transport.py` | Local sink and SMTP transports; classifies every failure |
| `notifications/render.py` | Builds the plain-text email from fixed sentences |
| `notifications/settings.py` | Reads and validates the environment variables |
| `notifications/timeutil.py` | The queue's UTC clock and timestamp format |
| `helpdesk/notification_cli.py` | `notifications-send`, `notifications-status`, `notifications-check-config`, `set-email` |
| `migrations.py` (migration 10) | `users.email`, `notifications`, `notification_attempts` |

### Database objects (migration 10)

| Object | Purpose |
|---|---|
| `users.email` | Where an account is notified. `NULL` for every existing account; nothing is guessed. |
| `notifications` | One row per message to one account: `id`, `event_type`, `dedupe_key` (UNIQUE), `recipient_user_id`, `recipient_username`, `recipient_reason`, `ticket_id`, `payload` (reviewed fields only), `status`, `attempts`, `deferrals`, `created_at`, `updated_at`, `next_attempt_at`, claim columns (`claim_token`, `claimed_by`, `claimed_at`, `claimed_until`), `sent_at`, `delivery_mode`, `last_error` (sanitised). |
| `notification_attempts` | Append-only delivery history (triggers refuse UPDATE and DELETE): attempt number, worker, mode, start/end time, outcome, sanitised error. |
| `idx_notifications_due`, `idx_notifications_claims`, `idx_notification_attempts_notification` | Indexes for the worker's queries. |

Statuses: `pending` → `processing` → one of `sent`, `simulated`, `failed`,
`cancelled` (or back to `pending` for a retry).

The migration adds no rows: no notifications are invented for anything that
happened before version 10, and no existing ticket, history or audit row is
changed.

---

## Supported events and recipient rules

Every recipient must be an **existing, active account** when the row is
queued. Events marked *staff only* also require the `technician` or `admin`
role. The account that performed a **ticket** action is not told about its
own action.

| Event (`event_type`) | Trigger | Recipients (`recipient_reason`) |
|---|---|---|
| `ticket_created` | A ticket is created | The requester (`requester`, a confirmation) and every active technician and administrator except the requester (`staff`, *staff only*) |
| `ticket_assigned` | Ticket assigned or reassigned | The new assignee (`assignee`, *staff only*), unless they assigned it to themselves |
| `ticket_unassigned` | Ticket reassigned or unassigned | The previous assignee (`previous_assignee`, *staff only*), unless they made the change |
| `staff_comment` | A technician or admin comments on someone else's ticket | The requester (`requester`) and the assignee (`assignee`, *staff only*), except the author |
| `requester_comment` | The requester comments on their own ticket (even if the requester is staff — the same rule as the SLA) | The assignee (`assignee`, *staff only*), unless that is the author. Unassigned tickets notify nobody. |
| `ticket_resolved` | Status changed to Resolved | The requester, unless they made the change |
| `ticket_closed` | Status changed to Closed | The requester, unless they made the change |
| `password_changed` | The owner changed their own password | The owner (`account_owner`) — sent even though they did it, so a change made with a stolen session is noticed |
| `password_reset` | An administrator (web) or operator (`flask reset-password`) reset the password | The owner. The message says "an administrator"; it never names the admin or the operating-system account. |
| `role_changed` | An administrator changed the account's role | The owner |
| `account_reactivated` | An administrator reactivated the account | The owner |

Events that deliberately send nothing:

| Event | Why |
|---|---|
| Status changes to Open or In Progress | Routine; the requester sees them on the ticket page |
| Account deactivated | Deactivated accounts are never notified |
| Account created | The account has no address yet, and a welcome email could not contain the password anyway |
| Failed sign-ins | An attacker could use them to flood a mailbox |
| `email_changed` (`flask set-email`) | Operator-only command on the server; see [Known limitations](#known-limitations) |

### Accounts that are never notified

* An account that does not exist, including names that were never accounts —
  for example the historical assignee "Technician B" on ticket #1. The name
  stays on the ticket; it is simply not emailed.
* A deactivated account.
* A demoted former technician, for staff-only events: they can no longer see
  the ticket.
* An account without an email address (the row is queued, then cancelled with
  `recipient has no email address`).

### Checked again at delivery

Just before sending, the worker re-reads the account and the ticket. The row
is **cancelled** (never sent) if the account no longer exists under the same
id and username, has been deactivated, no longer has a staff role for a
staff-only reason, is no longer the ticket's requester, or has no address.
The address used is always the account's current one.

Ticket ownership and assignment rules are unchanged: notifications are
queued *after* the existing checks, by the same database functions, so a
refused assignment or an unauthorised comment queues nothing.

---

## What a message contains

Messages are built only from fixed sentences and these payload fields:
ticket number, priority, old/new status, old/new role, the acting username
and whether the owner or "an administrator" made an account change.

Never stored in the queue or put in any message:

* ticket titles, descriptions and comment text (the reader signs in to see
  them, so the helpdesk's own access checks still decide who reads them);
* passwords, temporary passwords, password hashes, reset tokens, session
  cookies or session versions;
* the name of the administrator or operating-system account that reset a
  password.

Every message is plain text with the subject prefix `[IT Helpdesk]` and the
header `Auto-Submitted: auto-generated`. Account-security messages end with
a warning to contact IT through a trusted channel if the change was
unexpected. When `NOTIFICATION_BASE_URL` is set, ticket messages include a
link to the ticket page; otherwise they say "Sign in to the IT Helpdesk to
view ticket #N".

Header values come only from the configured sender, the validated recipient
address and fixed text. A stored value containing a line break cannot add a
header or a recipient: the email library refuses it and the row fails
permanently.

---

## Email addresses

Addresses are managed by an operator on the server:

```bash
flask --app app set-email alice alice@example.org
flask --app app set-email alice --clear
```

* Only a plain address is accepted (`name@example.org`): no display name,
  quoting, spaces or line breaks, at most 254 characters.
* The username must match exactly.
* Each change is recorded in the audit log as `email_changed` with actor
  `cli:<operating-system account>` and the value `set`, `replaced` or
  `cleared`. The address itself is not written to the audit log, because
  audit events are exported to archives.
* Addresses are not required to be unique (one person may have a user and
  an admin account).

---

## Delivery modes

### Local development mode (`NOTIFICATION_DELIVERY=local`, the default outside production)

* Each message is written to `instance/notification-sink/<id>.eml` (or
  `NOTIFICATION_SINK_DIR`). `instance/` is in `.gitignore`, so simulated
  messages — which name real accounts — never reach GitHub.
* The folder is forced to mode `0700` and each file is created `0600`, without
  following symbolic links.
* Every simulated message has the subject prefix `[SIMULATED]` and the header
  `X-Helpdesk-Delivery: simulated; written to a local folder, not sent`. Its
  row gets the status `simulated` (not `sent`) and `delivery_mode = local`.
  `notifications-status` and `notifications-send` both say SIMULATED.
* Writing is idempotent: if `<id>.eml` already exists (a worker stopped after
  writing it), it is kept and counted as delivered, so a retry never writes a
  second copy.
* Nothing is sent over the network.

Read a simulated message with any text viewer, for example
`less instance/notification-sink/00000001.eml`.

### SMTP mode (`NOTIFICATION_DELIVERY=smtp`)

Real delivery through an SMTP server. **No SMTP provider has been chosen yet,
and no real email has been sent during development.** The notification tests
replace SMTP with a fake class, and their fixtures make any DNS lookup or
network connection fail the test.

---

## SMTP configuration

All settings come from environment variables. Only the worker reads them; the
web application needs none. Problems are reported by variable name only; no
value is ever printed.

| Variable | Default | Notes |
|---|---|---|
| `NOTIFICATION_DELIVERY` | `local` (development/testing); **required** in production | `local` or `smtp` |
| `NOTIFICATION_SENDER` | `helpdesk@localhost.invalid` in local mode; **required** for smtp | Plain address |
| `NOTIFICATION_BASE_URL` | none (no links) | `https://host[/path]`; `http://` refused in production |
| `NOTIFICATION_SINK_DIR` | `instance/notification-sink` | Local mode only |
| `NOTIFICATION_SMTP_HOST` | — | Required for smtp |
| `NOTIFICATION_SMTP_PORT` | 587 / 465 / 25 by security mode | |
| `NOTIFICATION_SMTP_SECURITY` | `starttls` | `starttls` (certificate and hostname verified, **no fallback to plain text**), `ssl` (TLS from the first byte), or `none` (only to a relay on `localhost`/`127.0.0.1`/`::1`, and never with credentials) |
| `NOTIFICATION_SMTP_USERNAME` / `NOTIFICATION_SMTP_PASSWORD` | none | Set both or neither. Supplied **only** through the environment — never in Git, never in `.env` on a server. |
| `NOTIFICATION_SMTP_TIMEOUT` | 20 | Seconds, 1–120, for connecting and for each SMTP command |
| `NOTIFICATION_MAX_ATTEMPTS` | 5 | 1–10 deliveries that may have reached the server |
| `NOTIFICATION_RETRY_BASE_SECONDS` | 60 | First retry delay |
| `NOTIFICATION_RETRY_MAX_SECONDS` | 3600 | Longest retry delay |
| `NOTIFICATION_MAX_AGE_HOURS` | 24 | Give up on anything older |
| `NOTIFICATION_LEASE_SECONDS` | 300 | How long a claim is reserved; must be more than 4 × the SMTP timeout |

The password is excluded from `repr()` of the settings and from every log
line, error, status output and stored error. `notifications-check-config`
prints only whether credentials are set.

---

## Queue-processing commands

| Command | What it does | Changes the database? |
|---|---|---|
| `flask --app app notifications-send` | One run: delivers up to `--limit` (default 100) due notifications, then exits. Exit 0 when done; **2** if delivery was paused because the server was unreachable or refused the sign-in; 1 for configuration or schema problems. | Yes (queue rows and attempt history only) |
| `flask --app app notifications-send --loop [--interval 30]` | Runs until SIGTERM or Ctrl+C, finishing the current message first. Waits `--interval` seconds when idle and backs off (doubling up to 15 minutes) while the server is unavailable. | Yes |
| `flask --app app notifications-status` | Counts per status, oldest pending row, last 10 permanent failures (id, event, username, sanitised error). No addresses or content. | Never |
| `flask --app app notifications-check-config` | Validates the settings without connecting to anything. | Never |
| `flask --app app set-email USERNAME ADDRESS` / `--clear` | Sets or removes an account's notification address (audited). | Yes (one account) |

All commands refuse to run unless the schema is current (`flask --app app
init-db`).

Local development:

```bash
flask --app app init-db                     # upgrade a COPY of your database to version 10
flask --app app set-email alice alice@example.org
python app.py                               # use the helpdesk; rows are queued
flask --app app notifications-send          # writes instance/notification-sink/*.eml
flask --app app notifications-status
```

---

## Retries, failures and duplicates

Every failure is classified by the transport:

| Class | Examples | Attempt counted? | What happens |
|---|---|---|---|
| **Deferred** — nothing can have been sent | connection refused, DNS failure, connect timeout, TLS or certificate failure, STARTTLS not offered, sign-in refused (535) | No (`deferrals` + 1) | Back to `pending` with backoff; **the run stops**, so an outage or bad password does not burn every queued message. Fails as expired after `NOTIFICATION_MAX_AGE_HOURS`. |
| **Retry** — may or may not have been sent, or try later | timeout or disconnect during the SMTP transaction, 4xx replies, sender refused | Yes | Back to `pending` with exponential backoff (60 s, 120 s, 240 s … capped at 1 h); `failed` once `attempts` reaches `NOTIFICATION_MAX_ATTEMPTS`. Connection-level errors also stop the run. |
| **Permanent** | 5xx recipient or data refusal, a message that cannot be built | Yes | `failed` immediately; other messages continue. |
| **Cancelled** | recipient deactivated, demoted, deleted, no address, no longer requester | No | `cancelled`, never sent. |
| **Interrupted** | worker killed or machine stopped mid-delivery | Yes (it may have been sent) | Found when its lease expires; recorded as `interrupted`, then retried with backoff or `failed` at the attempt limit. |
| **Expired** | still pending after `NOTIFICATION_MAX_AGE_HOURS` | — | `failed` with "expired before it could be delivered". |

Every outcome adds a row to `notification_attempts`. Stored errors contain
only the stage, the exception class and the numeric SMTP code — for example
`recipient: SMTPRecipientsRefused 550` or `sign-in: SMTPAuthenticationError
535` — never the server's reply text (which can quote addresses), the message
or a credential. They are at most 200 characters.

### Why exactly-once delivery is impossible

Ordinary SMTP cannot guarantee that a message is delivered exactly once. The
server accepts a message when it replies `250` to the end of the data; if the
connection breaks or times out after the server has accepted it but before the
reply reaches the worker, the worker cannot know whether the message was
delivered. The same is true if the worker process dies between the server's
reply and recording the result. Any system must then choose between possibly
sending a duplicate (at-least-once) or possibly losing the message
(at-most-once).

**Chosen behaviour: at-least-once, with bounded duplicates.**

* Duplicates are only possible after an ambiguous failure (a timeout or
  disconnect mid-transaction, or a worker killed mid-delivery).
* `attempts` counts every delivery that might have reached the server, is
  increased *before* the send, and is capped by `NOTIFICATION_MAX_ATTEMPTS`
  (default 5). One notification can therefore never be emailed more than that
  many times, however often workers crash or the server misbehaves. Unlimited
  duplicates are impossible.
* Failures that prove nothing was sent (could not connect, sign-in refused,
  recipient cancelled) do not count, so an outage does not use up attempts.
* Every attempt uses the same `Message-ID`, derived from the notification id
  and its dedupe key, so mail systems that discard repeated Message-IDs can
  drop a duplicate.
* The local sink is idempotent (one file per notification).
* A claim's lease (default 300 s) is more than four times the SMTP timeout, so
  a slow but healthy delivery is never taken over by a second worker.

Operators who prefer at-most-once can set `NOTIFICATION_MAX_ATTEMPTS=1`:
ambiguous failures then fail permanently instead of being retried.

---

## Security considerations

* **No credentials in code or Git.** SMTP credentials come only from the
  environment. `.env.example` documents the variables without values; real
  passwords belong in the service manager's environment (for example a systemd
  `EnvironmentFile` readable only by the service account).
* **Encrypted transport by default.** STARTTLS with certificate and hostname
  verification is the default and never falls back to plain text. Plain SMTP
  is accepted only to a relay on the same machine and never with credentials.
* **Bounded network calls.** Every SMTP operation has a timeout.
* **Least information.** Messages carry ticket numbers and statuses, not
  ticket contents. Recipients are re-checked at delivery.
* **No enumeration or flooding.** Failed sign-ins never notify. The web forms
  behave exactly as before; nothing a user types can choose a recipient.
* **Private local data.** Simulated messages live in a `0700` folder that Git
  ignores.
* **Sanitised diagnostics.** Errors, logs and status output never contain
  passwords, server replies, message bodies or addresses (the status command
  shows usernames only).
* **Existing protections unchanged.** Authentication, CSRF, sessions, rate
  limits, the password policy, ownership and assignment rules are untouched;
  the full existing suite still passes.
* **Append-only history.** `notification_attempts` cannot be edited or deleted
  through SQL, like `ticket_history` and `audit_events`.

---

## Deployment requirements

1. **Back up, then migrate** the production database to version 10:
   `flask --app app init-db --backup-to /secure/backups/helpdesk-pre-v10.db`.
2. **Choose an SMTP provider** (open decision) and create a dedicated sending
   account with the smallest possible permissions. Configure SPF, DKIM and
   DMARC for the sender domain.
3. **Set the environment** for the worker service: `APP_ENV=production`,
   `SECRET_KEY`, `NOTIFICATION_DELIVERY=smtp`, `NOTIFICATION_SENDER`,
   `NOTIFICATION_SMTP_HOST`, credentials, and `NOTIFICATION_BASE_URL=https://…`.
   Run `flask --app app notifications-check-config`.
4. **Set addresses** with `flask --app app set-email` for the pilot accounts.
5. **Dry run first**: `NOTIFICATION_DELIVERY=local` in production writes
   simulated messages only. Inspect them, then switch to `smtp`.
6. **Run the worker under supervision**, as the same operating-system account
   that owns the database, for example:

   ```ini
   # /etc/systemd/system/helpdesk-notifications.service
   [Unit]
   Description=IT Helpdesk notification worker
   After=network-online.target

   [Service]
   User=helpdesk
   WorkingDirectory=/srv/helpdesk
   EnvironmentFile=/etc/helpdesk/notifications.env   # mode 0600
   ExecStartPre=/srv/helpdesk/.venv/bin/flask --app app db-status
   ExecStart=/srv/helpdesk/.venv/bin/flask --app app notifications-send --loop --interval 30
   Restart=on-failure
   RestartSec=30
   KillSignal=SIGTERM
   TimeoutStopSec=150

   [Install]
   WantedBy=multi-user.target
   ```

   `TimeoutStopSec` should exceed the time one delivery can take (several
   SMTP round trips of up to `NOTIFICATION_SMTP_TIMEOUT` each). Alternatively,
   run `notifications-send` (without `--loop`) from a timer or cron job and
   alert on exit status 2.
7. **Monitor** `flask --app app notifications-status`: a growing `pending`
   count or new `failed` rows need attention. Logs go to stderr (journald).
8. Running more than one worker is safe but unnecessary for a helpdesk.

---

## Known limitations

* **No exactly-once delivery** (see above). Duplicates are possible after an
  ambiguous failure, at most `NOTIFICATION_MAX_ATTEMPTS` per notification.
* **Delivery is not confirmed.** `sent` means the SMTP server accepted the
  message, not that it reached the inbox. Bounces are not processed.
* **No web interface** for email addresses or for the queue; both are managed
  with CLI commands on the server.
* **No manual requeue command.** A permanently failed notification is not
  retried; after fixing the cause an operator can only change the row with SQL.
* **No retention policy** for `notifications`, `notification_attempts` or the
  local sink folder yet; rows are kept indefinitely.
* **No notification preferences or opt-out.** Every rule applies to everyone.
* **No digests or rate limiting per recipient.** A busy ticket produces one
  email per qualifying event.
* **No `email_changed` notification.** Because the worker uses the current
  address, a message to the old address is not possible in this design.
* **Single SQLite database.** Workers must run on the same machine as the
  database file.
* **Plain-text English messages only.**
* **Notifications are not shown in the web interface.**

---

## Decisions for review

These are not decided by the code; each has a conservative default.

| Decision | Current default |
|---|---|
| SMTP provider, sender address and domain authentication | Not chosen; local simulated delivery only |
| Should all staff be told about every new ticket, or only a triage group? | All active technicians and administrators |
| Should requesters be told when a ticket is assigned or moved to In Progress? | No |
| Should ticket titles appear in emails? | No — numbers and statuses only |
| Who may set email addresses, and should users manage their own? | Operators via `flask set-email` only |
| Retention period for notification rows, attempt history and the sink folder | Kept indefinitely |
| At-least-once (default 5 attempts) or at-most-once (`NOTIFICATION_MAX_ATTEMPTS=1`) | At-least-once, 5 attempts |
| Maximum age before giving up | 24 hours |
