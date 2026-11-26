"""
Shared (Redis) rate-limit storage for multi-worker deployments.

Gunicorn runs several worker processes. With in-memory counters each
worker counts separately, so "5 attempts per minute" would really allow
5 per worker. These tests check that:

  * production refuses to start without a deliberate storage choice, and
    never silently falls back to in-memory counting;
  * unsafe Redis addresses (plain text over the network) are refused;
  * when Redis cannot be reached, sign-in and password changes are
    refused with 503 (fail closed) and nothing is processed or recorded;
  * with a real Redis, separate application instances, separate
    processes and concurrent requests all share one limit.

The real-Redis tests need HELPDESK_TEST_REDIS_URL (see redis_helpers.py).
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import database
import migrations
import secret_files
import security_config as config
from helpdesk import readiness
from helpdesk.security import (
    RATE_LIMIT_KEY_PREFIX,
    RATE_LIMIT_UNAVAILABLE_MESSAGE,
    get_limiter,
)

from redis_helpers import (
    UNREACHABLE_REDIS_URL,
    flush_test_redis,
    requires_redis,
    test_redis_url,
)
from security_helpers import (
    HTTPS_URL,
    PASSWORD,
    PRODUCTION_ENV,
    PROJECT_ROOT,
    SINGLE_PROCESS_RATE_LIMITS,
    TEST_SECRET_KEY,
    SecurityTestCase,
    clean_environment,
)

# Production settings with no rate-limit storage chosen at all
BARE_PRODUCTION = {
    key: value for key, value in PRODUCTION_ENV.items()
    if key not in SINGLE_PROCESS_RATE_LIMITS
}


def production(**overrides):
    return {**BARE_PRODUCTION, **overrides}


def refused(environ):
    """Return the ConfigurationError message for an environment."""

    try:
        config.load_settings(environ)
    except config.ConfigurationError as error:
        return str(error)

    raise AssertionError("configuration was accepted")


# ---------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------

class TestStorageConfiguration(unittest.TestCase):

    def test_production_requires_an_explicit_choice(self):
        message = refused(BARE_PRODUCTION)
        self.assertIn("RATELIMIT_STORAGE_URI is required in production",
                      message)

    def test_production_refuses_memory_without_single_process(self):
        message = refused(production(RATELIMIT_STORAGE_URI="memory://"))
        self.assertIn("multiplies the login limit", message)

    def test_production_accepts_declared_single_process_memory(self):
        settings = config.load_settings(production(
            RATELIMIT_STORAGE_URI="memory://", RATELIMIT_SINGLE_PROCESS="1"
        ))
        self.assertEqual(settings.rate_limit_storage_kind, config.MEMORY)

    def test_single_process_flag_must_be_exactly_one(self):
        for value in ("yes", "true", "2", "0"):
            with self.subTest(value=value):
                message = refused(production(
                    RATELIMIT_STORAGE_URI="memory://",
                    RATELIMIT_SINGLE_PROCESS=value
                ))
                self.assertIn("RATELIMIT_SINGLE_PROCESS must be 1", message)

    def test_single_process_flag_is_refused_with_redis(self):
        message = refused(production(
            RATELIMIT_STORAGE_URI="redis://127.0.0.1:6379/0",
            RATELIMIT_SINGLE_PROCESS="1"
        ))
        self.assertIn("applies only to RATELIMIT_STORAGE_URI=memory://",
                      message)

    def test_accepted_redis_addresses(self):
        for uri in (
            "redis+unix:///run/redis/redis-server.sock",
            "redis+unix://:pw@/run/redis/redis-server.sock?db=2",
            "redis://127.0.0.1:6379/0",
            "redis://:pw@localhost:6379/1",
            "redis://[::1]:6379/0",
            "rediss://:pw@redis.internal.example:6380/0",
        ):
            with self.subTest(uri=uri):
                settings = config.load_settings(
                    production(RATELIMIT_STORAGE_URI=uri)
                )
                self.assertEqual(settings.rate_limit_storage_kind,
                                 config.REDIS)
                self.assertEqual(settings.rate_limit_storage_uri, uri)

    def test_plain_redis_to_another_machine_is_refused(self):
        for uri in ("redis://10.0.0.5:6379/0",
                    "redis://:pw@redis.example.org:6379/0",
                    "redis://0.0.0.0:6379/0"):
            with self.subTest(uri=uri):
                self.assertIn("only for a Redis server on this machine",
                              refused(production(RATELIMIT_STORAGE_URI=uri)))

    def test_plain_redis_to_another_machine_is_allowed_in_development(self):
        settings = config.load_settings({
            "APP_ENV": "development", "SECRET_KEY": TEST_SECRET_KEY,
            "RATELIMIT_STORAGE_URI": "redis://10.0.0.5:6379/0",
        })
        self.assertEqual(settings.rate_limit_storage_kind, config.REDIS)

    def test_unsupported_or_malformed_values_are_refused(self):
        for uri in ("memcached://127.0.0.1:11211",
                    "mongodb://127.0.0.1", "127.0.0.1:6379",
                    "redis://", "redis://127.0.0.1:notaport/0",
                    "redis+unix://relative/socket",
                    "redis+unix://host/run/socket", "memory"):
            with self.subTest(uri=uri):
                refused(production(RATELIMIT_STORAGE_URI=uri))

    def test_development_and_testing_default_to_memory(self):
        for environment in ("development", "testing"):
            with self.subTest(environment=environment):
                settings = config.load_settings({
                    "APP_ENV": environment, "SECRET_KEY": TEST_SECRET_KEY
                })
                self.assertEqual(settings.rate_limit_storage_uri,
                                 config.MEMORY_STORAGE)

    def test_redis_password_never_appears_in_errors_or_repr(self):
        secret = "hunter2-redis-password-value"
        message = refused(production(
            RATELIMIT_STORAGE_URI=f"redis://:{secret}@10.9.9.9:6379/0",
            FLASK_DEBUG="1"
        ))
        self.assertNotIn(secret, message)

        settings = config.load_settings(production(
            RATELIMIT_STORAGE_URI=f"redis://:{secret}@127.0.0.1:6379/0"
        ))
        self.assertNotIn(secret, repr(settings))


class TestSecretFiles(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name

    def write(self, name, text, mode=0o600):
        path = os.path.join(self.directory, name)
        with open(path, "w") as handle:
            handle.write(text)
        os.chmod(path, mode)
        return path

    def test_storage_uri_can_come_from_a_file(self):
        path = self.write("redis-uri", "redis://:pw@127.0.0.1:6379/0\n")
        settings = config.load_settings(
            production(RATELIMIT_STORAGE_URI_FILE=path)
        )
        self.assertEqual(settings.rate_limit_storage_uri,
                         "redis://:pw@127.0.0.1:6379/0")

    def test_secret_key_can_come_from_a_file(self):
        path = self.write("secret-key", TEST_SECRET_KEY + "\n")
        environ = dict(PRODUCTION_ENV, SECRET_KEY_FILE=path)
        del environ["SECRET_KEY"]

        self.assertEqual(config.load_settings(environ).secret_key,
                         TEST_SECRET_KEY)

    def test_only_one_trailing_line_break_is_removed(self):
        path = self.write("value", "  pass word \n\n")
        self.assertEqual(
            secret_files.read_secret({"PW_FILE": path}, "PW"),
            "  pass word \n"
        )

    def test_value_and_file_together_are_refused(self):
        path = self.write("secret-key", TEST_SECRET_KEY)
        message = refused(dict(PRODUCTION_ENV, SECRET_KEY_FILE=path))
        self.assertIn("Set only one of SECRET_KEY or SECRET_KEY_FILE",
                      message)

    def test_readable_by_others_is_refused_in_production(self):
        path = self.write("secret-key", TEST_SECRET_KEY, mode=0o644)
        environ = dict(PRODUCTION_ENV, SECRET_KEY_FILE=path)
        del environ["SECRET_KEY"]

        self.assertIn("must not be readable by other accounts",
                      refused(environ))

        # Development accepts it: a laptop is not a shared server
        settings = config.load_settings({"APP_ENV": "development",
                                         "SECRET_KEY_FILE": path})
        self.assertEqual(settings.secret_key, TEST_SECRET_KEY)

    def test_missing_relative_or_oversized_files_are_refused(self):
        big = self.write("big", "x" * (secret_files.MAX_SECRET_FILE_BYTES + 1))
        fifo = os.path.join(self.directory, "fifo")
        os.mkfifo(fifo, 0o600)
        cases = {
            fifo: "regular file",  # and opening it must not hang
            os.path.join(self.directory, "missing"): "could not be read",
            "relative/secret": "must be an absolute path",
            big: "too large",
            self.directory: "regular file",
        }

        for path, expected in cases.items():
            with self.subTest(path=path):
                environ = dict(PRODUCTION_ENV, SECRET_KEY_FILE=path)
                del environ["SECRET_KEY"]
                message = refused(environ)
                self.assertIn(expected, message)
                # The path itself is never echoed
                self.assertNotIn(self.directory, message)

    def test_file_contents_never_appear_in_errors(self):
        weak = "weak-secret-in-a-file"
        path = self.write("secret-key", weak)
        environ = dict(PRODUCTION_ENV, SECRET_KEY_FILE=path)
        del environ["SECRET_KEY"]

        message = refused(environ)
        self.assertIn("SECRET_KEY must be at least", message)
        self.assertNotIn(weak, message)


# ---------------------------------------------------
# FAILING CLOSED WHEN REDIS IS UNREACHABLE (no server needed)
# ---------------------------------------------------

class RateLimitTestCase(SecurityTestCase):

    def attempt_login(self, client, password, address="198.51.100.7"):
        token_response = client.get(
            "/login", base_url=self.base_url,
            environ_overrides={"REMOTE_ADDR": address}
        )
        token = re.search(r'name="csrf_token"\s+value="([^"]+)"',
                          token_response.text).group(1)

        return client.post(
            "/login",
            data={"csrf_token": token, "username": "alice",
                  "password": password},
            base_url=self.base_url,
            headers=self.referer_headers(),
            environ_overrides={"REMOTE_ADDR": address}
        )

    def login_events(self):
        conn = database.get_db()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM audit_events "
                "WHERE action LIKE 'login_%'"
            ).fetchone()[0]
        finally:
            conn.close()


class TestRedisUnavailable(RateLimitTestCase):

    settings = production(RATELIMIT_STORAGE_URI=UNREACHABLE_REDIS_URL)

    def test_sign_in_is_refused_with_503_and_nothing_is_processed(self):
        client = self.app.test_client()

        with self.assertLogs(self.app.logger, "ERROR") as logs:
            response = self.attempt_login(client, PASSWORD)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.text, RATE_LIMIT_UNAVAILABLE_MESSAGE)
        self.assertEqual(response.headers["Retry-After"], "30")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

        # The correct password did not sign anyone in, and nothing was
        # recorded, because the view never ran
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(self.login_events(), 0)
        self.assertEqual(client.get("/", base_url=self.base_url)
                         .status_code, 302)

        # The log names the problem but not the address or password
        joined = "\n".join(logs.output)
        self.assertIn("Rate-limit storage unavailable", joined)
        self.assertNotIn("not-a-real-password", joined)
        self.assertNotIn("127.0.0.1", joined)

    def test_every_attempt_is_refused_not_just_the_first(self):
        client = self.app.test_client()

        with self.assertLogs(self.app.logger, "ERROR"):
            codes = [self.attempt_login(client, "wrong-password").status_code
                     for _ in range(8)]

        self.assertEqual(codes, [503] * 8)
        self.assertEqual(self.login_events(), 0)

    def test_password_change_is_refused_too(self):
        # Sign in through a working limiter, then lose Redis
        healthy = self.module.create_app(environ=dict(PRODUCTION_ENV))
        client = healthy.test_client()
        self.login("alice", client)
        cookie = client.get_cookie("__Host-helpdesk_session",
                                   domain="helpdesk.test")

        broken = self.app.test_client()
        broken.set_cookie("__Host-helpdesk_session", cookie.value,
                          domain="helpdesk.test", secure=True)
        token_page = broken.get("/account/password", base_url=HTTPS_URL)
        self.assertEqual(token_page.status_code, 200)
        token = re.search(r'name="csrf_token"\s+value="([^"]+)"',
                          token_page.text).group(1)

        with self.assertLogs(self.app.logger, "ERROR"):
            response = broken.post(
                "/account/password",
                data={"csrf_token": token, "current_password": PASSWORD,
                      "new_password": "another-long-passphrase-42",
                      "confirm_password": "another-long-passphrase-42"},
                base_url=HTTPS_URL, headers=self.referer_headers()
            )

        self.assertEqual(response.status_code, 503)

        # The old password still works: nothing was changed
        self.login("alice", self.module.create_app(
            environ=dict(PRODUCTION_ENV)).test_client())

    def test_pages_without_a_limit_keep_working(self):
        client = self.app.test_client()
        self.assertEqual(client.get("/login", base_url=HTTPS_URL)
                         .status_code, 200)

    def test_readiness_reports_the_storage_without_details(self):
        with self.app.app_context():
            with self.assertLogs(self.app.logger, "ERROR") as logs:
                ready, summary = readiness.readiness(self.app)

        self.assertFalse(ready)
        self.assertEqual(summary["rate_limit_storage"], "fail")
        self.assertEqual(summary["schema"], "ok")
        self.assertNotIn("not-a-real-password", "\n".join(logs.output))

    def test_unreachable_redis_answers_quickly(self):
        client = self.app.test_client()
        started = time.monotonic()

        with self.assertLogs(self.app.logger, "ERROR"):
            self.attempt_login(client, PASSWORD)

        self.assertLess(time.monotonic() - started, 5)

    def test_there_is_no_in_memory_fallback(self):
        limiter = get_limiter(self.app)
        self.assertFalse(limiter._in_memory_fallback_enabled)
        self.assertFalse(limiter._swallow_errors)
        self.assertEqual(type(limiter.storage).__name__, "RedisStorage")


# ---------------------------------------------------
# REAL REDIS: ONE LIMIT SHARED BY EVERY WORKER
# ---------------------------------------------------

@requires_redis
class TestSharedLimitWithRedis(RateLimitTestCase):

    @property
    def settings(self):
        return production(RATELIMIT_STORAGE_URI=test_redis_url())

    def setUp(self):
        self.redis = flush_test_redis()
        super().setUp()

    def second_worker(self):
        """Another application instance, as a second Gunicorn worker is."""

        return self.module.create_app(environ=clean_environment(self.settings))

    def test_two_application_instances_share_one_counter(self):
        first = self.app.test_client()
        second = self.second_worker().test_client()

        codes = []
        for n in range(8):
            client = first if n % 2 == 0 else second
            codes.append(self.attempt_login(client, "wrong").status_code)

        self.assertEqual(codes, [200] * 5 + [429] * 3)
        self.assertEqual(self.login_events(), 5)

    def test_counters_are_kept_under_the_helpdesk_prefix(self):
        self.attempt_login(self.app.test_client(), "wrong")

        keys = [key.decode() for key in self.redis.keys("*")]
        self.assertTrue(keys)
        self.assertTrue(all(RATE_LIMIT_KEY_PREFIX in key for key in keys),
                        keys)

    def test_different_addresses_have_separate_limits(self):
        client = self.app.test_client()

        for _ in range(5):
            self.attempt_login(client, "wrong", address="198.51.100.1")

        self.assertEqual(
            self.attempt_login(client, "wrong", "198.51.100.1").status_code,
            429)
        self.assertEqual(
            self.attempt_login(client, "wrong", "198.51.100.2").status_code,
            200)

    def test_concurrent_requests_across_workers_allow_exactly_five(self):
        apps = [self.app, self.second_worker(), self.second_worker()]
        codes = []
        lock = threading.Lock()
        start = threading.Barrier(15)

        def worker(index):
            client = apps[index % 3].test_client()
            token_response = client.get(
                "/login", base_url=HTTPS_URL,
                environ_overrides={"REMOTE_ADDR": "198.51.100.9"}
            )
            token = re.search(r'name="csrf_token"\s+value="([^"]+)"',
                              token_response.text).group(1)
            start.wait()
            response = client.post(
                "/login",
                data={"csrf_token": token, "username": "alice",
                      "password": "wrong"},
                base_url=HTTPS_URL, headers=self.referer_headers(),
                environ_overrides={"REMOTE_ADDR": "198.51.100.9"}
            )
            with lock:
                codes.append(response.status_code)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(15)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

        self.assertEqual(sorted(codes), [200] * 5 + [429] * 10)
        self.assertEqual(self.login_events(), 5)

    def test_separate_processes_share_the_limit(self):
        """Three real Python processes, as three Gunicorn workers are."""

        script = f"""
import json, re, sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})
from helpdesk import create_app
app = create_app()
client = app.test_client()
codes = []
for _ in range(4):
    page = client.get("/login", base_url="https://helpdesk.test",
                      environ_overrides={{"REMOTE_ADDR": "198.51.100.33"}})
    token = re.search(r'name="csrf_token"\\s+value="([^"]+)"',
                      page.text).group(1)
    codes.append(client.post(
        "/login", data={{"csrf_token": token, "username": "alice",
                        "password": "wrong"}},
        base_url="https://helpdesk.test",
        headers={{"Referer": "https://helpdesk.test/"}},
        environ_overrides={{"REMOTE_ADDR": "198.51.100.33"}}).status_code)
print(json.dumps(codes))
"""
        environ = clean_environment(dict(
            self.settings, HELPDESK_DATABASE_PATH=self.db_path
        ))
        processes = [
            subprocess.Popen([sys.executable, "-c", script], env=environ,
                             cwd=tempfile.gettempdir(),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
            for _ in range(3)
        ]

        codes = []
        for process in processes:
            out, err = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, err)
            codes.extend(json.loads(out))

        self.assertEqual(codes.count(200), 5)
        self.assertEqual(codes.count(429), 7)
        self.assertEqual(self.login_events(), 5)

    def test_a_stalled_redis_fails_closed_within_the_timeout(self):
        client = self.app.test_client()
        self.attempt_login(client, "wrong")

        # Redis stops answering commands from every other client
        self.redis.execute_command("CLIENT", "PAUSE", "4000", "ALL")
        self.addCleanup(self.redis.execute_command, "CLIENT", "UNPAUSE")

        started = time.monotonic()
        with self.assertLogs(self.app.logger, "ERROR"):
            response = self.attempt_login(client, PASSWORD)

        self.assertEqual(response.status_code, 503)
        self.assertLess(time.monotonic() - started, 3.5)
        self.assertEqual(self.login_events(), 1)

    def test_readiness_is_ok_with_redis(self):
        with self.app.app_context():
            ready, summary = readiness.readiness(self.app)

        self.assertTrue(ready, summary)
        self.assertEqual(summary, {"schema": "ok", "database": "ok",
                                   "rate_limit_storage": "ok"})


class TestLocalDevelopmentStaysSimple(unittest.TestCase):

    def test_development_needs_no_redis(self):
        from helpdesk import create_app

        app = create_app(environ={"APP_ENV": "development",
                                  "SECRET_KEY": TEST_SECRET_KEY})
        self.assertEqual(type(get_limiter(app).storage).__name__,
                         "MemoryStorage")


if __name__ == "__main__":
    unittest.main()
