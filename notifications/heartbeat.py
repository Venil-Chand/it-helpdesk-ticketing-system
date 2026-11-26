"""
A small status file the notification worker rewrites while it runs.

    <HELPDESK_STATE_DIR>/notification-worker.json

    {"time": "...", "pid": 1234, "worker": "host:1234",
     "state": "running" | "paused" | "stopped",
     "paused_reason": "...", "last_run": {"claimed": 3, ...}}

"flask ops-check" reads it: a file older than the allowed age means the
worker has died or is stuck, even if systemd still shows it as active.
It holds counts and the transport's sanitised error text only, never
addresses or message content. Writes are atomic (temporary file, then
rename) and throttled, so the worker does not hammer the disk.
"""

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

FILE_NAME = "notification-worker.json"

STATE_DIR_VARIABLE = "HELPDESK_STATE_DIR"
DEFAULT_STATE_DIR = Path(__file__).resolve().parent.parent / "instance" \
    / "state"

RUNNING = "running"
PAUSED = "paused"
STOPPED = "stopped"


def state_dir(environ=None):
    environ = os.environ if environ is None else environ
    configured = environ.get(STATE_DIR_VARIABLE, "").strip()
    return Path(configured) if configured else DEFAULT_STATE_DIR


def heartbeat_path(environ=None):
    return state_dir(environ) / FILE_NAME


def write_status(path, status):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    descriptor, temporary = tempfile.mkstemp(prefix=".heartbeat-",
                                             dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(status, handle, sort_keys=True)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def read_status(path):
    """The last status written, or None if missing or unreadable."""

    try:
        with open(path, "rb") as handle:
            status = json.loads(handle.read(64 * 1024))
    except (OSError, ValueError):
        return None

    return status if isinstance(status, dict) else None


def status_age_seconds(status, now=None):
    try:
        written = datetime.fromisoformat(status["time"])
    except (KeyError, TypeError, ValueError):
        return None

    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - written).total_seconds())


class Heartbeat:
    """Callable passed to NotificationWorker.run_forever(heartbeat=...)."""

    def __init__(self, path, worker_id, *, min_interval=15.0,
                 clock=time.monotonic):
        self.path = Path(path)
        self.worker_id = worker_id
        self.min_interval = min_interval
        self.clock = clock
        self._last_write = None
        self._last_run = {}
        self._state = RUNNING
        self._paused_reason = ""

    def __call__(self, summary=None):
        if summary is not None:
            self._last_run = {
                "claimed": summary.claimed,
                "outcomes": dict(summary.outcomes),
                "recovered": summary.recovered,
                "expired": summary.expired,
                "lost_claims": summary.lost_claims,
            }
            paused = bool(summary.stopped_reason) and (
                summary.stopped_reason != "stop requested")
            self._state = PAUSED if paused else RUNNING
            self._paused_reason = summary.stopped_reason if paused else ""
            # A finished run is always recorded
            self._write()
            return

        now = self.clock()
        if self._last_write is None or (
            now - self._last_write >= self.min_interval
        ):
            self._write()

    def stopped(self):
        self._state = STOPPED
        self._paused_reason = ""
        self._write()

    def _write(self):
        self._last_write = self.clock()
        status = {
            "time": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "pid": os.getpid(),
            "worker": self.worker_id,
            "state": self._state,
            "last_run": self._last_run,
        }
        if self._paused_reason:
            status["paused_reason"] = self._paused_reason[:200]

        try:
            write_status(self.path, status)
        except OSError:
            # Monitoring will report the stale file; delivery carries on
            pass
