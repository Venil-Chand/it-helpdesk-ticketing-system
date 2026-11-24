"""
Tests for the application factory (helpdesk.create_app) and blueprints.

Every application here is built with create_app(environ=...), so .env is
never read, and every test uses its own temporary SQLite database made by
the real migrations. The real helpdesk.db is never opened.

The database location (database.DB_PATH) is one setting for the whole
process, so it is patched per test rather than per application.
"""

import csv
import io
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from flask import Flask, url_for
from werkzeug.routing import BuildError
from werkzeug.security import generate_password_hash

import database
import migrations
import security_config
from helpdesk import BLUEPRINTS, create_app
from helpdesk.auth import login_required, staff_required
from helpdesk.security import get_limiter, get_settings
from security_helpers import (
    DEVELOPMENT_ENV,
    HTTP_URL,
    HTTPS_URL,
    PASSWORD,
    PRODUCTION_ENV,
    PROJECT_ROOT,
    TEST_SECRET_KEY,
    TESTING_ENV,
)

# Every route, exactly as it was before the blueprints: URL rule, methods
# (without the automatic HEAD and OPTIONS) and the new endpoint name.
EXPECTED_ROUTES = {
    ("/", frozenset({"GET"}), "dashboard.index"),
    ("/login", frozenset({"GET", "POST"}), "auth.login"),
    ("/logout", frozenset({"POST"}), "auth.logout"),
    ("/tickets/new", frozenset({"GET", "POST"}), "tickets.new_ticket"),
    ("/tickets/<int:ticket_id>", frozenset({"GET"}),
     "tickets.ticket_detail"),
    ("/tickets/<int:ticket_id>/update", frozenset({"POST"}),
     "tickets.update_ticket"),
    ("/tickets/<int:ticket_id>/comment", frozenset({"POST"}),
     "tickets.add_comment"),
    ("/tickets/export", frozenset({"GET"}), "reports.export_tickets"),
    ("/static/<path:filename>", frozenset({"GET"}), "static"),
}

# Endpoint names used before the blueprints; none may still resolve
OLD_ENDPOINTS = (
    "index", "login", "logout", "new_ticket", "ticket_detail",
    "update_ticket", "add_comment", "export_tickets",
)

# The application-wide hooks, in the order they must run
EXPECTED_BEFORE_REQUEST = [
    "require_https_in_production",
    "csrf_protect",
    "_check_request_limit",
    "load_current_user",
]

NO_CSRF = {"WTF_CSRF_ENABLED": False}


def testing_app(**config):
    """A testing application with its settings given explicitly."""

    return create_app(environ=dict(TESTING_ENV), config=config or None)


class TemporaryDatabaseMixin:
    """A fresh, migrated database with four accounts for each test."""

    def setUp(self):
        super().setUp()

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = os.path.join(directory.name, "factory_test.db")

        patcher = patch.object(database, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        migrations.migrate(self.db_path)

        password_hash = generate_password_hash(
            PASSWORD, method="pbkdf2:sha256:1000"
        )

        conn = database.get_db()
        try:
            with conn:
                conn.executemany(
                    "INSERT INTO users (username, password_hash, role) "
                    "VALUES (?, ?, ?)",
                    [
                        ("admin", password_hash, "admin"),
                        ("tech", password_hash, "technician"),
                        ("alice", password_hash, "user"),
                        ("bob", password_hash, "user"),
                    ]
                )
        finally:
            conn.close()

    def login(self, app, username, client=None):
        client = client or app.test_client()
        response = client.post(
            "/login", data={"username": username, "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 302, response.text)
        self.assertEqual(response.headers["Location"], "/")
        return client

    def query(self, sql, *params):
        conn = database.get_db()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


# ---------------------------------------------------
# 1. create_app() INITIALISATION
# ---------------------------------------------------

class CreateAppTests(unittest.TestCase):

    def test_returns_a_configured_flask_application(self):
        app = testing_app()

        self.assertIsInstance(app, Flask)
        self.assertEqual(app.secret_key, TEST_SECRET_KEY)
        self.assertEqual(app.permanent_session_lifetime, timedelta(hours=8))
        self.assertFalse(app.config["SESSION_REFRESH_EACH_REQUEST"])
        self.assertTrue(app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(app.config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertTrue(app.config["WTF_CSRF_ENABLED"])
        self.assertEqual(app.config["TRUSTED_HOSTS"],
                         ["localhost", "127.0.0.1"])
        self.assertEqual(get_settings(app).environment, "testing")

    def test_templates_and_static_files_come_from_the_project_folder(self):
        app = testing_app()

        self.assertEqual(Path(app.root_path), PROJECT_ROOT)
        self.assertEqual(Path(app.static_folder), PROJECT_ROOT / "static")

        with app.app_context():
            # Raises TemplateNotFound if the folder is wrong
            app.jinja_env.get_template("base.html")

    def test_config_argument_is_applied(self):
        app = testing_app(TESTING=True, WTF_CSRF_ENABLED=False)

        self.assertTrue(app.testing)
        self.assertFalse(app.config["WTF_CSRF_ENABLED"])

    def test_utc_time_filter_is_registered(self):
        app = testing_app()
        utc_time = app.jinja_env.filters["utc_time"]

        self.assertEqual(utc_time("2026-11-18 10:30:00"),
                         "2026-11-18 10:30 UTC")
        self.assertEqual(utc_time(None), "")

    def test_app_module_keeps_its_public_names(self):
        import app as app_module

        self.assertIsInstance(app_module.app, Flask)
        self.assertIs(app_module.limiter, get_limiter(app_module.app))
        self.assertIs(app_module.db, database)

        for name in ("SESSION_MAX_LIFETIME", "build_pagination",
                     "page_window", "parse_page",
                     "schema_ready_for_startup"):
            self.assertTrue(hasattr(app_module, name), name)

    def test_app_module_does_not_offer_stale_patch_targets(self):
        # A test that patches one of these on app.py must fail loudly,
        # because patching it there would no longer affect any route
        import app as app_module

        for name in ("current_time", "datetime", "render_template",
                     "check_password_hash", "login_required"):
            self.assertFalse(hasattr(app_module, name), name)


# ---------------------------------------------------
# 2. MULTIPLE INDEPENDENT APPLICATIONS
# ---------------------------------------------------

class IndependentApplicationTests(TemporaryDatabaseMixin, unittest.TestCase):

    def test_each_call_builds_a_new_application(self):
        first, second = testing_app(), testing_app()

        self.assertIsNot(first, second)
        self.assertIsNot(first.config, second.config)
        self.assertIsNot(get_limiter(first), get_limiter(second))

    def test_configuration_changes_stay_in_one_application(self):
        first, second = testing_app(), testing_app()

        first.config["WTF_CSRF_ENABLED"] = False

        self.assertTrue(second.config["WTF_CSRF_ENABLED"])

    def test_development_and_production_apps_coexist(self):
        development = create_app(environ=dict(DEVELOPMENT_ENV))
        production = create_app(environ=dict(PRODUCTION_ENV))

        self.assertEqual(development.config["SESSION_COOKIE_NAME"],
                         "session")
        self.assertFalse(development.config["SESSION_COOKIE_SECURE"])
        self.assertEqual(production.config["SESSION_COOKIE_NAME"],
                         "__Host-helpdesk_session")
        self.assertTrue(production.config["SESSION_COOKIE_SECURE"])

        # Each keeps its own HTTPS rule
        self.assertEqual(
            production.test_client().get("/login", base_url=HTTP_URL)
            .status_code, 400
        )
        self.assertEqual(
            development.test_client().get("/login").status_code, 200
        )

        # And its own security headers
        secure = production.test_client().get("/login", base_url=HTTPS_URL)
        self.assertIn("Strict-Transport-Security", secure.headers)
        self.assertIn("upgrade-insecure-requests",
                      secure.headers["Content-Security-Policy"])

        local = development.test_client().get("/login")
        self.assertNotIn("Strict-Transport-Security", local.headers)
        self.assertNotIn("upgrade-insecure-requests",
                         local.headers["Content-Security-Policy"])

    def test_login_rate_limits_are_counted_per_application(self):
        first, second = testing_app(**NO_CSRF), testing_app(**NO_CSRF)
        wrong = {"username": "alice", "password": "wrong"}

        client = first.test_client()
        codes = [client.post("/login", data=wrong).status_code
                 for _ in range(6)]

        self.assertEqual(codes, [200] * 5 + [429])

        # The other application has not seen any of those attempts
        self.assertEqual(
            second.test_client().post("/login", data=wrong).status_code, 200
        )

    def test_a_session_from_one_secret_key_is_refused_by_another(self):
        first = testing_app(**NO_CSRF)
        other_key = dict(TESTING_ENV, SECRET_KEY="a" * 16 + "b" * 16)
        second = create_app(environ=other_key, config=NO_CSRF)

        cookie = self.login(first, "alice").get_cookie("session").value

        stranger = second.test_client()
        stranger.set_cookie("session", cookie)
        response = stranger.get("/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login")

    def test_explicit_environ_leaves_the_process_environment_alone(self):
        before = dict(os.environ)

        create_app(environ=dict(PRODUCTION_ENV))

        self.assertEqual(dict(os.environ), before)


# ---------------------------------------------------
# 3, 4. BLUEPRINT REGISTRATION AND ROUTE COMPATIBILITY
# ---------------------------------------------------

class BlueprintRegistrationTests(unittest.TestCase):

    def setUp(self):
        self.app = testing_app()

    def test_expected_blueprints_are_registered(self):
        self.assertEqual(
            set(self.app.blueprints),
            {"auth", "dashboard", "tickets", "reports"}
        )
        self.assertEqual(len(BLUEPRINTS), len(self.app.blueprints))

    def test_url_map_matches_the_pre_blueprint_routes_exactly(self):
        actual = {
            (rule.rule, frozenset(rule.methods - {"HEAD", "OPTIONS"}),
             rule.endpoint)
            for rule in self.app.url_map.iter_rules()
        }

        self.assertEqual(actual, EXPECTED_ROUTES)

    def test_old_endpoint_names_no_longer_resolve(self):
        with self.app.test_request_context():
            for endpoint in OLD_ENDPOINTS:
                with self.subTest(endpoint=endpoint):
                    with self.assertRaises(BuildError):
                        url_for(endpoint)

    def test_new_endpoint_names_build_the_original_urls(self):
        with self.app.test_request_context():
            self.assertEqual(url_for("dashboard.index"), "/")
            self.assertEqual(url_for("auth.login"), "/login")
            self.assertEqual(url_for("auth.logout"), "/logout")
            self.assertEqual(url_for("tickets.new_ticket"), "/tickets/new")
            self.assertEqual(url_for("tickets.ticket_detail", ticket_id=7),
                             "/tickets/7")
            self.assertEqual(url_for("tickets.update_ticket", ticket_id=7),
                             "/tickets/7/update")
            self.assertEqual(url_for("tickets.add_comment", ticket_id=7),
                             "/tickets/7/comment")
            self.assertEqual(url_for("reports.export_tickets", sla="met"),
                             "/tickets/export?sla=met")

    def test_every_url_for_in_the_templates_resolves(self):
        pattern = re.compile(r"url_for\(\s*'([^']+)'")
        found = set()

        for template in (PROJECT_ROOT / "templates").glob("*.html"):
            found.update(pattern.findall(template.read_text()))

        self.assertTrue(found)

        with self.app.test_request_context():
            for endpoint in sorted(found):
                with self.subTest(endpoint=endpoint):
                    url_for(endpoint, ticket_id=1, filename="style.css")

    def test_security_hooks_are_registered_once_in_order(self):
        names = [
            getattr(hook, "__name__", "")
            for hook in self.app.before_request_funcs[None]
        ]

        self.assertEqual(names, EXPECTED_BEFORE_REQUEST)
        self.assertEqual(len(self.app.after_request_funcs[None]), 2)

        # No blueprint adds hooks of its own that could run twice or
        # cover only part of the site
        for blueprint in self.app.blueprints:
            self.assertNotIn(blueprint, self.app.before_request_funcs)
            self.assertNotIn(blueprint, self.app.after_request_funcs)

    def test_cli_commands_are_registered(self):
        commands = set(self.app.cli.list_commands(None))

        self.assertLessEqual(
            {"db-status", "init-db", "assignment-audit"}, commands
        )


class ExistingRouteBehaviourTests(TemporaryDatabaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.app = testing_app(**NO_CSRF)

    def test_signed_out_visitors_are_redirected_from_every_protected_route(
            self):
        ticket_id = database.create_ticket("T", "D", "Low", "alice")
        protected = [
            ("GET", "/"),
            ("GET", "/tickets/new"),
            ("POST", "/tickets/new"),
            ("GET", f"/tickets/{ticket_id}"),
            ("POST", f"/tickets/{ticket_id}/update"),
            ("POST", f"/tickets/{ticket_id}/comment"),
            ("GET", "/tickets/export"),
        ]

        for method, url in protected:
            with self.subTest(method=method, url=url):
                response = self.app.test_client().open(url, method=method)

                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], "/login")

    def test_wrong_methods_and_unknown_urls_keep_their_status_codes(self):
        client = self.app.test_client()

        self.assertEqual(client.get("/logout").status_code, 405)
        self.assertEqual(client.post("/").status_code, 405)
        self.assertEqual(client.get("/tickets/1/update").status_code, 405)
        self.assertEqual(client.get("/no-such-page").status_code, 404)
        self.assertEqual(client.get("/tickets/abc").status_code, 404)

    def test_signed_in_pages_render_their_templates(self):
        client = self.login(self.app, "alice")
        ticket_id = database.create_ticket("Printer", "D", "Low", "alice")

        pages = {
            "/": "Printer",
            "/tickets/new": 'name="title"',
            f"/tickets/{ticket_id}": "Printer",
        }

        for url, text in pages.items():
            with self.subTest(url=url):
                response = client.get(url)

                self.assertEqual(response.status_code, 200)
                self.assertIn(text, response.text)

    def test_navigation_links_point_at_the_original_urls(self):
        html = self.login(self.app, "alice").get("/").text

        for link in ('href="/"', 'href="/tickets/new"',
                     'action="/logout"', 'href="/tickets/export'):
            self.assertIn(link, html)

    def test_signed_in_user_visiting_login_goes_to_the_dashboard(self):
        response = self.login(self.app, "alice").get("/login")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")


# ---------------------------------------------------
# 5, 6. AUTHENTICATION, AUTHORIZATION AND SESSIONS
# ---------------------------------------------------

class AuthenticationThroughFactoryTests(TemporaryDatabaseMixin,
                                        unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.app = testing_app(**NO_CSRF)

    def test_session_stores_only_claims_and_never_the_role(self):
        client = self.login(self.app, "alice")

        with client.session_transaction() as sess:
            self.assertEqual(
                set(sess) - {"_flashes", "_permanent"},
                {"user_id", "session_version", "issued_at"}
            )

    def test_role_is_read_from_the_database_on_every_request(self):
        client = self.login(self.app, "alice")
        ticket_id = database.create_ticket("Bob's", "D", "Low", "bob")

        self.assertEqual(client.get(f"/tickets/{ticket_id}").status_code,
                         404)

        conn = database.get_db()
        with conn:
            conn.execute("UPDATE users SET role='technician' "
                         "WHERE username='alice'")
        conn.close()

        self.assertEqual(client.get(f"/tickets/{ticket_id}").status_code,
                         200)

    def test_other_users_tickets_are_hidden_with_a_404(self):
        ticket_id = database.create_ticket("Bob's", "D", "Low", "bob")
        client = self.login(self.app, "alice")

        for method, url in (("GET", f"/tickets/{ticket_id}"),
                            ("POST", f"/tickets/{ticket_id}/comment")):
            with self.subTest(method=method):
                response = client.open(url, method=method,
                                       data={"content": "hi"})
                self.assertEqual(response.status_code, 404)

        self.assertEqual(self.query("SELECT * FROM comments"), [])

    def test_staff_only_update_redirects_users_to_the_ticket(self):
        ticket_id = database.create_ticket("Mine", "D", "Low", "alice")
        client = self.login(self.app, "alice")

        response = client.post(f"/tickets/{ticket_id}/update",
                               data={"status": "Closed"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"],
                         f"/tickets/{ticket_id}")
        self.assertEqual(
            self.query("SELECT status FROM tickets")[0]["status"], "Open"
        )

    def test_decorators_protect_a_route_added_later(self):
        # How a future route is added: it inherits every security hook
        # from the application and protects itself with a decorator.
        app = testing_app(**NO_CSRF)
        app.add_url_rule("/probe", "probe",
                         login_required(lambda: "member area"))
        app.add_url_rule("/staff-probe", "staff_probe",
                         staff_required(lambda: "staff area"))

        anonymous = app.test_client().get("/probe")
        self.assertEqual(anonymous.headers["Location"], "/login")
        self.assertIn("Content-Security-Policy", anonymous.headers)

        alice = self.login(app, "alice")
        self.assertEqual(alice.get("/probe").text, "member area")

        refused = alice.get("/staff-probe")
        self.assertEqual(refused.status_code, 302)
        self.assertEqual(refused.headers["Location"], "/")

        tech = self.login(app, "tech")
        self.assertEqual(tech.get("/staff-probe").text, "staff area")

    def test_the_account_is_loaded_once_per_request(self):
        client = self.login(self.app, "alice")
        ticket_id = database.create_ticket("Mine", "D", "Low", "alice")

        for url in ("/", "/tickets/new", f"/tickets/{ticket_id}",
                    "/tickets/export"):
            with self.subTest(url=url):
                with patch.object(database, "get_user_by_id",
                                  wraps=database.get_user_by_id) as lookup:
                    self.assertEqual(client.get(url).status_code, 200)

                self.assertEqual(lookup.call_count, 1)

    def test_logout_revokes_every_copy_of_the_session(self):
        client = self.login(self.app, "alice")
        stolen = client.get_cookie("session").value

        self.assertEqual(client.post("/logout").headers["Location"],
                         "/login")

        thief = self.app.test_client()
        thief.set_cookie("session", stolen)
        self.assertEqual(thief.get("/").headers["Location"], "/login")
        self.assertEqual(
            self.query("SELECT session_version FROM users "
                       "WHERE username='alice'")[0][0], 2
        )

    def test_sessions_expire_eight_hours_after_login(self):
        start = 2_000_000_000

        with patch("helpdesk.clock.current_time", return_value=start):
            client = self.login(self.app, "alice")

        with patch("helpdesk.clock.current_time",
                   return_value=start + 8 * 3600 - 1):
            self.assertEqual(client.get("/").status_code, 200)

        with patch("helpdesk.clock.current_time",
                   return_value=start + 8 * 3600):
            response = client.get("/")

        self.assertEqual(response.headers["Location"], "/login")

    def test_missing_schema_gets_an_explained_503_from_every_blueprint(self):
        client = self.login(self.app, "alice")
        ticket_id = database.create_ticket("Mine", "D", "Low", "alice")

        # A plain connection: get_db() turns on foreign keys, which would
        # refuse to drop a table that comments and history point at
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute("DROP TABLE tickets")
        conn.close()

        for url in ("/", f"/tickets/{ticket_id}", "/tickets/export"):
            with self.subTest(url=url):
                response = client.get(url)

                self.assertEqual(response.status_code, 503)
                self.assertIn("administrator must run the database "
                              "migration", response.text)
                self.assertEqual(response.headers["Retry-After"], "60")

    def test_logout_works_while_the_database_is_unavailable(self):
        client = self.login(self.app, "alice")

        with patch.object(database, "get_user_by_id",
                          side_effect=sqlite3.OperationalError("locked")), \
                patch.object(database, "revoke_user_sessions",
                             side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual(client.get("/").status_code, 503)

            response = client.post("/logout")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login")

        with client.session_transaction() as sess:
            self.assertNotIn("user_id", sess)


# ---------------------------------------------------
# 7. CSRF AND SECURITY HEADERS
# ---------------------------------------------------

class CsrfAndHeaderTests(TemporaryDatabaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.app = testing_app()

    def test_every_post_route_in_every_blueprint_requires_a_csrf_token(self):
        ticket_id = database.create_ticket("Mine", "D", "Low", "alice")
        posts = ("/login", "/logout", "/tickets/new",
                 f"/tickets/{ticket_id}/update",
                 f"/tickets/{ticket_id}/comment")

        for url in posts:
            with self.subTest(url=url):
                response = self.app.test_client().post(
                    url, data={"username": "alice", "password": PASSWORD,
                               "title": "x", "description": "y",
                               "content": "z", "status": "Closed"}
                )

                self.assertEqual(response.status_code, 400)

                # The shared plain-text 400 page, never an HTML template
                self.assertEqual(response.content_type,
                                 "text/plain; charset=utf-8")
                self.assertIn("could not be processed", response.text)

        self.assertEqual(len(self.query("SELECT * FROM tickets")), 1)
        self.assertEqual(self.query("SELECT * FROM comments"), [])

    def test_a_real_token_is_accepted(self):
        client = self.app.test_client()
        page = client.get("/login").text
        token = re.search(r'name="csrf_token"\s+value="([^"]+)"',
                          page).group(1)

        response = client.post("/login", data={
            "csrf_token": token, "username": "alice", "password": PASSWORD
        })

        self.assertEqual(response.status_code, 302)

    def test_headers_are_on_responses_from_every_blueprint_and_errors(self):
        app = testing_app(**NO_CSRF)
        client = self.login(app, "alice")
        ticket_id = database.create_ticket("Mine", "D", "Low", "alice")

        for url in ("/login", "/", "/tickets/new", f"/tickets/{ticket_id}",
                    "/tickets/export", "/no-such-page"):
            with self.subTest(url=url):
                headers = client.get(url).headers

                self.assertIn("default-src 'none'",
                              headers["Content-Security-Policy"])
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertEqual(headers["X-Content-Type-Options"],
                                 "nosniff")
                self.assertEqual(headers["Referrer-Policy"], "same-origin")
                self.assertEqual(headers["Cache-Control"], "no-store")

    def test_static_files_get_headers_but_keep_normal_caching(self):
        response = self.app.test_client().get("/static/style.css")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Content-Security-Policy", response.headers)
        self.assertNotEqual(response.headers.get("Cache-Control"),
                            "no-store")
        response.close()

    def test_untrusted_host_is_refused(self):
        response = self.app.test_client().get(
            "/login", base_url="http://evil.example"
        )

        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------
# 8, 9. TICKET CREATION, UPDATES AND ASSIGNMENT VALIDATION
# ---------------------------------------------------

class TicketWorkflowTests(TemporaryDatabaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.app = testing_app(**NO_CSRF)

    def test_create_comment_and_update_a_ticket(self):
        alice = self.login(self.app, "alice")

        created = alice.post("/tickets/new", data={
            "title": "VPN down", "description": "Cannot connect",
            "priority": "High",
        })
        ticket_id = self.query("SELECT id FROM tickets")[0]["id"]

        self.assertEqual(created.headers["Location"], f"/tickets/{ticket_id}")
        self.assertEqual(
            alice.post(f"/tickets/{ticket_id}/comment",
                       data={"content": "Still down"}).status_code, 302
        )

        tech = self.login(self.app, "tech")
        updated = tech.post(f"/tickets/{ticket_id}/update", data={
            "status": "In Progress", "assigned_to": "tech",
        })

        self.assertEqual(updated.headers["Location"], f"/tickets/{ticket_id}")

        row = self.query("SELECT * FROM tickets")[0]
        self.assertEqual(row["created_by"], "alice")
        self.assertEqual(row["priority"], "High")
        self.assertEqual(row["status"], "In Progress")
        self.assertEqual(row["assigned_to"], "tech")
        self.assertEqual(
            [r["author"] for r in self.query("SELECT author FROM comments")],
            ["alice"]
        )

        page = tech.get(f"/tickets/{ticket_id}").text
        self.assertIn("Still down", page)
        self.assertIn("Ticket updated.", page)

    def test_invalid_ticket_input_is_rejected_without_saving(self):
        alice = self.login(self.app, "alice")

        too_long = alice.post("/tickets/new", data={
            "title": "x" * 1000, "description": "d", "priority": "Low",
        })
        bad_priority = alice.post("/tickets/new", data={
            "title": "t", "description": "d", "priority": "Urgent!!",
        })

        self.assertEqual(too_long.status_code, 400)
        self.assertEqual(bad_priority.status_code, 200)
        self.assertIn("Invalid priority.", bad_priority.text)
        self.assertEqual(self.query("SELECT * FROM tickets"), [])

    def test_assignment_to_an_ineligible_account_saves_nothing(self):
        ticket_id = database.create_ticket("T", "D", "Low", "alice")
        tech = self.login(self.app, "tech")

        for assignee in ("bob", "nobody"):
            with self.subTest(assignee=assignee):
                response = tech.post(
                    f"/tickets/{ticket_id}/update",
                    data={"status": "Closed", "assigned_to": assignee},
                    follow_redirects=True
                )

                self.assertIn("can only be assigned to an active "
                              "technician or administrator", response.text)

        row = self.query("SELECT status, assigned_to FROM tickets")[0]
        self.assertEqual((row["status"], row["assigned_to"]),
                         ("Open", None))

    def test_assignment_form_offers_only_eligible_staff(self):
        ticket_id = database.create_ticket("T", "D", "Low", "alice")
        html = self.login(self.app, "admin").get(f"/tickets/{ticket_id}").text

        options = set(re.findall(r'<option value="([^"]*)"', html))

        self.assertIn("tech", options)
        self.assertIn("admin", options)
        self.assertNotIn("alice", options)
        self.assertNotIn("bob", options)


# ---------------------------------------------------
# 10. SLA DASHBOARD AND CSV EXPORT
# ---------------------------------------------------

class FrozenDateTime(datetime):
    """datetime whose now() is far after any ticket's deadline."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2099, 1, 1, tzinfo=timezone.utc).astimezone(tz)


class SlaAndExportTests(TemporaryDatabaseMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.app = testing_app(**NO_CSRF)
        self.alice_ticket = database.create_ticket(
            "Alice server down", "D", "Critical", "alice")
        self.bob_ticket = database.create_ticket(
            "Bob printer", "D", "Low", "bob")

    def export_rows(self, client, query=""):
        response = client.get("/tickets/export" + query)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"],
                         "text/csv; charset=utf-8")
        self.assertRegex(
            response.headers["Content-Disposition"],
            r'^attachment; filename="helpdesk-tickets-\d{8}-\d{6}\.csv"$'
        )
        text = response.data.decode("utf-8-sig")

        return list(csv.DictReader(io.StringIO(text)))

    def test_users_export_only_their_own_tickets(self):
        rows = self.export_rows(self.login(self.app, "alice"))

        self.assertEqual([r["Title"] for r in rows], ["Alice server down"])

    def test_staff_export_every_ticket(self):
        rows = self.export_rows(self.login(self.app, "tech"))

        self.assertEqual(len(rows), 2)

    def test_dashboard_export_and_ticket_page_share_one_clock(self):
        client = self.login(self.app, "admin")

        self.assertEqual(
            self.export_rows(client, "?sla=overdue"), []
        )

        with patch("helpdesk.clock.datetime", FrozenDateTime):
            rows = self.export_rows(client, "?sla=overdue")
            dashboard = client.get("/?sla=overdue").text
            detail = client.get(f"/tickets/{self.alice_ticket}").text

        self.assertEqual(
            sorted(r["SLA Status"] for r in rows), ["Overdue", "Overdue"]
        )
        self.assertIn("Alice server down", dashboard)
        self.assertIn("Bob printer", dashboard)
        self.assertIn("Overdue", detail)

    def test_dashboard_sla_filter_respects_ownership(self):
        with patch("helpdesk.clock.datetime", FrozenDateTime):
            html = self.login(self.app, "alice").get("/?sla=overdue").text

        self.assertIn("Alice server down", html)
        self.assertNotIn("Bob printer", html)


# ---------------------------------------------------
# 11. DATABASE CLI COMMANDS
# ---------------------------------------------------

class CliThroughFactoryTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.db_path = os.path.join(directory.name, "cli.db")

        patcher = patch.object(database, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.runner = testing_app().test_cli_runner()

    def run_command(self, *args):
        return self.runner.invoke(args=list(args))

    def test_full_lifecycle_on_a_new_database(self):
        missing = self.run_command("db-status")
        self.assertEqual(missing.exit_code, 1)
        self.assertIn("database file does not exist yet", missing.output)
        self.assertFalse(os.path.exists(self.db_path))

        created = self.run_command("init-db")
        self.assertEqual(created.exit_code, 0, created.output)
        self.assertIn(
            f"Database schema is now at version {migrations.SCHEMA_VERSION}",
            created.output
        )

        ready = self.run_command("db-status")
        self.assertEqual(ready.exit_code, 0)
        self.assertIn("Pending migrations: none", ready.output)

        again = self.run_command("init-db")
        self.assertEqual(again.exit_code, 0)
        self.assertIn("already up to date", again.output)

        audit = self.run_command("assignment-audit")
        self.assertEqual(audit.exit_code, 0)
        self.assertIn("Every assigned ticket has an eligible assignee",
                      audit.output)

    def test_schema_version_eight_is_current(self):
        self.assertEqual(migrations.SCHEMA_VERSION, 8)

        self.run_command("init-db")
        status = migrations.schema_status(self.db_path)

        self.assertEqual((status.version, status.state), (8, "current"))

    def test_init_db_backup_option_still_works(self):
        self.run_command("init-db")
        backup = os.path.join(self.directory, "backup.db")

        # Already current, so nothing to back up or migrate
        result = self.run_command("init-db", "--backup-to", backup)

        self.assertEqual(result.exit_code, 0)
        self.assertFalse(os.path.exists(backup))

    def test_audit_reports_an_ineligible_assignee(self):
        self.run_command("init-db")
        conn = database.get_db()
        with conn:
            conn.execute(
                "INSERT INTO tickets (title, description, priority, "
                "created_by, assigned_to) VALUES ('t', 'd', 'Low', 'a', "
                "'ghost')"
            )
        conn.close()

        result = self.run_command("assignment-audit")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("'ghost' (account does not exist)", result.output)

    def test_audit_without_a_database_does_not_create_one(self):
        result = self.run_command("assignment-audit")

        self.assertEqual(result.exit_code, 1)
        self.assertFalse(os.path.exists(self.db_path))

    def test_flask_app_entry_point_lists_the_commands(self):
        # The real "python -m flask --app app" command line. --help reads
        # no database, and FLASK_SKIP_DOTENV keeps .env out of it.
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("APP_ENV", "SECRET_KEY", "FLASK_"))
        }
        environment.update(TESTING_ENV, FLASK_SKIP_DOTENV="1")

        result = subprocess.run(
            [sys.executable, "-m", "flask", "--app", "app", "--help"],
            cwd=PROJECT_ROOT, env=environment, capture_output=True,
            text=True, timeout=60
        )

        self.assertEqual(result.returncode, 0, result.stderr)

        for command in ("db-status", "init-db", "assignment-audit"):
            self.assertIn(command, result.stdout)


# ---------------------------------------------------
# 12, 13. CLEAN ENVIRONMENTS AND PRODUCTION FAILURES
# ---------------------------------------------------

class EnvironmentLoadingTests(unittest.TestCase):

    def run_without_environ(self, settings):
        environment = {
            key: value for key, value in os.environ.items()
            if key not in ("APP_ENV", "SECRET_KEY", "TRUSTED_HOSTS",
                           "FLASK_DEBUG", "TRUSTED_PROXY_COUNT",
                           "TRUSTED_PROXY_IPS")
        }
        environment.update(settings)

        with patch.dict(os.environ, environment, clear=True), \
                patch("dotenv.load_dotenv") as load:
            app = create_app()

        return app, load

    def test_development_loads_the_dotenv_file(self):
        _, load = self.run_without_environ(DEVELOPMENT_ENV)

        load.assert_called_once_with()

    def test_testing_and_production_never_load_dotenv(self):
        for settings in (TESTING_ENV, PRODUCTION_ENV):
            with self.subTest(environment=settings["APP_ENV"]):
                _, load = self.run_without_environ(settings)

                load.assert_not_called()

    def test_explicit_environ_never_loads_dotenv(self):
        with patch("dotenv.load_dotenv") as load:
            create_app(environ=dict(DEVELOPMENT_ENV))

        load.assert_not_called()

    def test_clean_testing_environment_needs_only_a_secret_key(self):
        app = create_app(environ={"APP_ENV": "testing",
                                  "SECRET_KEY": TEST_SECRET_KEY})

        self.assertEqual(app.test_client().get("/login").status_code, 200)


class ProductionConfigurationFailureTests(unittest.TestCase):

    def assert_refused(self, environ, *expected):
        with self.assertRaises(security_config.ConfigurationError) as caught:
            create_app(environ=environ)

        message = str(caught.exception)

        for text in expected:
            self.assertIn(text, message)

        return message

    def test_missing_secret_key_is_refused(self):
        environ = dict(PRODUCTION_ENV)
        del environ["SECRET_KEY"]

        self.assert_refused(environ, "SECRET_KEY is not set")

    def test_weak_key_is_refused_without_being_echoed(self):
        weak = "changeme-changeme-changeme-changeme"
        message = self.assert_refused(
            dict(PRODUCTION_ENV, SECRET_KEY=weak), "SECRET_KEY"
        )

        self.assertNotIn(weak, message)

    def test_missing_trusted_hosts_is_refused(self):
        environ = dict(PRODUCTION_ENV)
        del environ["TRUSTED_HOSTS"]

        self.assert_refused(environ, "TRUSTED_HOSTS is required")

    def test_debug_mode_is_refused(self):
        self.assert_refused(dict(PRODUCTION_ENV, FLASK_DEBUG="1"),
                            "FLASK_DEBUG")

    def test_half_configured_proxy_is_refused(self):
        self.assert_refused(dict(PRODUCTION_ENV, TRUSTED_PROXY_COUNT="1"),
                            "TRUSTED_PROXY_IPS is required")

    def test_unknown_environment_is_refused(self):
        self.assert_refused(dict(PRODUCTION_ENV, APP_ENV="staging"),
                            "APP_ENV must be one of")

    def test_every_problem_is_reported_together(self):
        self.assert_refused(
            {"APP_ENV": "production", "FLASK_DEBUG": "true"},
            "SECRET_KEY", "FLASK_DEBUG", "TRUSTED_HOSTS"
        )

    def test_production_app_refuses_plain_http_before_anything_else(self):
        app = create_app(environ=dict(PRODUCTION_ENV))

        response = app.test_client().post("/login", base_url=HTTP_URL)

        self.assertEqual(response.status_code, 400)
        self.assertIn("must be accessed over HTTPS", response.text)

    def test_trusted_proxy_makes_forwarded_https_secure(self):
        app = create_app(environ=dict(PRODUCTION_ENV,
                                      TRUSTED_PROXY_COUNT="1",
                                      TRUSTED_PROXY_IPS="10.0.0.2"))
        client = app.test_client()

        from_proxy = client.get(
            "/login", base_url=HTTP_URL,
            headers={"X-Forwarded-Proto": "https"},
            environ_base={"REMOTE_ADDR": "10.0.0.2"}
        )
        forged = client.get(
            "/login", base_url=HTTP_URL,
            headers={"X-Forwarded-Proto": "https"},
            environ_base={"REMOTE_ADDR": "203.0.113.9"}
        )

        self.assertEqual(from_proxy.status_code, 200)
        self.assertEqual(forged.status_code, 400)


# ---------------------------------------------------
# 14. RESOURCE AND CONNECTION CLEANUP
# ---------------------------------------------------

class ConnectionCleanupTests(TemporaryDatabaseMixin, unittest.TestCase):
    """Every SQLite connection a request opens is closed again."""

    def setUp(self):
        super().setUp()
        self.app = testing_app(**NO_CSRF)
        self.opened = []

        real_connect = sqlite3.connect

        def tracking_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            self.opened.append(conn)
            return conn

        patcher = patch("sqlite3.connect", side_effect=tracking_connect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_all_closed(self):
        self.assertTrue(self.opened, "no connections were tracked")

        for conn in self.opened:
            # Any use of a closed connection raises ProgrammingError
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.total_changes

    def test_a_full_session_closes_every_connection(self):
        alice = self.login(self.app, "alice")
        alice.post("/tickets/new", data={
            "title": "t", "description": "d", "priority": "Low"})
        ticket_id = database.get_db().execute(
            "SELECT id FROM tickets").fetchone()[0]

        # That last helper connection is closed here, as callers must
        self.opened[-1].close()

        alice.get("/")
        alice.get(f"/tickets/{ticket_id}")
        alice.post(f"/tickets/{ticket_id}/comment", data={"content": "c"})
        alice.get("/tickets/export")

        tech = self.login(self.app, "tech")
        tech.post(f"/tickets/{ticket_id}/update",
                  data={"status": "Closed", "assigned_to": "tech"})
        tech.post(f"/tickets/{ticket_id}/update",
                  data={"status": "Closed", "assigned_to": "bob"})
        alice.post("/logout")

        self.assert_all_closed()

    def test_error_paths_close_their_connections(self):
        alice = self.login(self.app, "alice")
        other = database.create_ticket("Bob's", "D", "Low", "bob")

        self.assertEqual(alice.get(f"/tickets/{other}").status_code, 404)
        self.assertEqual(alice.get("/tickets/999999").status_code, 404)
        self.assertEqual(
            alice.post("/login",
                       data={"username": "x", "password": "y"}).status_code,
            302
        )

        self.assert_all_closed()

    def test_cli_commands_close_their_connections(self):
        runner = self.app.test_cli_runner()

        for command in ("db-status", "init-db", "assignment-audit"):
            runner.invoke(args=[command])

        self.assert_all_closed()

    def test_failed_write_rolls_back_and_closes(self):
        ticket_id = database.create_ticket("T", "D", "Low", "alice")
        tech = self.login(self.app, "tech")
        history_sql = ("SELECT action FROM ticket_history "
                       "WHERE ticket_id = ? ORDER BY id")

        # Only the "created" event exists before the update
        self.assertEqual([row[0] for row in self.query(history_sql,
                                                       ticket_id)],
                         ["created"])

        # Status and a refused assignee in one request: nothing is saved
        tech.post(f"/tickets/{ticket_id}/update",
                  data={"status": "Resolved", "assigned_to": "alice"})

        self.assert_all_closed()
        self.assertEqual(
            self.query("SELECT status FROM tickets")[0]["status"], "Open"
        )
        self.assertEqual([row[0] for row in self.query(history_sql,
                                                       ticket_id)],
                         ["created"])


if __name__ == "__main__":
    unittest.main()
