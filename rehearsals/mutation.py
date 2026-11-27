"""
Mutation rehearsal: does the test suite notice when a security control
is broken?

Each mutant in rehearsals/mutants.py is applied, one at a time, to a copy
of the source in a temporary workspace (never to the repository), and
the test modules named for it are run. The mutant is KILLED when at
least one of them fails, and SURVIVED when they all pass: a survivor is
a security regression the tests would not catch.

First, every named test module is run once on the unmodified copy. If
one fails there, its failures would say nothing about the mutants, so the
rehearsal stops.

Unit tests always run. Browser tests (e2e/) run only with --browser,
because they need Playwright and its headless Chromium. A disposable
Redis is used for the shared rate-limit tests when redis-server is
available; without it those tests skip, as in a plain unit-test run.
"""

import os
import sys
from pathlib import Path

from .common import (DisposableRedis, RehearsalUnavailable, Report, Workspace,
                     find_redis_server)
from .mutants import MUTANTS

# The value CI uses; the tests build their own applications and databases
TEST_SECRET_KEY = "ci-testing-key-not-for-production"


def run(keep=False, browser=False):
    ws = Workspace("rehearsal-mutation", keep=keep)
    report = Report("Mutation rehearsal", ws)
    redis = None
    try:
        extra = {"SECRET_KEY": TEST_SECRET_KEY}
        if find_redis_server():
            redis = DisposableRedis(ws).start()
            extra["HELPDESK_TEST_REDIS_URL"] = redis.url(db=15)
        else:
            report.skip("disposable Redis", "redis-server not found; the "
                        "tests that need it will skip")
        env = ws.env(**extra)
        if browser and not _playwright_available():
            raise RehearsalUnavailable("--browser needs the playwright "
                                       "package (pip install -r "
                                       "e2e/requirements.txt)")
        _rehearse(ws, report, env, browser)
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


def _playwright_available():
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _unit(ws, env, module):
    return ws.run([sys.executable, "-m", "unittest", "discover", "-s",
                   "tests", "-p", module], env, timeout=900)


def _browser(ws, env, test):
    # The browser tests import their harness as a top-level module, and
    # Playwright finds its browsers under the real home directory (HOME
    # is the workspace), unless PLAYWRIGHT_BROWSERS_PATH says otherwise
    browsers = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
                or str(Path.home() / ".cache" / "ms-playwright"))
    env = {**env, "PYTHONPATH": str(ws.src / "e2e"),
           "PLAYWRIGHT_BROWSERS_PATH": browsers}
    return ws.run([sys.executable, "-m", "unittest", test], env,
                  timeout=900)


def _suites(mutant, browser):
    suites = [("unit", module) for module in mutant.unit]
    if browser:
        suites += [("browser", test) for test in mutant.browser]
    return suites


def _execute(ws, env, kind, name):
    result = (_unit if kind == "unit" else _browser)(ws, env, name)
    # unittest exits 0 only when every test passed; 5 means none ran
    return result.returncode, result.stdout + result.stderr


def _rehearse(ws, report, env, browser):
    needed = []
    for mutant in MUTANTS:
        for suite in _suites(mutant, browser):
            if suite not in needed:
                needed.append(suite)

    for kind, name in needed:
        code, output = _execute(ws, env, kind, name)
        if not report.check(f"baseline passes: {kind} {name}", code == 0,
                            output[-1500:]):
            report.check("baseline is clean, so mutants can be judged",
                         False, "fix the failing tests first")
            return

    survivors = [mutant.name for mutant in MUTANTS
                 if _try_mutant(ws, report, env, mutant, browser) is False]
    if survivors:
        print("Not killed: " + "; ".join(survivors))


def _try_mutant(ws, report, env, mutant, browser):
    """True if killed, False if not (or stale), None if skipped."""

    label = f"mutant killed: {mutant.name}"
    target = ws.src / mutant.path
    original = target.read_text()
    found = original.count(mutant.original)
    if found != mutant.count:
        report.check(label, False,
                     f"STALE: the text to replace occurs {found} time(s) in "
                     f"{mutant.path}, expected {mutant.count}; update "
                     "rehearsals/mutants.py to match the code")
        return False

    suites = _suites(mutant, browser)
    if not suites:
        report.skip(label, "only browser tests can kill it; run with "
                    "--browser")
        return None

    target.write_text(original.replace(mutant.original, mutant.replacement))
    try:
        killers = []
        for kind, name in suites:
            code, _ = _execute(ws, env, kind, name)
            if code not in (0, 5):
                killers.append(name)
    finally:
        target.write_text(original)

    report.check(label, bool(killers),
                 "SURVIVED: every named test still passed")
    if killers:
        print(f"      killed by {', '.join(killers)}", flush=True)
    return bool(killers)
