"""
The delivery worker behind "flask --app app notifications-send".

One run (run_once) delivers up to `limit` due notifications, one at a
time:

    claim (short write transaction, committed)
      -> check the recipient again (read only)
      -> build the message
      -> deliver it, with no database transaction open
      -> record the outcome (short write transaction, claim token checked)

The run stops early when the transport says the next message would fail
the same way (server unreachable, sign-in refused), so an outage does not
use up the attempts of every queued message.

run_forever() repeats runs until asked to stop, waiting between them,
and waits longer (doubling up to 15 minutes) while the server is
unavailable. It is meant to run under a process supervisor such as
systemd. A stop request is honoured between messages, so a message is
never abandoned half-way through on purpose; a hard kill is covered by
lease recovery in queue.py.
"""

import logging
import os
import socket
import threading
from dataclasses import dataclass, field

from . import events, queue, render, transport as transport_module

logger = logging.getLogger(__name__)

MAX_IDLE_BACKOFF_SECONDS = 900


def default_worker_id():
    """host:pid, so attempt history shows which process did what."""

    return f"{socket.gethostname()[:64]}:{os.getpid()}"


@dataclass
class RunSummary:
    claimed: int = 0
    outcomes: dict = field(default_factory=dict)
    recovered: int = 0
    expired: int = 0
    lost_claims: int = 0
    stopped_reason: str = ""

    def count(self, status):
        self.outcomes[status] = self.outcomes.get(status, 0) + 1


class NotificationWorker:

    def __init__(self, settings, transport, *, db_path, worker_id=None):
        self.settings = settings
        self.transport = transport
        self.db_path = db_path
        self.worker_id = worker_id or default_worker_id()

    def _deliver(self, claim):
        """Return (outcome, safe_error, stop_reason)."""

        row = claim.row

        address, problem = queue.recipient_problem(
            self.db_path, row, events.STAFF_ROLES, events.STAFF_REASONS
        )

        if problem:
            return queue.CANCELLED, problem, ""

        try:
            message = render.build_message(
                row, to_address=address, settings=self.settings,
                simulated=self.transport.simulated
            )
        except (ValueError, KeyError, TypeError) as error:
            # A row this code cannot turn into a message will never work
            return (queue.FAILED,
                    f"render: {type(error).__name__}", "")

        try:
            self.transport.send(message, row["id"])

        except transport_module.DeliveryError as error:
            outcome = {
                transport_module.DEFERRED: "deferred",
                transport_module.RETRY: "retry",
                transport_module.PERMANENT: queue.FAILED,
            }[error.kind]
            return (outcome, error.safe_error,
                    error.safe_error if error.stop_run else "")

        return (queue.SIMULATED if self.transport.simulated else queue.SENT,
                None, "")

    def run_once(self, limit=100, should_stop=None):
        summary = RunSummary()

        try:
            while summary.claimed < limit:
                if should_stop is not None and should_stop():
                    summary.stopped_reason = "stop requested"
                    break

                claim, recovered, expired = queue.claim_next(
                    self.db_path, self.settings, worker=self.worker_id,
                    mode=self.transport.mode
                )
                summary.recovered += recovered
                summary.expired += expired

                if claim is None:
                    break

                summary.claimed += 1

                try:
                    outcome, error, stop_reason = self._deliver(claim)
                except Exception:
                    # Unexpected: record a safe retry and keep the details
                    # in the log only. The lease would also recover it.
                    logger.exception("Notification %s: unexpected error",
                                     claim.id)
                    outcome, error, stop_reason = (
                        "retry", "worker: unexpected error",
                        "unexpected error"
                    )

                status = queue.finish(
                    self.db_path, claim, self.settings,
                    worker=self.worker_id, outcome=outcome, error=error
                )

                if status is None:
                    summary.lost_claims += 1
                    logger.warning(
                        "Notification %s: claim expired before the result "
                        "was recorded; another worker recovered it",
                        claim.id
                    )
                else:
                    summary.count(status)
                    logger.info("Notification %s (%s): %s%s", claim.id,
                                claim.row["event_type"], status,
                                f" ({error})" if error else "")

                if stop_reason:
                    summary.stopped_reason = stop_reason
                    break

        finally:
            self.transport.close()

        return summary

    def run_forever(self, *, interval, limit=100, stop_event=None,
                    on_summary=None):
        """Repeat run_once() until stop_event is set."""

        stop_event = stop_event or threading.Event()
        idle_wait = interval

        while not stop_event.is_set():
            summary = self.run_once(limit, should_stop=stop_event.is_set)

            if on_summary is not None:
                on_summary(summary)

            if summary.stopped_reason and not stop_event.is_set():
                # Server unavailable: wait longer each time, up to a cap
                idle_wait = min(max(idle_wait * 2, interval),
                                MAX_IDLE_BACKOFF_SECONDS)
                wait = idle_wait
            elif summary.claimed >= limit:
                idle_wait = interval
                wait = 0  # more may be waiting
            else:
                idle_wait = interval
                wait = interval

            stop_event.wait(wait)
