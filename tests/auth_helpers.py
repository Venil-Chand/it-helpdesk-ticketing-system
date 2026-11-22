"""
Shared sign-in helper for route tests.

Requests are authenticated from the database on every request. Route
tests that are about something else (search, pagination, CSV and so on)
use sign_in() to supply an active account without adding users to their
fixtures. The real account checks are tested without this stub in
test_database_authentication.py.
"""

from unittest.mock import patch


def sign_in(test, client, username, role="user"):
    """Give `client` a session for a stubbed, active account."""

    accounts = getattr(test, "_stub_accounts", None)

    if accounts is None:
        accounts = test._stub_accounts = {}
        patcher = patch("app.db.get_user_by_id", side_effect=accounts.get)
        patcher.start()
        test.addCleanup(patcher.stop)

    user_id = next(
        (key for key, account in accounts.items()
         if account["username"] == username),
        len(accounts) + 1
    )

    accounts[user_id] = {
        "id": user_id,
        "username": username,
        "role": role,
        "is_active": 1,
    }

    with client.session_transaction() as sess:
        sess.pop("username", None)
        sess.pop("role", None)
        sess["user_id"] = user_id

    return user_id
