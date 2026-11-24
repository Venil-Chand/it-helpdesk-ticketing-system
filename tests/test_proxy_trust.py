"""Tests for forwarded-header trust, HTTPS detection and Host validation."""

import ipaddress
import re
import unittest

from proxy_trust import TrustedProxyMiddleware, is_trusted_peer
from security_helpers import (
    HTTP_URL,
    HTTPS_URL,
    PASSWORD,
    PRODUCTION_ENV,
    PROXIED_PRODUCTION_ENV,
    SecurityTestCase,
)

PROXY_ADDRESS = "10.0.0.2"
ATTACKER_ADDRESS = "203.0.113.9"


def network(*values):
    return tuple(ipaddress.ip_network(value) for value in values)


class TestIsTrustedPeer(unittest.TestCase):

    def test_listed_address_and_range_are_trusted(self):
        networks = network("10.0.0.2", "192.168.1.0/24", "fd00::1")

        for address in ("10.0.0.2", "192.168.1.200", "fd00::1"):
            with self.subTest(address=address):
                self.assertTrue(is_trusted_peer(address, networks))

    def test_other_addresses_are_not_trusted(self):
        networks = network("10.0.0.2", "192.168.1.0/24")

        for address in ("10.0.0.3", "192.168.2.1", "127.0.0.1", "fd00::2"):
            with self.subTest(address=address):
                self.assertFalse(is_trusted_peer(address, networks))

    def test_ipv4_mapped_address_is_recognised(self):
        networks = network("10.0.0.2")

        self.assertTrue(is_trusted_peer("::ffff:10.0.0.2", networks))
        self.assertFalse(is_trusted_peer("::ffff:10.0.0.3", networks))

    def test_missing_or_invalid_peers_are_not_trusted(self):
        networks = network("10.0.0.2")

        for address in (None, "", "unix:", "/run/app.sock", "not-an-ip"):
            with self.subTest(address=address):
                self.assertFalse(is_trusted_peer(address, networks))


class TestMiddleware(unittest.TestCase):

    def run_request(self, count, networks, **environ):
        seen = {}

        def application(wsgi_environ, start_response):
            seen.update(wsgi_environ)
            return []

        middleware = TrustedProxyMiddleware(application, count, networks)
        request = {"wsgi.url_scheme": "http", **environ}
        middleware(request, lambda *args: None)

        return seen

    def test_no_proxy_configured_removes_forwarded_headers(self):
        seen = self.run_request(
            0, (),
            REMOTE_ADDR=PROXY_ADDRESS,
            HTTP_X_FORWARDED_FOR="1.2.3.4",
            HTTP_X_FORWARDED_PROTO="https"
        )

        self.assertEqual(seen["REMOTE_ADDR"], PROXY_ADDRESS)
        self.assertEqual(seen["wsgi.url_scheme"], "http")
        self.assertNotIn("HTTP_X_FORWARDED_FOR", seen)
        self.assertNotIn("HTTP_X_FORWARDED_PROTO", seen)

    def test_untrusted_peer_cannot_use_forwarded_headers(self):
        seen = self.run_request(
            1, network(PROXY_ADDRESS),
            REMOTE_ADDR=ATTACKER_ADDRESS,
            HTTP_X_FORWARDED_FOR="1.2.3.4",
            HTTP_X_FORWARDED_PROTO="https"
        )

        self.assertEqual(seen["REMOTE_ADDR"], ATTACKER_ADDRESS)
        self.assertEqual(seen["wsgi.url_scheme"], "http")
        self.assertNotIn("HTTP_X_FORWARDED_FOR", seen)

    def test_trusted_proxy_supplies_client_and_scheme(self):
        seen = self.run_request(
            1, network(PROXY_ADDRESS),
            REMOTE_ADDR=PROXY_ADDRESS,
            HTTP_X_FORWARDED_FOR="198.51.100.7",
            HTTP_X_FORWARDED_PROTO="https"
        )

        self.assertEqual(seen["REMOTE_ADDR"], "198.51.100.7")
        self.assertEqual(seen["wsgi.url_scheme"], "https")

    def test_only_the_proxy_written_value_is_used(self):
        seen = self.run_request(
            1, network(PROXY_ADDRESS),
            REMOTE_ADDR=PROXY_ADDRESS,
            HTTP_X_FORWARDED_FOR="6.6.6.6, 198.51.100.7"
        )

        self.assertEqual(seen["REMOTE_ADDR"], "198.51.100.7")

    def test_two_proxies_use_the_second_value_from_the_right(self):
        seen = self.run_request(
            2, network(PROXY_ADDRESS),
            REMOTE_ADDR=PROXY_ADDRESS,
            HTTP_X_FORWARDED_FOR="6.6.6.6, 198.51.100.7, 10.0.0.1",
            HTTP_X_FORWARDED_PROTO="http, https"
        )

        self.assertEqual(seen["REMOTE_ADDR"], "198.51.100.7")
        self.assertEqual(seen["wsgi.url_scheme"], "http")

    def test_too_few_forwarded_values_are_ignored(self):
        seen = self.run_request(
            2, network(PROXY_ADDRESS),
            REMOTE_ADDR=PROXY_ADDRESS,
            HTTP_X_FORWARDED_FOR="198.51.100.7"
        )

        self.assertEqual(seen["REMOTE_ADDR"], PROXY_ADDRESS)

    def test_host_port_and_prefix_are_never_used(self):
        for peer in (PROXY_ADDRESS, ATTACKER_ADDRESS):
            with self.subTest(peer=peer):
                seen = self.run_request(
                    1, network(PROXY_ADDRESS),
                    REMOTE_ADDR=peer,
                    HTTP_HOST="helpdesk.test",
                    HTTP_X_FORWARDED_HOST="evil.test",
                    HTTP_X_FORWARDED_PORT="8443",
                    HTTP_X_FORWARDED_PREFIX="/evil",
                    HTTP_FORWARDED="host=evil.test;proto=https"
                )

                self.assertEqual(seen["HTTP_HOST"], "helpdesk.test")
                self.assertEqual(seen.get("SCRIPT_NAME", ""), "")

                for header in (
                    "HTTP_X_FORWARDED_HOST",
                    "HTTP_X_FORWARDED_PORT",
                    "HTTP_X_FORWARDED_PREFIX",
                    "HTTP_FORWARDED",
                ):
                    self.assertNotIn(header, seen)


class TestProductionWithoutProxy(SecurityTestCase):

    settings = PRODUCTION_ENV
    base_url = HTTP_URL

    def test_forged_https_header_is_not_believed(self):
        response = self.app.test_client().get(
            "/login",
            base_url=HTTP_URL,
            headers={"X-Forwarded-Proto": "https"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("Strict-Transport-Security", response.headers)

    def test_rotating_forwarded_for_cannot_avoid_rate_limit(self):
        statuses = []

        for number in range(7):
            client = self.app.test_client()
            page = client.get("/login", base_url=HTTPS_URL)
            token = re.search(
                r'name="csrf_token"\s+value="([^"]+)"', page.text
            ).group(1)

            statuses.append(client.post(
                "/login",
                data={
                    "csrf_token": token,
                    "username": "alice",
                    "password": "wrong-password"
                },
                base_url=HTTPS_URL,
                headers={
                    "Referer": f"{HTTPS_URL}/",
                    "X-Forwarded-For": f"198.51.100.{number}"
                }
            ).status_code)

        self.assertEqual(statuses[:5], [200] * 5)
        self.assertEqual(statuses[5:], [429, 429])


class TestProductionBehindProxy(SecurityTestCase):

    settings = PROXIED_PRODUCTION_ENV
    base_url = HTTPS_URL

    def send(self, client, method, path, *, client_ip="198.51.100.7",
             proto="https", peer=PROXY_ADDRESS, headers=None, **kwargs):
        """Send a request the way the reverse proxy would forward it."""

        all_headers = {
            "Host": "helpdesk.test",
            "X-Forwarded-For": client_ip,
            "X-Forwarded-Proto": proto,
            **(headers or {}),
        }

        return client.open(
            path,
            method=method,
            base_url=HTTP_URL,
            headers=all_headers,
            environ_overrides={"REMOTE_ADDR": peer},
            **kwargs
        )

    def proxied_login(self, client, password, client_ip, peer=PROXY_ADDRESS):
        page = self.send(client, "GET", "/login", client_ip=client_ip)
        token = re.search(
            r'name="csrf_token"\s+value="([^"]+)"', page.text
        ).group(1)

        return self.send(
            client, "POST", "/login",
            client_ip=client_ip,
            peer=peer,
            headers={"Referer": f"{HTTPS_URL}/login"},
            data={
                "csrf_token": token,
                "username": "alice",
                "password": password
            }
        )

    def test_https_from_proxy_is_accepted(self):
        response = self.send(self.app.test_client(), "GET", "/login")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Strict-Transport-Security", response.headers)

    def test_http_from_proxy_is_refused(self):
        response = self.send(
            self.app.test_client(), "GET", "/login", proto="http"
        )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("Strict-Transport-Security", response.headers)

    def test_forged_https_from_other_address_is_refused(self):
        response = self.send(
            self.app.test_client(), "GET", "/login", peer=ATTACKER_ADDRESS
        )

        self.assertEqual(response.status_code, 400)

    def test_login_works_through_proxy(self):
        response = self.proxied_login(
            self.app.test_client(), PASSWORD, "198.51.100.7"
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("Secure", response.headers["Set-Cookie"])

    def test_spoofed_left_values_share_one_rate_limit(self):
        statuses = [
            self.proxied_login(
                self.app.test_client(),
                "wrong-password",
                f"{number}.1.1.1, 198.51.100.50"
            ).status_code
            for number in range(1, 8)
        ]

        self.assertEqual(statuses[:5], [200] * 5)
        self.assertEqual(statuses[5:], [429, 429])

    def test_different_real_clients_have_separate_limits(self):
        for _ in range(6):
            blocked = self.proxied_login(
                self.app.test_client(), "wrong-password", "198.51.100.50"
            )

        self.assertEqual(blocked.status_code, 429)

        other = self.proxied_login(
            self.app.test_client(), "wrong-password", "198.51.100.51"
        )

        self.assertEqual(other.status_code, 200)

    def test_forwarded_host_cannot_replace_the_host(self):
        response = self.send(
            self.app.test_client(), "GET", "/login",
            headers={"X-Forwarded-Host": "evil.test"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("evil.test", response.text)


class TestHostValidation(SecurityTestCase):

    settings = PRODUCTION_ENV
    base_url = HTTPS_URL

    def test_configured_host_is_accepted(self):
        response = self.get(self.app.test_client(), "/login")

        self.assertEqual(response.status_code, 200)

    def test_other_hosts_are_refused_without_reflection(self):
        for host in (
            "evil.test",
            "helpdesk.test.evil.test",
            "evilhelpdesk.test",
            "localhost",
            "127.0.0.1",
        ):
            with self.subTest(host=host):
                response = self.app.test_client().get(
                    "/login",
                    base_url=HTTPS_URL,
                    headers={"Host": host}
                )

                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.mimetype, "text/plain")
                self.assertNotIn(host, response.text)

    def test_bad_host_cannot_reach_a_view_with_valid_session(self):
        client = self.login("alice")

        response = client.get(
            "/",
            base_url=HTTPS_URL,
            headers={"Host": "evil.test"}
        )

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
