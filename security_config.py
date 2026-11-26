"""
Environment-aware security configuration.

The application runs in one of three environments, chosen by APP_ENV:

    development  the default; works over plain HTTP on this computer
    testing      like development, but never reads the .env file
    production   every setting below is checked before traffic is accepted

SECRET_KEY may instead be supplied as SECRET_KEY_FILE, the path of a file
holding it (see secret_files.py); in production that file must not be
readable by other accounts.

Problems are reported by variable name only. A rejected value is never
placed in an error message, because error messages end up in logs.
"""

import ipaddress
import os
import re
from urllib.parse import urlsplit
from dataclasses import dataclass, field

import secret_files
import validation

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


PRODUCTION_SESSION_COOKIE_NAME = "__Host-helpdesk_session"

# One year. includeSubDomains and preload are deliberately left out because
# they affect other sites on the domain and are hard to undo.
HSTS_MAX_AGE_SECONDS = 31536000

# Exact CDN files rather than the whole CDN: cdnjs hosts many old libraries
# an attacker could otherwise load to get around the policy.
_CDN = "https://cdnjs.cloudflare.com/ajax/libs"

CSP_DIRECTIVES = (
    "default-src 'none'",
    f"script-src 'self' {_CDN}/bootstrap/5.3.2/js/bootstrap.bundle.min.js",
    "style-src 'self' "
    f"{_CDN}/bootstrap/5.3.2/css/bootstrap.min.css "
    f"{_CDN}/bootstrap-icons/1.11.3/font/bootstrap-icons.min.css",
    f"font-src {_CDN}/bootstrap-icons/1.11.3/font/fonts/",

    # Exception: Bootstrap's CSS draws its small icons (menu, close button,
    # select arrow) as data: SVG images. Images cannot run script here.
    "img-src 'self' data:",

    "connect-src 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
)


# More proxies than this in front of one helpdesk is almost certainly a typo
MAX_TRUSTED_PROXY_COUNT = 5

# Ranges wider than these contain networks the operator does not control
MIN_TRUSTED_PROXY_PREFIX = {4: 8, 6: 32}


# Rate-limit storage. Counters in memory:// live inside one process, so
# several Gunicorn workers would each keep their own and the login limit
# would be multiplied by the number of workers. Production must therefore
# name a shared Redis server, or explicitly declare a single process.
MEMORY = "memory"
REDIS = "redis"
MEMORY_STORAGE = "memory://"
REDIS_SCHEMES = ("redis", "rediss", "redis+unix")
LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
SINGLE_PROCESS_VARIABLE = "RATELIMIT_SINGLE_PROCESS"


class ConfigurationError(RuntimeError):
    """The environment is unsafe, so the application must not start."""


@dataclass(frozen=True)
class SecuritySettings:
    """Validated security settings for this process."""

    environment: str

    # repr=False keeps the key out of logs if the settings are ever printed
    secret_key: str = field(repr=False)

    trusted_hosts: tuple

    # How many reverse proxies sit in front of the app, and their addresses.
    # The default of none means forwarded headers are never believed.
    trusted_proxy_count: int = 0
    trusted_proxy_networks: tuple = ()

    # Where login and password-change attempts are counted. May contain a
    # Redis password, so it is kept out of repr() like the secret key.
    rate_limit_storage_uri: str = field(default=MEMORY_STORAGE, repr=False)

    @property
    def is_production(self):
        return self.environment == PRODUCTION

    @property
    def rate_limit_storage_kind(self):
        """"memory" or "redis": safe to log, unlike the URI itself."""

        return MEMORY if self.rate_limit_storage_uri == MEMORY_STORAGE \
            else REDIS


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


def _parse_proxy_count(raw, problems):
    """Read TRUSTED_PROXY_COUNT, which defaults to no proxy."""

    text = raw.strip()

    if not text:
        return 0

    if not (text.isascii() and text.isdigit()) or (
        int(text) > MAX_TRUSTED_PROXY_COUNT
    ):
        problems.append(
            "TRUSTED_PROXY_COUNT must be a whole number from 0 to "
            f"{MAX_TRUSTED_PROXY_COUNT}."
        )
        return 0

    return int(text)


def _parse_proxy_networks(raw, problems):
    """Read TRUSTED_PROXY_IPS, a comma-separated list of IPs or CIDR ranges."""

    networks = []

    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue

        try:
            network = ipaddress.ip_network(item, strict=True)
        except ValueError:
            problems.append(
                "TRUSTED_PROXY_IPS must contain only IP addresses or "
                "CIDR ranges, for example 10.0.0.2 or 10.0.0.0/24."
            )
            return ()

        if (
            network.prefixlen < MIN_TRUSTED_PROXY_PREFIX[network.version]
            or (network.version == 6 and network.network_address.ipv4_mapped)
        ):
            problems.append(
                "TRUSTED_PROXY_IPS contains a range that is too broad or "
                "unsupported. List only your actual proxy addresses."
            )
            return ()

        networks.append(network)

    return tuple(dict.fromkeys(networks))


def _read_secret_key(environ, production):
    """Return (key, problem); a file that cannot be used is a problem."""

    try:
        return secret_files.read_secret(
            environ, "SECRET_KEY", require_private=production
        ), None
    except secret_files.SecretFileError as error:
        return "", str(error)


def _parse_rate_limit_storage(environ, production, problems):
    """
    Read RATELIMIT_STORAGE_URI (or RATELIMIT_STORAGE_URI_FILE).

    Accepted:
        redis+unix:///run/redis/redis-server.sock   recommended
        redis://[:password@]127.0.0.1:6379/0           this machine only
        rediss://[:password@]redis.internal:6380/0     TLS, any host
        memory://   development and testing; production only together
                    with RATELIMIT_SINGLE_PROCESS=1 and one worker

    There is never a silent fallback: in production a missing or
    unusable value stops the application from starting.
    """

    try:
        uri = secret_files.read_secret(
            environ, "RATELIMIT_STORAGE_URI", strip=True,
            require_private=production
        )
    except secret_files.SecretFileError as error:
        problems.append(str(error))
        return MEMORY_STORAGE

    single_process = environ.get(SINGLE_PROCESS_VARIABLE, "").strip()

    if single_process not in ("", "1"):
        problems.append(f"{SINGLE_PROCESS_VARIABLE} must be 1 or unset.")

    if not uri:
        if production:
            problems.append(
                "RATELIMIT_STORAGE_URI is required in production. Use a "
                "Redis server shared by every worker (for example "
                "redis+unix:///run/redis/redis-server.sock), or set "
                "RATELIMIT_STORAGE_URI=memory:// together with "
                f"{SINGLE_PROCESS_VARIABLE}=1 and run a single worker."
            )
        return MEMORY_STORAGE

    if uri == MEMORY_STORAGE:
        if production and single_process != "1":
            problems.append(
                "RATELIMIT_STORAGE_URI=memory:// keeps separate counters "
                "in each worker process, which multiplies the login limit. "
                f"Use Redis, or set {SINGLE_PROCESS_VARIABLE}=1 and run "
                "exactly one worker."
            )
        return MEMORY_STORAGE

    try:
        parts = urlsplit(uri)
        hostname = parts.hostname
        parts.port  # raises ValueError for a malformed port
    except ValueError:
        parts, hostname = None, None

    if parts is None or parts.scheme not in REDIS_SCHEMES:
        problems.append(
            "RATELIMIT_STORAGE_URI must start with redis+unix://, "
            "redis://, rediss:// or memory://."
        )
        return MEMORY_STORAGE

    if parts.scheme == "redis+unix":
        if not parts.path.startswith("/") or parts.netloc.split("@")[-1]:
            problems.append(
                "RATELIMIT_STORAGE_URI with redis+unix:// must name an "
                "absolute socket path, for example "
                "redis+unix:///run/redis/redis-server.sock"
            )
            return MEMORY_STORAGE

    elif not hostname:
        problems.append("RATELIMIT_STORAGE_URI must include a host name.")
        return MEMORY_STORAGE

    # Plain redis:// sends the password and the counters unencrypted
    elif production and parts.scheme == "redis" and (
        hostname.lower() not in LOOPBACK_HOSTNAMES
    ):
        problems.append(
            "RATELIMIT_STORAGE_URI may use redis:// only for a Redis "
            "server on this machine (127.0.0.1). Use redis+unix://, or "
            "rediss:// (TLS) for a separate Redis server."
        )
        return MEMORY_STORAGE

    if single_process:
        problems.append(
            f"{SINGLE_PROCESS_VARIABLE} applies only to "
            "RATELIMIT_STORAGE_URI=memory://; remove it when using Redis."
        )

    return uri


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

    # SECRET_KEY, or SECRET_KEY_FILE naming a file that holds it
    secret_key, secret_problem = _read_secret_key(environ, production)

    if secret_problem:
        problems.append(secret_problem)
    elif not secret_key:
        problems.append(
            "SECRET_KEY is not set. "
            + ("Supply it through the environment or SECRET_KEY_FILE."
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

    proxy_count = _parse_proxy_count(
        environ.get("TRUSTED_PROXY_COUNT", ""), problems
    )
    proxy_networks = _parse_proxy_networks(
        environ.get("TRUSTED_PROXY_IPS", ""), problems
    )

    # Trusting forwarded headers without knowing who sent them would let
    # any client forge its address, so the two settings must be used together
    if not problems_mention_proxy(problems):
        if proxy_count and not proxy_networks:
            problems.append(
                "TRUSTED_PROXY_IPS is required when TRUSTED_PROXY_COUNT "
                "is above 0."
            )
        elif proxy_networks and not proxy_count:
            problems.append(
                "TRUSTED_PROXY_COUNT must be above 0 when TRUSTED_PROXY_IPS "
                "is set."
            )

    rate_limit_storage_uri = _parse_rate_limit_storage(
        environ, production, problems
    )

    if problems:
        raise ConfigurationError(
            f"Unsafe {environment} configuration:\n  - "
            + "\n  - ".join(problems)
        )

    return SecuritySettings(
        environment=environment,
        secret_key=secret_key,
        trusted_hosts=trusted_hosts,
        trusted_proxy_count=proxy_count,
        trusted_proxy_networks=proxy_networks,
        rate_limit_storage_uri=rate_limit_storage_uri,
    )


def problems_mention_hosts(problems):
    """Avoid reporting the same TRUSTED_HOSTS mistake twice."""

    return any(problem.startswith("TRUSTED_HOSTS") for problem in problems)


def problems_mention_proxy(problems):
    """Avoid a second, misleading error about the proxy settings."""

    return any(problem.startswith("TRUSTED_PROXY") for problem in problems)


def flask_config(settings):
    """Translate validated settings into Flask configuration values."""

    production = settings.is_production

    config = {
        "TRUSTED_HOSTS": list(settings.trusted_hosts),

        # JavaScript cannot read the session cookie
        "SESSION_COOKIE_HTTPONLY": True,

        # Sent on normal link clicks but not on cross-site form posts.
        # "Strict" would make links to a ticket from email look signed out.
        "SESSION_COOKIE_SAMESITE": "Lax",

        # Browsers will not send a Secure cookie over plain HTTP, so this
        # must stay off for http://127.0.0.1:5000
        "SESSION_COOKIE_SECURE": production,

        # The __Host- prefix makes browsers refuse this cookie unless it is
        # Secure, host-only and for path /, so a sibling subdomain cannot
        # plant a session. It requires HTTPS, so production only.
        "SESSION_COOKIE_NAME": (
            PRODUCTION_SESSION_COOKIE_NAME if production else "session"
        ),

        # CSRF stays on in every environment, and HTTPS requests must also
        # carry a same-site Referer header
        "WTF_CSRF_ENABLED": True,
        "WTF_CSRF_SSL_STRICT": True,

        "PREFERRED_URL_SCHEME": "https" if production else "http",

        # Oversized requests are refused with 413 before they are parsed
        "MAX_CONTENT_LENGTH": validation.MAX_REQUEST_BODY_BYTES,
        "MAX_FORM_MEMORY_SIZE": validation.MAX_REQUEST_BODY_BYTES,
        "MAX_FORM_PARTS": validation.MAX_FORM_PARTS,
    }

    if production:
        config["DEBUG"] = False

    return config


def content_security_policy(settings):
    """Build the Content-Security-Policy header value."""

    directives = list(CSP_DIRECTIVES)

    if settings.is_production:
        directives.append("upgrade-insecure-requests")

    return "; ".join(directives)


def apply_security_headers(response, settings, *, is_secure, is_static):
    """Add the security headers to one response."""

    headers = response.headers

    headers["Content-Security-Policy"] = content_security_policy(settings)
    headers["X-Content-Type-Options"] = "nosniff"
    headers["X-Frame-Options"] = "DENY"

    # Not "no-referrer": Flask-WTF requires a same-origin Referer on HTTPS
    # form posts. This sends it to this site and to nobody else.
    headers["Referrer-Policy"] = "same-origin"

    headers["Permissions-Policy"] = (
        "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
    )
    headers["Cross-Origin-Opener-Policy"] = "same-origin"
    headers["Cross-Origin-Resource-Policy"] = "same-origin"

    # Pages can contain tickets or CSRF tokens. Public static files keep
    # Flask's normal caching.
    if not is_static:
        headers["Cache-Control"] = "no-store"

    # Browsers remember HSTS, so it is sent only by production over HTTPS
    if settings.is_production and is_secure:
        headers["Strict-Transport-Security"] = (
            f"max-age={HSTS_MAX_AGE_SECONDS}"
        )

    return response
