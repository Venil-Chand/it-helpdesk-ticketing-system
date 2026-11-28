# Staging runbook: Ubuntu 24.04 LTS

Step-by-step installation of the helpdesk on **one fresh Ubuntu 24.04 LTS
server** for a staging rehearsal or a controlled internal pilot. It uses
the architecture described in
[PRODUCTION_OPERATIONS.md](PRODUCTION_OPERATIONS.md): Caddy (HTTPS) in
front of Gunicorn, Redis for rate limits, SQLite, the notification worker,
and systemd timers for backups, drills and checks. That guide explains
*why*; this runbook is the exact *how* and corrects four of its earlier
instructions:

| Earlier instruction | Problem on Ubuntu 24.04 | Corrected here |
|---|---|---|
| `apt install python3.13` | Ubuntu 24.04 ships Python 3.12 only | Phase 3: a separate, checksum-verified CPython 3.13 in `/opt/helpdesk/python`; the system Python is not touched |
| `apt install caddy` | Ubuntu's package is older than the Caddy 2.7 the Caddyfile needs | Phase 2: Caddy's official stable repository, signing key checked by fingerprint |
| `sudo -u helpdesk -E flask …` | The secrets are root-only (0600), so the service account cannot read them | `helpdesk-admin` (phase 5): systemd passes the secrets to one command, as it does for the services |
| `pip install -r requirements.txt` | Ranges and unpinned dependencies: the server could install versions CI never tested | Phase 5: `requirements.lock`, every package pinned and hash-checked, the same file CI installs |
| Off-server copy: "uncomment `ReadWritePaths`" | Easy to miss; an unmounted share silently fills the local disk | Phase 12: the backup unit already allows `/mnt/helpdesk-offsite`; `run-backup.sh` refuses a folder that is not a separate mount |

**How this runbook is tested.** Every block marked `runbook:` in this file
is executed, in order, by the `Staging runbook` GitHub Actions workflow
([`staging-runbook.yml`](../.github/workflows/staging-runbook.yml)) on a
disposable GitHub-hosted Ubuntu 24.04 virtual machine, followed by the
acceptance checks that can be automated there (section *Acceptance*). The
blocks without a marker need a person or a real server: SSH, firewall,
DNS, certificates, passwords typed at a prompt, the off-server share and
the alert destination. A runner is not a pristine installation (GitHub
pre-installs tools) and is not reachable from the internet, so the
workflow proves the procedure works together, not that your server is
ready; that is what the acceptance checklist is for.

**Nothing in this runbook has been run on a real server yet.**

---

## 0. Before you start

You need (decided and available):

* a VM: **Ubuntu 24.04 LTS** (x86-64), 2 vCPU, 2 GB RAM, 20 GB SSD
  (minimum 1 vCPU, 1 GB RAM + swap); outbound HTTPS to the Ubuntu
  mirrors, `dl.cloudsmith.io`, `github.com`, `pypi.org`/
  `files.pythonhosted.org`, `python-build-standalone` downloads
  (GitHub) and, for public certificates, Let's Encrypt; NTP;
* a **dedicated staging hostname** (see *HSTS* below) and its DNS record;
* the certificate choice: `acme` (public Let's Encrypt; ports 80 and 443
  must be reachable from the internet) or `internal` (Caddy's own CA;
  testers install its root certificate) or your organisation's CA;
* an **off-server storage** location (network share or disk on other
  hardware) and its credentials;
* an alert destination (mail relay, chat webhook or pager) and an
  external uptime monitor;
* a password manager (or safe) for the backup key and the first
  administrator's password, and the named person who holds them;
* a reviewed release tag that contains `requirements.lock` and
  `deploy/scripts/helpdesk-admin` (**`v1.0.0-rc1` does not**; a later tag
  is needed, see the release notes of that tag).

No real email is sent: notifications stay `local` (written to a folder)
in staging. No existing database is copied: the staging database is
created empty (phase 8).

### Variables

Every block below assumes a root shell (`sudo -i`) in which these
variables are loaded. Create the file once, with **your** values:

```bash
umask 077
cat > /root/helpdesk-staging.vars <<'EOF'
STAGING_HOST=helpdesk-staging.example.org
TLS_MODE=acme
ACME_EMAIL=ops@example.org
REPO_URL=https://github.com/Venil-Chand/it-helpdesk-ticketing-system.git
RELEASE_TAG=REPLACE-WITH-THE-REVIEWED-TAG
RELEASE_COMMIT=REPLACE-WITH-THE-40-CHARACTER-COMMIT-OF-THAT-TAG
OFFSITE_MOUNT=/mnt/helpdesk-offsite
EOF
```

`TLS_MODE` is `acme` or `internal`. `RELEASE_COMMIT` is checked against
the tag in phase 5: take it from the reviewed pull request or release
page, not from the server.

Then append the fixed values (pinned versions and derived paths; do not
change them unless you are deliberately upgrading):

<!-- runbook: vars-fixed -->
```bash
cat >> /root/helpdesk-staging.vars <<'EOF'
# --- fixed by this runbook ---
UV_VERSION=0.10.4
UV_SHA256=6b52a47358deea1c5e173278bf46b2b489747a59ae31f2a4362ed5c6c1c269f7
PYTHON_VERSION=3.13.12
HELPDESK_PYTHON=/opt/helpdesk/python/cpython-3.13.12-linux-x86_64-gnu/bin/python3.13
CADDY_KEY_FINGERPRINT=65760C51EDEA2017CEA2CA15155B6D79CA56EA34
CADDY_MIN_VERSION=2.7.0
REL=/opt/helpdesk/releases/$RELEASE_COMMIT
EOF
chmod 0600 /root/helpdesk-staging.vars
```

In every new root shell: `. /root/helpdesk-staging.vars`. Run the blocks
one at a time and read their output before going on (the CI workflow runs
each one with `bash -euo pipefail`, so any failing command stops it).

---

## 1. Base system (server only)

These change SSH, the firewall and system settings, so they are not run
on the CI runner. Adapt them to your organisation's baseline.

```bash
apt-get update && apt-get -y full-upgrade
apt-get install -y unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades

# Named administrators: personal accounts with SSH keys and sudo
#   adduser alice && usermod -aG sudo alice && install keys in ~alice/.ssh
# Then, in /etc/ssh/sshd_config.d/10-helpdesk.conf:
#   PasswordAuthentication no
#   PermitRootLogin no
systemctl reload ssh

# Firewall: SSH from administrators only, HTTP/HTTPS from users
ufw default deny incoming
ufw default allow outgoing
ufw allow from <admin network> to any port 22 proto tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable

timedatectl set-ntp true

# Journal size and retention (adjust to your data-protection policy)
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=1G\nMaxRetentionSec=30day\n' \
    > /etc/systemd/journald.conf.d/helpdesk.conf
systemctl restart systemd-journald
```

## 2. Packages and Caddy

Ubuntu's own packages for Redis, SQLite and Git; Caddy from Caddy's
official stable repository. The repository's signing key is downloaded
and **refused unless its fingerprint matches** the one recorded above.

<!-- runbook: packages -->
```bash
. /root/helpdesk-staging.vars
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg git sqlite3 jq redis-server redis-tools

# The Caddy repository key, checked before it is trusted
work=$(mktemp -d)
curl -fsSL -o "$work/caddy.asc" https://dl.cloudsmith.io/public/caddy/stable/gpg.key
fingerprint=$(GNUPGHOME="$work" gpg --batch --show-keys --with-colons "$work/caddy.asc" \
    | awk -F: '$1 == "fpr" { print $10; exit }')
if [ "$fingerprint" != "$CADDY_KEY_FINGERPRINT" ]; then
    echo "Unexpected Caddy signing key: $fingerprint" >&2
    exit 1
fi
GNUPGHOME="$work" gpg --batch --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg "$work/caddy.asc"
chmod 0644 /usr/share/keyrings/caddy-stable-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" \
    > /etc/apt/sources.list.d/caddy-stable.list
rm -rf "$work"

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y caddy

# At least the version the Caddyfile needs (CI validates it with 2.11.9)
installed=$(caddy version | awk '{ print $1 }' | sed 's/^v//')
lowest=$(printf '%s\n%s\n' "$CADDY_MIN_VERSION" "$installed" | sort -V | head -1)
if [ "$lowest" != "$CADDY_MIN_VERSION" ]; then
    echo "Caddy $installed is older than $CADDY_MIN_VERSION" >&2
    exit 1
fi
echo "Caddy $installed installed from the official repository"
```

Caddy updates arrive with `apt upgrade` from that repository
(`unattended-upgrades` only installs Ubuntu's own security updates by
default). After every Caddy update, run the validation in phase 10 again.

## 3. Python 3.13 (separate from the system Python)

Ubuntu 24.04's `python3` is 3.12 and is used by the operating system: it
is **not** replaced or upgraded. The helpdesk gets its own CPython 3.13
in `/opt/helpdesk/python`, installed by `uv` (whose download is checked
against the SHA-256 recorded above; `uv` in turn checks the Python
download against its own built-in checksums). Nothing is added to `PATH`:
`--no-bin` installs no `python3.13` command.

<!-- runbook: python -->
```bash
. /root/helpdesk-staging.vars
work=$(mktemp -d)
curl -fsSL -o "$work/uv.tar.gz" \
    "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz"
echo "$UV_SHA256  $work/uv.tar.gz" | sha256sum --check --strict -
tar -xzf "$work/uv.tar.gz" -C "$work"
install -d -m 0755 /opt/helpdesk /opt/helpdesk/bin /opt/helpdesk/python
install -m 0755 "$work/uv-x86_64-unknown-linux-gnu/uv" /opt/helpdesk/bin/uv
rm -rf "$work"

/opt/helpdesk/bin/uv python install --no-bin --install-dir /opt/helpdesk/python "$PYTHON_VERSION"
"$HELPDESK_PYTHON" -VV
echo "System Python, unchanged: $(/usr/bin/python3 -V)"
```

## 4. Service account and folders

<!-- runbook: accounts -->
```bash
. /root/helpdesk-staging.vars
adduser --system --group --home /var/lib/helpdesk --no-create-home \
    --shell /usr/sbin/nologin helpdesk
install -d -m 0700 -o helpdesk -g helpdesk /var/lib/helpdesk \
    /var/lib/helpdesk/state /var/lib/helpdesk/notification-sink \
    /var/backups/helpdesk
install -d -m 0755 -o root -g root /opt/helpdesk/releases /opt/helpdesk/tools
install -d -m 0750 -o root -g helpdesk /etc/helpdesk
install -d -m 0700 -o root -g root /etc/helpdesk/credentials
```

## 5. The release

The tag is cloned, checked against the expected commit, and installed
into its own virtual environment from **`requirements.lock`** with
`--require-hashes`: pip refuses any package, version or file not listed
there with its SHA-256, so the server gets exactly the set CI tested.

<!-- runbook: release -->
```bash
. /root/helpdesk-staging.vars
git clone --quiet --depth 1 --branch "$RELEASE_TAG" "$REPO_URL" "$REL"
actual=$(git -C "$REL" rev-parse HEAD)
if [ "$actual" != "$RELEASE_COMMIT" ]; then
    echo "Tag $RELEASE_TAG is $actual, expected $RELEASE_COMMIT" >&2
    exit 1
fi
test -f "$REL/requirements.lock" -a -f "$REL/deploy/scripts/helpdesk-admin" || {
    echo "This release predates the staging runbook (no lock or helpdesk-admin)" >&2
    exit 1
}

"$HELPDESK_PYTHON" -m venv "$REL/.venv"
"$REL/.venv/bin/python" -m pip install --quiet --no-input --require-hashes \
    -r "$REL/requirements.lock"
"$REL/.venv/bin/python" -m pip check
"$REL/.venv/bin/python" -m pip freeze --all > "/root/helpdesk-$RELEASE_COMMIT-freeze.txt"

# Known vulnerabilities in exactly these versions (the tool lives apart
# from the application's environment)
test -x /opt/helpdesk/tools/audit/bin/pip-audit || {
    "$HELPDESK_PYTHON" -m venv /opt/helpdesk/tools/audit
    /opt/helpdesk/tools/audit/bin/python -m pip install --quiet --no-input pip-audit
}
/opt/helpdesk/tools/audit/bin/pip-audit --strict --disable-pip -r "$REL/requirements.lock"

# Code is root-owned and read-only for the service
chown -R root:root "$REL"
chmod -R go-w "$REL"
ln -sfn "$REL" /opt/helpdesk/current
install -m 0755 -o root -g root "$REL/deploy/scripts/helpdesk-admin" /usr/local/sbin/helpdesk-admin
```

### Operator commands: `helpdesk-admin`

Every `flask --app app …` command and `setup_accounts.py` must run as
the `helpdesk` account **with the production settings and secrets**. The
secrets stay root-only; `helpdesk-admin` (run with `sudo`) asks systemd to
start the command as a one-off service with `LoadCredential=`, exactly as
the real units receive them. The secrets are never in the environment,
the shell history or `ps` output, and disappear when the command ends.

```bash
sudo helpdesk-admin flask --app app db-status
sudo helpdesk-admin flask --app app ops-check
sudo helpdesk-admin python setup_accounts.py                        # interactive
sudo helpdesk-admin --release /opt/helpdesk/releases/<commit> flask --app app db-status
```

The exit status is the command's own. Do not use `sudo -u helpdesk` for
these commands: it cannot read the secrets, and copying a secret to a
place the account can read would weaken it.

## 6. Redis

Redis listens on a Unix socket only, with the default account disabled
and a `helpdesk` account limited to the rate limiter's commands and keys
(`deploy/redis/`). The settings are added as an `include` at the end of
Ubuntu's `redis.conf`, so the package's own file is otherwise unchanged
and later lines win.

<!-- runbook: redis -->
```bash
. /root/helpdesk-staging.vars
install -m 0640 -o root -g redis "$REL/deploy/redis/redis-helpdesk.conf" /etc/redis/helpdesk.conf
grep -qx 'include /etc/redis/helpdesk.conf' /etc/redis/redis.conf \
    || echo 'include /etc/redis/helpdesk.conf' >> /etc/redis/redis.conf
usermod -aG redis helpdesk
```

## 7. Secrets and settings

All secrets are generated **on the server** and never typed, pasted or
sent anywhere. The Redis password goes into two files (Redis's ACL and
the helpdesk's connection string) without appearing on a command line.

<!-- runbook: secrets -->
```bash
. /root/helpdesk-staging.vars
umask 077
/usr/bin/python3 -c 'import secrets; print(secrets.token_hex(32))' \
    > /etc/helpdesk/credentials/secret-key
REDIS_PASSWORD=$(/usr/bin/python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
printf 'redis+unix://helpdesk:%s@/run/redis/redis-server.sock\n' "$REDIS_PASSWORD" \
    > /etc/helpdesk/credentials/redis-uri
REDIS_PASSWORD="$REDIS_PASSWORD" /usr/bin/python3 -c '
import os, sys
text = sys.stdin.read()
assert text.count(">CHANGE-ME") == 1
sys.stdout.write(text.replace(">CHANGE-ME", ">" + os.environ["REDIS_PASSWORD"]))
' < "$REL/deploy/redis/users.acl" > /etc/redis/users.acl
unset REDIS_PASSWORD
chown redis:redis /etc/redis/users.acl
chmod 0600 /etc/redis/users.acl /etc/helpdesk/credentials/*
systemctl restart redis-server

# Non-secret settings, from the template
install -m 0640 -o root -g helpdesk "$REL/deploy/helpdesk.env.example" /etc/helpdesk/helpdesk.env
sed -i -e "s|^TRUSTED_HOSTS=.*|TRUSTED_HOSTS=$STAGING_HOST|" \
    -e "s|^NOTIFICATION_BASE_URL=.*|NOTIFICATION_BASE_URL=https://$STAGING_HOST|" \
    /etc/helpdesk/helpdesk.env
grep -E '^(APP_ENV|TRUSTED_HOSTS|NOTIFICATION_DELIVERY|NOTIFICATION_BASE_URL)=' /etc/helpdesk/helpdesk.env
```

`NOTIFICATION_DELIVERY` stays `local`: simulated messages are written to
`/var/lib/helpdesk/notification-sink`, nothing is sent.

## 8. systemd units and the disposable database

<!-- runbook: units -->
```bash
. /root/helpdesk-staging.vars
install -m 0644 -o root -g root "$REL"/deploy/systemd/helpdesk-* /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/helpdesk-web.service \
    /etc/systemd/system/helpdesk-notifications.service \
    /etc/systemd/system/helpdesk-backup.service /etc/systemd/system/helpdesk-backup.timer \
    /etc/systemd/system/helpdesk-backup-drill.service /etc/systemd/system/helpdesk-backup-drill.timer \
    /etc/systemd/system/helpdesk-ops-check.service /etc/systemd/system/helpdesk-ops-check.timer
systemctl daemon-reload
```

A **new, empty** database: nothing is copied from any other machine.
Then the backup encryption key, created by the application and moved
into the root-only credentials folder.

<!-- runbook: database -->
```bash
. /root/helpdesk-staging.vars
helpdesk-admin flask --app app init-db
helpdesk-admin flask --app app db-status

helpdesk-admin flask --app app backup-generate-key /var/lib/helpdesk/new-backup-key
install -m 0600 -o root -g root /var/lib/helpdesk/new-backup-key /etc/helpdesk/credentials/backup-key
shred -u /var/lib/helpdesk/new-backup-key
```

**Store the backup key offline now** (password manager or sealed
envelope; without it no backup can be restored, with it anyone holding a
backup can read it):

```bash
cat /etc/helpdesk/credentials/backup-key     # copy into the password manager, then clear the screen
```

**The first administrator** (always named `admin`; you type the password
twice, it is not echoed):

```bash
helpdesk-admin python setup_accounts.py
```

## 9. Web service

<!-- runbook: web -->
```bash
. /root/helpdesk-staging.vars
systemctl enable --now helpdesk-web
systemctl is-active helpdesk-web
# On the server, as Caddy will send it
curl -fsS -H "Host: $STAGING_HOST" -H 'X-Forwarded-Proto: https' http://127.0.0.1:8000/readyz
echo
```

Expected: `{"checks":{…"ok"…},"status":"ready"}`. If the unit does not
start: `journalctl -u helpdesk-web -n 50` names the failing check.

## 10. Caddy (HTTPS)

The repository's Caddyfile with your hostname, and either the ACME
contact address or Caddy's internal CA. It is validated **as the `caddy`
user** (validating as root would create log files Caddy cannot write
later) before it is loaded.

<!-- runbook: caddy -->
```bash
. /root/helpdesk-staging.vars
install -m 0644 -o root -g root "$REL/deploy/Caddyfile" /etc/caddy/Caddyfile
/usr/bin/python3 - "$STAGING_HOST" "$TLS_MODE" "${ACME_EMAIL:-}" <<'EOF'
import sys
host, mode, email = sys.argv[1:4]
path = "/etc/caddy/Caddyfile"
text = open(path).read()
assert text.count("\nhelpdesk.example.org {\n") == 1
text = text.replace("helpdesk.example.org", host)
if mode == "internal":
    text = text.replace("\temail admin@example.org\n\n", "")
    text = text.replace(f"\n{host} {{\n", f"\n{host} {{\n\ttls internal\n", 1)
elif mode == "acme":
    if not email:
        sys.exit("ACME_EMAIL is required with TLS_MODE=acme")
    text = text.replace("admin@example.org", email)
else:
    sys.exit("TLS_MODE must be acme or internal")
open(path, "w").write(text)
EOF
runuser -u caddy -- caddy fmt --diff /etc/caddy/Caddyfile >/dev/null
runuser -u caddy -- caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl reload caddy
systemctl is-active caddy
```

For your organisation's CA instead, replace the `tls internal` line by
`tls /etc/caddy/certs/fullchain.pem /etc/caddy/certs/privkey.pem` (files
readable by the `caddy` user only) and validate again.

With `TLS_MODE=internal`, give testers Caddy's root certificate
(`/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`) to
install as a trusted authority **before** their first visit (see *HSTS*).

### HSTS on the staging hostname

The application sends `Strict-Transport-Security: max-age=31536000`
(one year) on every HTTPS response in production, **without**
`includeSubDomains` or `preload`. Staging runs with `APP_ENV=production`
on purpose (so that it tests the production behaviour), and this is not
weakened for staging. What follows from it:

* **Use a dedicated hostname** such as `helpdesk-staging.<your domain>`,
  never the future production name and never a name that ever needs
  plain HTTP. HSTS then binds only that exact name: other names on the
  domain, including subdomains, are unaffected.
* **The certificate must be trusted before the first visit.** With HSTS a
  browser offers no "continue anyway" button. With `tls internal`,
  install Caddy's root certificate first; with an unknown CA the site is
  unreachable for that browser for a year.
* **Do not submit the staging name to the HSTS preload list.**
* Removing the staging server later: each browser keeps the HSTS entry
  until it expires (or is deleted by hand, for example
  `chrome://net-internals/#hsts`); harmless if the name is not reused.
* `curl` and scripts ignore HSTS; port 80 is still answered by Caddy with
  a redirect to HTTPS.

## 11. Notification worker

<!-- runbook: worker -->
```bash
. /root/helpdesk-staging.vars
systemctl enable --now helpdesk-notifications
systemctl is-active helpdesk-notifications
```

## 12. Off-server backup storage

The storage must be on **other hardware**, mounted at `$OFFSITE_MOUNT`
(default `/mnt/helpdesk-offsite`), with a folder inside it owned by
`helpdesk` with mode 0700. Mount it (site-specific; examples, pick one,
in `/etc/fstab`):

```bash
# NFS export (the export must map to the helpdesk account's uid/gid):
#   nas.example.org:/export/helpdesk  /mnt/helpdesk-offsite  nfs  rw,nosuid,nodev,noexec,_netdev,nofail  0 0
# SMB/CIFS share (apt install cifs-utils; credentials in a root-only 0600 file):
#   //nas.example.org/helpdesk  /mnt/helpdesk-offsite  cifs  credentials=/etc/helpdesk/offsite.cred,uid=helpdesk,gid=helpdesk,dir_mode=0700,file_mode=0600,nosuid,nodev,noexec,_netdev,nofail  0 0
# A second disk:
#   UUID=<uuid>  /mnt/helpdesk-offsite  ext4  nosuid,nodev,noexec,nofail  0 2
install -d -m 0755 /mnt/helpdesk-offsite
mount /mnt/helpdesk-offsite
```

`nofail` keeps the server booting when the share is down; the nightly
backup is then still made locally, and the job fails (and alerts)
because the off-server folder is not mounted.

Then create the folder and point the helpdesk at it:

<!-- runbook: offsite -->
```bash
. /root/helpdesk-staging.vars
mountpoint -q "$OFFSITE_MOUNT" || {
    echo "Mount the off-server storage at $OFFSITE_MOUNT first" >&2
    exit 1
}
runuser -u helpdesk -- mkdir -p -m 0700 "$OFFSITE_MOUNT/helpdesk"
stat -c '%U %a %n' "$OFFSITE_MOUNT/helpdesk"
grep -q '^HELPDESK_BACKUP_OFFSITE_DIR=' /etc/helpdesk/helpdesk.env \
    || echo "HELPDESK_BACKUP_OFFSITE_DIR=$OFFSITE_MOUNT/helpdesk" >> /etc/helpdesk/helpdesk.env

# The backup unit may write to /mnt/helpdesk-offsite; another mount point
# needs the same permission, given in a drop-in (the unit stays unchanged)
if [ "$OFFSITE_MOUNT" != /mnt/helpdesk-offsite ]; then
    install -d -m 0755 /etc/systemd/system/helpdesk-backup.service.d
    printf '[Service]\nReadWritePaths=-%s\n' "$OFFSITE_MOUNT" \
        > /etc/systemd/system/helpdesk-backup.service.d/offsite.conf
    systemctl daemon-reload
fi
```

`stat` must show `helpdesk 700`. If a share's own permissions cannot be
restricted to the helpdesk account, the copy is refused (the backups are
encrypted, but the folder must still be private).

## 13. Backups, drill and monitoring

A first backup (with its off-server copy) and drill by hand, then the
timers: nightly backup, weekly drill, `ops-check` every 5 minutes.

<!-- runbook: backups -->
```bash
. /root/helpdesk-staging.vars
systemctl start helpdesk-backup
journalctl -u helpdesk-backup -n 5 --no-pager -o cat
ls -l /var/backups/helpdesk "$OFFSITE_MOUNT/helpdesk"
systemctl start helpdesk-backup-drill
journalctl -u helpdesk-backup-drill -n 3 --no-pager -o cat
systemctl enable --now helpdesk-backup.timer helpdesk-backup-drill.timer helpdesk-ops-check.timer
systemctl list-timers 'helpdesk-*' --no-pager
helpdesk-admin flask --app app ops-check || [ $? -eq 1 ]
```

`ops-check` should report only the expected warning that notification
delivery is simulated (exit status 1 = warnings only).

**Alerts** (site-specific, not automated): write `/etc/helpdesk/alert-hook`
(root-owned, mode 0750) that forwards its argument, the failed unit's
name, to your alert destination, without printing secrets. For example,
through the server's mail relay:

```bash
#!/bin/sh
printf 'Helpdesk staging: %s failed on %s\n' "$1" "$(hostname)" \
    | mail -s "helpdesk alert: $1" ops@example.org
```

Then test it (phase 14, A10) and add an external uptime check of
`https://$STAGING_HOST/healthz`.

## 14. Pilot accounts

Sign in at `https://$STAGING_HOST` as `admin`, open **Admin → Users →
New**, and create role-named accounts (no real names), for example
`pilot-tech1`, `pilot-tech2` (technician) and `pilot-user1`…`3` (user),
each with a temporary password given to one person. Each is forced to
choose their own password at first sign-in. Optional addresses for the
simulated notifications use the reserved domain `example.invalid`:

```bash
helpdesk-admin flask --app app set-email pilot-user1 pilot-user1@example.invalid
```

---

## Acceptance

| # | Check | Workflow (CI runner) | Real server |
|---|---|---|---|
| A1 | Every helpdesk unit starts under its sandbox (ready notification, `/dev/shm`, credentials) and is `active` | Yes | Yes |
| A2 | Redis: no TCP listener; `helpdesk` account can `PING`, cannot `SET`/`KEYS`/`CONFIG`; default account refused | Yes | Yes |
| A3 | `helpdesk-admin` works for `db-status`, `ops-check`, `setup_accounts.py` and `backup-restore-live`, and returns the command's exit status; the service account cannot read `/etc/helpdesk/credentials` | Yes | Yes |
| A4 | HTTPS with the chosen certificate; HTTP redirects to HTTPS; `Strict-Transport-Security: max-age=31536000` without `includeSubDomains`; an external TLS scan | Internal CA only | Yes (scan from outside) |
| A5 | From outside, only 22 (admins), 80 and 443 answer; Gunicorn (8000) and Redis are not reachable | Listening sockets only | Yes (port scan) |
| A6 | `/readyz` is 404 from other addresses and 200 from the server itself | Yes | Yes |
| A7 | No secret in the journal or `systemctl show`; Caddy access log without query strings | Yes | Yes |
| A8 | Nightly backup reaches the off-server storage, verified there; an unmounted share fails the job and alerts | Yes (loop-mounted file system) | Yes (real share) |
| A9 | Weekly drill passes; a live restore with services stopped | Yes | Yes |
| A10 | An alert reaches a person; the external uptime monitor alerts when Caddy stops | Alert unit runs | Yes (real destination) |
| A11 | Redis stop (sign-in 503, recovers), worker kill, **reboot** (everything returns without a person) | Restart only | Yes |
| A12 | Rollback: switch `current` to another release and back, services healthy each time | Yes | Yes |
| A13 | UAT checklist ([UAT_CHECKLIST.md](UAT_CHECKLIST.md)) by the named pilot administrator | No | Yes |
| A14 | Response times through TLS on the target hardware (indicative) | No | Yes |
| A15 | The offline backup key is retrieved by its holder and decrypts a backup | No | Yes |

Commands for the server checks:

```bash
. /root/helpdesk-staging.vars
systemctl --no-pager status helpdesk-web helpdesk-notifications caddy redis-server
ss -ltnpx | grep -E ':8000|:80 |:443 |redis'                    # A5 (inside view)
curl -sI "http://$STAGING_HOST/" | head -3                       # A4: 308 to https
curl -sI "https://$STAGING_HOST/login" | grep -i strict-transport  # A4
curl -s -o /dev/null -w '%{http_code}\n' "https://$STAGING_HOST/readyz"                     # A6: 404
curl -s --resolve "$STAGING_HOST:443:127.0.0.1" "https://$STAGING_HOST/readyz"; echo       # A6: ready
runuser -u helpdesk -- cat /etc/helpdesk/credentials/secret-key   # A3: must fail
systemctl stop redis-server; sleep 2; curl -s -o /dev/null -w '%{http_code}\n' \
    "https://$STAGING_HOST/readyz" --resolve "$STAGING_HOST:443:127.0.0.1"; systemctl start redis-server  # A11: 503
systemctl reboot                                                  # A11: then check A1 again
```

From another machine: `nmap -Pn -p- <server>` (A5), an SSL Labs or
`testssl.sh` scan (A4).

---

## Updating to a new release

```bash
. /root/helpdesk-staging.vars            # with RELEASE_TAG/RELEASE_COMMIT/REL set to the NEW release
# 1. Prepare the new release next to the running one: run the "release"
#    block of phase 5 WITHOUT its last two lines (ln -sfn, install
#    helpdesk-admin), so nothing changes yet
# 2. A verified backup
systemctl start helpdesk-backup
# 3. Stop, migrate if needed (with the NEW code), switch, start
systemctl stop helpdesk-web helpdesk-notifications
helpdesk-admin --release "$REL" flask --app app db-status \
    || helpdesk-admin --release "$REL" flask --app app init-db \
        --backup-to "/var/backups/helpdesk/pre-upgrade-$(date -u +%Y%m%dT%H%M%SZ).db"
ln -sfn "$REL" /opt/helpdesk/current
install -m 0755 -o root -g root "$REL/deploy/scripts/helpdesk-admin" /usr/local/sbin/helpdesk-admin
install -m 0644 -o root -g root "$REL"/deploy/systemd/helpdesk-* /etc/systemd/system/ && systemctl daemon-reload
systemctl start helpdesk-web helpdesk-notifications
# 4. Verify
curl -fsS "https://$STAGING_HOST/healthz" && helpdesk-admin flask --app app ops-check
```

Keep the previous release folder until the new one has run for a while.

## Rolling back

* **No schema change:** `ln -sfn /opt/helpdesk/releases/<previous commit>
  /opt/helpdesk/current`, then `systemctl restart helpdesk-web
  helpdesk-notifications`. Seconds; no data lost.
* **The release migrated the schema:** the old code refuses the newer
  database (by design). Restore the pre-upgrade copy (`init-db
  --backup-to`, an unencrypted verified SQLite file: protect and delete
  it like any backup) or the encrypted backup from step 2 as below, then
  switch `current` back. Everything written since the upgrade is lost.

## Restoring the database

```bash
. /root/helpdesk-staging.vars
systemctl stop helpdesk-web helpdesk-notifications helpdesk-backup.timer
helpdesk-admin flask --app app backup-list
helpdesk-admin flask --app app backup-verify /var/backups/helpdesk/helpdesk-<time>.hdbk
helpdesk-admin flask --app app backup-restore-live /var/backups/helpdesk/helpdesk-<time>.hdbk
#   type: replace helpdesk.db    (a safety backup of the replaced database is written first)
helpdesk-admin flask --app app db-status          # "outdated": run init-db
systemctl start helpdesk-web helpdesk-notifications helpdesk-backup.timer
curl -fsS "https://$STAGING_HOST/healthz" && helpdesk-admin flask --app app ops-check
```

If the local copies are damaged, use one from `$OFFSITE_MOUNT/helpdesk`
(`backup-verify` it first). Without the backup key, no backup can be
restored: retrieve the offline copy.

## Tearing staging down

Stop and disable the helpdesk units and timers; delete
`/var/lib/helpdesk` (database, notification sink, state),
`/var/backups/helpdesk`, the staging folder on the off-server storage and
`/etc/helpdesk`; destroy or reimage the VM; remove the DNS record; revoke
the staging backup key's offline copy. The notification sink holds
usernames and addresses, and backups hold the staging database.

## Dependency lock

`requirements.txt` states what the application needs (some ranges);
`requirements.lock` is the **fully resolved** set for Python 3.13 on
x86-64 Linux, every package pinned with the SHA-256 of each released
file. CI, the rehearsals and this runbook all install the lock with
`pip install --require-hashes -r requirements.lock`, and CI fails when the
lock no longer satisfies `requirements.txt`. After changing
`requirements.txt` (including a Dependabot update), regenerate the lock
with uv 0.10.4 and commit both:

```bash
uv pip compile requirements.txt --python-version 3.13 \
    --python-platform x86_64-unknown-linux-gnu --generate-hashes \
    --output-file requirements.lock
```

(The exact command is in the lock's header. Existing pins are kept unless
`--upgrade` or `--upgrade-package NAME` is added.) An ARM (aarch64)
server would need its own lock and a CI run on that platform.
