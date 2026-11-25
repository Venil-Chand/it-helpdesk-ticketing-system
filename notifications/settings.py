"""
Notification delivery settings, read from environment variables.

Only the worker ("flask notifications-send") reads these; the web
application never sends email, so it needs none of them.

    NOTIFICATION_DELIVERY      local | smtp
                               Default "local" in development and testing.
                               Production has no default: it must be set.
    NOTIFICATION_SENDER        From address. Required for smtp.
    NOTIFICATION_BASE_URL      Optional https://host used for ticket links
                               (http:// is accepted outside production).
    NOTIFICATION_SINK_DIR      local mode only: where simulated messages are
                               written. Default <project>/instance/
                               notification-sink (ignored by Git).

    NOTIFICATION_SMTP_HOST     smtp mode only, required
    NOTIFICATION_SMTP_PORT     default 587 (starttls), 465 (ssl), 25 (none)
    NOTIFICATION_SMTP_SECURITY starttls (default) | ssl | none
                               none is accepted only for a relay on this
                               machine (localhost/127.0.0.1/::1) and never
                               with credentials.
    NOTIFICATION_SMTP_USERNAME optional; requires the password
    NOTIFICATION_SMTP_PASSWORD optional; only ever from the environment
    NOTIFICATION_SMTP_TIMEOUT  seconds for connecting and each SMTP
                               command, 1-120, default 20

    NOTIFICATION_MAX_ATTEMPTS        deliveries that may have reached the
                                     server before giving up, 1-10,
                                     default 5
    NOTIFICATION_RETRY_BASE_SECONDS  first retry delay, default 60
    NOTIFICATION_RETRY_MAX_SECONDS   longest retry delay, default 3600
    NOTIFICATION_MAX_AGE_HOURS       give up on anything older, default 24
    NOTIFICATION_LEASE_SECONDS       how long a claimed message is
                               reserved for one worker, default 300; must be
                               more than 4 x the SMTP timeout

Problems are reported by variable name only. No value, and in particular
no password, is ever placed in an error message or in repr().
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import validation

LOCAL = "local"
SMTP = "smtp"
MODES = (LOCAL, SMTP)

STARTTLS = "starttls"
SSL = "ssl"
NO_TLS = "none"
SECURITY_MODES = (STARTTLS, SSL, NO_TLS)

DEFAULT_PORTS = {STARTTLS: 587, SSL: 465, NO_TLS: 25}

# Plain-text SMTP is only acceptable to a relay on this same machine
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

DEFAULT_LOCAL_SENDER = "helpdesk@localhost.invalid"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SINK_DIR = PROJECT_ROOT / "instance" / "notification-sink"

_HOSTNAME = re.compile(
    r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*",
    re.ASCII
)

_BASE_URL = re.compile(
    r"(https?)://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?",
    re.ASCII
)


class NotificationConfigError(RuntimeError):
    """The delivery settings are missing or unsafe. Names variables only."""


@dataclass(frozen=True)
class DeliverySettings:
    mode: str
    sender: str
    base_url: str = ""
    sink_dir: Path = DEFAULT_SINK_DIR

    smtp_host: str = ""
    smtp_port: int = 0
    smtp_security: str = STARTTLS
    smtp_username: str = ""
    # repr=False keeps the password out of logs and tracebacks
    smtp_password: str = field(default="", repr=False)
    smtp_timeout: float = 20.0

    max_attempts: int = 5
    retry_base_seconds: int = 60
    retry_max_seconds: int = 3600
    max_age_hours: int = 24
    lease_seconds: int = 300

    @property
    def is_simulated(self):
        return self.mode == LOCAL

    def describe(self):
        """Lines safe to print: no password, and only whether one is set."""

        lines = [f"Delivery mode: {self.mode}"
                 + (" (SIMULATED - messages are written to a local folder,"
                    " never sent)" if self.is_simulated else "")]
        lines.append(f"Sender: {self.sender}")
        lines.append(f"Ticket links: {self.base_url or 'not included'}")

        if self.is_simulated:
            lines.append(f"Local sink folder: {self.sink_dir}")
        else:
            lines.append(f"SMTP server: {self.smtp_host}:{self.smtp_port} "
                         f"({self.smtp_security})")
            lines.append("SMTP sign-in: "
                         + ("yes (username and password set)"
                            if self.smtp_username else "no"))
            lines.append(f"SMTP timeout: {self.smtp_timeout:g} s")

        lines.append(
            f"Retries: up to {self.max_attempts} attempts, "
            f"{self.retry_base_seconds}s doubling to "
            f"{self.retry_max_seconds}s; give up after "
            f"{self.max_age_hours}h"
        )
        return lines


def _text(environ, name, default=""):
    return environ.get(name, default).strip()


def _integer(environ, name, default, low, high):
    raw = _text(environ, name)

    if not raw:
        return default

    if not raw.isascii() or not raw.isdigit():
        raise NotificationConfigError(f"{name} must be a whole number")

    value = int(raw)

    if not low <= value <= high:
        raise NotificationConfigError(
            f"{name} must be between {low} and {high}"
        )

    return value


def load_delivery_settings(environ=None, *, production=False):
    """
    Validate the delivery settings. Raises NotificationConfigError.

    `production` comes from the application's own security settings
    (APP_ENV), so this module never decides that by itself.
    """

    if environ is None:
        environ = os.environ

    mode = _text(environ, "NOTIFICATION_DELIVERY").lower()

    if not mode:
        if production:
            raise NotificationConfigError(
                "NOTIFICATION_DELIVERY must be set in production "
                "(smtp, or local for a simulated dry run)"
            )
        mode = LOCAL

    if mode not in MODES:
        raise NotificationConfigError(
            "NOTIFICATION_DELIVERY must be 'local' or 'smtp'"
        )

    sender = _text(environ, "NOTIFICATION_SENDER")

    if not sender:
        if mode == SMTP:
            raise NotificationConfigError(
                "NOTIFICATION_SENDER is required for smtp delivery"
            )
        sender = DEFAULT_LOCAL_SENDER

    if validation.email_problem(sender):
        raise NotificationConfigError(
            "NOTIFICATION_SENDER must be a plain email address"
        )

    base_url = _text(environ, "NOTIFICATION_BASE_URL").rstrip("/")

    if base_url:
        match = _BASE_URL.fullmatch(base_url)
        if not match:
            raise NotificationConfigError(
                "NOTIFICATION_BASE_URL must look like https://host[/path]"
            )
        if production and match.group(1) != "https":
            raise NotificationConfigError(
                "NOTIFICATION_BASE_URL must use https in production"
            )

    timeout = _integer(environ, "NOTIFICATION_SMTP_TIMEOUT", 20, 1, 120)

    retry_base = _integer(environ, "NOTIFICATION_RETRY_BASE_SECONDS",
                          60, 1, 86400)
    retry_max = _integer(environ, "NOTIFICATION_RETRY_MAX_SECONDS",
                         3600, 1, 86400)

    if retry_max < retry_base:
        raise NotificationConfigError(
            "NOTIFICATION_RETRY_MAX_SECONDS must not be less than "
            "NOTIFICATION_RETRY_BASE_SECONDS"
        )

    lease = _integer(environ, "NOTIFICATION_LEASE_SECONDS", 300, 30, 3600)

    # One delivery makes several SMTP round trips, each up to `timeout`.
    # The lease must outlast them, or a slow but healthy send could be
    # taken over by another worker and sent twice.
    if lease <= 4 * timeout:
        raise NotificationConfigError(
            "NOTIFICATION_LEASE_SECONDS must be more than 4 x "
            "NOTIFICATION_SMTP_TIMEOUT"
        )

    common = dict(
        sender=sender,
        base_url=base_url,
        smtp_timeout=float(timeout),
        max_attempts=_integer(environ, "NOTIFICATION_MAX_ATTEMPTS",
                              5, 1, 10),
        retry_base_seconds=retry_base,
        retry_max_seconds=retry_max,
        max_age_hours=_integer(environ, "NOTIFICATION_MAX_AGE_HOURS",
                               24, 1, 24 * 14),
        lease_seconds=lease,
    )

    if mode == LOCAL:
        sink = _text(environ, "NOTIFICATION_SINK_DIR")
        return DeliverySettings(
            mode=LOCAL,
            sink_dir=Path(sink) if sink else DEFAULT_SINK_DIR,
            **common
        )

    host = _text(environ, "NOTIFICATION_SMTP_HOST")

    if not host or not (_HOSTNAME.fullmatch(host) or host == "::1"):
        raise NotificationConfigError(
            "NOTIFICATION_SMTP_HOST must be a hostname or IP address"
        )

    security = _text(environ, "NOTIFICATION_SMTP_SECURITY",
                     STARTTLS).lower()

    if security not in SECURITY_MODES:
        raise NotificationConfigError(
            "NOTIFICATION_SMTP_SECURITY must be starttls, ssl or none"
        )

    username = _text(environ, "NOTIFICATION_SMTP_USERNAME")
    # Not stripped: spaces may be part of a password
    password = environ.get("NOTIFICATION_SMTP_PASSWORD", "")

    if bool(username) != bool(password):
        raise NotificationConfigError(
            "NOTIFICATION_SMTP_USERNAME and NOTIFICATION_SMTP_PASSWORD "
            "must be set together"
        )

    if security == NO_TLS:
        if username:
            raise NotificationConfigError(
                "NOTIFICATION_SMTP_SECURITY=none cannot be used with SMTP "
                "credentials; they would be sent unencrypted"
            )
        if host.lower() not in LOOPBACK_HOSTS:
            raise NotificationConfigError(
                "NOTIFICATION_SMTP_SECURITY=none is only allowed for a "
                "relay on this machine (NOTIFICATION_SMTP_HOST=localhost)"
            )

    port = _integer(environ, "NOTIFICATION_SMTP_PORT",
                    DEFAULT_PORTS[security], 1, 65535)

    return DeliverySettings(
        mode=SMTP,
        smtp_host=host,
        smtp_port=port,
        smtp_security=security,
        smtp_username=username,
        smtp_password=password,
        **common
    )
