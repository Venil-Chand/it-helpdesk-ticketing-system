"""
Access to a disposable Redis server for the shared rate-limit tests.

Tests that need a real Redis server run only when HELPDESK_TEST_REDIS_URL
names one, for example:

    HELPDESK_TEST_REDIS_URL=redis://:password@127.0.0.1:6390/15

The database it names is EMPTIED before every such test (FLUSHDB), so it
must be a throwaway database on a test server, never one holding real
data. GitHub Actions provides one as a service container. Without the
variable, those tests are skipped and say so; every other test still
runs, including the Redis failure tests, which need no server.
"""

import os
import unittest

REDIS_URL_VARIABLE = "HELPDESK_TEST_REDIS_URL"

# A port on this machine that nothing listens on: connections are refused
UNREACHABLE_REDIS_URL = "redis://:not-a-real-password@127.0.0.1:1/0"


def test_redis_url():
    return os.environ.get(REDIS_URL_VARIABLE, "").strip()


def requires_redis(test_item):
    """Skip a test or class unless a disposable test Redis is configured."""

    return unittest.skipUnless(
        test_redis_url(),
        f"set {REDIS_URL_VARIABLE} to a disposable Redis database to run"
    )(test_item)


def flush_test_redis():
    """Empty the test database and return a client connected to it."""

    import redis

    client = redis.Redis.from_url(test_redis_url(), socket_timeout=5)
    client.flushdb()
    return client
