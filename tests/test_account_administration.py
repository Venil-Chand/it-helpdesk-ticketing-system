"""
Account administration: who may use it, what each action does to the
account and its sessions, what is refused, and what is audited.
"""

import re
import unittest

import accounts
import database as db
from app import app

from account_helpers import (
    NEW_PASSWORD,
    PASSWORD,
    AccountTestCase,
)


class TestAdminAuthorization(AccountTestCase):

    def admin_requests(self):
        target = self.ids["alice"]
        return [
            ("GET", "/admin/users", {}),
            ("GET", "/admin/users/new", {}),
            ("GET", "/admin/audit", {}),
            ("POST", "/admin/users/new",
             {"username": "mallory", "role": "admin",
              "password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD}),
            ("POST", f"/admin/users/{target}/role", {"role": "admin"}),
            ("POST", f"/admin/users/{target}/active", {"active": "0"}),
            ("POST", f"/admin/users/{target}/password",
             {"password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD}),
        ]

    def test_users_and_technicians_get_403_and_change_nothing(self):
        users_before = self.sql("SELECT * FROM users ORDER BY id")

        for username in ("alice", "tech"):
            client = self.signed_in(username)

            for method, url, data in self.admin_requests():
                with self.subTest(username=username, method=method, url=url):
                    response = client.open(url, method=method, data=data)
                    self.assertEqual(response.status_code, 403)

        self.assertEqual(self.sql("SELECT * FROM users ORDER BY id"),
                         users_before)
        self.assertEqual(self.audit(), [])

    def test_signed_out_visitors_are_sent_to_login(self):
        for method, url, data in self.admin_requests():
            with self.subTest(method=method, url=url):
                response = self.new_client().open(url, method=method,
                                                  data=data)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], "/login")

    def test_admin_role_is_read_from_the_database_not_the_cookie(self):
        client = self.signed_in("root")
        self.assertEqual(client.get("/admin/users").status_code, 200)

        # Demoted outside the application: the next request is refused
        self.sql("UPDATE users SET role = 'technician' WHERE username = "
                 "'root'")

        self.assertEqual(client.get("/admin/users").status_code, 403)

    def test_users_link_is_shown_only_to_admins(self):
        self.assertIn('href="/admin/users"',
                      self.signed_in("root").get("/").get_data(as_text=True))

        for username in ("alice", "tech"):
            html = self.signed_in(username).get("/").get_data(as_text=True)
            self.assertNotIn('href="/admin/users"', html)

    def test_user_list_shows_accounts_but_never_password_hashes(self):
        hashes = [row[0] for row in self.sql("SELECT password_hash FROM users")]
        html = self.signed_in("root").get("/admin/users").get_data(
            as_text=True
        )

        for username in ("alice", "bob", "tech", "root"):
            self.assertIn(f'data-username="{username}"', html)

        for value in hashes:
            self.assertNotIn(value, html)
        self.assertNotIn("pbkdf2", html)


class TestAdminCsrf(AccountTestCase):

    def test_admin_post_without_csrf_token_is_refused(self):
        client = self.signed_in("root")
        app.config["WTF_CSRF_ENABLED"] = True

        response = client.post(
            f"/admin/users/{self.ids['alice']}/role", data={"role": "admin"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.account("alice")[2], "user")
        self.assertEqual(self.audit(), [])

    def test_admin_post_with_csrf_token_is_accepted(self):
        client = self.signed_in("root")
        app.config["WTF_CSRF_ENABLED"] = True

        html = client.get("/admin/users").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html)[1]

        response = client.post(
            f"/admin/users/{self.ids['alice']}/role",
            data={"role": "technician", "csrf_token": token}
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.account("alice")[2], "technician")


class TestCreateAccount(AccountTestCase):

    def create(self, client=None, **form):
        data = {
            "username": "carol", "role": "technician",
            "password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD,
        }
        data.update(form)
        return (client or self.signed_in("root")).post(
            "/admin/users/new", data=data
        )

    def test_admin_creates_an_active_account_that_can_log_in(self):
        response = self.create()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/admin/users")

        carol = self.account("carol")
        self.assertEqual((carol[2], carol[3]), ("technician", 1))
        self.assertTrue(self.can_log_in("carol", NEW_PASSWORD))

        self.assertEqual(self.audit(), [(
            self.ids["root"], "root", "account_created", carol[0], "carol",
            None, "technician"
        )])

    def test_password_is_stored_hashed_and_never_audited(self):
        self.create()

        stored = self.sql(
            "SELECT password_hash FROM users WHERE username = 'carol'"
        )[0][0]
        self.assertNotEqual(stored, NEW_PASSWORD)

        for row in self.sql("SELECT * FROM audit_events"):
            for value in row:
                self.assertNotIn(NEW_PASSWORD, str(value))
                self.assertNotIn(stored, str(value))

    def test_invalid_input_creates_nothing(self):
        cases = {
            "too short": {"username": "ab"},
            "space": {"username": "carol smith"},
            "non-ascii look-alike": {"username": "аdmin"},
            "leading dot": {"username": ".carol"},
            "too long": {"username": "c" * 65},
            "unknown role": {"role": "superuser"},
            "empty role": {"role": ""},
            "short password": {"password": "short",
                               "confirm_password": "short"},
            "mismatch": {"confirm_password": NEW_PASSWORD + "x"},
            "too long password": {"password": "p" * 1025,
                                  "confirm_password": "p" * 1025},
        }

        users_before = self.sql("SELECT * FROM users ORDER BY id")

        for label, form in cases.items():
            with self.subTest(label):
                response = self.create(**form)
                self.assertEqual(response.status_code, 400)

        self.assertEqual(self.sql("SELECT * FROM users ORDER BY id"),
                         users_before)
        self.assertEqual(self.audit(), [])

    def test_names_differing_only_in_case_are_refused(self):
        response = self.create(username="ALICE")

        self.assertEqual(response.status_code, 400)
        self.assertIn("not available", response.get_data(as_text=True))
        self.assertIsNone(self.account("ALICE"))

    def test_name_used_by_historical_records_is_refused(self):
        # A deleted account's name is still on its ticket
        db.create_ticket("Old", "Request", "Low", "former")

        response = self.create(username="former")

        self.assertEqual(response.status_code, 400)
        self.assertIn("not available", response.get_data(as_text=True))
        self.assertIsNone(self.account("former"))
        self.assertEqual(self.audit(), [])

    def test_rejected_form_keeps_username_but_not_password(self):
        html = self.create(confirm_password="different-passphrase!!"
                           ).get_data(as_text=True)

        self.assertIn('value="carol"', html)
        self.assertNotIn(NEW_PASSWORD, html)


class TestChangeRole(AccountTestCase):

    def change(self, username, role, client=None):
        return (client or self.signed_in("root")).post(
            f"/admin/users/{self.ids[username]}/role", data={"role": role}
        )

    def test_promotion_gives_staff_access_on_the_next_request(self):
        alice = self.signed_in("alice")
        url = f"/tickets/{self.bob_ticket}"
        self.assertEqual(alice.get(url).status_code, 404)

        self.change("alice", "technician")

        self.assertEqual(alice.get(url).status_code, 200)
        self.assertEqual(self.audit(), [(
            self.ids["root"], "root", "role_changed", self.ids["alice"],
            "alice", "user", "technician"
        )])

    def test_demotion_removes_access_and_keeps_assigned_tickets(self):
        db.update_ticket(self.bob_ticket, "In Progress", "tech",
                         actor="root")
        tech = self.signed_in("tech")

        response = self.change("tech", "user")
        follow = self.signed_in("root").get("/admin/users")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(tech.get(f"/tickets/{self.bob_ticket}").status_code,
                         404)
        # History is never rewritten: the ticket keeps its assignee
        self.assertEqual(db.get_ticket(self.bob_ticket)["assigned_to"],
                         "tech")
        self.assertEqual(follow.status_code, 200)

    def test_demotion_warns_about_open_assigned_tickets(self):
        db.update_ticket(self.bob_ticket, "Open", "tech", actor="root")

        client = self.signed_in("root")
        response = self.change("tech", "user", client=client)
        html = client.get(response.headers["Location"]).get_data(
            as_text=True
        )

        self.assertIn("1 open or in-progress ticket(s) are still assigned "
                      "to tech", html)

    def test_admin_cannot_change_own_role(self):
        response = self.change("root", "user")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.account("root")[2], "admin")
        self.assertEqual(self.audit(), [])

    def test_unknown_role_is_refused(self):
        self.change("alice", "superuser")

        self.assertEqual(self.account("alice")[2], "user")
        self.assertEqual(self.audit(), [])

    def test_same_role_records_nothing(self):
        self.change("alice", "user")
        self.assertEqual(self.audit(), [])

    def test_missing_and_oversized_ids_are_404(self):
        client = self.signed_in("root")

        for user_id in (999, 2 ** 63):
            with self.subTest(user_id=user_id):
                response = client.post(f"/admin/users/{user_id}/role",
                                       data={"role": "admin"})
                self.assertEqual(response.status_code, 404)


class TestActivation(AccountTestCase):

    def set_active(self, username, value, client=None):
        return (client or self.signed_in("root")).post(
            f"/admin/users/{self.ids[username]}/active",
            data={"active": value}
        )

    def test_deactivation_ends_sessions_and_keeps_records(self):
        db.add_comment(self.bob_ticket, "bob", "Still jammed")
        bob = self.signed_in("bob")
        tickets_before = self.sql("SELECT * FROM tickets")
        comments_before = self.sql("SELECT * FROM comments")

        self.set_active("bob", "0")

        self.assertEqual(self.account("bob")[3], 0)
        self.assert_signed_out(bob)
        self.assertFalse(self.can_log_in("bob", PASSWORD))
        self.assertEqual(self.sql("SELECT * FROM tickets"), tickets_before)
        self.assertEqual(self.sql("SELECT * FROM comments"), comments_before)
        self.assertEqual(self.audit()[-1][2:5],
                         ("account_deactivated", self.ids["bob"], "bob"))

    def test_reactivation_allows_login_but_not_old_sessions(self):
        bob = self.signed_in("bob")
        self.set_active("bob", "0")
        self.set_active("bob", "1")

        self.assert_signed_out(bob)
        self.assertTrue(self.can_log_in("bob", PASSWORD))
        self.assertEqual([row[2] for row in self.audit()],
                         ["account_deactivated", "account_reactivated"])

    def test_admin_cannot_deactivate_self(self):
        client = self.signed_in("root")
        self.set_active("root", "0", client=client)

        self.assertEqual(self.account("root")[3], 1)
        self.assert_signed_in(client)
        self.assertEqual(self.audit(), [])

    def test_unchanged_status_records_nothing(self):
        self.set_active("bob", "1")
        self.assertEqual(self.audit(), [])

    def test_forged_values_are_400(self):
        for value in ("", "2", "true", "yes"):
            with self.subTest(value=value):
                self.assertEqual(self.set_active("bob", value).status_code,
                                 400)

        self.assertEqual(self.account("bob")[3], 1)


class TestPasswordReset(AccountTestCase):

    def reset(self, username, password=NEW_PASSWORD, confirmation=None,
              client=None):
        return (client or self.signed_in("root")).post(
            f"/admin/users/{self.ids[username]}/password",
            data={"password": password,
                  "confirm_password": confirmation or password}
        )

    def test_reset_sets_new_password_and_ends_sessions(self):
        bob = self.signed_in("bob")

        self.reset("bob")

        self.assert_signed_out(bob)
        self.assertFalse(self.can_log_in("bob", PASSWORD))
        self.assertTrue(self.can_log_in("bob", NEW_PASSWORD))
        self.assertEqual(self.audit(), [(
            self.ids["root"], "root", "password_reset", self.ids["bob"],
            "bob", None, None
        )])

    def test_invalid_passwords_change_nothing(self):
        before = self.account("bob")

        self.reset("bob", confirmation=NEW_PASSWORD + "x")
        self.reset("bob", password="too-short")

        self.assertEqual(self.account("bob"), before)
        self.assertTrue(self.can_log_in("bob", PASSWORD))
        self.assertEqual(self.audit(), [])

    def test_admin_cannot_reset_own_password_here(self):
        self.reset("root")

        self.assertTrue(self.can_log_in("root", PASSWORD))
        self.assertEqual(self.audit(), [])


class TestServiceRules(AccountTestCase):
    """The rules hold even when called without the web layer."""

    def test_non_admin_or_inactive_actor_is_refused(self):
        self.sql("UPDATE users SET is_active = 0 WHERE username = 'root'")

        for actor in ("alice", "tech", "root"):
            with self.subTest(actor=actor):
                with self.assertRaises(accounts.AccountError):
                    accounts.change_role(self.ids[actor], self.ids["bob"],
                                         "admin")
                with self.assertRaises(accounts.AccountError):
                    accounts.create_account(self.ids[actor], "zed", "admin",
                                            NEW_PASSWORD)

        self.assertEqual(self.account("bob")[2], "user")
        self.assertIsNone(self.account("zed"))
        self.assertEqual(self.audit(), [])

    def test_missing_actor_is_refused(self):
        with self.assertRaises(accounts.AccountError):
            accounts.set_active(9999, self.ids["bob"], False)

    def test_last_active_admin_always_remains(self):
        # The only admin cannot remove its own access in any way
        for action in (
            lambda: accounts.change_role(self.ids["root"], self.ids["root"],
                                         "user"),
            lambda: accounts.set_active(self.ids["root"], self.ids["root"],
                                        False),
        ):
            with self.assertRaises(accounts.AccountError):
                action()

        self.assertEqual(self.account("root")[2:4], ("admin", 1))

    def test_one_admin_can_remove_another(self):
        other = self.add_user("root2", "admin")

        accounts.change_role(self.ids["root"], other, "technician")

        self.assertEqual(self.account("root2")[2], "technician")

    def test_failed_change_saves_no_audit_event(self):
        # The UPDATE fails inside the transaction, so the event is rolled
        # back with it
        self.sql("""
            CREATE TRIGGER block_role BEFORE UPDATE OF role ON users
            BEGIN SELECT RAISE(ABORT, 'blocked'); END
        """)

        with self.assertRaises(Exception):
            accounts.change_role(self.ids["root"], self.ids["bob"], "admin")

        self.assertEqual(self.account("bob")[2], "user")
        self.assertEqual(self.audit(), [])

    def test_unknown_audit_action_is_refused(self):
        conn = db.get_db()
        self.addCleanup(conn.close)

        with self.assertRaises(ValueError):
            accounts.record_audit_event(conn, actor="root", action="deleted")


class TestAuditLogPage(AccountTestCase):

    def test_events_are_shown_newest_first(self):
        accounts.change_role(self.ids["root"], self.ids["alice"],
                             "technician")
        accounts.set_active(self.ids["root"], self.ids["bob"], False)

        html = self.signed_in("root").get("/admin/audit").get_data(
            as_text=True
        )

        self.assertLess(html.index("Account deactivated"),
                        html.index("Role changed"))
        self.assertIn("user", html)
        self.assertIn("technician", html)

    def test_list_is_limited(self):
        for _ in range(3):
            accounts.set_active(self.ids["root"], self.ids["bob"], False)
            accounts.set_active(self.ids["root"], self.ids["bob"], True)

        self.assertEqual(len(accounts.list_audit_events(limit=4)), 4)
        self.assertEqual(len(accounts.list_audit_events(limit=0)), 1)
        self.assertEqual(len(accounts.list_audit_events()), 6)


if __name__ == "__main__":
    unittest.main()
