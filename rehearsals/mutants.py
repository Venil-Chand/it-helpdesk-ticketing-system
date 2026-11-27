"""
The deliberate security regressions used by rehearsals/mutation.py.

Each mutant replaces one exact piece of source text with a weaker
version. `count` is how often the text must occur (every occurrence is
replaced); if the source has changed so that the text is no longer found
exactly that often, the mutant is reported as STALE rather than silently
skipped, and this file must be updated to match the new code.

`unit` lists the test modules (in tests/) expected to fail when the
mutant is applied, and `browser` the browser tests (in e2e/, run only
with --browser). A mutant is "killed" when at least one of them fails.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Mutant:
    name: str
    path: str
    original: str
    replacement: str
    unit: tuple = ()
    browser: tuple = ()
    count: int = 1


E2E = "test_browser_workflows"

MUTANTS = (
    Mutant(
        "CSRF protection removed",
        "helpdesk/security.py",
        "    # CSRF protection for every POST, in every blueprint\n"
        "    CSRFProtect(app)\n",
        "    # CSRF protection for every POST, in every blueprint\n"
        "    pass\n",
        unit=("test_csrf.py",),
        browser=(f"{E2E}.TestSessionsAndCsrf.test_csrf_protection",),
    ),
    Mutant(
        "sessions never expire",
        "helpdesk/auth.py",
        "    if now - issued_at >= max_age:\n",
        "    if False:\n",
        unit=("test_session_revocation.py",),
        browser=(f"{E2E}.TestSessionsAndCsrf."
                 "test_session_expires_after_eight_hours",),
    ),
    Mutant(
        "session version not compared (revocation ignored)",
        "helpdesk/auth.py",
        '        or account["session_version"] != session_version\n',
        "",
        unit=("test_session_revocation.py", "test_logout.py"),
        browser=(f"{E2E}.TestSessionsAndCsrf."
                 "test_logout_and_password_change_end_every_session",),
    ),
    Mutant(
        "ticket ownership check removed",
        "helpdesk/tickets.py",
        '    return logged_in() and ticket["created_by"] == '
        'g.user.username\n',
        "    return logged_in()\n",
        unit=("test_ticket_ownership.py",),
        browser=(f"{E2E}.TestSignInAndAccounts.test_permissions_by_role",),
    ),
    Mutant(
        "every account treated as administrator",
        "helpdesk/auth.py",
        "        return self.role == ADMIN_ROLE\n",
        "        return True\n",
        unit=("test_account_administration.py",),
        browser=(f"{E2E}.TestSignInAndAccounts.test_permissions_by_role",),
    ),
    Mutant(
        "login limit raised from 5 to 50 per minute",
        "helpdesk/security.py",
        'LOGIN_RATE_LIMIT = "5 per minute"\n',
        'LOGIN_RATE_LIMIT = "50 per minute"\n',
        unit=("test_rate_limit.py", "test_shared_rate_limit.py"),
        browser=(f"{E2E}.TestSignInAndAccounts."
                 "test_login_rate_limit_and_audit",),
    ),
    Mutant(
        "rate limit fails open when Redis is down",
        "helpdesk/security.py",
        "        swallow_errors=False,\n",
        "        swallow_errors=True,\n",
        unit=("test_shared_rate_limit.py",),
    ),
    Mutant(
        "CSV formula neutralisation removed",
        "csv_export.py",
        "    if starts_with_control or hidden_trigger:\n",
        "    if False:\n",
        unit=("test_csv_export.py",),
        browser=(f"{E2E}.TestTicketWorkflow."
                 "test_csv_export_matches_the_dashboard_filter",),
    ),
    Mutant(
        "SMTP 5xx recipient refusal treated as temporary",
        "notifications/transport.py",
        "            kind = PERMANENT if 500 <= code < 600 else RETRY\n",
        "            kind = RETRY\n",
        unit=("test_notification_delivery.py",),
    ),
    Mutant(
        "SMTP 5xx errors treated as temporary",
        "notifications/transport.py",
        "    kind = PERMANENT if 500 <= code < 600 else RETRY\n"
        "    return DeliveryError(",
        "    kind = RETRY\n"
        "    return DeliveryError(",
        unit=("test_notification_delivery.py",),
    ),
    Mutant(
        "CSP frame-ancestors removed",
        "security_config.py",
        "    \"frame-ancestors 'none'\",\n",
        "",
        unit=("test_security_headers_cookies.py",),
        browser=(f"{E2E}.TestSessionsAndCsrf."
                 "test_security_headers_reach_the_browser",),
    ),
    Mutant(
        "forwarded headers trusted from one proxy too many",
        "proxy_trust.py",
        "ProxyFix(wsgi_app, x_for=proxy_count, x_proto=proxy_count)",
        "ProxyFix(wsgi_app, x_for=proxy_count + 1, x_proto=proxy_count)",
        unit=("test_proxy_trust.py",),
    ),
    Mutant(
        "first-login password change not enforced",
        "helpdesk/auth.py",
        "    if user is None or not user.must_change_password:\n",
        "    if True:\n",
        unit=("test_forced_password_change.py",),
        browser=(f"{E2E}.TestSignInAndAccounts."
                 "test_first_login_requires_a_new_password",),
    ),
    Mutant(
        "decrypted-backup checksum not compared",
        "backups.py",
        "    if hashlib.sha256(image).hexdigest() != "
        "manifest.get(\"database_sha256\"):\n",
        "    if False:\n",
        unit=("test_backups.py",),
    ),
    Mutant(
        "live restore allowed while the database is in use",
        "backups.py",
        "        problem = database_in_use(live_db_path)\n",
        "        problem = None\n",
        unit=("test_backups.py",),
        count=2,
    ),
)
