"""
Shared parts of the rehearsals: the disposable workspace, the safety
guard, a disposable Redis server, Gunicorn, a small HTTP client and the
pass/fail report.

Safety model (tested by rehearsals/tests/test_common.py):

  * Workspace copies the source code (the files git tracks or would
    track, never ignored ones) into a new temporary directory and runs
    every command there. Files such as .env, *.db and key files are never
    copied, even if one were tracked by mistake.
  * guard_environment() is called before every command. It refuses to run
    unless HELPDESK_DATABASE_PATH and every other path setting point
    inside the workspace, APP_ENV is "testing" or "production" (neither
    reads a .env file) and FLASK_SKIP_DOTENV=1 (Flask's own command line
    would otherwise look for one).
  * Secrets (SECRET_KEY, the Redis password, account passwords) are new
    random values for each run. Everything the rehearsal prints passes
    through Workspace.redact(), which replaces them with "[redacted]".
  * DisposableRedis starts a private redis-server on a free loopback port
    with a random password and no persistence. Rehearsals never connect
    to a Redis server they did not start.
"""

import hashlib
import http.client
import os
import pty
import re
import secrets
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode

REPO_ROOT = Path(__file__).resolve().parent.parent

# Never copied into a workspace, whatever git says
PRIVATE_NAMES = frozenset({".env", "helpdesk.db"})
PRIVATE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite",
                    ".sqlite3", ".key", ".pem", ".hdbk",
                    ".manifest.json")
PRIVATE_DIRECTORIES = frozenset({".git", ".venv", "venv", "instance",
                                 "credentials", "__pycache__"})

# Environment variables that name a file or directory the application
# reads or writes; each must point inside the workspace
PATH_VARIABLES = (
    "HELPDESK_DATABASE_PATH",
    "HELPDESK_BACKUP_DIR",
    "HELPDESK_BACKUP_OFFSITE_DIR",
    "HELPDESK_BACKUP_KEY_FILE",
    "HELPDESK_STATE_DIR",
    "NOTIFICATION_SINK_DIR",
    "SECRET_KEY_FILE",
    "RATELIMIT_STORAGE_URI_FILE",
    "NOTIFICATION_SMTP_PASSWORD_FILE",
    "HOME",
)

SAFE_APP_ENVIRONMENTS = ("testing", "production")

# Variables a rehearsal may pass on from the operator's shell. Nothing
# else is inherited, so a SECRET_KEY or Redis URL exported in the shell
# can never leak into a rehearsal.
INHERITED_VARIABLES = ("PATH", "LD_LIBRARY_PATH", "LANG", "TZ")

HOST = "helpdesk.test"


class UnsafeRehearsal(RuntimeError):
    """A command could reach real data, so it was not started."""


class RehearsalUnavailable(RuntimeError):
    """Something the rehearsal needs (Redis, Gunicorn, ...) is missing."""


# ---------------------------------------------------------------------
# Copying the source code
# ---------------------------------------------------------------------

def is_private_path(relative):
    parts = Path(relative).parts
    name = parts[-1] if parts else ""
    return (
        name in PRIVATE_NAMES
        or name.startswith(".env.") and not name.endswith(".example")
        or name.endswith(PRIVATE_SUFFIXES)
        or any(part in PRIVATE_DIRECTORIES for part in parts[:-1])
    )


def source_files(root=REPO_ROOT):
    """Paths, relative to root, of the files to copy into a workspace."""

    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others",
             "--exclude-standard"],
            cwd=root, capture_output=True, check=True, timeout=60,
        ).stdout.decode().split("\0")
    except (OSError, subprocess.SubprocessError):
        listed = [
            str(path.relative_to(root))
            for path in root.rglob("*") if path.is_file()
        ]

    return sorted(
        item for item in listed
        if item and not is_private_path(item) and (root / item).is_file()
    )


def copy_source(destination, root=REPO_ROOT):
    destination = Path(destination)
    for relative in source_files(root):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, target)

    leftovers = [
        str(path.relative_to(destination)) for path in destination.rglob("*")
        if path.is_file() and is_private_path(path.relative_to(destination))
    ]
    if leftovers:
        raise UnsafeRehearsal(f"private files were copied: {leftovers}")


# ---------------------------------------------------------------------
# The safety guard
# ---------------------------------------------------------------------

def _inside(path, directory):
    path = Path(os.path.realpath(path))
    directory = Path(os.path.realpath(directory))
    return path == directory or directory in path.parents


def guard_environment(env, workspace_root, repo_root=REPO_ROOT):
    """Raise UnsafeRehearsal unless `env` can only reach the workspace."""

    if _inside(workspace_root, repo_root):
        raise UnsafeRehearsal("the workspace must be outside the repository")

    database = env.get("HELPDESK_DATABASE_PATH", "").strip()
    if not database:
        raise UnsafeRehearsal("HELPDESK_DATABASE_PATH is not set; the "
                              "application would use the real helpdesk.db")

    for name in PATH_VARIABLES:
        value = env.get(name, "").strip()
        if value and not _inside(value, workspace_root):
            raise UnsafeRehearsal(f"{name} points outside the workspace")

    for private in (repo_root / "helpdesk.db", repo_root / ".env"):
        if any(os.path.realpath(env.get(name, "")) == os.path.realpath(private)
               for name in PATH_VARIABLES if env.get(name)):
            raise UnsafeRehearsal(f"a setting names {private.name}")

    if env.get("APP_ENV", "").strip().lower() not in SAFE_APP_ENVIRONMENTS:
        raise UnsafeRehearsal("APP_ENV must be testing or production, which "
                              "never read a .env file")

    if env.get("FLASK_SKIP_DOTENV") != "1":
        raise UnsafeRehearsal("FLASK_SKIP_DOTENV must be 1")


# ---------------------------------------------------------------------
# The workspace
# ---------------------------------------------------------------------

def free_port():
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def new_secret_key():
    return secrets.token_hex(32)


def new_password():
    # Long, random and readable; passes the password policy
    return "rehearsal " + secrets.token_urlsafe(18)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


class Workspace:
    """A temporary directory with a copy of the code and its own data."""

    def __init__(self, name, *, keep=False):
        self.keep = keep
        self.root = Path(tempfile.mkdtemp(prefix=f"helpdesk-{name}-"))
        os.chmod(self.root, 0o700)
        self.src = self.root / "src"
        copy_source(self.src)
        self.data = private_dir(self.root / "data")
        self.db_path = self.data / "helpdesk.db"
        self.state = private_dir(self.root / "state")
        self.logs = private_dir(self.root / "logs")
        self.secret_key = new_secret_key()
        self._sensitive = {self.secret_key}

    # --- secrets ----------------------------------------------------------

    def sensitive(self, value):
        """Register a secret value so redact() hides it; returns it."""

        if value:
            self._sensitive.add(str(value))
        return value

    def redact(self, text):
        text = str(text)
        for value in sorted(self._sensitive, key=len, reverse=True):
            text = text.replace(value, "[redacted]")
        return text

    def leaks(self, text):
        """The registered secrets that appear in text (as labels only)."""

        return [f"secret #{index}" for index, value in
                enumerate(sorted(self._sensitive)) if value in str(text)]

    # --- environments -----------------------------------------------------

    def env(self, app_env="testing", **extra):
        values = {name: os.environ[name] for name in INHERITED_VARIABLES
                  if os.environ.get(name)}
        values.setdefault("PATH", "/usr/bin:/bin")
        values.update({
            "HOME": str(self.root),
            "APP_ENV": app_env,
            "SECRET_KEY": self.secret_key,
            "HELPDESK_DATABASE_PATH": str(self.db_path),
            "HELPDESK_STATE_DIR": str(self.state),
            "NOTIFICATION_DELIVERY": "local",
            "NOTIFICATION_SINK_DIR": str(self.root / "notification-sink"),
            "NOTIFICATION_BASE_URL": f"https://{HOST}",
            "FLASK_SKIP_DOTENV": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        })
        values.update({key: str(value) for key, value in extra.items()
                       if value is not None})
        return values

    def production_env(self, redis_url, **extra):
        return self.env(
            "production",
            TRUSTED_HOSTS=HOST,
            TRUSTED_PROXY_COUNT="1",
            TRUSTED_PROXY_IPS="127.0.0.1",
            RATELIMIT_STORAGE_URI=redis_url,
            **extra,
        )

    # --- running commands -------------------------------------------------

    def run(self, argv, env, *, input=None, timeout=300):
        guard_environment(env, self.root)
        return subprocess.run(
            argv, cwd=self.src, env=env, input=input, capture_output=True,
            text=True, timeout=timeout,
            stdin=None if input is not None else subprocess.DEVNULL,
        )

    def popen(self, argv, env, log_name):
        guard_environment(env, self.root)
        log_path = self.logs / log_name
        with open(log_path, "ab") as log:
            process = subprocess.Popen(argv, cwd=self.src, env=env,
                                       stdout=log, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL)
        return process, log_path

    def flask(self, *args, env, input=None, timeout=300):
        return self.run([sys.executable, "-m", "flask", "--app", "app",
                         *args], env, input=input, timeout=timeout)

    def seed(self, env, *, tickets, password):
        """Fill the database through the application's own functions."""

        self.sensitive(password)
        result = self.run(
            [sys.executable, str(self.src / "rehearsals" / "seed.py"),
             str(tickets)],
            {**env, "REHEARSAL_PASSWORD": password},
        )
        if result.returncode != 0:
            raise RuntimeError("seeding failed:\n"
                               + self.redact(result.stdout + result.stderr))
        return result.stdout

    def at_terminal(self, argv, env, answers, timeout=120):
        """
        Run argv at a pseudo-terminal, typing each (prompt, answer) pair
        when the prompt text appears. Returns (exit code, output).
        """

        guard_environment(env, self.root)
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(self.src)
                os.execve(argv[0], argv, env)
            finally:
                os._exit(127)

        output = b""
        pending = list(answers)
        answered = 0
        deadline = time.monotonic() + timeout
        status = None
        try:
            while status is None and time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], 0.2)
                if ready:
                    try:
                        chunk = os.read(fd, 4096)
                    except OSError:
                        chunk = b""
                    output += chunk
                    if chunk:
                        continue
                if pending and pending[0][0].encode() in output[answered:]:
                    os.write(fd, pending.pop(0)[1].encode() + b"\n")
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

        code = None if status is None else os.waitstatus_to_exitcode(status)
        return code, output.decode(errors="replace")

    def close(self):
        if self.keep:
            print(f"Workspace kept: {self.root}")
        else:
            shutil.rmtree(self.root, ignore_errors=True)


# ---------------------------------------------------------------------
# Read-only database checks
# ---------------------------------------------------------------------

COUNTED_TABLES = ("users", "tickets", "comments", "ticket_history",
                  "ticket_sla", "audit_events", "notifications",
                  "notification_attempts")


def query(path, sql, params=()):
    uri = f"file:{path}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        return conn.execute(sql, params).fetchall()


def row_counts(path):
    return {table: query(path, f"SELECT COUNT(*) FROM {table}")[0][0]
            for table in COUNTED_TABLES}


# ---------------------------------------------------------------------
# A disposable Redis server
# ---------------------------------------------------------------------

def find_redis_server():
    configured = os.environ.get("HELPDESK_REHEARSAL_REDIS_SERVER", "").strip()
    return configured or shutil.which("redis-server")


class DisposableRedis:
    """A private redis-server: loopback only, random password, no disk."""

    def __init__(self, workspace):
        self.binary = find_redis_server()
        if not self.binary:
            raise RehearsalUnavailable(
                "redis-server not found: install it or set "
                "HELPDESK_REHEARSAL_REDIS_SERVER to its path")
        self.workspace = workspace
        self.port = free_port()
        self.password = workspace.sensitive(secrets.token_urlsafe(24))
        self.dir = private_dir(workspace.root / "redis")
        self.config = self.dir / "redis.conf"
        self.config.write_text("\n".join([
            "bind 127.0.0.1",
            f"port {self.port}",
            "protected-mode yes",
            f"requirepass {self.password}",
            'save ""',
            "appendonly no",
            f"dir {self.dir}",
            "daemonize no",
            "",
        ]))
        os.chmod(self.config, 0o600)
        self.process = None

    def url(self, db=0, password=None):
        password = self.password if password is None else password
        return f"redis://:{password}@127.0.0.1:{self.port}/{db}"

    def start(self, timeout=15):
        log = open(self.workspace.logs / "redis.log", "ab")
        self.process = subprocess.Popen(
            [self.binary, str(self.config)], stdout=log,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env={name: os.environ[name] for name in INHERITED_VARIABLES
                 if os.environ.get(name)},
        )
        log.close()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RehearsalUnavailable("redis-server exited at once; see "
                                           "its log in the workspace")
            if self.ping():
                return self
            time.sleep(0.1)
        raise RehearsalUnavailable("redis-server did not start")

    def ping(self):
        import redis
        try:
            return redis.Redis.from_url(self.url(), socket_timeout=1).ping()
        except redis.RedisError:
            return False

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(10)
        self.process = None


# ---------------------------------------------------------------------
# Gunicorn and HTTP
# ---------------------------------------------------------------------

def gunicorn_available():
    try:
        import gunicorn  # noqa: F401
        return True
    except ImportError:
        return False


class Gunicorn:
    """deploy/gunicorn.conf.py serving wsgi:application from the copy."""

    def __init__(self, workspace, env, *, workers=2, port=None,
                 log_name="gunicorn.log", bind=None):
        self.workspace = workspace
        self.port = port or free_port()
        self.env = {**env, "WEB_CONCURRENCY": str(workers),
                    "HELPDESK_BIND": bind or f"127.0.0.1:{self.port}"}
        self.process, self.log_path = workspace.popen(
            [sys.executable, "-m", "gunicorn", "--config",
             str(workspace.src / "deploy" / "gunicorn.conf.py"),
             "wsgi:application"],
            self.env, log_name)

    def log(self):
        try:
            return self.log_path.read_text(errors="replace")
        except OSError:
            return ""

    def wait_ready(self, timeout=30):
        """True once /healthz answers; False if Gunicorn exited first."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                return False
            try:
                if Client(self.port).get("/healthz").status == 200:
                    return True
            except OSError:
                pass
            time.sleep(0.2)
        return False

    def wait_exit(self, timeout=30):
        try:
            return self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def worker_pids(self):
        return child_pids(self.process.pid)

    def stop(self, sig=signal.SIGTERM):
        if self.process.poll() is None:
            self.process.send_signal(sig)
            try:
                self.process.wait(40)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(10)
        return self.process.returncode


def child_pids(pid):
    children = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if int(fields[1]) == pid:
            children.append(int(entry.name))
    return sorted(children)


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def header(self, name):
        name = name.lower()
        return next((value for key, value in self.headers
                     if key.lower() == name), None)

    def all_headers(self, name):
        name = name.lower()
        return [value for key, value in self.headers if key.lower() == name]

    @property
    def text(self):
        return self.body.decode(errors="replace")


CSRF_FIELD = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')


class Client:
    """
    Requests as Caddy would send them: Host, X-Forwarded-Proto: https and
    X-Forwarded-For. Cookies are kept by hand, because the production
    session cookie is "__Host-" and Secure, which ordinary clients only
    return over real HTTPS.
    """

    def __init__(self, port, *, host=HOST, client_ip="203.0.113.10"):
        self.port = port
        self.host = host
        self.client_ip = client_ip
        self.cookies = {}

    def request(self, method, path, *, body=None, headers=None,
                forwarded_for=None):
        sent = {
            "Host": self.host,
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": forwarded_for or self.client_ip,
        }
        if self.cookies:
            sent["Cookie"] = "; ".join(f"{k}={v}"
                                       for k, v in self.cookies.items())
        sent.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path, body=body, headers=sent)
            raw = conn.getresponse()
            response = Response(raw.status, raw.getheaders(), raw.read())
        finally:
            conn.close()
        for cookie in response.all_headers("Set-Cookie"):
            name, _, rest = cookie.partition("=")
            value = rest.split(";", 1)[0]
            if value and "max-age=0" not in cookie.lower():
                self.cookies[name] = value
            else:
                self.cookies.pop(name, None)
        return response

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post_form(self, path, fields, **kwargs):
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   "Origin": f"https://{self.host}",
                   "Referer": f"https://{self.host}{path}"}
        headers.update(kwargs.pop("headers", {}))
        return self.request("POST", path, body=urlencode(fields),
                            headers=headers, **kwargs)

    def csrf_token(self, path="/login", **kwargs):
        match = CSRF_FIELD.search(self.get(path, **kwargs).text)
        return match.group(1) if match else ""

    def login(self, username, password, **kwargs):
        token = self.csrf_token(**kwargs)
        return self.post_form("/login", {
            "csrf_token": token, "username": username, "password": password,
        }, **kwargs)


def wait_for(condition, timeout, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return bool(condition())


# ---------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------

class Report:
    """Checks by name; prints each result and a summary."""

    def __init__(self, title, workspace=None):
        self.title = title
        self.workspace = workspace
        self.results = []
        print(f"\n=== {title} ===", flush=True)

    def _clean(self, text):
        return self.workspace.redact(text) if self.workspace else str(text)

    def check(self, name, passed, detail=""):
        status = "PASS" if passed else "FAIL"
        self.results.append((status, name, self._clean(detail)))
        line = f"{status}  {name}"
        if detail and not passed:
            line += "\n      " + self._clean(detail).strip()[-1500:].replace(
                "\n", "\n      ")
        print(line, flush=True)
        return passed

    def skip(self, name, reason):
        self.results.append(("SKIP", name, reason))
        print(f"SKIP  {name}: {reason}", flush=True)

    def counts(self):
        return {status: sum(1 for item in self.results if item[0] == status)
                for status in ("PASS", "FAIL", "SKIP")}

    def finish(self):
        counts = self.counts()
        total = counts["PASS"] + counts["FAIL"]
        print(f"--- {self.title}: {counts['PASS']}/{total} passed"
              + (f", {counts['SKIP']} skipped" if counts["SKIP"] else ""),
              flush=True)
        return counts["FAIL"] == 0
