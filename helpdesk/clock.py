"""
The application's clock.

Every part of the helpdesk asks this module for the time, instead of
calling datetime.now() or time.time() itself. That gives two guarantees:

* The dashboard, the CSV export and the ticket page all read the same
  clock, so an SLA status can never differ between them.
* Tests can freeze time in one place (helpdesk.clock.datetime or
  helpdesk.clock.current_time) and every route sees the frozen value.
"""

import time
from datetime import datetime, timezone


def utc_now():
    """The current moment as a timezone-aware UTC datetime."""

    return datetime.now(timezone.utc)


def current_time():
    """The current time in whole seconds since the Unix epoch."""

    return int(time.time())
