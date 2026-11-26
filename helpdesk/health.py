"""
Blueprint "health": endpoints for load balancers and monitoring.

    GET /healthz   liveness: the process is running and answering. Touches
                   nothing else, so it stays fast even when the database
                   is in trouble. Safe to expose publicly.
    GET /readyz    readiness: this process could serve a real request now
                   (database readable, schema current, rate-limit storage
                   answering). 200 or 503, with one word per check.

Neither endpoint needs a sign-in, sets a cookie or reveals anything an
attacker could use: no versions, paths, hostnames, counts, exception text
or account data, only check names and "ok"/"fail". The reasons for a
failure go to the server log (helpdesk/readiness.py). The Caddyfile keeps
/readyz reachable from the server itself only.

The production HTTPS and host checks still apply: monitoring must use
https:// and the real host name, as visitors do.
"""

from flask import Blueprint, jsonify

from . import readiness as readiness_checks
from .errors import plain_error

bp = Blueprint("health", __name__)


@bp.route("/healthz")
def healthz():
    return plain_error("ok\n", 200)


@bp.route("/readyz")
def readyz():
    ready, checks = readiness_checks.readiness()

    response = jsonify({"status": "ready" if ready else "not ready",
                        "checks": checks})
    response.status_code = 200 if ready else 503

    if not ready:
        response.headers["Retry-After"] = "30"

    return response
