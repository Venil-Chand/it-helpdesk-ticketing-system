"""
Decide when headers added by a reverse proxy can be believed.

Anyone can send a request containing "X-Forwarded-For: 1.2.3.4". If the
application believed it, an attacker could pretend to be a new visitor on
every attempt and never reach the login rate limit.

So forwarded headers are used only when the network connection itself comes
from a configured proxy address. A client cannot fake that address, because
it is the other end of the TCP connection. Otherwise the headers are removed
before Flask or any extension can read them.
"""

import ipaddress

from werkzeug.middleware.proxy_fix import ProxyFix

# Believed only from a trusted proxy
_CLIENT_AND_SCHEME_HEADERS = (
    "HTTP_X_FORWARDED_FOR",
    "HTTP_X_FORWARDED_PROTO",
)

# Never used. The proxy must pass the real Host header, which TRUSTED_HOSTS
# then checks, so these could only be used to get around that check.
_ALWAYS_REMOVED_HEADERS = (
    "HTTP_FORWARDED",
    "HTTP_X_FORWARDED_HOST",
    "HTTP_X_FORWARDED_PORT",
    "HTTP_X_FORWARDED_PREFIX",
)


def is_trusted_peer(remote_addr, networks):
    """Check whether the connection comes from a configured proxy."""

    try:
        address = ipaddress.ip_address(remote_addr or "")
    except ValueError:
        # Includes Unix sockets, which have no address to verify
        return False

    # A dual-stack server reports IPv4 clients as ::ffff:10.0.0.1
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped

    return any(address in network for network in networks)


class TrustedProxyMiddleware:
    """
    Apply ProxyFix only to connections from a trusted proxy.

    WSGI middleware wraps the application and sees each request first.
    x_for and x_proto are set to the number of proxies, because each proxy
    appends one value and only the rightmost values were written by a
    proxy. Anything further left may have been supplied by the client.
    """

    def __init__(self, wsgi_app, proxy_count, networks):
        self.direct_app = wsgi_app
        self.proxy_count = proxy_count
        self.networks = tuple(networks)
        self.proxied_app = (
            ProxyFix(wsgi_app, x_for=proxy_count, x_proto=proxy_count)
            if proxy_count else None
        )

    def __call__(self, environ, start_response):

        for header in _ALWAYS_REMOVED_HEADERS:
            environ.pop(header, None)

        if self.proxied_app and is_trusted_peer(
            environ.get("REMOTE_ADDR"), self.networks
        ):
            return self.proxied_app(environ, start_response)

        for header in _CLIENT_AND_SCHEME_HEADERS:
            environ.pop(header, None)

        return self.direct_app(environ, start_response)
