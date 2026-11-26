# Known Limitations: 1.0.0-rc1

## Deployment and operations

* **Never deployed.** Everything was verified on a development machine and
  in CI. The systemd units, Redis ACL, firewall, real certificates and log
  rotation have not run on a server.
* **Single server, single SQLite file.** No high availability or
  replication; recovery is from backup (see [disaster recovery](DISASTER_RECOVERY.md)).
* **No off-server backup location exists yet**, and the offline copy of
  the backup key is a manual procedure.
* **Nightly backups** mean up to 24 hours of data can be lost, unless the
  schedule is changed.
* **No "down" migrations.** Undoing a schema upgrade means restoring the
  pre-upgrade backup.
* Server health (disk, memory, certificate expiry) is not monitored beyond
  `ops-check`'s disk check; an external uptime check is required.

## Security

* **No multi-factor authentication.**
* **No independent security review or penetration test** has been carried
  out; only developer-run automated checks.
* Rate limits are per client address; there is no per-account lockout.
* Logout ends **all** sessions of the account; individual devices cannot
  be signed out separately.
* If Redis is unavailable, **sign-in and password changes are refused**
  (by design: fail closed). On a heavily overloaded host, Redis answers
  slower than the 1-second limit can also cause refusals.
* Pages load Bootstrap from a public CDN (pinned with integrity hashes);
  without access to it the pages work but are unstyled.

## Features

* **No self-service password reset**; an administrator resets passwords.
* **No email delivery has been enabled**: no SMTP provider has been chosen,
  so notifications are simulated (written to files). Real SMTP delivery has
  only been tested against fake local servers.
* Email addresses are set only from the server command line.
* No file attachments, no ticket editing or deletion, no
  categories, no knowledge base.
* SLA targets are example values in calendar hours, without business
  hours, holidays or pausing. Only the first-response time is measured,
  not the resolution time. There are no automatic SLA alert emails.
* Search covers title and description only; no date-range or assignee
  search.
* Usernames cannot be changed. Accounts cannot be deleted from the web
  interface (deactivate instead).
* Removing archived audit events is designed but not implemented
  ([AUDIT_RETENTION.md](AUDIT_RETENTION.md)).
* Notification delivery is **at-least-once**: after an interrupted
  delivery a message may arrive twice (same Message-ID).
* On databases first upgraded by the retired `migrate.py`, new tickets show
  "Not recorded" as their last-updated time until first updated.

## Testing

* Browser tests run the application in testing mode over plain HTTP. HTTPS
  and production cookies are covered separately by the Caddy proxy check.
* In the browser tests the moved clock changes what pages show, but stored
  times use the real clock, so a late ("Breached") response is covered by
  unit tests only.
* Load figures come from a development machine without TLS and are
  indicative only.
* The backup, production and reliability rehearsals were run with scripts
  that are not part of the repository.
