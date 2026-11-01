
import os
import tempfile
import unittest
from unittest.mock import patch

import database as db
from setup_accounts import setup_account

from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)


class TestAccountSetup(unittest.TestCase):

    def setUp(self):
        # Create a temporary database for each test
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        test_db = os.path.join(
            self.temp_dir.name,
            "test_helpdesk.db"
        )

        # Redirect database operations to the test database
        db_patcher = patch.object(db, "DB_PATH", test_db)
        db_patcher.start()
        self.addCleanup(db_patcher.stop)

    def create_test_admin(self, password):
        # Create a user using a test password
        db.init_db()

        conn = db.get_db()
        try:
            conn.execute(
                """
                INSERT INTO users
                (username, password_hash, role)
                VALUES (?, ?, ?)
                """,
                (
                    "admin",
                    generate_password_hash(password),
                    "admin"
                )
            )
            conn.commit()
        finally:
            conn.close()

    def test_admin_password_is_hashed(self):
        password = "ExampleTestPassword123!"

        with patch(
            "setup_accounts.getpass",
            side_effect=[password, password]
        ):
            setup_account()

        user = db.get_user("admin")

        self.assertIsNotNone(user)
        self.assertEqual(user["role"], "admin")
        self.assertNotEqual(user["password_hash"], password)
        self.assertTrue(
            check_password_hash(user["password_hash"], password)
        )

    def test_wrong_current_password_is_rejected(self):
        old_password = "OriginalTestPassword123!"

        self.create_test_admin(old_password)

        with patch("builtins.input", return_value="admin"):
            with patch(
                "setup_accounts.getpass",
                return_value="incorrect-password"
            ):
                setup_account()

        user = db.get_user("admin")

        self.assertTrue(
            check_password_hash(
                user["password_hash"],
                old_password
            )
        )

    def test_password_change_invalidates_old_password(self):
        old_password = "OriginalTestPassword123!"
        new_password = "UpdatedTestPassword456!"

        self.create_test_admin(old_password)

        with patch("builtins.input", return_value="admin"):
            with patch(
                "setup_accounts.getpass",
                side_effect=[
                    old_password,
                    new_password,
                    new_password
                ]
            ):
                setup_account()

        user = db.get_user("admin")

        self.assertFalse(
            check_password_hash(
                user["password_hash"],
                old_password
            )
        )

        self.assertTrue(
            check_password_hash(
                user["password_hash"],
                new_password
            )
        )


if __name__ == "__main__":
    unittest.main()
