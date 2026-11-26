"""
End-to-end tests in a real headless Chromium.

Each test class gets its own disposable helpdesk (see harness.py): a new
temporary database, the real application on a loopback port, the real
operator commands. Before every test the web process is restarted, which
also empties the in-memory login rate limit, so the tests do not depend
on each other's sign-in attempts.

The browser always connects from 127.0.0.1, and the real limit allows 5
sign-in (and 5 password-change) attempts per minute per address. When a
test needs more, the helpers restart the web process first, exactly as
before each test; sessions survive a restart (tested below). Only
test_login_rate_limit_and_audit deliberately runs into the limit.

Every page the browser opens is also checked for Content-Security-Policy
violations and JavaScript errors (see tearDown).

Run (from the repository root, with requirements.txt and
e2e/requirements.txt installed, and "playwright install chromium"):

    python -m unittest discover -s e2e -v
"""

import csv
import http.server
import io
import re
import socketserver
import threading
import time
import unittest

from playwright.sync_api import expect, sync_playwright

from harness import ADMIN, ADMIN_PASSWORD, PASSWORDS, FakeSmtpServer, \
    HelpdeskServer, dump, free_port

# Shorter than Playwright's 30 s default: everything here is on loopback
expect.set_options(timeout=10_000)


class BrowserTestCase(unittest.TestCase):

    server_env = None

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()
        cls.server = HelpdeskServer(cls.server_env).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        # A fresh web process: empty rate-limit counters, same database,
        # and the real time again
        self.server.clock_file.write_text("0")
        self.server.stop_web()
        self.server.start_web()
        self.page_problems = []
        self.contexts = []
        self.limited_posts = {"login": 0, "password": 0}

    def tearDown(self):
        for context in self.contexts:
            context.close()
        self.assertEqual(self.page_problems, [],
                         "CSP violations or JavaScript errors in the browser")

    # --- browser helpers -------------------------------------------------

    def new_page(self):
        context = self.browser.new_context(base_url=self.server.url)
        self.contexts.append(context)
        page = context.new_page()

        def on_console(message):
            if message.type == "error" and \
                    "Failed to load resource" not in message.text:
                self.page_problems.append(message.text)

        page.on("console", on_console)
        page.on("pageerror", lambda error: self.page_problems.append(
            str(error)))
        # The admin screen asks "Deactivate ...?" through window.confirm
        page.on("dialog", lambda dialog: dialog.accept())
        return page

    def count_limited_post(self, kind):
        """Restart the web process before the 6th attempt in this minute."""

        if self.limited_posts[kind] >= 5:
            self.server.stop_web()
            self.server.start_web()
            self.limited_posts = {"login": 0, "password": 0}
        self.limited_posts[kind] += 1

    def submit(self, page, button):
        with page.expect_navigation():
            button.click()

    def login(self, page, username, password):
        self.count_limited_post("login")
        page.goto("/login")
        page.fill("#username", username)
        page.fill("#password", password)
        self.submit(page, page.locator("form[action='/login'] button"))

    def login_ok(self, page, username, password):
        self.login(page, username, password)
        expect(page.locator("form[action='/logout']")).to_be_visible()

    def logout(self, page):
        self.submit(page, page.locator("form[action='/logout'] button"))

    def flash(self, page):
        return page.locator(".alert").all_inner_texts()

    def admin_page(self):
        page = self.new_page()
        self.login_ok(page, ADMIN, ADMIN_PASSWORD)
        return page

    def create_account(self, admin, username, role="user",
                       password=PASSWORDS["first"]):
        admin.goto("/admin/users/new")
        admin.fill("#username", username)
        admin.select_option("#role", role)
        admin.fill("#password", password)
        admin.fill("#confirm_password", password)
        self.submit(admin, admin.locator(
            "form[action='/admin/users/new'] button"))
        expect(admin.locator(f"tr[data-username='{username}']")).to_be_visible()

    def change_password(self, page, current, new):
        self.count_limited_post("password")
        page.goto("/account/password")
        page.fill("#current_password", current)
        page.fill("#new_password", new)
        page.fill("#confirm_password", new)
        self.submit(page, page.locator(
            "form[action='/account/password'] button"))

    def first_sign_in(self, page, username, temporary=PASSWORDS["first"],
                      chosen=PASSWORDS["chosen"]):
        self.login(page, username, temporary)
        expect(page).to_have_url(re.compile(r"/account/password$"))
        self.change_password(page, temporary, chosen)
        expect(page).to_have_url(re.compile(r"/$"))

    def ready_account(self, username, role="user"):
        """An account that has already chosen its own password."""

        admin = self.admin_page()
        self.create_account(admin, username, role)
        page = self.new_page()
        self.first_sign_in(page, username)
        return page

    def new_ticket(self, page, title, priority="Medium",
                   description="Steps to reproduce: described in E2E."):
        page.goto("/tickets/new")
        page.fill("input[name=title]", title)
        page.fill("textarea[name=description]", description)
        page.check(f"#p_{priority}")
        self.submit(page, page.locator(
            "form[action='/tickets/new'] button[type=submit]"))
        match = re.search(r"/tickets/(\d+)$", page.url)
        self.assertIsNotNone(match, page.url)
        return int(match.group(1))

    def update_ticket(self, page, ticket_id, status=None, assignee=None):
        page.goto(f"/tickets/{ticket_id}")
        if status is not None:
            page.select_option("#status", status)
        if assignee is not None:
            page.select_option("#assigned_to", assignee)
        self.submit(page, page.locator(
            "form[action$='/update'] button[type=submit]"))

    def comment(self, page, ticket_id, text):
        page.goto(f"/tickets/{ticket_id}")
        page.fill("form[action$='/comment'] textarea[name=content]", text)
        self.submit(page, page.locator(
            "form[action$='/comment'] button[type=submit]"))

    def csrf_token(self, page):
        return page.locator("input[name=csrf_token]").first.get_attribute(
            "value")

    def account_id(self, username):
        return self.server.query(
            "SELECT id FROM users WHERE username = ?", (username,))[0]["id"]


# ---------------------------------------------------------------------------

class TestSignInAndAccounts(BrowserTestCase):

    def test_login_and_logout(self):
        page = self.new_page()

        page.goto("/")
        expect(page).to_have_url(re.compile(r"/login"))

        self.login(page, ADMIN, "not the password at all")
        expect(page.locator("form[action='/logout']")).to_have_count(0)
        self.assertTrue(any("Invalid" in text or "incorrect" in text.lower()
                            for text in self.flash(page)), self.flash(page))

        self.login_ok(page, ADMIN, ADMIN_PASSWORD)
        expect(page.get_by_role("heading", name=re.compile("SLA"))) \
            .to_be_visible()

        self.logout(page)
        expect(page).to_have_url(re.compile(r"/login"))
        page.goto("/")
        expect(page).to_have_url(re.compile(r"/login"))

        events = [row["action"] for row in self.server.query(
            "SELECT action FROM audit_events WHERE target_username = ? "
            "ORDER BY id", (ADMIN,))]
        self.assertIn("login_failed", events)
        self.assertIn("login_succeeded", events)

    def test_first_login_requires_a_new_password(self):
        admin = self.admin_page()
        self.create_account(admin, "first-login-user")

        page = self.new_page()
        self.login(page, "first-login-user", PASSWORDS["first"])
        expect(page).to_have_url(re.compile(r"/account/password$"))
        expect(page.get_by_text("Choose your own password")).to_be_visible()

        # Nothing else is reachable until the password is changed
        for path in ("/", "/tickets/new", "/tickets/export"):
            page.goto(path)
            expect(page).to_have_url(re.compile(r"/account/password$"))

        # The server's password policy applies (a password containing the
        # username passes the browser's length check but is refused)
        self.change_password(page, PASSWORDS["first"],
                             "first-login-user forever and ever")
        expect(page).to_have_url(re.compile(r"/account/password$"))
        self.assertTrue(any("username" in text for text in self.flash(page)),
                        self.flash(page))

        self.change_password(page, PASSWORDS["first"], PASSWORDS["chosen"])
        expect(page).to_have_url(re.compile(r"/$"))
        page.goto("/tickets/new")
        expect(page).to_have_url(re.compile(r"/tickets/new$"))

        # The temporary password no longer works
        other = self.new_page()
        self.login(other, "first-login-user", PASSWORDS["first"])
        expect(other.locator("form[action='/logout']")).to_have_count(0)

    def test_password_recovery_by_an_administrator(self):
        user = self.ready_account("forgetful-user")

        admin = self.admin_page()
        admin.goto("/admin/users")
        row = admin.locator("tr[data-username='forgetful-user']")
        row.locator("summary").click()
        row.locator("input[name=password]").fill(PASSWORDS["reset"])
        row.locator("input[name=confirm_password]").fill(PASSWORDS["reset"])
        self.submit(admin, row.get_by_role("button", name="Set password"))
        expect(admin.locator("tr[data-username='forgetful-user']")
               .get_by_text("Must change password")).to_be_visible()

        # The user's existing session ended with the reset
        user.goto("/")
        expect(user).to_have_url(re.compile(r"/login"))

        # The temporary password works once, and only for changing it
        self.first_sign_in(user, "forgetful-user", PASSWORDS["reset"],
                           PASSWORDS["again"])

        actions = [row["action"] for row in self.server.query(
            "SELECT action FROM audit_events WHERE target_username = ? "
            "ORDER BY id", ("forgetful-user",))]
        self.assertIn("password_reset", actions)

    def test_password_recovery_from_the_server_command_line(self):
        self.ready_account("locked-out-user")

        output = self.server.operator_reset_password("locked-out-user",
                                                     PASSWORDS["reset"])
        self.assertIn("reset", output)

        page = self.new_page()
        self.first_sign_in(page, "locked-out-user", PASSWORDS["reset"],
                           PASSWORDS["again"])

        actors = [row["actor"] for row in self.server.query(
            "SELECT actor FROM audit_events WHERE target_username = ? AND "
            "action = 'password_reset'", ("locked-out-user",))]
        self.assertTrue(actors and actors[0].startswith("cli:"), actors)

    def test_administrator_account_management(self):
        admin = self.admin_page()
        self.create_account(admin, "managed-user")
        user = self.new_page()
        self.first_sign_in(user, "managed-user")

        # Promote to technician: applies on the user's next request
        row = admin.locator("tr[data-username='managed-user']")
        row.locator("select[name=role]").select_option("technician")
        self.submit(admin, row.get_by_role("button", name="Save"))
        self.assertEqual(self.server.query(
            "SELECT role FROM users WHERE username='managed-user'")[0]["role"],
            "technician")

        # Deactivate (confirmed through the page's JavaScript dialog)
        row = admin.locator("tr[data-username='managed-user']")
        self.submit(admin, row.get_by_role("button", name="Deactivate"))
        expect(admin.locator("tr[data-username='managed-user']")
               .get_by_text("Deactivated")).to_be_visible()

        user.goto("/")
        expect(user).to_have_url(re.compile(r"/login"))
        self.login(user, "managed-user", PASSWORDS["chosen"])
        expect(user.locator("form[action='/logout']")).to_have_count(0)

        # Reactivate: the account may sign in again
        row = admin.locator("tr[data-username='managed-user']")
        self.submit(admin, row.get_by_role("button", name="Reactivate"))
        self.login_ok(user, "managed-user", PASSWORDS["chosen"])

        # An administrator cannot deactivate their own account, even by
        # posting the form directly
        response = admin.request.post(
            f"/admin/users/{self.account_id(ADMIN)}/active",
            form={"csrf_token": self.csrf_token(admin), "active": "0"},
            max_redirects=0)
        self.assertIn(response.status, (302, 303, 400, 403))
        self.assertEqual(self.server.query(
            "SELECT is_active FROM users WHERE username=?",
            (ADMIN,))[0]["is_active"], 1)

        # Every change is in the audit log page
        admin.goto("/admin/audit")
        for action in ("account_created", "role_changed",
                       "account_deactivated", "account_reactivated"):
            self.assertIn(action.split("_")[0], admin.content().lower())

    def test_permissions_by_role(self):
        user = self.ready_account("plain-user")
        other = self.ready_account("other-user")
        technician = self.ready_account("perm-tech", role="technician")

        mine = self.new_ticket(user, "Plain user's printer")
        theirs = self.new_ticket(other, "Other user's laptop")

        # Users see only their own tickets
        user.goto("/")
        expect(user.get_by_text("Plain user's printer")).to_be_visible()
        expect(user.get_by_text("Other user's laptop")).to_have_count(0)
        response = user.goto(f"/tickets/{theirs}")
        self.assertEqual(response.status, 404)

        # Users cannot change status or assignment, even by posting
        user.goto(f"/tickets/{mine}")
        expect(user.locator("form[action$='/update']")).to_have_count(0)
        user.request.post(f"/tickets/{mine}/update", form={
            "csrf_token": self.csrf_token(user), "status": "Closed",
            "assigned_to": ""})
        self.assertEqual(self.server.query(
            "SELECT status FROM tickets WHERE id=?", (mine,))[0]["status"],
            "Open")

        # Only administrators reach the administration pages
        for page in (user, technician):
            for path in ("/admin/users", "/admin/users/new", "/admin/audit"):
                self.assertEqual(page.goto(path).status, 403, path)

        # Staff see every ticket
        technician.goto("/")
        expect(technician.get_by_text("Other user's laptop")).to_be_visible()

        # The export follows the same rule
        export = user.request.get("/tickets/export")
        self.assertEqual(export.status, 200)
        self.assertIn("Plain user's printer", export.text())
        self.assertNotIn("Other user's laptop", export.text())

    def test_login_rate_limit_and_audit(self):
        page = self.new_page()
        for _ in range(5):
            self.login(page, ADMIN, "wrong guess number one")
        self.limited_posts["login"] = 0  # no restart: hit the real limit
        response = page.goto("/login")
        page.fill("#username", ADMIN)
        page.fill("#password", ADMIN_PASSWORD)
        with page.expect_response(lambda r: r.request.method == "POST") \
                as posted:
            page.locator("form[action='/login'] button").click()
        # Even the correct password is refused while the limit applies
        self.assertEqual(posted.value.status, 429)
        self.assertIsNotNone(response)
        failures = self.server.query(
            "SELECT COUNT(*) AS n FROM audit_events WHERE action = "
            "'login_failed' AND target_username = ?", (ADMIN,))[0]["n"]
        self.assertGreaterEqual(failures, 5)


# ---------------------------------------------------------------------------

class TestTicketWorkflow(BrowserTestCase):

    def test_ticket_lifecycle_history_and_notifications(self):
        admin = self.admin_page()
        self.create_account(admin, "requester")
        self.create_account(admin, "fixer", role="technician")
        for name in ("requester", "fixer"):
            self.server.flask("set-email", name, f"{name}@example.invalid")

        requester = self.new_page()
        self.first_sign_in(requester, "requester")
        technician = self.new_page()
        self.first_sign_in(technician, "fixer")

        ticket = self.new_ticket(requester, "VPN drops every hour",
                                 priority="High")
        expect(requester.get_by_text("VPN drops every hour")).to_be_visible()

        # The administrator assigns it to the technician
        self.update_ticket(admin, ticket, assignee="fixer")
        # The technician starts work and replies
        self.update_ticket(technician, ticket, status="In Progress")
        self.comment(technician, ticket, "Replaced the VPN profile.")
        # The requester replies; the technician resolves; admin closes
        self.comment(requester, ticket, "Looks fixed, thank you.")
        self.update_ticket(technician, ticket, status="Resolved")
        self.update_ticket(admin, ticket, status="Closed")

        row = self.server.query("SELECT status, assigned_to FROM tickets "
                                "WHERE id=?", (ticket,))[0]
        self.assertEqual(row, {"status": "Closed", "assigned_to": "fixer"})

        # The requester sees the whole story on the ticket page
        requester.goto(f"/tickets/{ticket}")
        for text in ("Replaced the VPN profile.", "Looks fixed, thank you."):
            expect(requester.get_by_text(text)).to_be_visible()
        timeline = requester.locator("ol.timeline").inner_text()
        for fragment in ("created this ticket", "assigned this ticket to",
                         "In Progress", "Resolved", "Closed"):
            self.assertIn(fragment, timeline)
        history = [r["action"] for r in self.server.query(
            "SELECT action FROM ticket_history WHERE ticket_id=? "
            "ORDER BY id", (ticket,))]
        self.assertEqual(history[0], "created")
        self.assertIn("assigned", history)
        self.assertEqual(history.count("status_changed"), 3)

        # Notifications were queued for each event, not sent
        queued = self.server.notifications()
        events = {(n["event_type"], n["recipient_username"]) for n in queued}
        for expected in (("ticket_created", "requester"),
                         ("ticket_created", "fixer"),
                         ("ticket_assigned", "fixer"),
                         ("staff_comment", "requester"),
                         ("requester_comment", "fixer"),
                         ("ticket_resolved", "requester")):
            self.assertIn(expected, events, dump(queued))
        self.assertTrue(all(n["status"] == "pending" for n in queued))

        # The worker delivers them to the local sink only (simulated).
        # Accounts with no email address (the administrator, and staff
        # from other tests) are cancelled instead.
        self.server.send_notifications()
        delivered = self.server.notifications()
        for row in delivered:
            has_address = row["recipient_username"] in ("requester", "fixer")
            expected = "simulated" if has_address else "cancelled"
            self.assertEqual(row["status"], expected, dump(row))
        messages = self.server.sink_messages()
        self.assertEqual(len(messages), sum(
            1 for row in delivered if row["status"] == "simulated"))
        self.assertTrue(all("[SIMULATED]" in m for m in messages))
        self.assertTrue(any(f"/tickets/{ticket}" in m for m in messages))

    def test_sla_dashboard_follows_the_clock(self):
        technician = self.ready_account("sla-tech", role="technician")
        requester = self.ready_account("sla-requester")

        def cards():
            technician.goto("/")
            return {key: int(technician.locator(
                f"[data-sla-card='{key}'] .sla-num").inner_text())
                for key in ("within", "due_soon", "overdue", "breached")}

        def row_status():
            technician.goto(f"/?q={title}")
            return technician.locator(
                f"tr:has(a[href='/tickets/{ticket}']) td.sla-cell"
            ).get_attribute("data-sla-status")

        before = cards()
        title = "Server room alarm"
        ticket = self.new_ticket(requester, title, priority="Critical")
        self.assertEqual(row_status(), "Within SLA")
        self.assertEqual(cards()["within"], before["within"] + 1)

        # 40 minutes later the 1-hour response target is due soon
        self.server.advance_clock(40 * 60)
        self.assertEqual(row_status(), "Due Soon")

        # Two hours after creation it is overdue, and the filter finds it
        self.server.advance_clock(80 * 60)
        self.assertEqual(row_status(), "Overdue")
        overdue = cards()["overdue"]
        self.assertGreaterEqual(overdue, 1)
        technician.goto("/?sla=overdue")
        expect(technician.locator(f"a[href='/tickets/{ticket}']").first) \
            .to_be_visible()

        # The ticket page shows the same verdict as the dashboard
        technician.goto(f"/tickets/{ticket}")
        expect(technician.get_by_test_id("response-sla")) \
            .to_contain_text("Overdue")

        # A technician reply records the first response, so the ticket
        # leaves the Overdue list. SQLite stamps the reply with the real
        # time (CURRENT_TIMESTAMP), not this test's moved clock, so the
        # reply counts as on time here ("Met"); a late reply ("Breached")
        # is covered by tests/test_sla_dashboard_routes.py and
        # tests/test_sla_status.py, which control stored timestamps.
        self.comment(technician, ticket, "On my way to the server room.")
        self.assertEqual(row_status(), "Met")
        self.assertEqual(cards()["overdue"], overdue - 1)

    def test_csv_export_matches_the_dashboard_filter(self):
        technician = self.ready_account("csv-tech", role="technician")
        requester = self.ready_account("csv-requester")
        self.new_ticket(requester, "=HYPERLINK(\"http://evil.invalid\")",
                        priority="Low")
        self.new_ticket(requester, "Keyboard, with a comma", priority="High")

        technician.goto("/?priority=High")
        with technician.expect_download() as download_info:
            technician.locator("a[href^='/tickets/export']").click()
        download = download_info.value
        self.assertTrue(download.suggested_filename.endswith(".csv"))
        rows = list(csv.reader(io.StringIO(
            open(download.path(), encoding="utf-8-sig").read())))
        self.assertEqual(rows[0][:2], ["Ticket ID", "Title"])
        titles = [row[1] for row in rows[1:]]
        self.assertEqual(titles, ["Keyboard, with a comma"])

        # Unfiltered: the formula is neutralised for spreadsheet programs
        text = technician.request.get("/tickets/export").text()
        rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
        formula = [row[1] for row in rows[1:] if "HYPERLINK" in row[1]]
        self.assertEqual(len(formula), 1)
        self.assertFalse(formula[0].startswith("="), formula)


# ---------------------------------------------------------------------------

class _EvilSite:
    """A page on a different site that tries to submit a helpdesk form."""

    def __init__(self, target):
        page = f"""<!doctype html><title>prize</title>
<form id=f method=POST action="{target}/tickets/new">
<input name=title value="Planted by another site">
<input name=description value="csrf">
<input name=priority value="Low">
</form><script>document.getElementById('f').submit()</script>"""

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = page.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.port = free_port()
        self.server = socketserver.TCPServer(("127.0.0.1", self.port),
                                             Handler)
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()
        # "localhost" is a different site from "127.0.0.1" for cookies
        self.url = f"http://localhost:{self.port}/"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class TestSessionsAndCsrf(BrowserTestCase):

    def test_csrf_protection(self):
        page = self.ready_account("csrf-user")
        page.goto("/")

        # No token, and a forged token, are both refused
        for form in ({"title": "No token", "description": "x",
                      "priority": "Low"},
                     {"title": "Bad token", "description": "x",
                      "priority": "Low", "csrf_token": "forged"}):
            response = page.request.post("/tickets/new", form=form)
            self.assertEqual(response.status, 400, form)

        # A page on another site submitting the form in the same browser
        evil = _EvilSite(self.server.url)
        self.addCleanup(evil.close)
        page.goto(evil.url)
        page.wait_for_load_state("load")
        time.sleep(0.5)

        titles = [r["title"] for r in self.server.query(
            "SELECT title FROM tickets")]
        self.assertNotIn("Planted by another site", titles)
        self.assertNotIn("No token", titles)
        self.assertNotIn("Bad token", titles)
        # The rejected forms leave no JavaScript errors to report
        self.page_problems = [p for p in self.page_problems
                              if "400" not in p]

    def test_session_expires_after_eight_hours(self):
        page = self.ready_account("long-day-user")
        page.goto("/")
        expect(page.locator("form[action='/logout']")).to_be_visible()

        self.server.advance_clock(7 * 3600 + 50 * 60)
        page.goto("/")
        expect(page.locator("form[action='/logout']")).to_be_visible()

        self.server.advance_clock(11 * 60)
        page.goto("/")
        expect(page).to_have_url(re.compile(r"/login"))

    def test_logout_and_password_change_end_every_session(self):
        self.ready_account("two-device-user")
        laptop = self.new_page()
        phone = self.new_page()
        self.login_ok(laptop, "two-device-user", PASSWORDS["chosen"])
        self.login_ok(phone, "two-device-user", PASSWORDS["chosen"])

        # Logging out on one device ends the other too
        self.logout(laptop)
        phone.goto("/")
        expect(phone).to_have_url(re.compile(r"/login"))

        # A password change keeps this device and ends the others
        self.login_ok(laptop, "two-device-user", PASSWORDS["chosen"])
        self.login_ok(phone, "two-device-user", PASSWORDS["chosen"])
        self.change_password(laptop, PASSWORDS["chosen"], PASSWORDS["again"])
        laptop.goto("/")
        expect(laptop.locator("form[action='/logout']")).to_be_visible()
        phone.goto("/")
        expect(phone).to_have_url(re.compile(r"/login"))

    def test_sessions_survive_an_application_restart(self):
        page = self.ready_account("restart-user")
        self.server.stop_web()
        self.server.start_web()
        page.goto("/")
        expect(page.locator("form[action='/logout']")).to_be_visible()

    def test_security_headers_reach_the_browser(self):
        page = self.new_page()
        response = page.goto("/login")
        headers = response.headers
        csp = headers.get("content-security-policy", "")
        for directive in ("default-src 'none'", "frame-ancestors 'none'",
                          "object-src 'none'"):
            self.assertIn(directive, csp)
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertIn(headers.get("x-frame-options", "").upper(),
                      ("DENY", "SAMEORIGIN"))

        # An inline script injected into the page is blocked by the CSP
        with self.assertRaisesRegex(Exception, "Content Security Policy"):
            page.add_script_tag(content="window.injected = true")
        self.assertIsNone(page.evaluate("window.injected"))
        self.assertTrue(any("Content Security Policy" in p
                            for p in self.page_problems))
        self.page_problems = []


# ---------------------------------------------------------------------------

class TestNotificationRecovery(BrowserTestCase):

    def queue_one_notification(self, title):
        """A ticket created in the browser queues a confirmation."""

        page = self.ready_account(f"notify-{title.split()[0].lower()}")
        username = self.server.query(
            "SELECT username FROM users ORDER BY id DESC LIMIT 1")[0][
                "username"]
        self.server.flask("set-email", username, f"{username}@example.invalid")
        # Deliver what is already queued (the password-change notice), so
        # only this ticket's confirmation is left for the mail server
        self.server.send_notifications()
        self.new_ticket(page, title)
        rows = [n for n in self.server.notifications()
                if n["event_type"] == "ticket_created"
                and n["recipient_username"] == username]
        self.assertEqual(len(rows), 1, dump(self.server.notifications()))
        return rows[0]["id"]

    def notification(self, notification_id):
        return next(n for n in self.server.notifications()
                    if n["id"] == notification_id)

    def test_unreachable_mail_server_defers_then_recovers(self):
        notification_id = self.queue_one_notification("Unreachable relay")

        # Nothing listens on this port: the attempt is deferred, not failed
        closed = free_port()
        result = self.server.flask("notifications-send", check=False, env={
            "NOTIFICATION_DELIVERY": "smtp",
            "NOTIFICATION_SENDER": "helpdesk@example.invalid",
            "NOTIFICATION_SMTP_HOST": "127.0.0.1",
            "NOTIFICATION_SMTP_PORT": str(closed),
            "NOTIFICATION_SMTP_SECURITY": "none",
            "NOTIFICATION_RETRY_BASE_SECONDS": "1",
        })
        # Exit status 2 tells the supervisor that delivery is paused
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Delivery paused", result.stdout + result.stderr)
        row = self.notification(notification_id)
        self.assertEqual(row["status"], "pending", dump(row))
        self.assertGreaterEqual(row["deferrals"], 1)
        self.assertEqual(row["attempts"], 0)
        self.assertIn("connect", row["last_error"])

        # Once a working transport is configured, the same row is delivered
        time.sleep(1.5)
        self.server.send_notifications()
        row = self.notification(notification_id)
        self.assertEqual(row["status"], "simulated", dump(row))

    def test_temporary_then_permanent_failure_and_requeue(self):
        notification_id = self.queue_one_notification("Flaky relay")
        smtp = FakeSmtpServer("451 4.3.0 please try again later")
        self.addCleanup(smtp.close)
        env = smtp.env(NOTIFICATION_RETRY_BASE_SECONDS=1,
                       NOTIFICATION_RETRY_MAX_SECONDS=1,
                       NOTIFICATION_MAX_ATTEMPTS=2)

        # First attempt: a temporary refusal schedules a delayed retry
        self.server.send_notifications(env=env)
        row = self.notification(notification_id)
        self.assertEqual((row["status"], row["attempts"]), ("pending", 1),
                         dump(row))
        self.assertIn("451", row["last_error"])
        first_retry_at = row["next_attempt_at"]

        # Not due yet: an immediate run leaves it alone
        self.server.send_notifications(env=env)
        self.assertEqual(self.notification(notification_id)["attempts"], 1)
        self.assertEqual(
            self.notification(notification_id)["next_attempt_at"],
            first_retry_at)

        # Second refusal reaches the attempt limit: permanently failed
        time.sleep(1.5)
        self.server.send_notifications(env=env)
        row = self.notification(notification_id)
        self.assertEqual((row["status"], row["attempts"]), ("failed", 2),
                         dump(row))
        self.assertEqual(smtp.attempts, 2)
        status = self.server.flask("notifications-status").stdout
        self.assertIn("failed: 1", status)
        self.assertIn(f"#{notification_id}", status)
        self.assertNotIn("example.invalid", status)

        # The administrator fixes the cause and requeues it
        self.server.flask("notifications-requeue", str(notification_id),
                          "--reason", "E2E relay fixed", "--yes")
        self.server.send_notifications()
        rows = self.server.notifications()
        self.assertEqual(self.notification(notification_id)["status"],
                         "failed")
        copies = [n for n in rows if n["id"] > notification_id
                  and n["status"] == "simulated"]
        self.assertEqual(len(copies), 1, dump(rows))
        audit = self.server.query(
            "SELECT action FROM audit_events WHERE action = "
            "'notification_requeued'")
        self.assertEqual(len(audit), 1)


if __name__ == "__main__":
    unittest.main()
