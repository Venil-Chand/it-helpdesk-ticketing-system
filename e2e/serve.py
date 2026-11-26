"""
Run the helpdesk for the browser tests, on loopback, with a movable clock.

Started by e2e/harness.py as a separate process:

    python e2e/serve.py <port> <clock-offset-file>

The application is the real one (helpdesk.create_app) with the settings
taken from the environment the harness prepared: APP_ENV=testing (so the
repository's .env is never read), a temporary HELPDESK_DATABASE_PATH and
a temporary notification sink. Nothing here changes application code.

The only test hook is the clock. Every part of the helpdesk asks
helpdesk.clock for the time, so session expiry and SLA deadlines can be
tested by moving that clock forward. The offset, in seconds, is read from
<clock-offset-file> on every call, so the harness can change it while the
server runs. The file belongs to the harness's temporary directory.
"""

import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from werkzeug.serving import make_server  # noqa: E402

import helpdesk.clock as clock  # noqa: E402
from helpdesk import create_app  # noqa: E402


def install_clock_offset(offset_file):
    real_utc_now = clock.utc_now
    real_current_time = clock.current_time

    def offset():
        try:
            return int(offset_file.read_text().strip() or 0)
        except (OSError, ValueError):
            return 0

    clock.utc_now = lambda: real_utc_now() + timedelta(seconds=offset())
    clock.current_time = lambda: real_current_time() + offset()


def main():
    port = int(sys.argv[1])
    install_clock_offset(Path(sys.argv[2]))

    app = create_app()
    server = make_server("127.0.0.1", port, app, threaded=True)
    print(f"serving on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
