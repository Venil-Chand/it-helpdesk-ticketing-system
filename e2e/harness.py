"""
A disposable helpdesk for the browser tests.

HelpdeskServer builds everything in a new temporary directory and removes
it afterwards:

  * a new SQLite database, created by the real "flask --app app init-db";
  * the first administrator, created by the real setup_accounts.py typed
    at a pseudo-terminal, exactly as an operator would;
  * the application, served on a free loopback port by e2e/serve.py;
  * a notification sink folder (local delivery writes files there).

The repository's helpdesk.db and .env are never opened: every process gets
HELPDESK_DATABASE_PATH inside the temporary directory and APP_ENV=testing,
which never reads .env. No email can be sent: the only SMTP server the
tests use is FakeSmtpServer below, on 127.0.0.1.
"""

import json
import os
import pty
import select
import signal
import socket
import socketserver
import sqlite3
import subprocess
import termios
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# setup_accounts.py always names the first administrator "admin"
ADMIN = "admin"
ADMIN_PASSWORD = "e2e operator pass phrase for tests"

# Password hashing in the browser tests is the real (slow) scrypt hashing;
# these are long enough for the password policy and not on its blocklist
PASSWORDS = {
    "first": "a first temporary pass phrase",
    "chosen": "the pass phrase I chose myself",
    "reset": "a reset temporary pass phrase",
    "again": "yet another chosen pass phrase",
}


def free_port():
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class HelpdeskServer:

    def __init__(self, extra_env=None):
        self._temp = tempfile.TemporaryDirectory(prefix="helpdesk-e2e-")
        self.dir = Path(self._temp.name)
        self.db_path = self.dir / "helpdesk.db"
        self.sink_dir = self.dir / "notification-sink"
        self.clock_file = self.dir / "clock-offset"
        self.clock_file.write_text("0")
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.log_path = self.dir / "server.log"
        self.process = None

        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.dir),
            "LANG": "C.UTF-8",
            "APP_ENV": "testing",
            "SECRET_KEY": "e2e-only-secret-key-not-for-production-use",
            "HELPDESK_DATABASE_PATH": str(self.db_path),
            "NOTIFICATION_DELIVERY": "local",
            "NOTIFICATION_SINK_DIR": str(self.sink_dir),
            "NOTIFICATION_BASE_URL": self.url,
            "PYTHONDONTWRITEBYTECODE": "1",
            # Flask's own command line would otherwise load ./.env itself
            "FLASK_SKIP_DOTENV": "1",
        }
        # A Python built with a shared libpython (as on GitHub's hosted
        # runners) needs its library path in every child process
        if os.environ.get("LD_LIBRARY_PATH"):
            self.env["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
        self.env.update(extra_env or {})

    # --- lifecycle -------------------------------------------------------

    def start(self):
        progress = lambda text: print(f"[harness] {text}", file=sys.stderr,
                                      flush=True)
        result = self.flask("init-db")
        assert "version 10" in result.stdout, result.stdout + result.stderr
        progress("database created (schema version 10)")
        self.create_first_admin()
        progress("first administrator created by setup_accounts.py")
        self.start_web()
        progress(f"application serving on {self.url}")
        return self

    def start_web(self):
        log = open(self.log_path, "ab")
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "e2e" / "serve.py"),
             str(self.port), str(self.clock_file)],
            cwd=self.dir, env=self.env, stdout=log, stderr=log
        )
        log.close()
        self.wait_until_healthy()

    def stop_web(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(10)
        self.process = None

    def close(self):
        self.stop_web()
        self._temp.cleanup()

    def wait_until_healthy(self, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("server exited:\n" + self.server_log())
            try:
                with urllib.request.urlopen(self.url + "/healthz",
                                            timeout=2) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("server did not start:\n" + self.server_log())

    def server_log(self):
        try:
            return self.log_path.read_text(errors="replace")[-4000:]
        except OSError:
            return ""

    # --- the clock -------------------------------------------------------

    def advance_clock(self, seconds):
        offset = int(self.clock_file.read_text()) + int(seconds)
        self.clock_file.write_text(str(offset))

    # --- operator commands ------------------------------------------------

    def flask(self, *args, env=None, check=True):
        result = subprocess.run(
            [sys.executable, "-m", "flask", "--app", "app", *args],
            cwd=ROOT, env={**self.env, **(env or {})},
            capture_output=True, text=True, timeout=120,
            stdin=subprocess.DEVNULL
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"flask {' '.join(args)} failed ({result.returncode}):\n"
                f"{result.stdout}\n{result.stderr}")
        return result

    def run_at_terminal(self, argv, answers, timeout=60):
        """
        Run a command at a pseudo-terminal and type `answers` in order,
        each when the previous prompt has appeared. Returns the output.

        Every prompt here is a hidden password prompt (getpass). An answer
        is typed only once the terminal's echo is off: getpass flushes
        typed-ahead input when it switches echo off, so an answer sent
        between the prompt and that switch would be lost.

        The command is killed, and the test fails with its output, if it
        has not finished within `timeout` seconds.
        """

        pid, fd = pty.fork()
        if pid == 0:  # the child: become the command, or exit at once
            try:
                os.chdir(ROOT)
                os.execve(argv[0], argv, self.env)
            finally:
                os._exit(127)

        output = b""
        pending = list(answers)
        answered = 0  # length of the output when the last answer was typed
        deadline = time.monotonic() + timeout
        status = None
        try:
            while status is None and time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], 0.2)
                if ready:
                    try:
                        chunk = os.read(fd, 4096)
                    except OSError:  # EIO: the child closed the terminal
                        chunk = b""
                    output += chunk
                    if chunk:
                        continue
                new_prompt = output[answered:].rstrip().endswith(b":")
                if pending and new_prompt and self._echo_is_off(fd):
                    os.write(fd, pending.pop(0).encode() + b"\n")
                    answered = len(output)
                    continue
                finished, wait_status = os.waitpid(pid, os.WNOHANG)
                if finished:
                    status = wait_status
            if status is None:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
        finally:
            os.close(fd)

        text = output.decode(errors="replace")
        if status is None:
            raise AssertionError(
                f"{argv} did not finish in {timeout} s ({len(pending)} "
                f"prompt(s) not answered); output:\n{text}")
        if os.waitstatus_to_exitcode(status) != 0:
            raise AssertionError(f"{argv} failed:\n{text}")
        return text

    @staticmethod
    def _echo_is_off(fd):
        try:
            return not termios.tcgetattr(fd)[3] & termios.ECHO
        except termios.error:
            return False

    def create_first_admin(self):
        text = self.run_at_terminal(
            [sys.executable, str(ROOT / "setup_accounts.py")],
            [ADMIN_PASSWORD, ADMIN_PASSWORD]
        )
        assert "Administrator account created" in text, text

    def operator_reset_password(self, username, password):
        return self.run_at_terminal(
            [sys.executable, "-m", "flask", "--app", "app",
             "reset-password", username],
            [password, password]
        )

    def send_notifications(self, env=None):
        return self.flask("notifications-send", env=env)

    # --- read-only checks of the temporary database ----------------------

    def query(self, sql, params=()):
        uri = f"file:{self.db_path}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, params)]

    def notifications(self):
        return self.query(
            "SELECT id, event_type, recipient_username, status, attempts, "
            "deferrals, next_attempt_at, last_error, delivery_mode "
            "FROM notifications ORDER BY id")

    def sink_messages(self):
        if not self.sink_dir.exists():
            return []
        return sorted(p.read_text() for p in self.sink_dir.glob("*.eml"))


class FakeSmtpServer:
    """
    A loopback SMTP server that never delivers anything.

    It answers every RCPT TO with `rcpt_reply` (for example
    "451 4.3.0 try again later" or "550 5.1.1 no such user") and counts the
    attempts, so the tests can produce temporary and permanent delivery
    failures without any real mail system.
    """

    def __init__(self, rcpt_reply):
        self.rcpt_reply = rcpt_reply
        self.attempts = 0
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                def reply(line):
                    self.wfile.write(line.encode() + b"\r\n")

                reply("220 fake-smtp ready")
                in_data = False
                for raw in self.rfile:
                    line = raw.decode(errors="replace").rstrip("\r\n")
                    if in_data:
                        if line == ".":
                            in_data = False
                            reply("250 discarded")
                        continue
                    verb = line[:4].upper()
                    if verb in ("EHLO", "HELO"):
                        reply("250 fake-smtp")
                    elif verb == "MAIL":
                        reply("250 ok")
                    elif verb == "RCPT":
                        outer.attempts += 1
                        reply(outer.rcpt_reply)
                    elif verb == "DATA":
                        in_data = True
                        reply("354 go ahead")
                    elif verb == "QUIT":
                        reply("221 bye")
                        return
                    else:
                        reply("250 ok")

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0),
                                                      Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def env(self, **extra):
        values = {
            "NOTIFICATION_DELIVERY": "smtp",
            "NOTIFICATION_SENDER": "helpdesk@example.invalid",
            "NOTIFICATION_SMTP_HOST": "127.0.0.1",
            "NOTIFICATION_SMTP_PORT": str(self.port),
            "NOTIFICATION_SMTP_SECURITY": "none",
            "NOTIFICATION_SMTP_TIMEOUT": "5",
        }
        values.update({key: str(value) for key, value in extra.items()})
        return values

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def dump(value):
    return json.dumps(value, indent=1, default=str)
