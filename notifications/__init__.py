"""
Email notifications, built as a transactional outbox.

    events.py    which events notify whom; queues rows inside the caller's
                 ticket or account transaction (never sends anything)
    timeutil.py  the queue's UTC clock and timestamp format
    settings.py  delivery configuration from environment variables
    render.py    turns a queued row into a plain-text email
    transport.py local sink (simulated) and SMTP delivery
    queue.py     claiming, finishing and recovering queued rows
    worker.py    the delivery loop run by "flask notifications-send"

Nothing here sends email from a web request. A request only adds rows to
the notifications table, in the same transaction as the change that
caused them; the worker delivers them later, after that transaction has
committed. See docs/NOTIFICATIONS.md.

This file deliberately imports nothing: database.py and accounts.py
import notifications.events, and must not pull in the worker or SMTP code.
"""
