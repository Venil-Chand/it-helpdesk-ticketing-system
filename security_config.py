"""
Environment-aware security configuration.

The application runs in one of three environments, chosen by APP_ENV:

    development  the default; works over plain HTTP on this computer
    testing      like development, but never reads the .env file
    production   every setting below is checked before traffic is accepted

Problems are reported by variable name only. A rejected value is never
placed in an error message, because error messages end up in logs.
"""

import os
import re
from dataclasses import dataclass, field

DEVELOPMENT = "development"
TESTING = "testing"
PRODUCTION = "production"
ENVIRONMENTS = (DEVELOPMENT, TESTING, PRODUCTION)

# 32 characters is the shortest key accepted in production. The command in
# .env.example generates 64 hexadecimal characters (256 bits of randomness).
MIN_SECRET_KEY_LENGTH = 32

# Even hexadecimal keys use around 16 different characters, so a key with
# fewer than this is a repeated pattern such as "abcabcabc..."
MIN_SECRET_KEY_DISTINCT_CHARACTERS = 10

# Text found in sample or placeholder keys, compared in lower case
UNSAFE_SECRET_KEY_FRAGMENTS = (
    "replace-this",
    "replace_this",
    "change-me",
    "changeme",
    "change_me",
    "not-for-production",
    "your-secret",
    "secret-key",
    "secret_key",
    "example",
    "password",
)

# Hostnames accepted when TRUSTED_HOSTS is not set outside production. If
# someone forgets APP_ENV=production on a real server, requests for its
# public name are refused instead of being served with development settings.
LOCAL_HOSTS = ("localhost", "127.0.0.1")

# A plain hostname or IPv4 address, with no scheme, port, path or wildcard.
# IPv6 literals are refused because Werkzeug's host check removes the text
# after their last colon, which would make different addresses compare equal.
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$"
)

# Flask treats any FLASK_DEBUG value except these as "debug on"
_FLASK_DEBUG_OFF_VALUES = frozenset({"", "0", "false", "no"})


class ConfigurationError(RuntimeError):
    """The environment is unsafe, so the application must not start."""


@dataclass(frozen=True)
class SecuritySettings:
    """Validated security settings for this process."""

    environment: str

    # repr=False keeps the key out of logs if the settings are ever printed
    secret_key: str = field(repr=False)

    trusted_hosts: tuple

    @property
    def is_production(self):
        return self.environment == PRODUCTION


def should_load_dotenv(environ):
    """
    Decide whether the local .env file may supply settings.

    Production secrets must come from the real environment, so a leftover
    development key in .env can never be picked up by a production server.
    """

    return environ.get("APP_ENV", "").strip().lower() not in (
        PRODUCTION, TESTING
    )


def debug_requested(environ):
    """Apply Flask's own rule for whether FLASK_DEBUG turns debug mode on."""

    value = environ.get("FLASK_DEBUG", "").strip().lower()

    return value not in _FLASK_DEBUG_OFF_VALUES


def secret_key_problem(value):
    """Explain why a key is unsafe for production, or return None."""

    if len(value) < MIN_SECRET_KEY_LENGTH:
        return (
            f"SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} "
            "characters long."
        )

    lowered = value.lower()

    if any(text in lowered for text in UNSAFE_SECRET_KEY_FRAGMENTS):
        return "SECRET_KEY looks like a sample or placeholder value."

    if len(set(value)) < MIN_SECRET_KEY_DISTINCT_CHARACTERS:
        return "SECRET_KEY is too repetitive to be a random value."

    return None


def _parse_trusted_hosts(raw, problems):
    """Read a comma-separated list of exact hostnames."""

    hosts = [item.strip().lower() for item in raw.split(",") if item.strip()]

    if not all(_HOSTNAME.match(host) for host in hosts):
        problems.append(
            "TRUSTED_HOSTS must be a comma-separated list of exact "
            "hostnames, without a scheme, port, path or wildcard."
        )
        return ()

    return tuple(dict.fromkeys(hosts))


def load_settings(environ=None):
    """
    Validate the environment and return its security settings.

    Every problem is collected and reported together, so an administrator
    can fix a deployment in one attempt.
    """

    environ = os.environ if environ is None else environ
    problems = []

    environment = environ.get("APP_ENV", DEVELOPMENT).strip().lower()

    if environment not in ENVIRONMENTS:
        raise ConfigurationError(
            "APP_ENV must be one of: " + ", ".join(ENVIRONMENTS) + "."
        )

    production = environment == PRODUCTION
    secret_key = environ.get("SECRET_KEY", "")

    if not secret_key:
        problems.append(
            "SECRET_KEY is not set. "
            + ("Supply it through the environment."
               if production else
               "Copy .env.example to .env and configure a secret key.")
        )
    elif production:
        problem = secret_key_problem(secret_key)
        if problem:
            problems.append(problem)

    if production and debug_requested(environ):
        problems.append("FLASK_DEBUG must not be enabled in production.")

    trusted_hosts = _parse_trusted_hosts(
        environ.get("TRUSTED_HOSTS", ""), problems
    )

    if not trusted_hosts and not problems_mention_hosts(problems):
        if production:
            problems.append(
                "TRUSTED_HOSTS is required in production, for example "
                "TRUSTED_HOSTS=helpdesk.example.org"
            )
        else:
            trusted_hosts = LOCAL_HOSTS

    if problems:
        raise ConfigurationError(
            f"Unsafe {environment} configuration:\n  - "
            + "\n  - ".join(problems)
        )

    return SecuritySettings(
        environment=environment,
        secret_key=secret_key,
        trusted_hosts=trusted_hosts,
    )


def problems_mention_hosts(problems):
    """Avoid reporting the same TRUSTED_HOSTS mistake twice."""

    return any(problem.startswith("TRUSTED_HOSTS") for problem in problems)


def flask_config(settings):
    """Translate validated settings into Flask configuration values."""

    config = {
        "TRUSTED_HOSTS": list(settings.trusted_hosts),
    }

    if settings.is_production:
        config["DEBUG"] = False

    return config
