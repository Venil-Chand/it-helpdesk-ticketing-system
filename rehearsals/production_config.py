"""
Production configuration rehearsal: Gunicorn with deploy/gunicorn.conf.py
and wsgi:application, APP_ENV=production, two workers and a disposable
Redis, receiving requests the way Caddy sends them.

It checks that a correct configuration starts and behaves as documented
(headers, cookies, trusted hosts, HTTPS, body limit, rate limits behind
the proxy, log hygiene), and that each unsafe configuration is refused
before any worker accepts a connection.

Not covered here (see docs/REHEARSALS.md): Caddy itself and real TLS
(the "caddy" CI job does that), systemd units, the firewall and real
certificates, which need a real server.
"""

import time

from .common import (Client, DisposableRedis, Gunicorn, RehearsalUnavailable,
                     Report, Workspace, free_port, gunicorn_available,
                     new_password, new_secret_key, wait_for)

TICKETS = 10


def run(keep=False):
    ws = Workspace("rehearsal-production", keep=keep)
    report = Report("Production configuration rehearsal", ws)
    redis = None
    try:
        if not gunicorn_available():
            raise RehearsalUnavailable("gunicorn is not installed")
        redis = DisposableRedis(ws).start()
        _rehearse(ws, report, redis)
    except RehearsalUnavailable as error:
        report.check("prerequisites available", False, str(error))
    except Exception as error:
        report.check("rehearsal finished without an unexpected error", False,
                     f"{type(error).__name__}: {error}")
    finally:
        if redis:
            redis.stop()
        ws.close()
    return report.finish()


def _rehearse(ws, report, redis):
    check = report.check
    password = new_password()
    env = ws.production_env(redis.url())

    result = ws.flask("init-db", env=env)
    if not check("init-db migrates the disposable database",
                 result.returncode == 0, result.stdout + result.stderr):
        return
    ws.seed(env, tickets=TICKETS, password=password)

    server = Gunicorn(ws, env, workers=2)
    try:
        if not check("Gunicorn starts with production settings and Redis",
                     server.wait_ready(), server.log()):
            return
        _running_server(ws, report, server, password)
    finally:
        server.stop()

    log = server.log()
    check("the Gunicorn log names no secret (secret key, Redis or account "
          "password)", not ws.leaks(log), ", ".join(ws.leaks(log)))
    check("the access log holds no query strings",
          "rehearsal-search-term" not in log and "?q=" not in log)

    _refusals(ws, report, redis, env)


def _running_server(ws, report, server, password):
    check = report.check
    # /healthz answers as soon as the first worker is up; the second may
    # still be booting
    check("two Gunicorn workers are running",
          wait_for(lambda: len(server.worker_pids()) == 2, 15),
          str(server.worker_pids()))

    client = Client(server.port)
    health = client.get("/healthz")
    ready = client.get("/readyz")
    check("/healthz is 200 and /readyz is 200 (database, schema, Redis)",
          health.status == 200 and ready.status == 200,
          f"{health.status} {ready.status} {ready.text}")

    page = client.get("/login")
    csp = page.header("Content-Security-Policy") or ""
    check("strict Content-Security-Policy on pages",
          all(part in csp for part in ("default-src 'none'",
                                       "frame-ancestors 'none'",
                                       "object-src 'none'")), csp)
    hsts = page.header("Strict-Transport-Security") or ""
    check("HSTS header on HTTPS responses", "max-age=" in hsts, hsts)
    check("X-Content-Type-Options: nosniff",
          (page.header("X-Content-Type-Options") or "") == "nosniff")

    login = client.login("user1", password)
    cookies = [value for value in login.all_headers("Set-Cookie")
               if value.startswith("__Host-")]
    check("sign-in succeeds (302 to the dashboard)",
          login.status == 302
          and (login.header("Location") or "").endswith("/"),
          f"{login.status} {login.header('Location')}")
    cookie = cookies[0].lower() if cookies else ""
    check("session cookie is __Host-, Secure, HttpOnly, SameSite=Lax",
          bool(cookies) and "secure" in cookie and "httponly" in cookie
          and "samesite=lax" in cookie and "path=/" in cookie
          and "domain=" not in cookie, " | ".join(cookies))
    search = client.get("/?q=rehearsal-search-term")
    check("signed-in dashboard search works", search.status == 200,
          str(search.status))

    stranger = Client(server.port, host="evil.example")
    check("unknown Host header is refused with 400",
          stranger.get("/login").status == 400)

    plain = client.get("/login", headers={"X-Forwarded-Proto": "http"})
    check("plain HTTP (behind the proxy) is refused with 400",
          plain.status == 400, str(plain.status))

    big = client.post_form("/login", {"username": "x" * (1024 * 1024 + 10)})
    check("request bodies over 1 MiB are refused with 413",
          big.status == 413, str(big.status))

    # Five sign-in attempts per minute per real client address, even when
    # the client invents a different left-most X-Forwarded-For each time
    guesser = Client(server.port, client_ip="198.51.100.77")
    statuses = []
    for attempt in range(6):
        spoofed = f"10.9.{attempt}.1, 198.51.100.77"
        token = guesser.csrf_token(forwarded_for=spoofed)
        statuses.append(guesser.post_form(
            "/login", {"csrf_token": token, "username": "user2",
                       "password": "not the password"},
            forwarded_for=spoofed).status)
    check("sign-in limit holds against spoofed X-Forwarded-For "
          "(5 attempts, then 429)",
          all(code != 429 for code in statuses[:5]) and statuses[5] == 429,
          str(statuses))

    server_log = server.log()
    check("Gunicorn logs the client address Caddy reported",
          "fwd=203.0.113.10" in server_log, server_log[-800:])


# Each unsafe setting must stop Gunicorn before it serves anything. The
# third value is text the log must contain, so the operator can tell why.
def _refusal_cases(ws, redis, env):
    unmigrated = ws.data / "never-migrated.db"
    wrong_password = ws.sensitive("wrong-" + new_secret_key()[:20])
    return [
        ("unmigrated database",
         {"HELPDESK_DATABASE_PATH": str(unmigrated)}, "Startup check failed"),
        ("weak SECRET_KEY", {"SECRET_KEY": "short"}, "SECRET_KEY"),
        ("no TRUSTED_HOSTS", {"TRUSTED_HOSTS": ""}, "TRUSTED_HOSTS"),
        ("FLASK_DEBUG=1 in production", {"FLASK_DEBUG": "1"}, "FLASK_DEBUG"),
        ("in-memory rate limits with several workers",
         {"RATELIMIT_STORAGE_URI": "memory://"}, "RATELIMIT_STORAGE_URI"),
        ("RATELIMIT_SINGLE_PROCESS=1 with two workers",
         {"RATELIMIT_STORAGE_URI": "memory://",
          "RATELIMIT_SINGLE_PROCESS": "1"}, "allows exactly one worker"),
        ("non-loopback bind address",
         {"HELPDESK_BIND": f"0.0.0.0:{free_port()}"}, "loopback"),
        ("Redis with the wrong password",
         {"RATELIMIT_STORAGE_URI": redis.url(password=wrong_password)},
         "Startup check failed"),
        ("Redis unreachable",
         {"RATELIMIT_STORAGE_URI": f"redis://:{redis.password}@127.0.0.1:"
                                   f"{free_port()}/0"},
         "Startup check failed"),
    ]


def _refusals(ws, report, redis, env):
    for index, (name, change, expected) in enumerate(
            _refusal_cases(ws, redis, env)):
        bind = change.pop("HELPDESK_BIND", None)
        server = Gunicorn(ws, {**env, **change}, workers=2, bind=bind,
                          log_name=f"refusal-{index}.log")
        started = time.monotonic()
        code = server.wait_exit(40)
        if code is None:
            server.stop()
        log = server.log()
        report.check(
            f"refused at startup: {name}",
            code not in (None, 0) and expected in log
            and "Booting worker" not in log and not ws.leaks(log),
            f"exit {code} after {time.monotonic() - started:.1f} s; "
            f"leaks: {ws.leaks(log)}\n{log[-1200:]}")
