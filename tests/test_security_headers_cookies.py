"""Tests for session cookies, HTTP security headers and the CSP."""

import re
import unittest
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from unittest.mock import patch

import database
import security_config as config
from security_helpers import (
    DEVELOPMENT_ENV,
    HTTP_URL,
    LOCAL_URL,
    PROJECT_ROOT,
    SecurityTestCase,
)

PROD_COOKIE = config.PRODUCTION_SESSION_COOKIE_NAME

# The published hashes of the exact CDN files this application uses
EXPECTED_INTEGRITY = {
    "bootstrap/5.3.2/css/bootstrap.min.css":
        "sha512-b2QcS5SsA8tZodcDtGRELiGv5SaKSk1vDHDaQRda0htPYWZ6046lr3kJ5bAAQ"
        "dpV2mmA/4v0wQF9MyU6/pDIAg==",
    "bootstrap-icons/1.11.3/font/bootstrap-icons.min.css":
        "sha512-dPXYcDub/aeb08c63jRq/k6GaKccl256JQy/AnOq7CAnEZ9FzSL9wSbcZkMp4"
        "R26vBsMLFYH4kQ67/bbV8XaCQ==",
    "bootstrap/5.3.2/js/bootstrap.bundle.min.js":
        "sha512-X/YkDZyjTf4wyc2Vy16YGCPHwAY8rZJY+POgokZjQB2mhIRFJCckEGc6YyX9e"
        "NsPfn0PzThEuNs+uaomE5CO6A==",
}


def session_cookie_header(response, name):
    for header in response.headers.getlist("Set-Cookie"):
        if header.startswith(f"{name}="):
            return header

    raise AssertionError(f"{name} cookie was not set")


def cookie_attributes(header):
    """The lower-case attribute names and values after the cookie value."""

    attributes = {}

    for part in header.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        attributes[key.lower()] = value

    return attributes


def csp_directives(response):
    policy = response.headers["Content-Security-Policy"]

    return {
        item.split()[0]: item.split()[1:]
        for item in policy.split(";")
    }


class TestProductionCookies(SecurityTestCase):

    def login_response(self):
        client = self.app.test_client()

        return client, self.post_form(
            client,
            "/login",
            {"username": "alice", "password": "correct-horse-battery-staple"},
            token_page="/login"
        )

    def test_cookie_has_all_production_protections(self):
        _, response = self.login_response()
        attributes = cookie_attributes(
            session_cookie_header(response, PROD_COOKIE)
        )

        self.assertIn("secure", attributes)
        self.assertIn("httponly", attributes)
        self.assertEqual(attributes["samesite"], "Lax")

    def test_host_prefix_requirements_are_met(self):
        _, response = self.login_response()
        attributes = cookie_attributes(
            session_cookie_header(response, PROD_COOKIE)
        )

        self.assertEqual(attributes["path"], "/")
        self.assertNotIn("domain", attributes)

    def test_cookie_lifetime_stays_eight_hours(self):
        _, response = self.login_response()
        attributes = cookie_attributes(
            session_cookie_header(response, PROD_COOKIE)
        )

        remaining = (
            parsedate_to_datetime(attributes["expires"])
            - datetime.now(timezone.utc)
        ).total_seconds()

        self.assertAlmostEqual(remaining, 8 * 60 * 60, delta=60)


class TestDevelopmentCookies(SecurityTestCase):

    settings = DEVELOPMENT_ENV
    base_url = LOCAL_URL

    def test_cookie_works_over_plain_http(self):
        client = self.login("alice")

        self.assertEqual(self.get(client, "/").status_code, 200)

    def test_cookie_is_not_secure_but_keeps_other_protections(self):
        client = self.app.test_client()
        response = self.post_form(
            client,
            "/login",
            {"username": "alice", "password": "correct-horse-battery-staple"},
            token_page="/login"
        )
        attributes = cookie_attributes(
            session_cookie_header(response, "session")
        )

        self.assertNotIn("secure", attributes)
        self.assertIn("httponly", attributes)
        self.assertEqual(attributes["samesite"], "Lax")

    def test_development_sends_no_hsts(self):
        response = self.get(self.app.test_client(), "/login")

        self.assertNotIn("Strict-Transport-Security", response.headers)

    def test_development_sends_no_hsts_even_over_https(self):
        response = self.app.test_client().get(
            "/login", base_url="https://localhost"
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Strict-Transport-Security", response.headers)

    def test_development_does_not_upgrade_requests(self):
        response = self.get(self.app.test_client(), "/login")

        self.assertNotIn(
            "upgrade-insecure-requests",
            response.headers["Content-Security-Policy"]
        )

    def test_development_rejects_other_hosts(self):
        response = self.app.test_client().get(
            "/login", base_url="http://evil.test"
        )

        self.assertEqual(response.status_code, 400)


class TestSessionProtectionsInProduction(SecurityTestCase):

    def replay(self, cookie_value):
        """Send a copied cookie from a different client."""

        return self.app.test_client(use_cookies=False).get(
            "/",
            base_url=self.base_url,
            headers={"Cookie": f"{PROD_COOKIE}={cookie_value}"}
        )

    def test_copied_cookie_is_revoked_by_logout(self):
        client = self.login("alice")
        stolen = client.get_cookie(PROD_COOKIE, domain="helpdesk.test").value

        self.assertEqual(self.replay(stolen).status_code, 200)

        response = self.post_form(client, "/logout", {})
        self.assertEqual(response.status_code, 302)

        replayed = self.replay(stolen)
        self.assertEqual(replayed.status_code, 302)
        self.assertIn("/login", replayed.headers["Location"])

    def test_session_expires_after_eight_hours(self):
        client = self.login("alice")
        now = self.module.current_time()

        with patch.object(
            self.module, "current_time", return_value=now + 8 * 3600 - 60
        ):
            self.assertEqual(self.get(client, "/").status_code, 200)

        with patch.object(
            self.module, "current_time", return_value=now + 8 * 3600
        ):
            response = self.get(client, "/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_deactivated_account_loses_access(self):
        client = self.login("alice")

        conn = database.get_db()
        conn.execute("UPDATE users SET is_active = 0 WHERE username='alice'")
        conn.commit()
        conn.close()

        self.assertEqual(self.get(client, "/").status_code, 302)


class TestCsrfInProduction(SecurityTestCase):

    def test_post_without_token_is_rejected(self):
        client = self.login("alice")
        before = self.count("tickets")

        response = client.post(
            "/tickets/new",
            data={"title": "x", "description": "y"},
            base_url=self.base_url,
            headers=self.referer_headers()
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.count("tickets"), before)

    def test_https_post_needs_same_site_referer(self):
        client = self.login("alice")
        token = self.csrf_token(client, "/tickets/new")
        before = self.count("tickets")

        for headers in ({}, {"Referer": "https://evil.test/"}):
            with self.subTest(headers=headers):
                response = client.post(
                    "/tickets/new",
                    data={
                        "csrf_token": token,
                        "title": "x",
                        "description": "y"
                    },
                    base_url=self.base_url,
                    headers=headers
                )

                self.assertEqual(response.status_code, 400)

        self.assertEqual(self.count("tickets"), before)

    def test_csrf_error_does_not_show_a_template_or_detail(self):
        response = self.app.test_client().post(
            "/login", data={}, base_url=self.base_url,
            headers=self.referer_headers()
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.mimetype, "text/plain")
        self.assertNotIn("Traceback", response.text)


class TestSecurityHeaders(SecurityTestCase):

    def assert_required_headers(self, response):
        headers = response.headers

        self.assertIn("Content-Security-Policy", headers)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Referrer-Policy"], "same-origin")
        self.assertIn("camera=()", headers["Permissions-Policy"])
        self.assertEqual(headers["Cross-Origin-Opener-Policy"], "same-origin")

    def test_headers_on_every_kind_of_response(self):
        client = self.login("admin")
        ticket_id = self.make_ticket()

        for path, status in (
            ("/", 200),
            ("/tickets/new", 200),
            (f"/tickets/{ticket_id}", 200),
            ("/tickets/export", 200),
            ("/tickets/999999", 404),
            ("/static/style.css", 200),
        ):
            with self.subTest(path=path):
                response = self.get(client, path)

                self.assertEqual(response.status_code, status)
                self.assert_required_headers(response)

    def test_headers_on_error_responses(self):
        client = self.app.test_client()

        for response in (
            client.get("/login", base_url="https://evil.test"),
            client.post("/login", base_url=self.base_url),
            client.get("/login", base_url=HTTP_URL),
        ):
            self.assertEqual(response.status_code, 400)
            self.assert_required_headers(response)

    def test_sensitive_pages_are_not_cached(self):
        client = self.login("admin")
        ticket_id = self.make_ticket()

        for path in (
            "/login", "/", "/tickets/new",
            f"/tickets/{ticket_id}", "/tickets/export", "/tickets/999999",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.get(client, path).headers["Cache-Control"],
                    "no-store"
                )

    def test_public_static_files_stay_cacheable(self):
        response = self.get(self.app.test_client(), "/static/style.css")

        self.assertNotIn("no-store", response.headers.get("Cache-Control", ""))

    def test_hsts_on_production_https(self):
        response = self.get(self.app.test_client(), "/login")

        self.assertEqual(
            response.headers["Strict-Transport-Security"],
            "max-age=31536000"
        )

    def test_no_hsts_on_production_plain_http(self):
        response = self.app.test_client().get("/login", base_url=HTTP_URL)

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("Strict-Transport-Security", response.headers)


class TestContentSecurityPolicy(SecurityTestCase):

    def policy(self):
        return csp_directives(self.get(self.app.test_client(), "/login"))

    def test_nothing_is_allowed_by_default(self):
        policy = self.policy()

        self.assertEqual(policy["default-src"], ["'none'"])
        self.assertEqual(policy["object-src"], ["'none'"])
        self.assertEqual(policy["base-uri"], ["'none'"])
        self.assertEqual(policy["frame-ancestors"], ["'none'"])
        self.assertEqual(policy["connect-src"], ["'none'"])
        self.assertEqual(policy["form-action"], ["'self'"])

    def test_no_unsafe_keywords_or_wildcards(self):
        header = self.get(
            self.app.test_client(), "/login"
        ).headers["Content-Security-Policy"]

        for forbidden in (
            "unsafe-inline", "unsafe-eval", "unsafe-hashes",
            "*", "http:", "https: ",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, header)

    def test_cdn_is_limited_to_exact_files(self):
        policy = self.policy()

        for source in policy["script-src"] + policy["style-src"]:
            if source.startswith("https://"):
                self.assertTrue(source.endswith((".min.js", ".min.css")))

    def test_only_images_may_use_data_urls(self):
        policy = self.policy()

        self.assertIn("data:", policy["img-src"])

        for name, sources in policy.items():
            if name != "img-src":
                self.assertNotIn("data:", sources)

    def test_production_upgrades_insecure_requests(self):
        self.assertIn("upgrade-insecure-requests", self.policy())


class TestTemplateCompatibility(SecurityTestCase):
    """The templates must not need anything the CSP blocks."""

    def rendered_pages(self):
        ticket_id = self.make_ticket()
        client = self.login("admin")

        return [
            self.get(self.app.test_client(), "/login").text,
            self.get(client, "/").text,
            self.get(client, "/tickets/new").text,
            self.get(client, f"/tickets/{ticket_id}").text,
        ]

    def test_rendered_pages_have_no_inline_code_or_styles(self):
        for html in self.rendered_pages():
            self.assertIsNone(re.search(r"<style\b", html, re.I))
            self.assertIsNone(re.search(r"<[^>]*\sstyle\s*=", html, re.I))
            self.assertIsNone(re.search(r"<[^>]*\son[a-z]+\s*=", html, re.I))
            self.assertNotIn("javascript:", html.lower())

            for tag in re.findall(r"<script\b[^>]*>", html, re.I):
                self.assertIn("src=", tag)

    def test_template_sources_have_no_inline_code_or_styles(self):
        for path in (PROJECT_ROOT / "templates").glob("*.html"):
            with self.subTest(template=path.name):
                source = path.read_text(encoding="utf-8")

                self.assertIsNone(re.search(r"\sstyle\s*=\s*[\"']", source))
                self.assertIsNone(re.search(r"\son[a-z]+\s*=\s*[\"']", source))

    def test_external_assets_are_allowed_and_integrity_checked(self):
        html = self.rendered_pages()[0]
        policy = csp_directives(self.get(self.app.test_client(), "/login"))

        tags = re.findall(
            r'<(?:link|script)\b[^>]*(?:href|src)="https://[^>]*>', html
        )
        self.assertEqual(len(tags), len(EXPECTED_INTEGRITY))

        for tag in tags:
            url = re.search(r'(?:href|src)="(https://[^"]+)"', tag).group(1)
            integrity = re.search(r'integrity="([^"]+)"', tag).group(1)
            library_path = url.split("/ajax/libs/")[1]

            self.assertEqual(integrity, EXPECTED_INTEGRITY[library_path])
            self.assertIn('crossorigin="anonymous"', tag)

            directive = "script-src" if url.endswith(".js") else "style-src"
            self.assertIn(url, policy[directive])

    def test_local_script_is_served_and_used(self):
        html = self.rendered_pages()[1]

        self.assertIn('src="/static/app.js"', html)
        self.assertIn("data-auto-submit", html)

        script = self.get(self.app.test_client(), "/static/app.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("data-auto-submit", script.text)


class TestApplicationRegressionInProduction(SecurityTestCase):
    """The main workflows must work with every protection switched on."""

    def test_full_ticket_workflow(self):
        user = self.login("alice")

        response = self.post_form(user, "/tickets/new", {
            "title": "VPN down",
            "description": "Cannot connect",
            "priority": "High"
        })
        self.assertEqual(response.status_code, 302)
        ticket_path = response.headers["Location"]

        response = self.post_form(
            user, f"{ticket_path}/comment", {"content": "Still down"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("Still down", self.get(user, ticket_path).text)

        staff = self.login("tech")
        response = self.post_form(staff, f"{ticket_path}/update", {
            "status": "In Progress",
            "assigned_to": "tech"
        })
        self.assertEqual(response.status_code, 302)
        self.assertIn("In Progress", self.get(staff, ticket_path).text)

    def test_users_cannot_reach_other_users_tickets(self):
        ticket_id = self.make_ticket(owner="alice")
        bob = self.login("bob")

        self.assertEqual(
            self.get(bob, f"/tickets/{ticket_id}").status_code, 404
        )

        before = self.count("comments")
        response = self.post_form(
            bob, f"/tickets/{ticket_id}/comment", {"content": "intrusion"}
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.count("comments"), before)

    def test_regular_user_cannot_update_ticket(self):
        ticket_id = self.make_ticket(owner="alice")
        alice = self.login("alice")

        self.post_form(alice, f"/tickets/{ticket_id}/update", {
            "status": "Closed", "assigned_to": "alice"
        })

        self.assertEqual(database.get_ticket(ticket_id)["status"], "Open")

    def test_signed_out_visitor_is_sent_to_login(self):
        for path in ("/", "/tickets/new", "/tickets/export", "/tickets/1"):
            with self.subTest(path=path):
                response = self.get(self.app.test_client(), path)

                self.assertEqual(response.status_code, 302)
                self.assertIn("/login", response.headers["Location"])

    def test_dashboard_search_and_sla_filter(self):
        self.make_ticket(title="Laptop screen")
        self.make_ticket(title="Keyboard fault")
        admin = self.login("admin")

        self.assertIn("Laptop screen", self.get(admin, "/").text)

        searched = self.get(admin, "/?q=Keyboard").text
        self.assertIn("Keyboard fault", searched)
        self.assertNotIn("Laptop screen", searched)

        filtered = self.get(admin, "/?sla=within")
        self.assertEqual(filtered.status_code, 200)
        self.assertIn("Laptop screen", filtered.text)

    def test_ticket_page_shows_sla(self):
        ticket_id = self.make_ticket()
        admin = self.login("admin")

        self.assertIn("SLA", self.get(admin, f"/tickets/{ticket_id}").text)

    def test_csv_export_respects_ownership(self):
        self.make_ticket(owner="alice", title="Alice ticket")
        self.make_ticket(owner="bob", title="Bob ticket")

        response = self.get(self.login("alice"), "/tickets/export")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("Alice ticket", response.text)
        self.assertNotIn("Bob ticket", response.text)


if __name__ == "__main__":
    unittest.main()
