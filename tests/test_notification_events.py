"""
Which events queue notifications, for whom, and in which transaction.

Covers every event type, the recipient rules in notifications/events.py
(active accounts only, staff-only events only for staff, the actor is not
told about their own ticket actions), invalid historical assignees,
duplicate events, rollback behaviour and sensitive-data exclusion.

Temporary databases only; nothing is delivered here.
"""

import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

import accounts
import database as db
import sla
from helpdesk import auth
from notifications import events

from account_helpers import NEW_PASSWORD, PASSWORD
from notification_helpers import NotificationTestCase

TITLE = "Laptop stolen with VPN token 4471"
DESCRIPTION = "My password is hunter2-do-not-share"
COMMENT = "Private note: the safe code is 9911"


class TestRuleConstants(unittest.TestCase):

    def test_staff_roles_match_the_ticket_rules(self):
        # Kept as a copy to avoid an import cycle; must never drift
        self.assertEqual(events.STAFF_ROLES, db.ASSIGNABLE_ROLES)
        self.assertEqual(events.STAFF_ROLES, auth.STAFF_ROLES)
        self.assertEqual(events.STAFF_ROLES, sla.RESPONDER_ROLES)

    def test_every_event_has_a_subject(self):
        from notifications import render
        self.assertEqual(set(render.SUBJECTS), events.EVENT_TYPES)

    def test_event_types_match_the_migration_check(self):
        import migrations
        self.assertEqual(set(migrations._NOTIFICATION_EVENT_TYPES),
                         events.EVENT_TYPES)
        self.assertEqual(
            set(migrations._NOTIFICATION_REASONS),
            {events.REQUESTER, events.ASSIGNEE, events.PREVIOUS_ASSIGNEE,
             events.STAFF, events.ACCOUNT_OWNER}
        )


# ---------------------------------------------------
# TICKET CREATION
# ---------------------------------------------------

class TestTicketCreated(NotificationTestCase):

    def test_requester_and_every_active_staff_member(self):
        self.add_user("tech2", "technician")
        self.add_user("gone", "technician", active=0)
        self.clear_queue()

        ticket = db.create_ticket(TITLE, DESCRIPTION, "High", "alice")

        self.assertEqual(self.summary(), [
            ("ticket_created", "alice", "requester"),
            ("ticket_created", "tech", "staff"),
            ("ticket_created", "root", "staff"),
            ("ticket_created", "tech2", "staff"),
        ])
        self.assertTrue(all(row[4] == ticket for row in self.notifications()))
        self.assertEqual({row[6] for row in self.notifications()},
                         {"pending"})

    def test_staff_requester_is_told_once(self):
        self.clear_queue()
        db.create_ticket("t", "d", "Low", "tech")

        self.assertEqual(self.summary(), [
            ("ticket_created", "tech", "requester"),
            ("ticket_created", "root", "staff"),
        ])

    def test_ordinary_users_never_hear_about_other_peoples_tickets(self):
        self.clear_queue()
        db.create_ticket("t", "d", "Low", "alice")

        self.assertNotIn("bob", [r[1] for r in self.summary()])

    def test_deactivated_requester_gets_no_confirmation(self):
        self.sql("UPDATE users SET is_active = 0 WHERE username = 'alice'")
        self.clear_queue()
        db.create_ticket("t", "d", "Low", "alice")

        self.assertNotIn("alice", [r[1] for r in self.summary()])

    def test_payload_has_only_reviewed_fields(self):
        self.clear_queue()
        ticket = db.create_ticket(TITLE, DESCRIPTION, "Critical", "alice")

        for row in self.notifications():
            self.assertEqual(self.payload(row[0]), {
                "ticket_id": ticket, "priority": "Critical", "actor": "alice"
            })


# ---------------------------------------------------
# ASSIGNMENT
# ---------------------------------------------------

class TestAssignment(NotificationTestCase):

    def setUp(self):
        super().setUp()
        self.add_user("tech2", "technician")
        self.clear_queue()

    def test_new_assignee_is_told(self):
        db.update_ticket(self.bob_ticket, "Open", "tech", actor="root")

        self.assertEqual(self.summary(),
                         [("ticket_assigned", "tech", "assignee")])

    def test_assigning_to_yourself_sends_nothing(self):
        db.update_ticket(self.bob_ticket, "Open", "tech", actor="tech")
        self.assertEqual(self.summary(), [])

    def test_reassignment_tells_both_technicians(self):
        db.update_ticket(self.bob_ticket, "Open", "tech", actor="root")
        self.clear_queue()

        db.update_ticket(self.bob_ticket, "Open", "tech2", actor="root")

        self.assertEqual(self.summary(), [
            ("ticket_assigned", "tech2", "assignee"),
            ("ticket_unassigned", "tech", "previous_assignee"),
        ])

    def test_unassignment_tells_the_previous_assignee(self):
        db.update_ticket(self.bob_ticket, "Open", "tech", actor="root")
        self.clear_queue()

        db.update_ticket(self.bob_ticket, "Open", None, actor="root")

        self.assertEqual(self.summary(), [
            ("ticket_unassigned", "tech", "previous_assignee"),
        ])

    def test_invalid_historical_assignee_is_never_notified(self):
        # Like real ticket #1: "Technician B" was never an account. The
        # name stays on the ticket; reassigning must not try to email it.
        self.sql("UPDATE tickets SET assigned_to = 'Technician B' "
                 "WHERE id = ?", (self.bob_ticket,))

        db.update_ticket(self.bob_ticket, "Open", "tech", actor="root")

        self.assertEqual(self.summary(),
                         [("ticket_assigned", "tech", "assignee")])
        self.assertEqual(
            db.get_ticket(self.bob_ticket)["assigned_to"], "tech"
        )

    def test_demoted_or_deactivated_previous_assignee_is_skipped(self):
        db.update_ticket(self.bob_ticket, "Open", "tech", actor="root")
        self.clear_queue()

        for change in ("UPDATE users SET role = 'user' WHERE username='tech'",
                       "UPDATE users SET is_active = 0, role = 'technician' "
                       "WHERE username = 'tech'"):
            with self.subTest(change=change):
                self.sql("UPDATE tickets SET assigned_to = 'tech' "
                         "WHERE id = ?", (self.bob_ticket,))
                self.sql(change)
                self.clear_queue()

                db.update_ticket(self.bob_ticket, "Open", "tech2",
                                 actor="root")

                self.assertEqual(self.summary(), [
                    ("ticket_assigned", "tech2", "assignee"),
                ])

    def test_refused_assignment_queues_nothing(self):
        with self.assertRaises(db.InvalidAssigneeError):
            db.update_ticket(self.bob_ticket, "Resolved", "alice",
                             actor="root")

        # Neither the assignment nor the status change was saved
        self.assertEqual(self.summary(), [])

    def test_keeping_the_assignee_and_status_sends_nothing(self):
        db.update_ticket(self.bob_ticket, "Open", "tech", actor="root")
        self.clear_queue()

        self.assertEqual(
            db.update_ticket(self.bob_ticket, "Open", "tech", actor="root"),
            []
        )
        self.assertEqual(self.summary(), [])


# ---------------------------------------------------
# STATUS CHANGES
# ---------------------------------------------------

class TestResolution(NotificationTestCase):

    def setUp(self):
        super().setUp()
        self.clear_queue()

    def test_resolved_and_closed_tell_the_requester(self):
        db.update_ticket(self.bob_ticket, "Resolved", None, actor="tech")
        db.update_ticket(self.bob_ticket, "Closed", None, actor="tech")

        self.assertEqual(self.summary(), [
            ("ticket_resolved", "bob", "requester"),
            ("ticket_closed", "bob", "requester"),
        ])
        first, second = [row[0] for row in self.notifications()]
        self.assertEqual(
            self.payload(first),
            {"ticket_id": self.bob_ticket, "priority": "Low",
             "actor": "tech", "old_status": "Open",
             "new_status": "Resolved"}
        )
        self.assertEqual(self.payload(second)["new_status"], "Closed")

    def test_in_progress_sends_nothing(self):
        db.update_ticket(self.bob_ticket, "In Progress", None, actor="tech")
        self.assertEqual(self.summary(), [])

    def test_requester_who_closes_their_own_ticket_is_not_told(self):
        db.update_ticket(self.bob_ticket, "Closed", None, actor="bob")
        self.assertEqual(self.summary(), [])

    def test_status_and_assignment_in_one_save(self):
        db.update_ticket(self.bob_ticket, "Resolved", "tech", actor="root")

        self.assertEqual(self.summary(), [
            ("ticket_resolved", "bob", "requester"),
            ("ticket_assigned", "tech", "assignee"),
        ])

    def test_legacy_ticket_without_requester(self):
        self.sql("UPDATE tickets SET created_by = NULL WHERE id = ?",
                 (self.bob_ticket,))
        db.update_ticket(self.bob_ticket, "Closed", None, actor="tech")
        self.assertEqual(self.summary(), [])


# ---------------------------------------------------
# COMMENTS
# ---------------------------------------------------

class TestComments(NotificationTestCase):

    def setUp(self):
        super().setUp()
        db.update_ticket(self.bob_ticket, "Open", "tech", actor="root")
        self.clear_queue()

    def test_staff_comment_reaches_requester_and_assignee(self):
        db.add_comment(self.bob_ticket, "root", COMMENT)

        self.assertEqual(self.summary(), [
            ("staff_comment", "bob", "requester"),
            ("staff_comment", "tech", "assignee"),
        ])

    def test_assignee_comment_reaches_only_the_requester(self):
        db.add_comment(self.bob_ticket, "tech", COMMENT)
        self.assertEqual(self.summary(),
                         [("staff_comment", "bob", "requester")])

    def test_requester_comment_reaches_the_assignee(self):
        db.add_comment(self.bob_ticket, "bob", COMMENT)
        self.assertEqual(self.summary(),
                         [("requester_comment", "tech", "assignee")])

    def test_requester_comment_on_unassigned_ticket_sends_nothing(self):
        db.update_ticket(self.bob_ticket, "Open", None, actor="root")
        self.clear_queue()

        db.add_comment(self.bob_ticket, "bob", COMMENT)
        self.assertEqual(self.summary(), [])

    def test_requester_comment_with_ineligible_assignee_sends_nothing(self):
        self.sql("UPDATE tickets SET assigned_to = 'Technician B' "
                 "WHERE id = ?", (self.bob_ticket,))
        db.add_comment(self.bob_ticket, "bob", COMMENT)
        self.assertEqual(self.summary(), [])

    def test_staff_requester_on_own_ticket_is_a_requester_comment(self):
        ticket = db.create_ticket("t", "d", "Low", "root")
        db.update_ticket(ticket, "Open", "tech", actor="root")
        self.clear_queue()

        db.add_comment(ticket, "root", COMMENT)

        self.assertEqual(self.summary(),
                         [("requester_comment", "tech", "assignee")])

    def test_staff_requester_who_is_also_assignee_is_told_once(self):
        ticket = db.create_ticket("t", "d", "Low", "tech")
        db.update_ticket(ticket, "Open", "tech", actor="root")
        self.clear_queue()

        db.add_comment(ticket, "root", COMMENT)

        self.assertEqual(self.summary(),
                         [("staff_comment", "tech", "requester")])

    def test_comment_text_is_never_queued(self):
        db.add_comment(self.bob_ticket, "root", COMMENT)

        for row in self.sql("SELECT * FROM notifications"):
            self.assertNotIn("9911", " ".join(str(v) for v in row))

    def test_former_staff_comment_is_not_treated_as_staff(self):
        # A demoted account commenting (it can no longer see the ticket,
        # but database functions may still be called) never emails
        self.add_user("exstaff", "user")
        db.add_comment(self.bob_ticket, "exstaff", COMMENT)
        self.assertEqual(self.summary(), [])


# ---------------------------------------------------
# ACCOUNT SECURITY EVENTS
# ---------------------------------------------------

class TestAccountEvents(NotificationTestCase):

    def setUp(self):
        super().setUp()
        self.clear_queue()

    def test_own_password_change_tells_the_owner(self):
        client = self.signed_in("alice")
        response = client.post("/account/password", data={
            "current_password": PASSWORD, "new_password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
        })
        self.assertEqual(response.status_code, 302)

        self.assertEqual(self.summary(),
                         [("password_changed", "alice", "account_owner")])
        row_id = self.notifications()[0][0]
        self.assertEqual(self.payload(row_id), {"changed_by": "owner"})

    def test_admin_reset_tells_the_owner_without_the_password(self):
        accounts.reset_password(self.ids["root"], self.ids["bob"],
                                "temporary-phrase-ocean-42")

        self.assertEqual(self.summary(),
                         [("password_reset", "bob", "account_owner")])
        everything = " ".join(
            str(v) for row in self.sql("SELECT * FROM notifications")
            for v in row
        )
        self.assertNotIn("temporary-phrase-ocean-42", everything)
        self.assertNotIn("pbkdf2", everything)
        self.assertNotIn("root", everything)  # which admin is not revealed

    def test_operator_reset_does_not_reveal_the_os_account(self):
        accounts.operator_reset_password(
            "bob", "temporary-phrase-ocean-42", operator="opsuser"
        )

        self.assertEqual(self.summary(),
                         [("password_reset", "bob", "account_owner")])
        self.assertEqual(self.payload(self.notifications()[0][0]),
                         {"changed_by": "administrator"})

    def test_role_change_tells_the_owner(self):
        accounts.change_role(self.ids["root"], self.ids["alice"],
                             "technician")

        self.assertEqual(self.summary(),
                         [("role_changed", "alice", "account_owner")])
        self.assertEqual(self.payload(self.notifications()[0][0]), {
            "changed_by": "administrator", "old_role": "user",
            "new_role": "technician",
        })

    def test_deactivation_sends_nothing_reactivation_tells_the_owner(self):
        accounts.set_active(self.ids["root"], self.ids["alice"], False)
        self.assertEqual(self.summary(), [])

        accounts.set_active(self.ids["root"], self.ids["alice"], True)
        self.assertEqual(self.summary(),
                         [("account_reactivated", "alice", "account_owner")])

    def test_role_change_of_deactivated_account_sends_nothing(self):
        accounts.set_active(self.ids["root"], self.ids["alice"], False)
        accounts.change_role(self.ids["root"], self.ids["alice"],
                             "technician")
        self.assertEqual(self.summary(), [])

    def test_failed_logins_never_notify(self):
        for _ in range(3):
            self.assertFalse(self.can_log_in("alice", "wrong-password-xx"))
        self.assertEqual(self.summary(), [])

    def test_refused_admin_action_queues_nothing(self):
        with self.assertRaises(accounts.AccountError):
            accounts.change_role(self.ids["tech"], self.ids["alice"],
                                 "admin")
        self.assertEqual(self.summary(), [])


# ---------------------------------------------------
# DUPLICATES AND TRANSACTIONS
# ---------------------------------------------------

class TestDeduplication(NotificationTestCase):

    def test_running_a_hook_twice_queues_each_message_once(self):
        self.clear_queue()
        ticket = db.create_ticket("t", "d", "Low", "alice")
        before = self.summary()

        history_id = self.sql(
            "SELECT id FROM ticket_history WHERE ticket_id = ? "
            "AND action = 'created'", (ticket,)
        )[0][0]

        with closing(db.get_db()) as conn, conn:
            added = events.ticket_created(conn, ticket_id=ticket,
                                          history_id=history_id)

        self.assertEqual(added, 0)
        self.assertEqual(self.summary(), before)

    def test_same_event_type_twice_is_two_messages(self):
        # Two real comments are two events, each notified once
        db.update_ticket(self.bob_ticket, "Open", "tech", actor="root")
        self.clear_queue()

        db.add_comment(self.bob_ticket, "bob", "first")
        db.add_comment(self.bob_ticket, "bob", "second")

        self.assertEqual(len(self.summary()), 2)
        keys = [row[10] for row in self.notifications()]
        self.assertEqual(len(set(keys)), 2)

    def test_database_enforces_unique_dedupe_key(self):
        row = self.sql("SELECT dedupe_key FROM notifications LIMIT 1")[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql(
                "INSERT INTO notifications (event_type, dedupe_key, "
                "recipient_user_id, recipient_username, recipient_reason, "
                "created_at, updated_at, next_attempt_at) VALUES "
                "('ticket_created', ?, 1, 'x', 'staff', 'a', 'a', 'a')",
                (row[0],)
            )


class TestTransactions(NotificationTestCase):

    def setUp(self):
        super().setUp()
        self.clear_queue()

    def test_rolled_back_ticket_leaves_no_notification(self):
        # An unknown priority makes create_ticket fail after the INSERT
        with self.assertRaises(ValueError):
            db.create_ticket("t", "d", "Unknown", "alice")

        self.assertEqual(self.summary(), [])
        self.assertEqual(
            self.sql("SELECT COUNT(*) FROM tickets")[0][0], 1
        )

    def test_failure_after_queueing_rolls_both_back(self):
        # The rows really are queued, then the comment's transaction fails
        # before COMMIT: the comment and its notifications vanish together
        real_hook = events.comment_added
        queued = []

        def queue_then_fail(conn, **kwargs):
            queued.append(real_hook(conn, **kwargs))
            raise sqlite3.OperationalError("disk I/O error")

        with patch.object(db.notification_events, "comment_added",
                          queue_then_fail):
            with self.assertRaises(sqlite3.OperationalError):
                db.add_comment(self.bob_ticket, "tech", COMMENT)

        self.assertEqual(queued, [1])
        self.assertEqual(self.summary(), [])
        self.assertEqual(db.get_comments(self.bob_ticket), [])

    def test_failure_before_queueing_queues_nothing(self):
        with patch.object(db, "_record_sla_response",
                          side_effect=sqlite3.OperationalError("boom")):
            with self.assertRaises(sqlite3.OperationalError):
                db.add_comment(self.bob_ticket, "tech", COMMENT)

        self.assertEqual(self.summary(), [])
        self.assertEqual(db.get_comments(self.bob_ticket), [])

    def test_notification_failure_never_undoes_the_ticket(self):
        with self.assertLogs("notifications.events", "ERROR") as logs, \
                patch.object(events, "_queue",
                             side_effect=RuntimeError("queue broken")):
            ticket = db.create_ticket(TITLE, DESCRIPTION, "Low", "alice")
            db.add_comment(ticket, "tech", COMMENT)
            changes = db.update_ticket(ticket, "Resolved", "tech",
                                       actor="root")
            accounts.change_role(self.ids["root"], self.ids["alice"],
                                 "technician")

        # Every change was saved, with its history and audit event
        self.assertEqual(db.get_ticket(ticket)["status"], "Resolved")
        self.assertEqual(len(db.get_comments(ticket)), 1)
        self.assertEqual(len(changes), 2)
        self.assertEqual(len(db.get_ticket_history(ticket)), 4)
        self.assertEqual(self.account("alice")[2], "technician")

        self.assertEqual(self.summary(), [])
        self.assertEqual(len(logs.records), 4)

        # The log says what failed, never the ticket contents
        text = "\n".join(logs.output)
        for secret in (TITLE, DESCRIPTION, COMMENT):
            self.assertNotIn(secret, text)

    def test_partial_failure_rolls_back_only_the_notifications(self):
        calls = {"n": 0}
        real_queue = events._queue

        def fail_second(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("second insert fails")
            return real_queue(*args, **kwargs)

        with self.assertLogs("notifications.events", "ERROR"), \
                patch.object(events, "_queue", side_effect=fail_second):
            ticket = db.create_ticket("t", "d", "Low", "alice")

        # The first row was inside the savepoint too: all or none
        self.assertEqual(self.summary(), [])
        self.assertIsNotNone(db.get_ticket(ticket))

    def test_unknown_payload_field_is_refused(self):
        with self.assertRaises(ValueError):
            events._clean_payload({"title": "secret"})

        with self.assertRaises(ValueError):
            events._clean_payload({"actor": object()})

    def test_long_payload_values_are_shortened(self):
        payload = events._clean_payload({"actor": "x" * 500})
        self.assertIn("x" * events.MAX_PAYLOAD_VALUE_LENGTH + '"', payload)
        self.assertNotIn("x" * (events.MAX_PAYLOAD_VALUE_LENGTH + 1),
                         payload)


# ---------------------------------------------------
# THROUGH THE WEB ROUTES (real sign-in, ownership, CSRF)
# ---------------------------------------------------

class TestRoutes(NotificationTestCase):

    def setUp(self):
        super().setUp()
        self.clear_queue()

    def test_new_ticket_route_queues_after_commit(self):
        client = self.signed_in("alice")
        response = client.post("/tickets/new", data={
            "title": TITLE, "description": DESCRIPTION, "priority": "High"
        })
        self.assertEqual(response.status_code, 302)

        self.assertEqual(
            [r[1] for r in self.summary()], ["alice", "tech", "root"]
        )

    def test_comment_on_someone_elses_ticket_queues_nothing(self):
        client = self.signed_in("alice")
        response = client.post(f"/tickets/{self.bob_ticket}/comment",
                               data={"content": COMMENT})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.summary(), [])

    def test_ordinary_user_cannot_trigger_assignment_notifications(self):
        client = self.signed_in("bob")
        response = client.post(f"/tickets/{self.bob_ticket}/update",
                               data={"status": "Closed",
                                     "assigned_to": "tech"})
        # staff_required refuses by redirecting back to the ticket
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.get_ticket(self.bob_ticket)["status"], "Open")
        self.assertEqual(self.summary(), [])

    def test_staff_update_route_queues(self):
        client = self.signed_in("root")
        client.post(f"/tickets/{self.bob_ticket}/update",
                    data={"status": "Resolved", "assigned_to": "tech"})

        self.assertEqual(self.summary(), [
            ("ticket_resolved", "bob", "requester"),
            ("ticket_assigned", "tech", "assignee"),
        ])

    def test_csrf_still_required(self):
        from app import app
        app.config["WTF_CSRF_ENABLED"] = True
        client = self.signed_in_without_csrf("alice")

        response = client.post("/tickets/new", data={
            "title": "t", "description": "d", "priority": "Low"
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.summary(), [])

    def signed_in_without_csrf(self, username):
        """Sign in with CSRF off, then switch it back on for the test."""

        from app import app
        app.config["WTF_CSRF_ENABLED"] = False
        client = self.signed_in(username)
        app.config["WTF_CSRF_ENABLED"] = True
        return client


if __name__ == "__main__":
    unittest.main()
