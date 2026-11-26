"""
Gunicorn configuration for the helpdesk.

    gunicorn --config deploy/gunicorn.conf.py wsgi:application

Gunicorn is the program that runs the Python application in production.
It listens only on this machine (127.0.0.1); Caddy, the HTTPS reverse
proxy, is the only thing that talks to it. Visitors never reach Gunicorn
directly.

Values can be adjusted through environment variables (set them in the
systemd unit, not in the code):

    HELPDESK_BIND       default 127.0.0.1:8000. Must stay on a loopback
                        address: the application believes X-Forwarded-*
                        headers only from TRUSTED_PROXY_IPS, and nothing
                        but Caddy should be able to connect.
    WEB_CONCURRENCY     worker processes, default 2. More than one needs
                        shared (Redis) rate-limit storage; this file
                        refuses to start otherwise.
    GUNICORN_TIMEOUT    seconds a request may take before its worker is
                        restarted, default 30

Every setting below is explained, because a WSGI server's defaults are
chosen for convenience, not for a server holding personal data.
"""

import ipaddress
import os

# ---------------------------------------------------------------
# Where to listen
# ---------------------------------------------------------------

bind = [os.environ.get("HELPDESK_BIND", "127.0.0.1:8000").strip()]


def _require_loopback(address):
    host = address.rsplit(":", 1)[0].strip("[]")

    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False

    if not loopback:
        raise RuntimeError(
            "HELPDESK_BIND must be a loopback address such as "
            "127.0.0.1:8000; only the reverse proxy may reach Gunicorn."
        )


for _address in bind:
    _require_loopback(_address)

# ---------------------------------------------------------------
# Workers
# ---------------------------------------------------------------

# Synchronous workers: one request at a time per process. Simple and
# predictable with SQLite, whose writes are serialised anyway.
worker_class = "sync"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))

if not 1 <= workers <= 8:
    raise RuntimeError("WEB_CONCURRENCY must be between 1 and 8.")

# In-memory rate limits are per process: two workers would mean twice the
# allowed sign-in attempts. security_config already refuses memory:// in
# production unless RATELIMIT_SINGLE_PROCESS=1; this enforces the "single"
# part.
if workers > 1 and os.environ.get("RATELIMIT_SINGLE_PROCESS", "") == "1":
    raise RuntimeError(
        "RATELIMIT_SINGLE_PROCESS=1 allows exactly one worker. Set "
        "WEB_CONCURRENCY=1, or configure Redis and remove "
        "RATELIMIT_SINGLE_PROCESS."
    )

# Load the application once in the master process before forking. Startup
# checks in wsgi.py (configuration, schema, Redis) then stop the service
# before any worker exists, instead of every worker failing in a loop.
preload_app = True

# Replace each worker after this many requests (with jitter so they do
# not all restart together), which limits the effect of any slow leak
max_requests = 1000
max_requests_jitter = 100

# ---------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------

# A request taking longer than this gets its worker killed and replaced.
# The slowest normal request is a password check (scrypt, well under a
# second) or a CSV export (capped in size). 30 s leaves a wide margin
# while still recovering from a stuck worker.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "30"))

# On shutdown or restart, workers finish the request in progress for up
# to this long before being killed. Keep systemd's TimeoutStopSec above it.
graceful_timeout = 30

# Caddy keeps connections to Gunicorn open between requests
keepalive = 5

# ---------------------------------------------------------------
# Request size limits (the application also refuses bodies over 1 MiB)
# ---------------------------------------------------------------

limit_request_line = 4094
limit_request_fields = 100
limit_request_field_size = 8190

# ---------------------------------------------------------------
# Forwarded headers: decided by the application only
# ---------------------------------------------------------------

# Gunicorn can itself believe X-Forwarded-Proto and similar headers from
# addresses it trusts. That decision belongs to one place only, the
# application's TrustedProxyMiddleware (TRUSTED_PROXY_COUNT and
# TRUSTED_PROXY_IPS), so Gunicorn trusts nobody and changes nothing.
forwarded_allow_ips = ""
secure_scheme_headers = {}
forwarder_headers = ""

# Headers with underscores (e.g. "X_Forwarded_For") are dropped, so they
# cannot be confused with the dashed form after WSGI conversion
header_map = "drop"

proxy_protocol = "off"

# ---------------------------------------------------------------
# Process hygiene
# ---------------------------------------------------------------

# The control socket (gunicornc) is not used; do not create one
control_socket_disable = True

# Worker heartbeat files in memory rather than on disk
worker_tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else None

# ---------------------------------------------------------------
# Logging: standard error, collected by systemd's journal
# ---------------------------------------------------------------

errorlog = "-"
loglevel = "info"
accesslog = "-"

# No query strings (they can hold search text), no cookies, no user agent.
# "fwd" is the client address as Caddy reported it; "rid" matches the
# application's X-Request-ID, so an access line can be tied to its
# application log lines.
access_log_format = (
    '%(h)s fwd=%({x-forwarded-for}i)s "%(m)s %(U)s %(H)s" '
    '%(s)s %(B)s %(M)sms rid=%({x-request-id}o)s'
)

# Shown in "ps" output
proc_name = "helpdesk-web"
