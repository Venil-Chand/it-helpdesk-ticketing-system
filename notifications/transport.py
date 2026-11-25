"""
Delivery transports: a local sink for development, and SMTP.

Both have the same interface:

    transport.send(message, notification_id)   deliver one message
    transport.close()                          end the run

and report problems only as DeliveryError, whose `kind` tells the worker
what to do and whose `safe_error` is the only text stored about it:

    DEFERRED   nothing can have been sent (no connection, TLS or sign-in
               failed). The attempt is not counted; the message waits.
    RETRY      the message may or may not have reached the server, or the
               server asked to try later (4xx). Counted as an attempt.
    PERMANENT  the server refused this message for good (5xx), or it
               cannot be built. Not retried.

`stop_run` asks the worker to stop this run, because the next message
would fail the same way (server unreachable, bad credentials).

safe_error never contains the server's reply text (which can quote
addresses), the message, or any credential: only the exception class and
the numeric SMTP code.
"""

import os
import smtplib
import ssl
from pathlib import Path

from . import settings as settings_module

DEFERRED = "deferred"
RETRY = "retry"
PERMANENT = "permanent"


class DeliveryError(Exception):

    def __init__(self, kind, safe_error, *, stop_run=False):
        super().__init__(safe_error)
        self.kind = kind
        self.safe_error = safe_error[:200]
        self.stop_run = stop_run


def _describe(error, stage):
    """'stage: ClassName [code]' with no server text."""

    code = getattr(error, "smtp_code", None)

    if isinstance(error, smtplib.SMTPRecipientsRefused):
        codes = [value[0] for value in error.recipients.values()]
        code = codes[0] if codes else None

    text = f"{stage}: {type(error).__name__}"

    if isinstance(code, int):
        text += f" {code}"

    return text


# ---------------------------------------------------
# LOCAL SINK (simulated delivery)
# ---------------------------------------------------

class LocalSinkTransport:
    """
    Write each message to <sink_dir>/<id>.eml instead of sending it.

    The folder is created readable only by the current user (0700) and
    each file 0600, because messages name real accounts. The default
    folder, instance/, is ignored by Git. Every file carries the header
    X-Helpdesk-Delivery: simulated, and the subject starts [SIMULATED].

    Writing is idempotent: if <id>.eml already exists (a worker stopped
    after writing it but before recording the result) it is kept and
    counted as delivered, so a retry never writes a second copy.
    """

    mode = settings_module.LOCAL
    simulated = True

    def __init__(self, sink_dir):
        self.sink_dir = Path(sink_dir)

    def _prepare(self):
        self.sink_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

        if self.sink_dir.is_symlink() or not self.sink_dir.is_dir():
            raise DeliveryError(
                DEFERRED, "local sink: folder unavailable", stop_run=True
            )

        # mkdir's mode is reduced by the umask and ignored for existing
        # folders; make sure other accounts cannot read the messages
        os.chmod(self.sink_dir, 0o700)

    def send(self, message, notification_id):
        try:
            self._prepare()
            path = self.sink_dir / f"{int(notification_id):08d}.eml"

            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)

            try:
                fd = os.open(path, flags, 0o600)
            except FileExistsError:
                return path

            with os.fdopen(fd, "wb") as handle:
                handle.write(message.as_bytes())

            return path

        except DeliveryError:
            raise

        except OSError as error:
            raise DeliveryError(
                DEFERRED, f"local sink: {type(error).__name__}",
                stop_run=True
            ) from None

    def close(self):
        pass


# ---------------------------------------------------
# SMTP
# ---------------------------------------------------

# Exceptions that mean the connection, TLS or greeting failed: nothing
# was handed to the server
_CONNECT_ERRORS = (
    OSError,                 # includes refused, timeouts, DNS, ssl.SSLError
    smtplib.SMTPException,
)


class SmtpTransport:
    """
    Deliver through an SMTP server, reusing one connection per run.

    starttls  plain connection, then STARTTLS with certificate and
              hostname verification; never falls back to plain text
    ssl       TLS from the first byte (usually port 465)
    none      only to a relay on this machine, never with credentials

    Every network operation is bounded by settings.smtp_timeout.
    """

    mode = settings_module.SMTP
    simulated = False

    def __init__(self, settings, *, smtp_class=None, smtp_ssl_class=None,
                 ssl_context=None):
        self.settings = settings
        self._smtp_class = smtp_class or smtplib.SMTP
        self._smtp_ssl_class = smtp_ssl_class or smtplib.SMTP_SSL
        self._context = ssl_context or ssl.create_default_context()
        self._connection = None

    def _connect(self):
        s = self.settings

        try:
            if s.smtp_security == settings_module.SSL:
                connection = self._smtp_ssl_class(
                    s.smtp_host, s.smtp_port, timeout=s.smtp_timeout,
                    context=self._context
                )
            else:
                connection = self._smtp_class(
                    s.smtp_host, s.smtp_port, timeout=s.smtp_timeout
                )

        except _CONNECT_ERRORS as error:
            raise DeliveryError(
                DEFERRED, _describe(error, "connect"), stop_run=True
            ) from None

        try:
            connection.ehlo()

            if s.smtp_security == settings_module.STARTTLS:
                # Raises SMTPNotSupportedError if the server does not
                # offer it; there is deliberately no plain-text fallback
                connection.starttls(context=self._context)
                connection.ehlo()

            if s.smtp_username:
                connection.login(s.smtp_username, s.smtp_password)

        except smtplib.SMTPAuthenticationError as error:
            self._discard(connection)
            raise DeliveryError(
                DEFERRED, _describe(error, "sign-in"), stop_run=True
            ) from None

        except _CONNECT_ERRORS as error:
            self._discard(connection)
            raise DeliveryError(
                DEFERRED, _describe(error, "setup"), stop_run=True
            ) from None

        return connection

    @staticmethod
    def _discard(connection):
        try:
            connection.close()
        except Exception:  # closing a broken connection may fail too
            pass

    def send(self, message, notification_id):
        if self._connection is None:
            self._connection = self._connect()

        try:
            refused = self._connection.send_message(message)

        except smtplib.SMTPRecipientsRefused as error:
            raise _by_code(error, "recipient") from None

        except smtplib.SMTPSenderRefused as error:
            # Affects every message: a configuration problem
            self._drop()
            raise DeliveryError(
                RETRY, _describe(error, "sender"), stop_run=True
            ) from None

        except smtplib.SMTPDataError as error:
            raise _by_code(error, "data") from None

        except smtplib.SMTPResponseException as error:
            raise _by_code(error, "send") from None

        # SMTPException is a subclass of OSError, so the specific SMTP
        # errors above must come first
        except (smtplib.SMTPServerDisconnected, OSError) as error:
            # The connection broke during the transaction: the server may
            # already have accepted the message
            self._drop()
            raise DeliveryError(
                RETRY, _describe(error, "send"), stop_run=True
            ) from None

        if refused:
            # One recipient per message, so this is that recipient
            code = next(iter(refused.values()))[0]
            kind = PERMANENT if 500 <= code < 600 else RETRY
            raise DeliveryError(kind, f"recipient: refused {code}")

        return None

    def _drop(self):
        if self._connection is not None:
            self._discard(self._connection)
            self._connection = None

    def close(self):
        if self._connection is not None:
            try:
                self._connection.quit()
            except Exception:
                self._discard(self._connection)
            self._connection = None


def _by_code(error, stage):
    """5xx is permanent, anything else may succeed later."""

    if isinstance(error, smtplib.SMTPRecipientsRefused):
        codes = [value[0] for value in error.recipients.values()]
        code = codes[0] if codes else 0
    else:
        code = getattr(error, "smtp_code", 0) or 0

    kind = PERMANENT if 500 <= code < 600 else RETRY
    return DeliveryError(kind, _describe(error, stage))


def make_transport(settings):
    """The transport the settings ask for."""

    if settings.mode == settings_module.LOCAL:
        return LocalSinkTransport(settings.sink_dir)

    return SmtpTransport(settings)
