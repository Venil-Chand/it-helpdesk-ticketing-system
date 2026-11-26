"""
The production application server: Gunicorn, the WSGI entry point
(wsgi.py) and what a reverse proxy such as Caddy sends.

The Gunicorn tests start real Gunicorn processes with
deploy/gunicorn.conf.py, listening on a free port on 127.0.0.1, against a
temporary database named by HELPDESK_DATABASE_PATH. Requests are sent
the way Caddy sends them (Host, X-Forwarded-For, X-Forwarded-Proto), so
the whole chain is exercised: Gunicorn -> TrustedProxyMiddleware ->
HTTPS check -> session cookies -> rate limits.

Nothing here reads .env or the real helpdesk.db, and nothing listens
beyond this machine.
"""

import http.client
import importlib
import io
import json
import logging
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import database
import migrations
from helpdesk import create_app, logging_setup, readiness

from redis_helpers import flush_test_redis, requires_redis, test_redis_url
from security_helpers import (
    HTTPS_URL,
    PRODUCTION_ENV,
    PROJECT_ROOT,
    SINGLE_PROCESS_RATE_LIMITS,
    TEST_SECRET_KEY,
    clean_environment,
)

try:
    import gunicorn  # noqa: F401
    HAVE_GUNICORN = True
except ImportError:
    HAVE_GUNICORN = False

GUNICORN_CONFIG = PROJECT_ROOT / "deploy" / "gunicorn.conf.py"

# What Caddy adds to every request it forwards
CADDY_HEADERS = {
    "Host": "helpdesk.test",
    "X-Forwarded-Proto": "https",
    "X-Forwarded-For": "203.0.113.5",
}

PROXIED_ENV = {
    **PRODUCTION_ENV,
    "TRUSTED_PROXY_COUNT": "1",
    "TRUSTED_PROXY_IPS": "127.0.0.1",
}

PASSWORD = "correct-horse-battery-staple"


def free_port():
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_database(path, *, migrate=True):
    """A migrated database with one account, alice (unless migrate=False)."""

    if not migrate:
        return

    migrations.migrate(path)

    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, role) "
            "VALUES ('alice', ?, 'user')",
            (generate_password_hash(PASSWORD, method="pbkdf2:sha256:1000"),)
        )
        conn.commit()
    finally:
        conn.close()


class GunicornProcess:
    """One Gunicorn master and its workers, stopped on cleanup."""

    def __init__(self, settings, *, db_path, workers=1, extra=None):
        self.port = free_port()
        self.log = tempfile.TemporaryFile(mode="w+")
        environ = clean_environment(dict(
            settings,
            HELPDESK_DATABASE_PATH=db_path,
            HELPDESK_BIND=f"127.0.0.1:{self.port}",
            WEB_CONCURRENCY=str(workers),
        ))
        environ.update(extra or {})
        # Gunicorn must not inherit a systemd notification socket
        environ.pop("NOTIFY_SOCKET", None)

        self.process = subprocess.Popen(
            [sys.executable, "-m", "gunicorn", "--config",
             str(GUNICORN_CONFIG), "wsgi:application"],
            cwd=PROJECT_ROOT, env=environ,
            stdout=self.log, stderr=subprocess.STDOUT,
        )

    def wait_until_listening(self, timeout=30):
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError("Gunicorn exited:\n" + self.output())
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.5):
                    return
            except OSError:
                time.sleep(0.1)

        raise AssertionError("Gunicorn did not start:\n" + self.output())

    def wait_for_exit(self, timeout=30):
        try:
            return self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError("Gunicorn did not exit:\n" + self.output())

    def output(self):
        self.log.flush()
        self.log.seek(0)
        return self.log.read()

    def stop(self):
        """Stop Gunicorn (if running). The log stays readable."""

        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(40)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def worker_pids(self, expected=None, timeout=15):
        """The worker processes; waits until `expected` have started."""

        children = Path(f"/proc/{self.process.pid}/task/{self.process.pid}"
                        "/children")
        deadline = time.monotonic() + timeout

        while True:
            pids = [int(pid) for pid in children.read_text().split()]
            if expected is None or len(pids) >= expected or (
                time.monotonic() > deadline
            ):
                return pids
            time.sleep(0.1)

    def request(self, method, path, *, headers=None, body=None):
        """Send one request on a new connection (any worker may answer)."""

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path, body=body,
                         headers={**CADDY_HEADERS, **(headers or {})})
            response = conn.getresponse()
            return response.status, response.getheaders(), \
                response.read().decode("utf-8", "replace")
        finally:
            conn.close()


@unittest.skipUnless(HAVE_GUNICORN, "gunicorn is not installed")
class GunicornTestCase(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = os.path.join(directory.name, "helpdesk.db")

    def start(self, settings=PROXIED_ENV, *, workers=1, migrate=True,
              wait=True, extra=None):
        make_database(self.db_path, migrate=migrate)
        server = GunicornProcess(settings, db_path=self.db_path,
                                 workers=workers, extra=extra)
        self.addCleanup(server.log.close)
        self.addCleanup(server.stop)

        if wait:
            server.wait_until_listening()

        return server

    @staticmethod
    def header(headers, name):
        return [value for key, value in headers if key.lower() == name]

    def login_attempt(self, server, cookie, token, password, forwarded_for):
        body = (f"csrf_token={token}&username=alice&password={password}")
        return server.request(
            "POST", "/login", body=body, headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": cookie, "Referer": "https://helpdesk.test/login",
                "X-Forwarded-For": forwarded_for,
            })[0]

    def csrf_session(self, server, forwarded_for="203.0.113.5"):
        status, headers, text = server.request(
            "GET", "/login", headers={"X-Forwarded-For": forwarded_for})
        self.assertEqual(status, 200)
        cookie = self.header(headers, "set-cookie")[0].split(";")[0]
        token = re.search(r'name="csrf_token"\s+value="([^"]+)"',
                          text).group(1)
        return cookie, token


class TestGunicornServesProduction(GunicornTestCase):

    def test_application_loads_and_serves_https_pages_through_the_proxy(self):
        server = self.start()

        status, headers, text = server.request("GET", "/login")

        self.assertEqual(status, 200)
        self.assertIn("csrf_token", text)

        cookie = self.header(headers, "set-cookie")[0]
        self.assertTrue(cookie.startswith("__Host-helpdesk_session="))
        for attribute in ("Secure", "HttpOnly", "Path=/", "SameSite=Lax"):
            self.assertIn(attribute, cookie)
        self.assertNotIn("Domain=", cookie)

        self.assertEqual(self.header(headers, "strict-transport-security"),
                         ["max-age=31536000"])
        self.assertEqual(self.header(headers, "cache-control"), ["no-store"])
        self.assertRegex(self.header(headers, "x-request-id")[0],
                         r"^[0-9a-f]{16}$")

        # The Server header does not advertise a version
        self.assertNotIn("26.", " ".join(self.header(headers, "server")))

        # Startup was logged as JSON
        lines = [line for line in server.output().splitlines()
                 if line.startswith("{")]
        self.assertTrue(any(json.loads(line)["message"].startswith(
            "Startup checks passed") for line in lines))

    def test_plain_http_is_refused(self):
        server = self.start()

        status, _, text = server.request(
            "GET", "/login", headers={"X-Forwarded-Proto": "http"})

        self.assertEqual(status, 400)
        self.assertIn("must be accessed over HTTPS", text)

    def test_unknown_host_is_refused(self):
        server = self.start()

        status, _, _ = server.request(
            "GET", "/login", headers={"Host": "attacker.example"})

        self.assertEqual(status, 400)

    def test_gunicorn_itself_never_believes_forwarded_headers(self):
        # The proxy is configured as 10.0.0.2, but the connection comes
        # from 127.0.0.1. Gunicorn's default would accept X-Forwarded-Proto
        # from 127.0.0.1; the deployment configuration turns that off, so
        # only the application decides, and it says no.
        server = self.start(dict(PROXIED_ENV, TRUSTED_PROXY_IPS="10.0.0.2"))

        for header in ("X-Forwarded-Proto", "X-Forwarded-Ssl",
                       "X-Forwarded-Protocol"):
            with self.subTest(header=header):
                value = {"X-Forwarded-Proto": "https",
                         "X-Forwarded-Ssl": "on",
                         "X-Forwarded-Protocol": "ssl"}[header]
                status, _, text = server.request(
                    "GET", "/login",
                    headers={"X-Forwarded-Proto": "", header: value})
                self.assertEqual(status, 400)
                self.assertIn("must be accessed over HTTPS", text)

    def test_query_strings_are_not_written_to_the_access_log(self):
        server = self.start()

        status, _, _ = server.request(
            "GET", "/?search=confidential-search-text")
        server.stop()

        log = server.output()
        self.assertIn(f'"GET / HTTP/1.1" {status}', log)
        self.assertNotIn("confidential-search-text", log)

    def test_rate_limit_uses_the_address_reported_by_the_proxy(self):
        server = self.start()
        cookie, token = self.csrf_session(server)

        # A client-supplied X-Forwarded-For value is to the left of the
        # one Caddy appends; only the rightmost is believed
        codes = [
            self.login_attempt(server, cookie, token, "wrong",
                               f"198.51.100.{n}, 203.0.113.77")
            for n in range(6)
        ]
        self.assertEqual(codes, [200] * 5 + [429])

        # Another real visitor is unaffected
        self.assertEqual(
            self.login_attempt(server, cookie, token, "wrong", "203.0.113.78"),
            200)

    def test_sigterm_stops_gracefully(self):
        server = self.start()
        self.assertEqual(len(server.worker_pids(expected=1)), 1)

        server.process.send_signal(signal.SIGTERM)

        self.assertEqual(server.wait_for_exit(35), 0)


class TestGunicornRefusesUnsafeStarts(GunicornTestCase):

    def test_unmigrated_database_stops_startup(self):
        server = self.start(migrate=False, wait=False)

        self.assertNotEqual(server.wait_for_exit(), 0)
        output = server.output()
        self.assertIn("Startup check failed", output)
        self.assertNotIn(self.db_path, output)
        self.assertNotIn(TEST_SECRET_KEY, output)

    def test_missing_secret_key_stops_startup_without_printing_secrets(self):
        settings = dict(PROXIED_ENV)
        del settings["SECRET_KEY"]
        server = self.start(settings, wait=False,
                            extra={"RATELIMIT_STORAGE_URI": "memory://"})

        self.assertNotEqual(server.wait_for_exit(), 0)
        self.assertIn("SECRET_KEY is not set", server.output())

    def test_single_process_memory_limits_refuse_several_workers(self):
        server = self.start(workers=3, wait=False)

        self.assertNotEqual(server.wait_for_exit(), 0)
        self.assertIn("RATELIMIT_SINGLE_PROCESS=1 allows exactly one worker",
                      server.output())

    def test_public_bind_address_is_refused(self):
        make_database(self.db_path)
        server = GunicornProcess(PROXIED_ENV, db_path=self.db_path,
                                 extra={"HELPDESK_BIND": "0.0.0.0:8000"})
        self.addCleanup(server.log.close)
        self.addCleanup(server.stop)

        self.assertNotEqual(server.wait_for_exit(), 0)
        self.assertIn("HELPDESK_BIND must be a loopback address",
                      server.output())

    def test_unreachable_redis_stops_startup(self):
        settings = {key: value for key, value in PROXIED_ENV.items()
                    if key not in SINGLE_PROCESS_RATE_LIMITS}
        server = self.start(dict(
            settings,
            RATELIMIT_STORAGE_URI="redis://:hidden-pass@127.0.0.1:1/0"),
            workers=2, wait=False)

        self.assertNotEqual(server.wait_for_exit(), 0)
        output = server.output()
        self.assertIn("rate-limit storage did not answer", output)
        self.assertNotIn("hidden-pass", output)


@requires_redis
class TestGunicornWorkersShareRateLimits(GunicornTestCase):

    def test_three_workers_allow_five_attempts_in_total(self):
        flush_test_redis()
        settings = {key: value for key, value in PROXIED_ENV.items()
                    if key not in SINGLE_PROCESS_RATE_LIMITS}
        server = self.start(dict(settings,
                                 RATELIMIT_STORAGE_URI=test_redis_url()),
                            workers=3)
        self.assertEqual(len(server.worker_pids(expected=3)), 3)

        cookie, token = self.csrf_session(server, "203.0.113.50")

        # Each attempt opens a new connection, so the workers share them
        codes = [self.login_attempt(server, cookie, token, f"wrong{n}",
                                    "203.0.113.50")
                 for n in range(12)]

        self.assertEqual(codes, [200] * 5 + [429] * 7)


# ---------------------------------------------------
# IN-PROCESS: ENTRY POINT, ERRORS, LOGGING, DATABASE PATH
# ---------------------------------------------------

class TemporaryDatabaseTestCase(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.db_path = os.path.join(directory.name, "helpdesk.db")
        patcher = patch.object(database, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestWsgiEntryPoint(TemporaryDatabaseTestCase):

    def import_wsgi(self):
        sys.modules.pop("wsgi", None)
        self.addCleanup(sys.modules.pop, "wsgi", None)

        # The JSON log handler is tested separately; installing it here
        # would change logging for the rest of the test run
        with patch.dict(os.environ, clean_environment(PRODUCTION_ENV),
                        clear=True), \
                patch.object(logging_setup, "configure_json_logging") as json:
            module = importlib.import_module("wsgi")

        json.assert_called_once_with()
        return module

    def test_ready_database_gives_an_application(self):
        migrations.migrate(self.db_path)
        module = self.import_wsgi()

        self.assertTrue(module.application.config["SESSION_COOKIE_SECURE"])

    def test_missing_database_refuses_to_start(self):
        with self.assertLogs("helpdesk.startup", "CRITICAL") as logs:
            with self.assertRaises(Exception) as caught:
                self.import_wsgi()

        self.assertEqual(type(caught.exception).__name__, "StartupCheckFailed")
        self.assertNotIn(self.directory, str(caught.exception))
        self.assertIn("database file does not exist", "\n".join(logs.output))

    def test_outdated_schema_refuses_to_start(self):
        migrations.migrate(self.db_path,
                           migrations=migrations.MIGRATIONS[:-1])

        with self.assertLogs("helpdesk.startup", "CRITICAL") as logs:
            with self.assertRaises(Exception):
                self.import_wsgi()

        self.assertIn("run 'flask --app app init-db'", "\n".join(logs.output))


class TestUnexpectedErrors(TemporaryDatabaseTestCase):

    def setUp(self):
        super().setUp()
        migrations.migrate(self.db_path)
        self.app = create_app(environ=dict(PRODUCTION_ENV))

        @self.app.route("/explode")
        def explode():
            raise RuntimeError(f"secret detail {self.db_path}")

    def test_500_shows_a_reference_and_nothing_else(self):
        with self.assertLogs(self.app.logger, "ERROR") as logs:
            response = self.app.test_client().get("/explode",
                                                  base_url=HTTPS_URL)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.content_type, "text/plain; charset=utf-8")
        request_id = response.headers["X-Request-ID"]
        self.assertEqual(
            response.text,
            "Something went wrong and the request could not be completed. "
            "If it keeps happening, tell your administrator and quote "
            f"reference {request_id}.\n")

        for leaked in ("secret detail", self.db_path, "Traceback",
                       "RuntimeError"):
            self.assertNotIn(leaked, response.text)

        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("Content-Security-Policy", response.headers)

        # The administrator gets the full story in the log
        self.assertIn("RuntimeError: secret detail", "\n".join(logs.output))

    def test_every_response_has_a_new_request_id(self):
        client = self.app.test_client()
        ids = {client.get("/login", base_url=HTTPS_URL)
               .headers["X-Request-ID"] for _ in range(5)}
        self.assertEqual(len(ids), 5)

    def test_a_client_supplied_request_id_is_ignored(self):
        response = self.app.test_client().get(
            "/login", base_url=HTTPS_URL,
            headers={"X-Request-ID": "forged-id"})
        self.assertNotEqual(response.headers["X-Request-ID"], "forged-id")

    def test_refused_requests_also_get_an_id(self):
        response = self.app.test_client().get("/login",
                                              base_url="http://helpdesk.test")
        self.assertEqual(response.status_code, 400)
        self.assertRegex(response.headers["X-Request-ID"], r"^[0-9a-f]{16}$")


class TestJsonLogging(unittest.TestCase):

    def format(self, record):
        handler_formatter = logging_setup.JsonFormatter()
        logging_setup.RequestContextFilter().filter(record)
        return handler_formatter.format(record)

    def make_record(self, message, *args, exc_info=None):
        return logging.LogRecord("helpdesk", logging.ERROR, __file__, 1,
                                 message, args, exc_info)

    def test_one_line_even_when_the_message_contains_line_breaks(self):
        line = self.format(self.make_record(
            "user typed %s", "evil\n{\"level\": \"INFO\", \"fake\": 1}"))

        self.assertEqual(len(line.splitlines()), 1)
        entry = json.loads(line)
        self.assertEqual(entry["level"], "ERROR")
        self.assertIn("evil\n", entry["message"])
        self.assertNotIn("fake", entry)

    def test_request_fields_are_added_without_the_query_string(self):
        app = create_app(environ={"APP_ENV": "testing",
                                  "SECRET_KEY": TEST_SECRET_KEY})

        with app.test_request_context("/tickets/7?search=private-words",
                                      method="POST"):
            logging_setup.g.request_id = "abc123"
            entry = json.loads(self.format(self.make_record("hello")))

        self.assertEqual(entry["request_id"], "abc123")
        self.assertEqual(entry["method"], "POST")
        self.assertEqual(entry["path"], "/tickets/7")
        self.assertNotIn("private-words", json.dumps(entry))

    def test_exceptions_are_included(self):
        try:
            raise ValueError("boom")
        except ValueError:
            entry = json.loads(self.format(self.make_record(
                "failed", exc_info=sys.exc_info())))

        self.assertIn("ValueError: boom", entry["exception"])

    def test_configuring_twice_adds_one_handler(self):
        root = logging.getLogger()
        before = list(root.handlers)
        level = root.level
        self.addCleanup(setattr, root, "handlers", before)
        self.addCleanup(root.setLevel, level)

        logging_setup.configure_json_logging()
        logging_setup.configure_json_logging()

        self.assertEqual(len(root.handlers), len(before) + 1)

        # It writes to whatever stderr is now (pytest/unittest swap it)
        # (a logger outside "helpdesk", which Flask may have given a
        # handler of its own earlier in the test run)
        captured = io.StringIO()
        with patch.object(sys, "stderr", captured):
            logging.getLogger("json_logging_test").warning("to stderr")
        lines = captured.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["message"], "to stderr")


class TestDatabasePathSetting(unittest.TestCase):

    def test_default_is_next_to_the_code(self):
        self.assertEqual(database.configured_db_path({}),
                         database.DEFAULT_DB_PATH)

    def test_absolute_path_is_used(self):
        self.assertEqual(
            database.configured_db_path(
                {"HELPDESK_DATABASE_PATH": "/var/lib/helpdesk//helpdesk.db"}),
            "/var/lib/helpdesk/helpdesk.db")

    def test_relative_path_is_refused(self):
        with self.assertRaises(database.DatabasePathError):
            database.configured_db_path(
                {"HELPDESK_DATABASE_PATH": "data/helpdesk.db"})

    def test_a_new_process_uses_the_configured_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "elsewhere.db")
            result = subprocess.run(
                [sys.executable, "-c",
                 "import database; print(database.DB_PATH)"],
                cwd=PROJECT_ROOT, capture_output=True, text=True,
                env=dict(os.environ, HELPDESK_DATABASE_PATH=path),
                timeout=30)

        self.assertEqual(result.stdout.strip(), path)


class TestReadinessChecks(TemporaryDatabaseTestCase):

    def test_ready_when_everything_is_ok(self):
        migrations.migrate(self.db_path)
        app = create_app(environ=dict(PRODUCTION_ENV))

        with app.app_context():
            self.assertEqual(readiness.readiness(app),
                             (True, {"schema": "ok", "database": "ok",
                                     "rate_limit_storage": "ok"}))
        self.assertEqual(readiness.startup_problems(app), [])

    def test_missing_database_stops_at_the_database_check(self):
        app = create_app(environ=dict(PRODUCTION_ENV))

        with app.app_context(), self.assertLogs(app.logger, "ERROR"):
            ready, summary = readiness.readiness(app)

        self.assertFalse(ready)
        self.assertEqual(summary, {"database": "fail"})


if __name__ == "__main__":
    unittest.main()
