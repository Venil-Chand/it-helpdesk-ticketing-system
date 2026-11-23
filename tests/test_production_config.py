"""Tests that unsafe production configuration stops the application."""

import os
import subprocess
import sys
import unittest

import security_config as config
from security_helpers import (
    DEVELOPMENT_ENV,
    PRODUCTION_ENV,
    PROJECT_ROOT,
    TEST_SECRET_KEY,
    TESTING_ENV,
    clean_environment,
    load_app,
)


def production(**overrides):
    return {**PRODUCTION_ENV, **overrides}


class TestSecretKeyValidation(unittest.TestCase):

    def assert_rejected(self, key):
        with self.assertRaises(config.ConfigurationError) as caught:
            config.load_settings(production(SECRET_KEY=key))

        message = str(caught.exception)
        self.assertIn("SECRET_KEY", message)

        if key:
            self.assertNotIn(key, message)

    def test_missing_key_is_rejected(self):
        settings = dict(PRODUCTION_ENV)
        del settings["SECRET_KEY"]

        with self.assertRaises(config.ConfigurationError):
            config.load_settings(settings)

    def test_empty_key_is_rejected(self):
        self.assert_rejected("")

    def test_key_one_character_too_short_is_rejected(self):
        self.assert_rejected(TEST_SECRET_KEY[:31])

    def test_key_at_minimum_length_is_accepted(self):
        key = TEST_SECRET_KEY[:32]

        # A random prefix can rarely look repetitive, so use a fixed shape
        key = "0123456789abcdef" + key[16:]

        self.assertEqual(
            config.load_settings(production(SECRET_KEY=key)).secret_key,
            key
        )

    def test_placeholder_keys_are_rejected(self):
        for key in (
            "replace-this-with-a-long-random-string",
            "ci-testing-key-not-for-production",
            "CHANGEME-0123456789-abcdefghijklmnop",
            "my-secret-key-0123456789-abcdefghijkl",
        ):
            with self.subTest(key=key):
                self.assert_rejected(key)

    def test_repetitive_keys_are_rejected(self):
        for key in ("a" * 64, "abc" * 20):
            with self.subTest(key=key):
                self.assert_rejected(key)

    def test_generated_key_is_accepted(self):
        settings = config.load_settings(PRODUCTION_ENV)
        self.assertTrue(settings.is_production)

    def test_secret_key_is_not_in_settings_repr(self):
        settings = config.load_settings(PRODUCTION_ENV)
        self.assertNotIn(TEST_SECRET_KEY, repr(settings))


class TestEnvironmentSelection(unittest.TestCase):

    def test_default_environment_is_development(self):
        settings = config.load_settings({"SECRET_KEY": "local-dev-key"})

        self.assertEqual(settings.environment, config.DEVELOPMENT)
        self.assertEqual(settings.trusted_hosts, config.LOCAL_HOSTS)

    def test_unknown_environment_is_rejected(self):
        with self.assertRaises(config.ConfigurationError):
            config.load_settings({"APP_ENV": "prod", "SECRET_KEY": "x"})

    def test_development_still_requires_a_key(self):
        with self.assertRaises(config.ConfigurationError):
            config.load_settings({"APP_ENV": "development"})

    def test_dotenv_is_never_read_in_production_or_testing(self):
        self.assertFalse(config.should_load_dotenv({"APP_ENV": "production"}))
        self.assertFalse(config.should_load_dotenv({"APP_ENV": " PRODUCTION "}))
        self.assertFalse(config.should_load_dotenv({"APP_ENV": "testing"}))

    def test_dotenv_is_read_for_local_development(self):
        self.assertTrue(config.should_load_dotenv({}))
        self.assertTrue(config.should_load_dotenv({"APP_ENV": "development"}))

    def test_all_problems_are_reported_together(self):
        with self.assertRaises(config.ConfigurationError) as caught:
            config.load_settings({
                "APP_ENV": "production",
                "FLASK_DEBUG": "1",
            })

        message = str(caught.exception)

        for variable in ("SECRET_KEY", "FLASK_DEBUG", "TRUSTED_HOSTS"):
            self.assertIn(variable, message)


class TestDebugRestrictions(unittest.TestCase):

    def test_debug_values_are_rejected_in_production(self):
        for value in ("1", "true", "TRUE", "yes", "on", "anything"):
            with self.subTest(value=value):
                with self.assertRaises(config.ConfigurationError):
                    config.load_settings(production(FLASK_DEBUG=value))

    def test_debug_off_values_are_accepted_in_production(self):
        for value in ("", "0", "false", "False", "no"):
            with self.subTest(value=value):
                config.load_settings(production(FLASK_DEBUG=value))

    def test_production_app_has_debug_off(self):
        module = load_app(PRODUCTION_ENV)
        self.addCleanup(sys.modules.pop, module.__name__, None)

        self.assertFalse(module.app.debug)
        self.assertFalse(module.app.config["DEBUG"])

    def test_debug_is_still_allowed_in_development(self):
        settings = {**DEVELOPMENT_ENV, "FLASK_DEBUG": "1"}
        self.assertFalse(config.load_settings(settings).is_production)


class TestStartupRefusal(unittest.TestCase):
    """Run real Python processes, as a server would."""

    def run_python(self, settings, *arguments):
        return subprocess.run(
            [sys.executable, *arguments],
            cwd=PROJECT_ROOT,
            env=clean_environment(settings),
            capture_output=True,
            text=True,
            timeout=30
        )

    def test_import_fails_without_a_secret_key(self):
        result = self.run_python(
            {"APP_ENV": "production", "TRUSTED_HOSTS": "helpdesk.test"},
            "-c", "import app"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SECRET_KEY", result.stderr)

    def test_rejected_key_never_appears_in_output(self):
        weak_key = "weak-key-value-that-must-stay-hidden"[:30]

        result = self.run_python(
            production(SECRET_KEY=weak_key),
            "-c", "import app"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(weak_key, result.stdout + result.stderr)

    def test_import_succeeds_with_safe_production_settings(self):
        result = self.run_python(PRODUCTION_ENV, "-c", "import app")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(TEST_SECRET_KEY, result.stdout + result.stderr)

    def test_development_server_refuses_to_run_in_production(self):
        result = self.run_python(PRODUCTION_ENV, "app.py")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Refusing to start", result.stderr)


class TestTrustedHostConfiguration(unittest.TestCase):

    def test_hosts_are_required_in_production(self):
        settings = production()
        del settings["TRUSTED_HOSTS"]

        with self.assertRaises(config.ConfigurationError):
            config.load_settings(settings)

    def test_unsafe_host_values_are_rejected(self):
        for value in (
            "*",
            "*.example.org",
            ".example.org",
            "https://helpdesk.test",
            "helpdesk.test:443",
            "helpdesk.test/path",
            "[::1]",
            "bad host",
        ):
            with self.subTest(value=value):
                with self.assertRaises(config.ConfigurationError):
                    config.load_settings(production(TRUSTED_HOSTS=value))

    def test_host_list_is_normalised(self):
        settings = config.load_settings(
            production(TRUSTED_HOSTS=" Helpdesk.Test, helpdesk.test,a.test ")
        )

        self.assertEqual(settings.trusted_hosts, ("helpdesk.test", "a.test"))


class TestProxyConfiguration(unittest.TestCase):

    def assert_rejected(self, **overrides):
        with self.assertRaises(config.ConfigurationError):
            config.load_settings(production(**overrides))

    def test_no_proxy_is_trusted_by_default(self):
        settings = config.load_settings(PRODUCTION_ENV)

        self.assertEqual(settings.trusted_proxy_count, 0)
        self.assertEqual(settings.trusted_proxy_networks, ())

    def test_count_requires_addresses(self):
        self.assert_rejected(TRUSTED_PROXY_COUNT="1")

    def test_addresses_require_count(self):
        self.assert_rejected(TRUSTED_PROXY_IPS="10.0.0.2")
        self.assert_rejected(
            TRUSTED_PROXY_COUNT="0", TRUSTED_PROXY_IPS="10.0.0.2"
        )

    def test_invalid_counts_are_rejected(self):
        for value in ("-1", "1.5", "one", "6", "٣"):
            with self.subTest(value=value):
                self.assert_rejected(
                    TRUSTED_PROXY_COUNT=value,
                    TRUSTED_PROXY_IPS="10.0.0.2"
                )

    def test_broad_or_invalid_networks_are_rejected(self):
        for value in (
            "0.0.0.0/0",
            "10.0.0.0/7",
            "::/0",
            "2001::/16",
            "*",
            "proxy.internal",
            "10.0.0.1/24",
            "::ffff:10.0.0.2",
        ):
            with self.subTest(value=value):
                self.assert_rejected(
                    TRUSTED_PROXY_COUNT="1",
                    TRUSTED_PROXY_IPS=value
                )

    def test_valid_proxy_settings_are_accepted(self):
        settings = config.load_settings(production(
            TRUSTED_PROXY_COUNT="2",
            TRUSTED_PROXY_IPS="10.0.0.2, 192.168.1.0/24, fd00::1"
        ))

        self.assertEqual(settings.trusted_proxy_count, 2)
        self.assertEqual(len(settings.trusted_proxy_networks), 3)


class TestTestingEnvironment(unittest.TestCase):

    def test_testing_environment_keeps_csrf_enabled(self):
        module = load_app(TESTING_ENV)
        self.addCleanup(sys.modules.pop, module.__name__, None)

        self.assertTrue(module.app.config["WTF_CSRF_ENABLED"])
        self.assertFalse(module.app.config["SESSION_COOKIE_SECURE"])


if __name__ == "__main__":
    unittest.main()
