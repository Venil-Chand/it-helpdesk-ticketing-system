"""
Shared sign-in helper for route tests.

Requests are authenticated from the database on every request. Route
tests that are about something else (search, pagination, CSV and so on)
use sign_in() to supply an active account without adding users to their
fixtures. The real account checks are tested without this stub in
test_database_authentication.py.
"""

import time
from unittest.mock import patch


def sign_in(test, client, username, role="user"):
    """Give `client` a session for a stubbed, active account."""

    accounts = getattr(test, "_stub_accounts", None)

    if accounts is None:
        accounts = test._stub_accounts = {}

        def revoke(user_id, expected_version):
            account = accounts.get(user_id)

            if account and account["session_version"] == expected_version:
                account["session_version"] += 1
                return True

            return False

        # Both are stubbed so a route test can never write to a real file
        for name, stub in (
            ("get_user_by_id", accounts.get),
            ("revoke_user_sessions", revoke),
        ):
            patcher = patch(f"app.db.{name}", side_effect=stub)
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
        "session_version": 1,
    }

    with client.session_transaction() as sess:
        sess.pop("username", None)
        sess.pop("role", None)
        sess["user_id"] = user_id
        sess["session_version"] = accounts[user_id]["session_version"]
        sess["issued_at"] = int(time.time())

    return user_id
