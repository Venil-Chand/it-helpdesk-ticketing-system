"""
Reliability rehearsal: what happens when parts of the service fail.

  * Redis outage while Gunicorn runs, and automatic recovery
  * a Gunicorn worker killed with SIGKILL
  * an application restart (SIGTERM), with sessions surviving it
  * the notification worker killed in the middle of a delivery
  * the notification worker stopped with SIGTERM during a delivery
  * delivery paused (exit 2) when the mail server cannot be reached
  * SQLite under concurrent writers

The notification tests use HoldingSmtpServer, a loopback SMTP server
that holds one message open at the end of DATA until the rehearsal has
acted. That makes "killed mid-delivery" deterministic instead of a race.
No email can leave the machine: it is the only SMTP server configured.
"""

import json
import os
import re
import signal
import socketserver
import subprocess
import sys
import threading
import time

from .common import (Client, DisposableRedis, Gunicorn, RehearsalUnavailable,
                     Report, Workspace, free_port, gunicorn_available,
                     new_password, query, wait_for)

WRITERS = 8
ITERATIONS = 40
# The shortest lease the settings allow; the recovery test waits it out
LEASE_SECONDS = 30


def run(keep=False):
    ws = Workspace("rehearsal-reliability", keep=keep)
    report = Report("Reliability rehearsal", ws)
    redis = None
    try:
        if not gunicorn_available():
            raise RehearsalUnavailable("gunicorn is not installed")
        redis = DisposableRedis(ws).start()
        password = new_password()
        env = ws.production_env(redis.url())
        result = ws.flask("init-db", env=env)
        if report.check("init-db migrates the disposable database",
                        result.returncode == 0, result.stdout + result.stderr):
            ws.seed(env, tickets=3, password=password)
            _web(ws, report, redis, env, password)
            _notification_kill(ws, report)
            _notification_sigterm(ws, report)
            _delivery_paused(ws, report)
            _concurrency(ws, report)
    except RehearsalUnavailable as error:
        report.check("prerequisites available", False, str(error))
    except Exception as error:
        report.check("rehearsal finished without an unexpected error", False,
                     f"{type(error).__name__}: {error}")
    finally:
        if redis:
            redis.stop()
        ws.close()
    return report.finish()


# ---------------------------------------------------------------------
# Web: Redis outage, worker crash, restart
# ---------------------------------------------------------------------

def _web(ws, report, redis, env, password):
    check = report.check
    server = Gunicorn(ws, env, workers=2, log_name="web.log")
    try:
        if not check("Gunicorn starts", server.wait_ready(), server.log()):
            return
        master = server.process.pid
        client = Client(server.port, client_ip="203.0.113.21")
        check("sign-in works before the outage",
              client.login("user1", password).status == 302)

        # --- Redis outage ---------------------------------------------
        redis.stop()
        attempt = Client(server.port, client_ip="203.0.113.22")
        during = attempt.login("user2", password)
        check("during a Redis outage sign-in fails closed: 503 with "
              "Retry-After",
              during.status == 503 and during.header("Retry-After"),
              f"{during.status} {during.headers}")
        ready = attempt.get("/readyz").status
        health = attempt.get("/healthz").status
        check("during the outage /readyz is 503 and /healthz is 200",
              ready == 503 and health == 200, f"{ready} {health}")
        log = server.log()
        check("the log names the Redis problem without its password",
              re.search(r"rate.limit|redis", log, re.I) is not None
              and redis.password not in log)

        redis.start()
        recovered = wait_for(
            lambda: Client(server.port, client_ip="203.0.113.23")
            .login("user3", password).status == 302, timeout=20, interval=1)
        check("after Redis returns, sign-in works again without a restart",
              recovered and server.process.pid == master
              and server.process.poll() is None)

        # --- a worker killed -------------------------------------------
        before = server.worker_pids()
        victim = before[0]
        os.kill(victim, signal.SIGKILL)
        statuses = [client.get("/").status for _ in range(20)]
        replaced = wait_for(lambda: len(server.worker_pids()) == 2
                            and victim not in server.worker_pids(), 20)
        check("a worker killed with SIGKILL is replaced and requests keep "
              "working",
              replaced and set(statuses) == {200}
              and server.process.poll() is None,
              f"workers {before} -> {server.worker_pids()}, "
              f"statuses {sorted(set(statuses))}")
    finally:
        code = server.stop()
    check("SIGTERM stops Gunicorn cleanly (exit 0)", code == 0, str(code))

    # --- restart ----------------------------------------------------------
    restarted = Gunicorn(ws, env, workers=2, port=server.port,
                         log_name="web-restarted.log")
    try:
        check("the restarted server becomes ready", restarted.wait_ready(),
              restarted.log())
        page = client.get("/")
        check("a session from before the restart is still signed in",
              page.status == 200 and "user1" in page.text,
              str(page.status))
    finally:
        restarted.stop()


# ---------------------------------------------------------------------
# Notification worker
# ---------------------------------------------------------------------

def smtp_env(port):
    """Worker settings for a loopback SMTP server on `port`."""

    return {
        "NOTIFICATION_DELIVERY": "smtp",
        "NOTIFICATION_SENDER": "helpdesk@example.invalid",
        "NOTIFICATION_SMTP_HOST": "127.0.0.1",
        "NOTIFICATION_SMTP_PORT": str(port),
        "NOTIFICATION_SMTP_SECURITY": "none",
        "NOTIFICATION_SMTP_TIMEOUT": "5",
        "NOTIFICATION_LEASE_SECONDS": str(LEASE_SECONDS),
        "NOTIFICATION_RETRY_BASE_SECONDS": "1",
        "NOTIFICATION_RETRY_MAX_SECONDS": "2",
    }


class HoldingSmtpServer:
    """
    Loopback SMTP that accepts everything and keeps nothing but each
    message's Message-ID. With hold_seconds set, the next message is held
    at the end of DATA (before the "250") for that long; `held` is set
    when that happens.
    """

    def __init__(self):
        self.message_ids = []
        self.hold_seconds = 0
        self.held = threading.Event()
        self.lock = threading.Lock()
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                def reply(line):
                    try:
                        self.wfile.write(line.encode() + b"\r\n")
                    except OSError:
                        pass

                reply("220 rehearsal-smtp ready")
                in_data, message_id = False, None
                for raw in self.rfile:
                    line = raw.decode(errors="replace").rstrip("\r\n")
                    if in_data:
                        if line.lower().startswith("message-id:"):
                            message_id = line.split(":", 1)[1].strip()
                        if line == ".":
                            in_data = False
                            with outer.lock:
                                outer.message_ids.append(message_id)
                                hold = outer.hold_seconds
                                outer.hold_seconds = 0
                            if hold:
                                outer.held.set()
                                time.sleep(hold)
                            reply("250 accepted")
                        continue
                    verb = line[:4].upper()
                    if verb in ("EHLO", "HELO"):
                        reply("250 rehearsal-smtp")
                    elif verb == "DATA":
                        in_data, message_id = True, None
                        reply("354 go ahead")
                    elif verb == "QUIT":
                        reply("221 bye")
                        return
                    else:
                        reply("250 ok")

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        socketserver.ThreadingTCPServer.daemon_threads = True
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0),
                                                      Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()

    def env(self):
        return smtp_env(self.port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _notification_db(ws, name):
    """A fresh seeded database just for one notification scenario."""

    db_path = ws.data / f"{name}.db"
    env = ws.env(HELPDESK_DATABASE_PATH=db_path)
    ws.flask("init-db", env=env)
    ws.seed(env, tickets=2, password=new_password())
    return db_path, env


def _statuses(db_path):
    return dict(query(db_path, "SELECT status, COUNT(*) FROM notifications "
                               "GROUP BY status"))


def _notification_kill(ws, report):
    check = report.check
    db_path, env = _notification_db(ws, "notify-kill")
    smtp = HoldingSmtpServer()
    try:
        env = {**env, **smtp.env()}
        total = query(db_path, "SELECT COUNT(*) FROM notifications")[0][0]
        smtp.hold_seconds = 60
        worker, _ = ws.popen(
            [sys.executable, "-m", "flask", "--app", "app",
             "notifications-send", "--loop", "--interval", "1"],
            env, "notify-kill-worker.log")
        if not check("the worker reaches the middle of a delivery",
                     smtp.held.wait(60)):
            worker.kill()
            return
        worker.send_signal(signal.SIGKILL)
        worker.wait(10)
        held_id = smtp.message_ids[-1]

        processing = query(db_path, "SELECT id, claimed_until FROM "
                                    "notifications WHERE status = "
                                    "'processing'")
        check("after SIGKILL the claimed row stays 'processing' with its "
              "lease", len(processing) == 1 and processing[0][1],
              str(processing))
        if len(processing) != 1:
            return
        row_id = processing[0][0]

        result = ws.flask("notifications-send", env=env)
        still = query(db_path, "SELECT status FROM notifications WHERE id = ?",
                      (row_id,))[0][0]
        others = _statuses(db_path)
        check("a new worker does not take the row before its lease expires "
              "and delivers the rest",
              result.returncode == 0 and still == "processing"
              and others.get("sent") == total - 1, f"{still} {others}")

        # Wait out the lease, then the row is recovered and retried
        time.sleep(LEASE_SECONDS + 1)
        ws.flask("notifications-send", env=env)
        outcomes = [row[0] for row in query(
            db_path, "SELECT outcome FROM notification_attempts WHERE "
                     "notification_id = ? ORDER BY id", (row_id,))]
        check("after the lease expires the attempt is recorded as "
              "'interrupted'", "interrupted" in outcomes, str(outcomes))

        time.sleep(3)
        ws.flask("notifications-send", env=env)
        final = _statuses(db_path)
        check("the interrupted message is retried and every row is sent",
              final == {"sent": total}, str(final))
        duplicates = [mid for mid in set(smtp.message_ids)
                      if smtp.message_ids.count(mid) > 1]
        check("only the in-flight message was delivered twice, with the "
              "same Message-ID (at-least-once)",
              duplicates == [held_id]
              and len(smtp.message_ids) == total + 1,
              f"{len(smtp.message_ids)} messages for {total} rows; "
              f"{len(duplicates)} duplicated")
    finally:
        smtp.close()


def _notification_sigterm(ws, report):
    check = report.check
    db_path, env = _notification_db(ws, "notify-term")
    smtp = HoldingSmtpServer()
    try:
        env = {**env, **smtp.env()}
        smtp.hold_seconds = 3
        worker, log_path = ws.popen(
            [sys.executable, "-m", "flask", "--app", "app",
             "notifications-send", "--loop", "--interval", "1"],
            env, "notify-term-worker.log")
        if not check("the worker starts delivering", smtp.held.wait(60)):
            worker.kill()
            return
        worker.send_signal(signal.SIGTERM)
        try:
            code = worker.wait(60)
        except Exception:
            worker.kill()
            code = None
        statuses = _statuses(db_path)
        check("SIGTERM during a delivery: the message is finished, exit 0, "
              "nothing left 'processing'",
              code == 0 and "processing" not in statuses
              and statuses.get("sent", 0) >= 1,
              f"exit {code}, {statuses}\n" + log_path.read_text()[-800:])
        beat_path = ws.state / "notification-worker.json"
        try:
            beat = json.loads(beat_path.read_text())
        except (OSError, ValueError):
            beat = {}
        check("the worker heartbeat was written and says 'stopped'",
              beat.get("state") == "stopped", str(beat))
    finally:
        smtp.close()


def _delivery_paused(ws, report):
    db_path, env = _notification_db(ws, "notify-paused")
    # Nothing listens on a fresh free port
    env = {**env, **smtp_env(free_port())}
    result = ws.flask("notifications-send", env=env)
    statuses = _statuses(db_path)
    report.check("an unreachable mail server pauses delivery (exit 2) and "
                 "nothing is lost",
                 result.returncode == 2 and "sent" not in statuses
                 and "processing" not in statuses,
                 f"exit {result.returncode}, {statuses}\n"
                 + result.stdout[-400:] + result.stderr[-400:])


# ---------------------------------------------------------------------
# SQLite concurrency
# ---------------------------------------------------------------------

def _concurrency(ws, report):
    check = report.check
    db_path, env = _notification_db(ws, "concurrency")
    started = time.monotonic()
    processes = []
    for index in range(WRITERS):
        processes.append(subprocess.Popen(
            [sys.executable, str(ws.src / "rehearsals" /
                                 "concurrency_worker.py"),
             str(index), str(ITERATIONS)],
            cwd=ws.src, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True))
    results, failures = [], []
    for process in processes:
        out, err = process.communicate(timeout=300)
        try:
            results.append(json.loads(out.strip().splitlines()[-1]))
        except (ValueError, IndexError):
            failures.append(ws.redact(err[-500:]))
    seconds = time.monotonic() - started

    errors = [e for r in results for e in r["errors"]]
    tickets = [t for r in results for t in r["tickets"]]
    expected = WRITERS * ITERATIONS
    check(f"{WRITERS} writers x {ITERATIONS} iterations "
          f"({expected * 3} write transactions) finish with 0 errors "
          f"({seconds:.1f} s)",
          not failures and not errors and len(tickets) == expected,
          f"{len(errors)} errors, {failures} {errors[:3]}")
    if not tickets:
        return

    marks = ",".join("?" * len(tickets))
    saved = query(db_path, "SELECT COUNT(*) FROM tickets WHERE title LIKE "
                           "'Concurrency ticket %'")[0][0]
    check("every ticket saved exactly once", saved == expected
          and len(set(tickets)) == expected, str(saved))
    history = query(db_path, f"SELECT ticket_id, COUNT(*) FROM ticket_history "
                             f"WHERE ticket_id IN ({marks}) GROUP BY "
                             f"ticket_id", tickets)
    check("every ticket has its 4 history rows (created, comment, status, "
          "assignment)",
          len(history) == expected and {c for _, c in history} == {4},
          str(sorted({c for _, c in history})))
    responded = query(db_path, f"SELECT COUNT(*) FROM ticket_sla WHERE "
                               f"first_response_at IS NOT NULL AND ticket_id "
                               f"IN ({marks})", tickets)[0][0]
    check("every first response was recorded", responded == expected,
          str(responded))
    integrity = query(db_path, "PRAGMA integrity_check")[0][0]
    violations = query(db_path, "PRAGMA foreign_key_check")
    check("integrity_check ok and no foreign-key violations",
          integrity == "ok" and not violations, f"{integrity} {violations}")
