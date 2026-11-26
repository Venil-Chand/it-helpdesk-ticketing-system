# Security Configuration

The security settings of release 1.0.0-rc1, and what was verified. The
design reasoning is in the README (*Production Security Configuration*,
*Security architecture*); server set-up is in
[PRODUCTION_OPERATIONS.md](PRODUCTION_OPERATIONS.md).

## 1. Environments

| `APP_ENV` | Reads `.env` | Cookies | Intended for |
|---|---|---|---|
| `development` (default) | yes | plain session cookie | one developer's machine |
| `testing` | **no** | as development | automated tests, the browser tests |
| `production` | **no** | `__Host-` + `Secure` | the server; requires HTTPS through Caddy |

Production refuses to start, before serving anything, if a setting is
unsafe. Verified refusals: weak or repetitive `SECRET_KEY`, missing
`TRUSTED_HOSTS`, in-memory rate limits with more than one worker,
Gunicorn bound to a non-loopback address, Redis unreachable or rejecting
the password, and an unmigrated database.

## 2. Required production settings

Non-secret settings go in `/etc/helpdesk/helpdesk.env`
([template](../deploy/helpdesk.env.example)). **Secrets go only in files**
under `/etc/helpdesk/credentials/` (mode 0600, owner root), passed by
systemd `LoadCredential=` and read through `*_FILE` variables.

| Variable | Purpose |
|---|---|
| `APP_ENV=production` | Production behaviour |
| `SECRET_KEY_FILE` | Session signing key (at least 32 random characters) |
| `TRUSTED_HOSTS` | The public host name(s); other `Host` headers get 400 |
| `TRUSTED_PROXY_COUNT=1`, `TRUSTED_PROXY_IPS=127.0.0.1` | Believe `X-Forwarded-*` only from Caddy on this machine |
| `RATELIMIT_STORAGE_URI_FILE` | Redis URL with password (shared limits for all workers) |
| `HELPDESK_DATABASE_PATH` | Outside the code directory, for example `/var/lib/helpdesk/helpdesk.db` |
| `HELPDESK_BIND=127.0.0.1:8000` | Gunicorn on loopback only (enforced) |
| `HELPDESK_BACKUP_KEY_FILE` | Backup encryption key; refused if inside the app or backup folders, or readable by others |
| `NOTIFICATION_SMTP_PASSWORD_FILE` | Only if SMTP is enabled |

## 3. Controls in place

| Area | Control | Verified by |
|---|---|---|
| Transport | HTTPS only through Caddy; HTTP redirected; HSTS | Caddy proxy check (CI) |
| Cookies | `__Host-` prefix, `Secure`, `HttpOnly`, `SameSite=Lax` | Caddy proxy check |
| Sessions | Signed cookie with a per-account version; logout, password change and deactivation revoke all sessions; 8-hour maximum | Unit tests, browser tests, mutation tests |
| CSRF | Token on every form (Flask-WTF), plus referrer checks over HTTPS | Unit tests, browser tests (forged, missing, cross-site) |
| Headers | `Content-Security-Policy` (`default-src 'none'`, scripts and styles only from self and pinned Bootstrap URLs, `frame-ancestors 'none'`, `object-src 'none'`, `form-action 'self'`), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, `Permissions-Policy`, `Cross-Origin-Opener-Policy` and `Cross-Origin-Resource-Policy: same-origin`; `Server` header removed by Caddy | Unit tests, browser test (inline script blocked) |
| Passwords | Werkzeug hashing; at least 16 characters; offline blocklist of common and breached passwords; username not allowed inside | Unit tests, browser tests |
| Brute force | 5 sign-in and 5 password-change attempts per minute per client address, shared through Redis; **fails closed** (503) if Redis is down | Unit tests, rehearsal |
| Proxy trust | Only the configured number of proxies from the configured addresses; forged left-most `X-Forwarded-For` ignored | Unit tests, proxy check, rehearsal |
| Authorisation | Role from the database on every request; users see only their own tickets (404 otherwise); staff-only updates; admin-only pages (403) | Unit tests, browser tests, mutation tests |
| Input | 1 MB request body (Caddy and Flask), form field and length limits | Unit tests, proxy check |
| Exports | CSV formula injection neutralised | Unit tests, browser test |
| Logs | JSON logs without query strings, passwords, cookies or the Redis password | Proxy check, rehearsal |
| Backups | Authenticated encryption, checksums, private files, key kept away from backups | Unit tests, rehearsal |
| Audit | Append-only events for accounts, sign-ins and operations | Unit tests, browser tests |

## 4. What is not in place

* **No multi-factor authentication.**
* **No independent security review or penetration test.**
* No account lockout beyond the per-address rate limit (a distributed
  guessing attack from many addresses is limited only per address).
* No web application firewall or intrusion detection.
* Session revocation is per account, not per device.
* The Bootstrap files come from a public CDN (allowed by exact URL in the
  CSP and pinned with Subresource Integrity hashes), so the pages depend on
  that CDN being reachable.

See [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## 5. Security maintenance

* Dependabot opens weekly update PRs; CI runs `pip-audit --strict` on
  every PR.
* Rotate `SECRET_KEY` to sign everyone out (for example after a
  suspected compromise).
* Follow [PRODUCTION_OPERATIONS.md §11 Security maintenance](PRODUCTION_OPERATIONS.md#11-security-maintenance)
  and [§12 Incident response](PRODUCTION_OPERATIONS.md#12-incident-response).
