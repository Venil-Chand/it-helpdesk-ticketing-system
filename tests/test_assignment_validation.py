"""
Ticket assignment integrity and username stability.

Every test builds its own temporary database with the real migrations,
signs in through the real login form, and runs the real SQL and
templates. The real helpdesk.db and .env are never opened.

The rules under test:
  - A ticket can be given only to an existing, active technician or admin.
  - The check happens on the server, inside the update's transaction, so
    a hand-made request cannot get around the form.
  - Unassigning is always allowed. Keeping a current assignee who is no
    longer eligible is allowed, so old tickets stay usable and their
    history is never rewritten.
  - A refused update saves nothing at all, not even the status.
  - Usernames cannot be renamed, and a name that ticket records still use
    cannot be given to a new account (migration 7).
"""

import hashlib
import itertools
import os
import re
import sqlite3
import tempfile
import unittest
from html import unescape
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import database as db
import migrations
import setup_accounts
from app import app


PASSWORD = "correct-horse-battery-staple"
FAST_HASH = "pbkdf2:sha256:1000"

REFUSED = "tickets can only be assigned to an active technician"

_addresses = itertools.count(1)


class AssignmentTestCase(unittest.TestCase):
    """A temporary database with one account of every kind."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = os.path.join(temp_dir.name, "assign.db")

        db_patch = patch.object(db, "DB_PATH", self.db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)

        old_config = {
            key: app.config.get(key) for key in ("TESTING", "WTF_CSRF_ENABLED")
        }
        self.addCleanup(app.config.update, old_config)
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

        migrations.migrate(self.db_path)

        for username, role in (
            ("alice", "user"),        # a requester
            ("tech", "technician"),
            ("tech2", "technician"),
            ("root", "admin"),
            ("odd", "superuser"),     # a role the application does not know
            ("gone", "technician"),   # deactivated below
        ):
            self.add_user(username, role)

        self.sql("UPDATE users SET is_active = 0 WHERE username = 'gone'")

        self.ticket_id = db.create_ticket(
            "Laptop", "Will not boot", "High", "alice"
        )

    # -----------------------------------------------
    # Helpers
    # -----------------------------------------------

    def add_user(self, username, role):
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                return conn.execute(
                    "INSERT INTO users (username, password_hash, role) "
                    "VALUES (?, ?, ?)",
                    (username, generate_password_hash(PASSWORD, FAST_HASH),
                     role)
                ).lastrowid
        finally:
            conn.close()

    def sql(self, statement, params=()):
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                return conn.execute(statement, params).fetchall()
        finally:
            conn.close()

    def snapshot(self, ticket_id=None):
        """Everything an update could change on one ticket."""

        ticket_id = ticket_id or self.ticket_id
        return (
            self.sql("SELECT status, assigned_to, updated_at FROM tickets "
                     "WHERE id = ?", (ticket_id,)),
            self.sql("SELECT * FROM ticket_history WHERE ticket_id = ? "
                     "ORDER BY id", (ticket_id,)),
            self.sql("SELECT * FROM ticket_sla WHERE ticket_id = ?",
                     (ticket_id,)),
        )

    def assigned_to(self, ticket_id=None):
        return self.sql("SELECT assigned_to FROM tickets WHERE id = ?",
                        (ticket_id or self.ticket_id,))[0][0]

    def force_assignee(self, username, ticket_id=None):
        """Store an assignee directly, as an older version might have."""

        self.sql("UPDATE tickets SET assigned_to = ? WHERE id = ?",
                 (username, ticket_id or self.ticket_id))

    def client_for(self, username):
        client = app.test_client()
        client.environ_base["REMOTE_ADDR"] = f"10.34.{next(_addresses)}.1"
        response = client.post(
            "/login", data={"username": username, "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 302)
        return client

    def post_update(self, client, assigned_to, status="Open", ticket_id=None):
        return client.post(
            f"/tickets/{ticket_id or self.ticket_id}/update",
            data={"status": status, "assigned_to": assigned_to},
            follow_redirects=True
        )


# ---------------------------------------------------
# 1: DATABASE RULES
# ---------------------------------------------------

class TestAssignmentRulesInDatabase(AssignmentTestCase):
    """db.update_ticket() is the single place the rule is enforced."""

    def test_active_technician_can_be_assigned(self):
        changes = db.update_ticket(self.ticket_id, "Open", "tech",
                                   actor="root")

        self.assertEqual(changes,
                         [("assigned", "assigned_to", None, "tech")])
        self.assertEqual(self.assigned_to(), "tech")

    def test_active_admin_can_be_assigned(self):
        db.update_ticket(self.ticket_id, "Open", "root", actor="tech")

        self.assertEqual(self.assigned_to(), "root")

    def test_ineligible_assignees_are_refused_with_a_reason(self):
        self.sql("DELETE FROM users WHERE username = 'tech2'")

        cases = {
            "nobody": db.ASSIGNEE_MISSING,          # never existed
            "tech2": db.ASSIGNEE_MISSING,           # deleted account
            "gone": db.ASSIGNEE_INACTIVE,           # deactivated account
            "alice": db.ASSIGNEE_WRONG_ROLE,        # ordinary user
            "odd": db.ASSIGNEE_WRONG_ROLE,          # unknown role
        }

        for username, reason in cases.items():
            with self.subTest(username=username):
                with self.assertRaises(db.InvalidAssigneeError) as caught:
                    db.update_ticket(self.ticket_id, "Open", username,
                                     actor="root")

                self.assertEqual(caught.exception.reason, reason)
                self.assertIsNone(self.assigned_to())

    def test_username_match_is_exact(self):
        # "TECH" and " tech" are not the account "tech"
        for lookalike in ("TECH", "Tech", "tech ", "tеch"):
            with self.subTest(lookalike=lookalike):
                with self.assertRaises(db.InvalidAssigneeError):
                    db.update_ticket(self.ticket_id, "Open", lookalike,
                                     actor="root")

    def test_refused_assignment_saves_nothing_not_even_the_status(self):
        before = self.snapshot()

        with self.assertRaises(db.InvalidAssigneeError):
            db.update_ticket(self.ticket_id, "Resolved", "alice",
                             actor="root")

        self.assertEqual(self.snapshot(), before)

    def test_unassigning_is_always_allowed(self):
        db.update_ticket(self.ticket_id, "Open", "tech", actor="root")
        self.sql("DELETE FROM users WHERE username = 'tech'")

        changes = db.update_ticket(self.ticket_id, "Open", None,
                                   actor="root")

        self.assertEqual(changes,
                         [("unassigned", "assigned_to", "tech", None)])
        self.assertIsNone(self.assigned_to())

    def test_ineligible_current_assignee_can_be_kept(self):
        # An older ticket assigned to someone who has since left can still
        # have its status changed, without touching the assignment
        self.force_assignee("former_staff")

        changes = db.update_ticket(self.ticket_id, "In Progress",
                                   "former_staff", actor="root")

        self.assertEqual(
            changes,
            [("status_changed", "status", "Open", "In Progress")]
        )
        self.assertEqual(self.assigned_to(), "former_staff")

    def test_reassigning_away_from_a_former_assignee_keeps_their_name(self):
        self.force_assignee("former_staff")

        db.update_ticket(self.ticket_id, "Open", "tech", actor="root")

        newest = self.sql(
            "SELECT action, old_value, new_value FROM ticket_history "
            "WHERE ticket_id = ? ORDER BY id DESC LIMIT 1", (self.ticket_id,)
        )
        self.assertEqual(newest, [("reassigned", "former_staff", "tech")])

    def test_reassignment_between_technicians(self):
        db.update_ticket(self.ticket_id, "Open", "tech", actor="root")

        changes = db.update_ticket(self.ticket_id, "Open", "tech2",
                                   actor="root")

        self.assertEqual(changes,
                         [("reassigned", "assigned_to", "tech", "tech2")])

    def test_assignee_deactivated_after_page_load_is_refused(self):
        # The form offered "tech", then the account was switched off
        # before the update arrived. The check runs at save time.
        self.sql("UPDATE users SET is_active = 0 WHERE username = 'tech'")

        with self.assertRaises(db.InvalidAssigneeError):
            db.update_ticket(self.ticket_id, "Open", "tech", actor="root")

    def test_demoted_technician_can_no_longer_be_assigned(self):
        self.sql("UPDATE users SET role = 'user' WHERE username = 'tech'")

        with self.assertRaises(db.InvalidAssigneeError):
            db.update_ticket(self.ticket_id, "Open", "tech", actor="root")

    def test_missing_ticket_is_reported_before_the_assignee(self):
        self.assertIsNone(
            db.update_ticket(999, "Open", "nobody", actor="root")
        )


class TestAssignableUsersAndAudit(AssignmentTestCase):

    def test_only_active_staff_are_offered_in_alphabetical_order(self):
        self.assertEqual(db.get_assignable_users(), ["root", "tech", "tech2"])

    def test_audit_lists_each_ineligible_assignment_with_its_reason(self):
        second = db.create_ticket("Phone", "Cracked", "Low", "alice")
        third = db.create_ticket("Mouse", "Broken", "Low", "alice")
        fourth = db.create_ticket("Desk", "Wobbly", "Low", "alice")

        self.force_assignee("former_staff", self.ticket_id)
        self.force_assignee("gone", second)
        self.force_assignee("alice", third)
        self.force_assignee("tech", fourth)  # fine, so not listed

        self.assertEqual(db.find_ineligible_assignments(), [
            (self.ticket_id, "former_staff", db.ASSIGNEE_MISSING),
            (second, "gone", db.ASSIGNEE_INACTIVE),
            (third, "alice", db.ASSIGNEE_WRONG_ROLE),
        ])

    def test_audit_never_changes_the_database(self):
        self.force_assignee("former_staff")

        with open(self.db_path, "rb") as handle:
            before = hashlib.sha256(handle.read()).hexdigest()

        db.find_ineligible_assignments()

        with open(self.db_path, "rb") as handle:
            after = hashlib.sha256(handle.read()).hexdigest()

        self.assertEqual(after, before)
        self.assertEqual(self.assigned_to(), "former_staff")

    def test_audit_command_reports_and_exits_with_failure(self):
        self.force_assignee("former_staff")

        result = app.test_cli_runner().invoke(args=["assignment-audit"])

        self.assertEqual(result.exit_code, 1)
        self.assertIn(f"ticket #{self.ticket_id}: 'former_staff'",
                      result.output)
        self.assertIn(db.ASSIGNEE_MISSING, result.output)

    def test_audit_command_succeeds_when_everything_is_valid(self):
        db.update_ticket(self.ticket_id, "Open", "tech", actor="root")

        result = app.test_cli_runner().invoke(args=["assignment-audit"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Every assigned ticket has an eligible assignee",
                      result.output)


# ---------------------------------------------------
# 2: THE UPDATE ROUTE
# ---------------------------------------------------

class TestAssignmentRoute(AssignmentTestCase):
    """Requests sent straight to the server, bypassing the HTML form."""

    def setUp(self):
        super().setUp()
        self.staff = self.client_for("tech")

    def test_staff_can_assign_a_technician(self):
        response = self.post_update(self.staff, "tech2")

        self.assertIn("Ticket updated.", response.get_data(as_text=True))
        self.assertEqual(self.assigned_to(), "tech2")

    def test_staff_can_assign_themselves(self):
        self.post_update(self.staff, "tech")

        self.assertEqual(self.assigned_to(), "tech")

    def test_surrounding_spaces_are_trimmed_like_other_fields(self):
        self.post_update(self.staff, "  tech2  ")

        self.assertEqual(self.assigned_to(), "tech2")

    def test_empty_assignee_means_unassigned(self):
        db.update_ticket(self.ticket_id, "Open", "tech2", actor="root")

        self.post_update(self.staff, "")

        self.assertIsNone(self.assigned_to())

    def test_ineligible_or_tampered_assignees_change_nothing(self):
        self.sql("DELETE FROM users WHERE username = 'tech2'")
        before = self.snapshot()

        for value in ("nobody", "tech2", "gone", "alice", "odd", "TECH",
                      "x'); DELETE FROM tickets; --", "tеch"):
            with self.subTest(value=value):
                response = self.post_update(self.staff, value,
                                            status="Closed")

                self.assertEqual(response.status_code, 200)
                self.assertIn(REFUSED, response.get_data(as_text=True))
                self.assertEqual(self.snapshot(), before)

    def test_refusal_message_does_not_reveal_why(self):
        # Missing, deactivated and wrong-role accounts look the same, so the
        # form cannot be used to discover which accounts exist
        messages = set()

        for value in ("nobody", "gone", "alice"):
            text = self.post_update(self.staff, value).get_data(as_text=True)
            messages.add(re.search(r"Ticket not updated[^<]*", text).group())

            for reason in (db.ASSIGNEE_MISSING, db.ASSIGNEE_INACTIVE,
                           db.ASSIGNEE_WRONG_ROLE):
                self.assertNotIn(reason, text)

        self.assertEqual(len(messages), 1)

    def test_malformed_assignees_are_rejected_before_the_database(self):
        before = self.snapshot()

        for value in ("a" * 151, "tech\nroot", "tech\x00"):
            with self.subTest(value=value):
                response = self.staff.post(
                    f"/tickets/{self.ticket_id}/update",
                    data={"status": "Open", "assigned_to": value}
                )

                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.snapshot(), before)

    def test_regular_user_cannot_assign_even_with_a_valid_name(self):
        requester = self.client_for("alice")
        before = self.snapshot()

        requester.post(f"/tickets/{self.ticket_id}/update",
                       data={"status": "Open", "assigned_to": "tech"})

        self.assertEqual(self.snapshot(), before)

    def test_signed_out_visitor_cannot_assign(self):
        before = self.snapshot()

        response = app.test_client().post(
            f"/tickets/{self.ticket_id}/update",
            data={"status": "Open", "assigned_to": "tech"}
        )

        self.assertIn("/login", response.headers["Location"])
        self.assertEqual(self.snapshot(), before)

    def test_deactivated_staff_session_cannot_assign(self):
        self.sql("UPDATE users SET is_active = 0 WHERE username = 'tech'")
        before = self.snapshot()

        response = self.staff.post(
            f"/tickets/{self.ticket_id}/update",
            data={"status": "Open", "assigned_to": "tech2"}
        )

        self.assertIn("/login", response.headers["Location"])
        self.assertEqual(self.snapshot(), before)

    def test_assignment_still_requires_a_csrf_token(self):
        app.config["WTF_CSRF_ENABLED"] = True
        before = self.snapshot()

        response = self.staff.post(
            f"/tickets/{self.ticket_id}/update",
            data={"status": "Open", "assigned_to": "tech2"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.snapshot(), before)

    def test_actor_comes_from_the_session_not_the_form(self):
        self.staff.post(
            f"/tickets/{self.ticket_id}/update",
            data={"status": "Open", "assigned_to": "tech2",
                  "actor": "root", "username": "root"}
        )

        actors = self.sql("SELECT actor FROM ticket_history "
                          "WHERE action = 'assigned'")
        self.assertEqual(actors, [("tech",)])


# ---------------------------------------------------
# 3: THE TICKET PAGE
# ---------------------------------------------------

class TestAssignmentForm(AssignmentTestCase):

    def setUp(self):
        super().setUp()
        self.staff = self.client_for("root")

    def page(self, client=None):
        return (client or self.staff).get(
            f"/tickets/{self.ticket_id}"
        ).get_data(as_text=True)

    def options(self, html):
        select = re.search(
            r'<select[^>]*name="assigned_to".*?</select>', html, re.S
        ).group()
        return [
            (unescape(value), "selected" in attrs)
            for value, attrs in re.findall(
                r'<option value="([^"]*)"([^>]*)>', select
            )
        ]

    def test_only_eligible_staff_are_offered(self):
        self.assertEqual(
            [value for value, _ in self.options(self.page())],
            ["", "root", "tech", "tech2"]
        )

    def test_current_eligible_assignee_is_selected(self):
        db.update_ticket(self.ticket_id, "Open", "tech2", actor="root")

        selected = [v for v, chosen in self.options(self.page()) if chosen]

        self.assertEqual(selected, ["tech2"])

    def test_ineligible_assignee_stays_selected_and_is_flagged(self):
        self.force_assignee("former_staff")

        html = self.page()
        selected = [v for v, chosen in self.options(html) if chosen]

        self.assertEqual(selected, ["former_staff"])
        self.assertIn("(current, no longer eligible)", html)
        self.assertIn('data-testid="assignee-not-eligible"', html)
        self.assertIn(db.ASSIGNEE_MISSING, html)

    def test_saving_a_status_change_keeps_a_former_assignee(self):
        # Regression: the browser used to fall back to "Unassigned" when
        # the current assignee was missing from the list, so saving a
        # status change silently removed the assignment
        self.force_assignee("former_staff")
        selected = [v for v, c in self.options(self.page()) if c][0]

        self.post_update(self.staff, selected, status="In Progress")

        self.assertEqual(self.assigned_to(), "former_staff")
        actions = self.sql("SELECT action FROM ticket_history "
                           "WHERE ticket_id = ? ORDER BY id",
                           (self.ticket_id,))
        self.assertEqual(actions, [("created",), ("status_changed",)])

    def test_eligible_assignee_shows_no_warning(self):
        db.update_ticket(self.ticket_id, "Open", "tech", actor="root")

        self.assertNotIn("assignee-not-eligible", self.page())

    def test_requester_sees_no_form_and_no_account_details(self):
        self.force_assignee("gone")

        html = self.page(self.client_for("alice"))

        self.assertNotIn('name="assigned_to"', html)
        self.assertNotIn("assignee-not-eligible", html)
        self.assertNotIn(db.ASSIGNEE_INACTIVE, html)


# ---------------------------------------------------
# 4: USERNAMES CANNOT BE REUSED OR RENAMED (MIGRATION 7)
# ---------------------------------------------------

class TestUsernameStability(AssignmentTestCase):

    def assert_name_reserved(self, username):
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.add_user(username, "user")

        self.assertIn("already used by existing ticket records",
                      str(caught.exception))

    def test_deleted_requester_name_cannot_be_given_to_someone_new(self):
        # Otherwise the newcomer would sign in and see alice's tickets
        self.sql("DELETE FROM users WHERE username = 'alice'")

        self.assert_name_reserved("alice")
        self.assertEqual(
            self.sql("SELECT created_by FROM tickets WHERE id = ?",
                     (self.ticket_id,)),
            [("alice",)]
        )

    def test_every_kind_of_ticket_record_reserves_a_name(self):
        db.add_comment(self.ticket_id, "tech", "Looking now")       # author
        db.update_ticket(self.ticket_id, "Open", "tech2", actor="root")
        db.update_ticket(self.ticket_id, "Open", None, actor="root")

        for username in ("tech", "tech2", "root"):
            with self.subTest(username=username):
                self.sql("DELETE FROM users WHERE username = ?", (username,))
                self.assert_name_reserved(username)

    def test_name_kept_only_in_history_is_reserved(self):
        # tech2 appears only as old_value of an "unassigned" event
        db.update_ticket(self.ticket_id, "Open", "tech2", actor="root")
        db.update_ticket(self.ticket_id, "Open", None, actor="root")
        self.sql("DELETE FROM users WHERE username = 'tech2'")

        self.assertEqual(
            self.sql("SELECT COUNT(*) FROM tickets "
                     "WHERE assigned_to = 'tech2'"), [(0,)]
        )
        self.assert_name_reserved("tech2")

    def test_unused_names_can_still_be_created_and_recreated(self):
        self.add_user("newstarter", "technician")
        self.sql("DELETE FROM users WHERE username = 'newstarter'")

        # Never used in any ticket, so nobody can inherit anything
        self.add_user("newstarter", "technician")

    def test_accounts_can_still_be_deleted_and_deactivated(self):
        self.sql("UPDATE users SET is_active = 0 WHERE username = 'alice'")
        self.sql("DELETE FROM users WHERE username = 'alice'")

        self.assertEqual(
            self.sql("SELECT COUNT(*) FROM users WHERE username = 'alice'"),
            [(0,)]
        )

    def test_usernames_cannot_be_renamed(self):
        for old, new in (("alice", "alice2"), ("tech", "root2")):
            with self.subTest(old=old):
                with self.assertRaises(sqlite3.IntegrityError) as caught:
                    self.sql("UPDATE users SET username = ? "
                             "WHERE username = ?", (new, old))

                self.assertIn("usernames cannot be changed",
                              str(caught.exception))

    def test_other_account_changes_still_work(self):
        self.sql("UPDATE users SET role = 'admin', is_active = 0, "
                 "password_hash = 'x' WHERE username = 'tech'")
        self.sql("UPDATE users SET username = username "
                 "WHERE username = 'tech'")

        self.assertEqual(
            self.sql("SELECT role, is_active FROM users "
                     "WHERE username = 'tech'"),
            [("admin", 0)]
        )

    def test_setup_script_refuses_a_reserved_first_admin_name(self):
        db.create_ticket("Old", "From the previous admin", "Low", "admin")
        self.sql("DELETE FROM users")

        # A valid password, so the reserved name is what gets refused
        with patch("setup_accounts.getpass",
                   return_value="quiet-orbit-lantern-58"), \
                patch("builtins.print") as printed:
            result = setup_accounts.setup_account()

        self.assertIs(result, False)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM users"), [(0,)])
        self.assertIn("No account was created",
                      " ".join(str(c) for c in printed.call_args_list))


class TestStableUsernameMigration(unittest.TestCase):
    """Migration 7 adds two triggers and changes no data."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.path = os.path.join(temp_dir.name, "v6.db")

    def rows(self, sql):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def build_version_six_with_history(self):
        migrations.migrate(self.path, migrations=migrations.MIGRATIONS[:6])

        conn = sqlite3.connect(self.path)
        with conn:
            conn.execute("INSERT INTO users (username, password_hash, role) "
                         "VALUES ('tech', 'h', 'technician')")
            conn.execute(
                "INSERT INTO tickets (title, description, created_by, "
                "assigned_to, created_at) VALUES ('Old', 'Ticket', "
                "'left_company', 'also_left', '2026-01-01 09:00:00')"
            )
            conn.execute(
                "INSERT INTO ticket_sla (ticket_id, response_due_at, "
                "first_response_at) VALUES (1, '2026-01-01T10:00:00+00:00', "
                "'2026-01-01T09:30:00+00:00')"
            )
        conn.close()

    def test_upgrade_keeps_every_row_and_reserves_historical_names(self):
        self.build_version_six_with_history()
        data = {
            table: self.rows(f"SELECT * FROM {table} ORDER BY 1")
            for table in ("users", "tickets", "ticket_sla", "comments",
                          "ticket_history")
        }

        result = migrations.migrate(self.path)

        self.assertEqual([m.version for m in result.applied], [7, 8])
        for table, before in data.items():
            with self.subTest(table=table):
                self.assertEqual(
                    self.rows(f"SELECT * FROM {table} ORDER BY 1"), before
                )

        # The pre-existing invalid assignment is kept, not rewritten
        self.assertEqual(self.rows("SELECT assigned_to FROM tickets"),
                         [("also_left",)])

        conn = sqlite3.connect(self.path)
        try:
            for name in ("left_company", "also_left"):
                with self.subTest(name=name):
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(
                            "INSERT INTO users (username, password_hash) "
                            "VALUES (?, 'h')", (name,)
                        )
        finally:
            conn.close()

    def test_missing_or_weakened_trigger_makes_the_schema_incompatible(self):
        migrations.migrate(self.path)

        conn = sqlite3.connect(self.path)
        with conn:
            conn.execute("DROP TRIGGER users_username_immutable")
            conn.execute("DROP TRIGGER users_no_historical_username_reuse")
            # Same name, but it no longer checks comment authors
            conn.execute("""
                CREATE TRIGGER users_no_historical_username_reuse
                BEFORE INSERT ON users
                WHEN EXISTS (SELECT 1 FROM tickets
                             WHERE created_by = NEW.username)
                BEGIN SELECT RAISE(ABORT, 'weak'); END
            """)
        conn.close()

        status = migrations.schema_status(self.path)

        self.assertEqual(status.state, "incompatible")
        self.assertIn("missing trigger users_username_immutable",
                      status.problems)
        self.assertIn(
            "trigger users_no_historical_username_reuse has unexpected "
            "behaviour", status.problems
        )


if __name__ == "__main__":
    unittest.main()
