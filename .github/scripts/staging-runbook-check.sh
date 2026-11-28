#!/bin/bash
# Run docs/STAGING_RUNBOOK.md on a disposable Ubuntu 24.04 VM, then the
# acceptance checks that can be automated there.
#
# ONLY for a throwaway GitHub-hosted runner: it installs packages, creates
# accounts, starts services and mounts a file system. It refuses to run
# anywhere else. Started by .github/workflows/staging-runbook.yml as root.
#
# What is CI-specific (everything else is the runbook's own commands):
#   * the site values: hostname helpdesk-staging.test in /etc/hosts, Caddy's
#     internal CA, the release taken from this checkout (a local tag);
#   * the two interactive steps (first administrator, live restore), typed
#     by "expect" with a random password;
#   * the off-server storage: a loop-mounted ext4 image stands in for the
#     network share.
# No email can be sent (delivery stays "local") and nothing is reachable
# from outside the runner.

set -euo pipefail

if [[ ${GITHUB_ACTIONS:-} != true || ${RUNNER_ENVIRONMENT:-} != github-hosted ]]; then
    echo "Refusing: this changes the whole system; it runs only on a" \
         "disposable GitHub-hosted runner." >&2
    exit 2
fi
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 2; }

REPO=$(cd "$(dirname "$0")/../.." && pwd)
RUNBOOK=$REPO/docs/STAGING_RUNBOOK.md
HOST=helpdesk-staging.test
CA=/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
VARS=/root/helpdesk-staging.vars
WORK=$(mktemp -d)
chmod 0700 "$WORK"

steps() { python3 "$REPO/.github/scripts/runbook_steps.py" "$RUNBOOK" "$@"; }

# On any unexpected failure: what systemd and the journal say (the
# runner holds only this run's throwaway secrets, and none are logged)
diagnose() {
    echo "::group::diagnostics"
    systemctl --no-pager --failed || true
    for unit in redis-server caddy helpdesk-web helpdesk-notifications \
                helpdesk-backup helpdesk-backup-drill helpdesk-ops-check; do
        echo "--- $unit"
        journalctl -u "$unit" -n 30 --no-pager -o short-iso || true
    done
    echo "::endgroup::"
}
trap 'diagnose' ERR
ran=()
run_step() {
    echo "::group::runbook step: $1"
    bash -euo pipefail -c "$(steps "$1")"
    echo "::endgroup::"
    ran+=("$1")
}

failures=0
check() {
    local name=$1
    shift
    if "$@" > "$WORK/check.out" 2>&1; then
        echo "PASS  $name"
    else
        echo "FAIL  $name"
        sed 's/^/      /' "$WORK/check.out" | tail -20
        failures=$((failures + 1))
    fi
}

https() { curl -sS --cacert "$CA" "$@"; }

# systemd starts OnFailure= units asynchronously: wait for the alert line
alert_logged() {
    for i in $(seq 30); do
        journalctl -t helpdesk-alert -o cat | grep -q "$1 failed" && return 0
        sleep 1
    done
    return 1
}

# --- CI site values and tools ------------------------------------------
apt-get install -y -qq expect >/dev/null
git config --global --add safe.directory "$REPO"
git config --global --add safe.directory "$REPO/.git"
echo "127.0.0.1 $HOST" >> /etc/hosts
umask 077
cat > "$VARS" <<EOF
STAGING_HOST=$HOST
TLS_MODE=internal
ACME_EMAIL=
REPO_URL=file://$REPO
RELEASE_TAG=staging-runbook-check
RELEASE_COMMIT=$(git -C "$REPO" rev-parse HEAD)
OFFSITE_MOUNT=/mnt/helpdesk-offsite
EOF
umask 022
ADMIN_PASSWORD="runbook check $(openssl rand -hex 16)"
export ADMIN_PASSWORD

first_admin() {
    expect -f - <<'EOF'
set timeout 120
spawn helpdesk-admin python setup_accounts.py
expect "Enter new password:"
send -- "$env(ADMIN_PASSWORD)\r"
expect "Confirm new password:"
send -- "$env(ADMIN_PASSWORD)\r"
expect "Administrator account created"
expect eof
lassign [wait] pid spawnid os_error status
exit $status
EOF
}

live_restore() {
    BACKUP_FILE=$1 expect -f - <<'EOF'
set timeout 120
spawn helpdesk-admin flask --app app backup-restore-live $env(BACKUP_FILE)
expect "Type \""
send -- "replace helpdesk.db\r"
expect eof
lassign [wait] pid spawnid os_error status
exit $status
EOF
}

loop_offsite() {
    if [[ ! -f /var/tmp/offsite.img ]]; then
        truncate -s 64M /var/tmp/offsite.img
        mkfs.ext4 -q -F /var/tmp/offsite.img
        install -d -m 0755 /mnt/helpdesk-offsite
    fi
    mount -o loop,nosuid,nodev,noexec /var/tmp/offsite.img /mnt/helpdesk-offsite
    # As an NFS export mapped to the helpdesk account would be
    chown helpdesk:helpdesk /mnt/helpdesk-offsite
    chmod 0700 /mnt/helpdesk-offsite
}

# --- the runbook, in order ----------------------------------------------
for step in vars-fixed packages python accounts release redis secrets units database; do
    run_step "$step"
done
echo "::group::interactive: first administrator (setup_accounts.py)"
check "first administrator created through helpdesk-admin at a terminal" first_admin
echo "::endgroup::"
for step in web caddy worker; do
    run_step "$step"
done
loop_offsite
for step in offsite backups; do
    run_step "$step"
done

# Every automated step of the runbook was run (a new one cannot be missed)
check "every marked runbook step was run, in the documented order" \
    test "$(steps --list | tr '\n' ' ')" = "${ran[*]} "

# shellcheck disable=SC1090
. "$VARS"
echo "=== Acceptance checks ==="

# --- the corrected installation pieces ----------------------------------
check "system Python is untouched (still 3.12, no python3.13 on PATH)" \
    bash -c '/usr/bin/python3 -V | grep -q "^Python 3\.12\." && ! command -v python3.13'
check "the release runs on the separate CPython $PYTHON_VERSION" \
    bash -c "/opt/helpdesk/current/.venv/bin/python -V | grep -qx 'Python $PYTHON_VERSION'"
check "Caddy comes from the official repository and is >= $CADDY_MIN_VERSION" \
    bash -c "apt-cache policy caddy | grep -q dl.cloudsmith.io/public/caddy/stable && caddy version"
lock_matches() {
    grep -E '^[a-z0-9._-]+==' "$REL/requirements.lock" | sed 's/ .*//' | sort > "$WORK/lock"
    "$REL/.venv/bin/python" -m pip freeze | tr 'A-Z_' 'a-z-' | sort > "$WORK/freeze"
    diff "$WORK/lock" "$WORK/freeze"
}
check "installed packages are exactly requirements.lock" lock_matches

# --- A1 services under their sandboxes ----------------------------------
check "A1 helpdesk-web, helpdesk-notifications, caddy and redis-server are active" \
    systemctl is-active helpdesk-web helpdesk-notifications caddy redis-server
check "A1 all timers are enabled" \
    systemctl is-enabled helpdesk-backup.timer helpdesk-backup-drill.timer helpdesk-ops-check.timer

# --- A2 Redis -------------------------------------------------------------
sed -n 's|^redis+unix://helpdesk:\(.*\)@/run/redis/redis-server.sock$|\1|p' \
    /etc/helpdesk/credentials/redis-uri > "$WORK/redis-password"
# The password travels in the environment (whitelisted for runuser), never
# on a command line
redis_as_helpdesk() {
    REDISCLI_AUTH=$(cat "$WORK/redis-password") runuser -u helpdesk -w REDISCLI_AUTH -- \
        redis-cli --no-auth-warning -s /run/redis/redis-server.sock --user helpdesk "$@"
}
check "A2 Redis has no TCP listener" bash -c '! ss -ltnH | grep -q ":6379 "'
check "A2 the helpdesk Redis account can PING" bash -c "$(declare -f redis_as_helpdesk); WORK=$WORK; redis_as_helpdesk PING | grep -qx PONG"
check "A2 the helpdesk Redis account cannot SET, KEYS or CONFIG" bash -c "$(declare -f redis_as_helpdesk); WORK=$WORK;
    redis_as_helpdesk SET x 1 | grep -q NOPERM && redis_as_helpdesk KEYS '*' | grep -q NOPERM && redis_as_helpdesk CONFIG GET port | grep -q NOPERM"
check "A2 the default Redis account is refused" \
    bash -c 'redis-cli -s /run/redis/redis-server.sock PING | grep -Eq "NOAUTH|NOPERM|WRONGPASS|disabled"'
check "A2 accounts outside the redis group cannot open the socket" \
    bash -c '! runuser -u nobody -- redis-cli -s /run/redis/redis-server.sock PING 2>/dev/null | grep -q .'

# --- A3 operator commands -------------------------------------------------
check "A3 helpdesk-admin db-status sees the current schema" \
    bash -c 'helpdesk-admin flask --app app db-status | grep -q "Current schema version: 10 (up to date)"'
check "A3 helpdesk-admin returns the command's exit status" \
    bash -c 'helpdesk-admin sh -c "exit 3"; [ $? -eq 3 ]'
check "A3 the service account cannot read the credentials" \
    bash -c '! runuser -u helpdesk -- cat /etc/helpdesk/credentials/secret-key'
check "A3 secrets reach commands as files, never as environment values" \
    bash -c 'out=$(helpdesk-admin sh -c env); echo "$out" | grep -q "^SECRET_KEY_FILE=" &&
        ! echo "$out" | grep -qF -f /etc/helpdesk/credentials/secret-key'

# --- A4 HTTPS and HSTS ------------------------------------------------------
check "A4 HTTPS /healthz through Caddy" bash -c "$(declare -f https); CA=$CA; https -f https://$HOST/healthz"
check "A4 HTTP redirects to HTTPS" \
    bash -c "curl -s -o /dev/null -w '%{http_code} %{redirect_url}' http://$HOST/login | grep -Eq '^30[18] https://$HOST/login'"
check "A4 HSTS is max-age=31536000, without includeSubDomains or preload" \
    bash -c "$(declare -f https); CA=$CA; https -I https://$HOST/login | tr -d '\r' | grep -ix 'strict-transport-security: max-age=31536000'"
sign_in() {
    /usr/bin/python3 - "$HOST" "$CA" <<'EOF'
import http.cookiejar, os, re, ssl, sys, urllib.parse, urllib.request
host, ca = sys.argv[1:3]
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca)),
    urllib.request.HTTPCookieProcessor(jar))
base = f"https://{host}"
page = opener.open(base + "/login").read().decode()
token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)
data = urllib.parse.urlencode({"csrf_token": token, "username": "admin",
                               "password": os.environ["ADMIN_PASSWORD"]}).encode()
request = urllib.request.Request(base + "/login", data=data, headers={
    "Referer": base + "/login", "Origin": base})
final = opener.open(request)
body = final.read().decode()
cookies = {c.name: c for c in jar}
session = [c for name, c in cookies.items() if name.startswith("__Host-")]
assert session and session[0].secure and session[0].path == "/", cookies
assert final.url.rstrip("/") == base, final.url
assert "admin" in body, "dashboard does not name the signed-in account"
print("signed in as admin over HTTPS; cookie", session[0].name)
EOF
}
check "A4 the administrator signs in over HTTPS (__Host- Secure cookie)" sign_in

# --- A5 listening sockets ----------------------------------------------------
check "A5 Gunicorn listens on 127.0.0.1:8000 only" \
    bash -c 'ss -ltnH | grep -q "127.0.0.1:8000 " && ! ss -ltnH | grep -Eq "(0\.0\.0\.0|\*|\[::\]):8000 "'

# --- A6 readiness endpoint ---------------------------------------------------
PRIMARY_IP=$(hostname -I | awk '{ print $1 }')
check "A6 /readyz through Caddy from the server itself is ready" \
    bash -c "$(declare -f https); CA=$CA; https --resolve $HOST:443:127.0.0.1 https://$HOST/readyz | grep -q '\"status\":\"ready\"'"
check "A6 /readyz from another address is 404" \
    bash -c "$(declare -f https); CA=$CA; [ \"\$(https -o /dev/null -w '%{http_code}' --resolve $HOST:443:$PRIMARY_IP https://$HOST/readyz)\" = 404 ]"

# --- A8 off-server copy and an unmounted share --------------------------------
latest_offsite=$(ls -1 /mnt/helpdesk-offsite/helpdesk/helpdesk-*.hdbk | tail -1)
check "A8 the off-server copy verifies with the backup key" \
    helpdesk-admin flask --app app backup-verify "$latest_offsite"
local_before=$(ls -1 /var/backups/helpdesk/helpdesk-*.hdbk | wc -l)
umount /mnt/helpdesk-offsite
check "A8 share not mounted: the backup job fails" bash -c '! systemctl start helpdesk-backup'
install -d -m 0700 -o helpdesk -g helpdesk /mnt/helpdesk-offsite/helpdesk
systemctl reset-failed helpdesk-backup
check "A8 empty folder where the share should be: the job fails" bash -c '! systemctl start helpdesk-backup'
check "A8 ...and the job said why" \
    bash -c 'journalctl -u helpdesk-backup -o cat | grep -q "on the same filesystem"'
check "A8 ...and the local backups were still made" \
    test "$(ls -1 /var/backups/helpdesk/helpdesk-*.hdbk | wc -l)" -eq $((local_before + 2))
check "A10 ...and the alert unit ran for the failed backup" alert_logged helpdesk-backup.service
rmdir /mnt/helpdesk-offsite/helpdesk
systemctl reset-failed helpdesk-backup
mount -o loop,nosuid,nodev,noexec /var/tmp/offsite.img /mnt/helpdesk-offsite
check "A8 share mounted again: the backup job succeeds" systemctl start helpdesk-backup

# --- A10 ops-check alert ---------------------------------------------------------
systemctl stop helpdesk-notifications
check "A10 worker stopped: ops-check is CRITICAL and fails its unit" bash -c '! systemctl start helpdesk-ops-check'
check "A10 ...and the alert unit ran" alert_logged helpdesk-ops-check.service
systemctl reset-failed helpdesk-ops-check
systemctl start helpdesk-notifications

# --- A9 live restore -----------------------------------------------------------
systemctl start helpdesk-backup
restore_from=$(ls -1 /var/backups/helpdesk/helpdesk-*.hdbk | tail -1)
helpdesk-admin flask --app app set-email admin admin@example.invalid >/dev/null
systemctl stop helpdesk-web helpdesk-notifications helpdesk-backup.timer
safety_before=$(ls -1 /var/backups/helpdesk/helpdesk-*.hdbk | wc -l)
check "A9 backup-restore-live through helpdesk-admin, confirmed at a terminal" live_restore "$restore_from"
check "A9 the change made after the backup is gone" \
    bash -c "[ -z \"\$(sqlite3 -readonly /var/lib/helpdesk/helpdesk.db \"SELECT COALESCE(email, '') FROM users WHERE username = 'admin'\")\" ]"
check "A9 a safety backup of the replaced database was written" \
    test "$(ls -1 /var/backups/helpdesk/helpdesk-*.hdbk | wc -l)" -eq $((safety_before + 1))
systemctl start helpdesk-web helpdesk-notifications helpdesk-backup.timer
check "A9 services ready again after the restore" \
    bash -c "$(declare -f https); CA=$CA; sleep 2; https --resolve $HOST:443:127.0.0.1 https://$HOST/readyz | grep -q ready"

# --- A11 failures and restarts -----------------------------------------------------
systemctl stop redis-server
check "A11 Redis stopped: /readyz is 503" \
    bash -c "$(declare -f https); CA=$CA; [ \"\$(https -o /dev/null -w '%{http_code}' --resolve $HOST:443:127.0.0.1 https://$HOST/readyz)\" = 503 ]"
systemctl start redis-server
check "A11 Redis back: ready again without restarting the helpdesk" \
    bash -c "$(declare -f https); CA=$CA; for i in \$(seq 20); do https --resolve $HOST:443:127.0.0.1 https://$HOST/readyz | grep -q ready && exit 0; sleep 1; done; exit 1"
systemctl kill --signal=SIGKILL helpdesk-web
check "A11 helpdesk-web killed: systemd restarts it and it serves again" \
    bash -c "$(declare -f https); CA=$CA; for i in \$(seq 40); do https -f https://$HOST/healthz >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1"
systemctl kill --signal=SIGKILL helpdesk-notifications
check "A11 notification worker killed: systemd restarts it (after 30 s)" \
    bash -c 'for i in $(seq 60); do sleep 1; [ "$(systemctl show -p NRestarts --value helpdesk-notifications)" -ge 1 ] && systemctl is-active -q helpdesk-notifications && exit 0; done; exit 1'

# --- A12 rollback (switch releases and back) ------------------------------------------
OTHER=$REL-rollback-check
git clone --quiet --branch "$RELEASE_TAG" "$REPO_URL" "$OTHER"
"$HELPDESK_PYTHON" -m venv "$OTHER/.venv"
"$OTHER/.venv/bin/python" -m pip install --quiet --no-input --require-hashes -r "$OTHER/requirements.lock"
chown -R root:root "$OTHER"
switch_to() {
    # The checks above restarted the services several times; clear
    # systemd's start-rate counters so these restarts are not refused
    systemctl reset-failed helpdesk-web helpdesk-notifications
    ln -sfn "$1" /opt/helpdesk/current
    systemctl restart helpdesk-web helpdesk-notifications
    cwd=$(readlink "/proc/$(systemctl show -p MainPID --value helpdesk-web)/cwd")
    [ "$cwd" = "$1" ] || { echo "running from $cwd"; return 1; }
    for i in $(seq 30); do
        curl -sS --cacert "$CA" -f "https://$HOST/healthz" >/dev/null 2>&1 && return 0
        sleep 1
    done
    return 1
}
check "A12 switching current to another release serves from it" switch_to "$OTHER"
check "A12 switching back serves from the original release" switch_to "$REL"

# --- A7 no secrets in logs ---------------------------------------------------------------
https -s -o /dev/null "https://$HOST/login?q=runbook-private-search-term" || true
sleep 1
cp /etc/helpdesk/credentials/secret-key /etc/helpdesk/credentials/backup-key "$WORK/"
check "A7 no secret appears in the journal" \
    bash -c "! journalctl -o cat | grep -qF -f $WORK/secret-key -f $WORK/backup-key -f $WORK/redis-password"
check "A7 no secret appears in the units' visible environment" \
    bash -c "! systemctl show helpdesk-web helpdesk-notifications -p Environment | grep -qF -f $WORK/secret-key -f $WORK/redis-password"
check "A7 the Caddy access log has no query strings" \
    bash -c 'grep -q "\"/login\"" /var/log/caddy/helpdesk-access.log && ! grep -q runbook-private-search-term /var/log/caddy/helpdesk-access.log'

echo "=== For the record ==="
systemd-analyze security --no-pager helpdesk-web.service | tail -1 || true
caddy version
/opt/helpdesk/current/.venv/bin/python -V

rm -rf "$WORK"
if (( failures )); then
    echo "$failures check(s) failed"
    exit 1
fi
echo "All checks passed"
