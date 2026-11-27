#!/usr/bin/env bash
# End-to-end check of the production Caddy + Gunicorn boundary, run by CI.
#
#   .github/scripts/caddy-proxy-check.sh CADDY_IMAGE REDIS_URL
#
# A test copy of deploy/Caddyfile is made with only these changes:
#   * site name helpdesk.example.org -> helpdesk.test (non-secret test name)
#   * global options: local_certs (Caddy's own test CA instead of Let's
#     Encrypt), skip_install_trust, http_port 8080, https_port 8443
# Everything else (request limits, header handling, /readyz restriction,
# log filtering, timeouts) is the production configuration unchanged.
#
# Real Gunicorn workers (deploy/gunicorn.conf.py, wsgi:application) run
# behind it with production settings against a throwaway database, and
# the checks below talk to Caddy over real TLS. Secrets are random values
# generated for this run only.

set -euo pipefail

IMAGE=$1
REDIS_URL=$2
ROOT=$(git rev-parse --show-toplevel)
WORK=$(mktemp -d)
CONTAINER=helpdesk-caddy-check
PYTHON=${PYTHON:-python}
failures=0

cleanup() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    if [[ -n "${GUNICORN_PID:-}" ]]; then
        kill "$GUNICORN_PID" 2>/dev/null || true
        wait "$GUNICORN_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'echo "ERROR: line $LINENO: $BASH_COMMAND" >&2' ERR

pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; failures=$((failures + 1)); }
check() {  # check DESCRIPTION COMMAND...
    local description=$1; shift
    if "$@"; then pass "$description"; else fail "$description"; fi
}

wait_for_port() {
    for _ in $(seq 1 100); do
        (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && return 0
        sleep 0.2
    done
    echo "port $1 did not open" >&2
    return 1
}

# --- 1. Test copy of the production Caddyfile -----------------------------

awk '
    { print }
    !done && $0 == "{" {
        print "\tlocal_certs"
        print "\tskip_install_trust"
        print "\thttp_port 8080"
        print "\thttps_port 8443"
        done = 1
    }
' "$ROOT/deploy/Caddyfile" \
    | sed 's/^helpdesk\.example\.org {$/helpdesk.test {/' > "$WORK/Caddyfile"

grep -q '^helpdesk.test {$' "$WORK/Caddyfile"
diff -u "$ROOT/deploy/Caddyfile" "$WORK/Caddyfile" || true

# --- 2. Gunicorn with production settings ---------------------------------

export APP_ENV=production
export SECRET_KEY
SECRET_KEY=$("$PYTHON" -c "import secrets; print(secrets.token_hex(32))")
export TRUSTED_HOSTS=helpdesk.test
export TRUSTED_PROXY_COUNT=1
export TRUSTED_PROXY_IPS=127.0.0.1
export RATELIMIT_STORAGE_URI=$REDIS_URL
export HELPDESK_DATABASE_PATH=$WORK/helpdesk.db
export HELPDESK_BIND=127.0.0.1:8000
export WEB_CONCURRENCY=2

"$PYTHON" - <<'EOF'
import os, sqlite3
import migrations
from werkzeug.security import generate_password_hash
path = os.environ["HELPDESK_DATABASE_PATH"]
migrations.migrate(path)
conn = sqlite3.connect(path)
conn.execute("INSERT INTO users (username, password_hash, role) VALUES "
             "('alice', ?, 'user')",
             (generate_password_hash("not-the-password-for-this-check"),))
conn.commit()
conn.close()
EOF

(cd "$ROOT" && exec "$PYTHON" -m gunicorn --config deploy/gunicorn.conf.py \
    wsgi:application) > "$WORK/gunicorn.log" 2>&1 &
GUNICORN_PID=$!
wait_for_port 8000

# --- 3. Caddy (official image), host networking ---------------------------

mkdir -p "$WORK/logs" "$WORK/data"
docker run -d --name "$CONTAINER" --network host \
    -v "$WORK/Caddyfile:/etc/caddy/Caddyfile:ro" \
    -v "$WORK/logs:/var/log/caddy" \
    -v "$WORK/data:/data" \
    "$IMAGE" >/dev/null
wait_for_port 8443

# Trust only Caddy's test CA for this run
for _ in $(seq 1 50); do
    docker exec "$CONTAINER" cat /data/caddy/pki/authorities/local/root.crt \
        > "$WORK/root.crt" 2>/dev/null && [[ -s "$WORK/root.crt" ]] && break
    sleep 0.2
done

BASE=https://helpdesk.test:8443
CURL=(curl -sS --max-time 15 --cacert "$WORK/root.crt"
      --resolve helpdesk.test:8443:127.0.0.1 --resolve helpdesk.test:8080:127.0.0.1)

# A non-loopback address of this machine, to act as "another machine"
OTHER_IP=$(hostname -I | tr ' ' '\n' | grep -vE '^(127\.|::1|$)' | head -1)
echo "Non-loopback address used for outside-access checks: $OTHER_IP"

# --- 4. Checks -------------------------------------------------------------

headers=$("${CURL[@]}" -D - -o /dev/null "$BASE/login")
echo "$headers" | sed 's/\(set-cookie: [^=]*=\)[^;]*/\1<redacted>/I'

check "HTTPS page through Caddy answers 200" \
    grep -qE '^HTTP/[0-9.]+ 200' <<<"$headers"
check "session cookie is __Host-, Secure, HttpOnly, SameSite=Lax" \
    bash -c 'grep -i "^set-cookie: __Host-helpdesk_session=" <<<"$1" |
             grep -i "Secure" | grep -i "HttpOnly" | grep -qi "SameSite=Lax"' _ "$headers"
check "HSTS is sent (the app saw HTTPS via X-Forwarded-Proto from Caddy)" \
    grep -qi '^strict-transport-security: max-age=31536000' <<<"$headers"
check "Server header removed" \
    bash -c '! grep -qi "^server:" <<<"$1"' _ "$headers"

redirect=$("${CURL[@]}" -o /dev/null -w '%{http_code} %{redirect_url}' \
    http://helpdesk.test:8080/login)
check "plain HTTP is redirected to HTTPS ($redirect)" \
    grep -qE '^30[18] https://helpdesk.test' <<<"$redirect"

# Forged forwarding headers from the client: Caddy replaces them, so the
# app still sees HTTPS and the real client address, and the rate limit
# cannot be dodged by inventing a new address on every attempt
"${CURL[@]}" -c "$WORK/jar" -o "$WORK/login.html" "$BASE/login"
# The input's attributes span several lines, so match across them
token=$("$PYTHON" -c '
import re, sys
match = re.search(r"name=\"csrf_token\"\s+value=\"([^\"]+)\"", open(sys.argv[1]).read())
print(match.group(1))' "$WORK/login.html")
test -n "$token"
codes=""
for n in 1 2 3 4 5 6 7; do
    code=$("${CURL[@]}" -b "$WORK/jar" -o /dev/null -w '%{http_code}' \
        -H "X-Forwarded-For: 198.51.100.$n" \
        -H "X-Forwarded-Proto: http" \
        -H "X-Forwarded-Host: attacker.example" \
        -H "Referer: $BASE/login" \
        --data-urlencode "csrf_token=$token" \
        --data-urlencode "username=alice" \
        --data-urlencode "password=wrong-$n" "$BASE/login")
    codes="$codes $code"
done
echo "Login attempts with forged X-Forwarded-For/-Proto/-Host:$codes"
check "forged forwarded headers are ignored (5 x 200, then 429)" \
    test "$codes" = " 200 200 200 200 200 429 429"

check "Gunicorn logged the address Caddy saw, not the forged one" \
    bash -c '! grep -q "fwd=198.51.100" "$1" && grep -q "fwd=127.0.0.1" "$1"' \
    _ "$WORK/gunicorn.log"

big=$("${CURL[@]}" -o /dev/null -w '%{http_code}' \
    -H "Content-Type: application/x-www-form-urlencoded" \
    --data-binary @<(head -c 2000000 /dev/zero | tr '\0' 'a') "$BASE/login")
check "bodies over 1 MB are refused (got $big)" test "$big" = 413

ready=$("${CURL[@]}" -o "$WORK/ready.json" -w '%{http_code}' "$BASE/readyz")
check "/readyz from this machine is ready ($ready)" \
    bash -c 'test "$1" = 200 && grep -q "\"status\":\"ready\"" "$2"' \
    _ "$ready" "$WORK/ready.json"

if [[ -n "$OTHER_IP" ]]; then
    outside=$(curl -sS --max-time 15 --cacert "$WORK/root.crt" \
        --resolve "helpdesk.test:8443:$OTHER_IP" -o /dev/null \
        -w '%{http_code}' "$BASE/readyz")
    check "/readyz from another address is 404 ($outside)" test "$outside" = 404

    check "Gunicorn cannot be reached except on loopback" \
        bash -c '! curl -s --max-time 5 -o /dev/null "http://$1:8000/login"' \
        _ "$OTHER_IP"
else
    fail "no non-loopback address available for the outside-access checks"
fi

check "Gunicorn listens on 127.0.0.1:8000 only" \
    bash -c 'ss -ltnH "sport = :8000" | awk "{print \$4}" | sort -u |
             grep -qx "127.0.0.1:8000" &&
             ! ss -ltnH "sport = :8000" | awk "{print \$4}" |
               grep -vqx "127.0.0.1:8000"'

"${CURL[@]}" -o /dev/null "$BASE/?search=private-query-text-1234"
sleep 1
check "Caddy access log exists and has no query strings" \
    bash -c 'test -s "$1/helpdesk-access.log" &&
             ! grep -q "private-query-text-1234" "$1/helpdesk-access.log"' \
    _ "$WORK/logs"
check "Gunicorn access log has no query strings" \
    bash -c '! grep -q "private-query-text-1234" "$1"' _ "$WORK/gunicorn.log"

echo
docker logs "$CONTAINER" 2>&1 | grep -iE '"level":"(warn|error)"' | head -20 || true

if (( failures )); then
    echo "$failures check(s) failed"
    echo "--- gunicorn log ---"; tail -50 "$WORK/gunicorn.log"
    echo "--- caddy log ---"; docker logs "$CONTAINER" 2>&1 | tail -50
    exit 1
fi

echo "All Caddy/Gunicorn boundary checks passed"
