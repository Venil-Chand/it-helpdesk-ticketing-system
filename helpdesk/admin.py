"""
Admin blueprint: account administration and the audit log.

Routes (administrators only; everyone else gets 403):
    /admin/users                          GET        "admin.users"
    /admin/users/new                      GET, POST  "admin.new_user"
    /admin/users/<int:user_id>/role       POST       "admin.change_role"
    /admin/users/<int:user_id>/active     POST       "admin.set_active"
    /admin/users/<int:user_id>/password   POST       "admin.reset_password"
    /admin/audit                          GET        "admin.audit_log"

The views only read the form and show the result. The rules (who may do
what, self-protection, audit events) live in accounts.py and are checked
again inside the database transaction.
"""

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

import accounts
import validation

from .auth import admin_required, is_positive_integer

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _back_to_users():
    return redirect(url_for("admin.users"))


def _require_valid_id(user_id):
    # SQLite cannot look up a number this large, and no such account exists
    if not is_positive_integer(user_id):
        abort(404)


def _stranded_warning(username, count):
    """Ask the admin to reassign tickets the account can no longer hold."""

    if count:
        flash(
            f"{count} open or in-progress ticket(s) are still assigned to "
            f"{username}. They have not been changed; reassign them from "
            "each ticket's page.",
            "warning"
        )


@bp.route("/users")
@admin_required
def users():
    """List every account."""

    return render_template(
        "admin_users.html",
        accounts=accounts.list_accounts(),
        roles=accounts.ROLES,
        min_password_length=validation.MIN_PASSWORD_LENGTH,
        max_password_length=validation.MAX_PASSWORD_LENGTH
    )


@bp.route("/users/new", methods=["GET", "POST"])
@admin_required
def new_user():
    """Create an account with an initial password set by the admin."""

    username = ""
    role = "user"

    if request.method == "POST":

        username = request.form.get("username", "").strip()
        role = request.form.get("role", "")
        password = request.form.get("password", "")
        confirmation = request.form.get("confirm_password", "")

        problem = validation.new_password_problem(password, confirmation)

        if problem is None:
            try:
                accounts.create_account(g.user.id, username, role, password)

            except accounts.AccountError as error:
                problem = str(error)

            else:
                flash(
                    f"Account {username} created with role {role}. Give "
                    "the user their password through a separate, trusted "
                    "channel; they must choose a new one when they first "
                    "sign in.",
                    "success"
                )
                return _back_to_users()

        flash(problem, "danger")

        # Keep what was typed, except the passwords
        return render_template(
            "admin_user_new.html",
            roles=accounts.ROLES,
            draft_username=username[:validation.MAX_USERNAME_LENGTH],
            draft_role=role if role in accounts.ROLES else "user",
            min_password_length=validation.MIN_PASSWORD_LENGTH,
            max_password_length=validation.MAX_PASSWORD_LENGTH
        ), 400

    return render_template(
        "admin_user_new.html",
        roles=accounts.ROLES,
        draft_username=username,
        draft_role=role,
        min_password_length=validation.MIN_PASSWORD_LENGTH,
        max_password_length=validation.MAX_PASSWORD_LENGTH
    )


@bp.route("/users/<int:user_id>/role", methods=["POST"])
@admin_required
def change_role(user_id):
    """Give another account a different role."""

    _require_valid_id(user_id)

    try:
        result = accounts.change_role(
            g.user.id, user_id, request.form.get("role", "")
        )

    except accounts.AccountNotFound:
        abort(404)

    except accounts.AccountError as error:
        flash(str(error), "danger")
        return _back_to_users()

    if result.changed:
        flash(f"Role of {result.username} changed.", "success")
        _stranded_warning(result.username,
                          result.tickets_needing_reassignment)
    else:
        flash("No changes to save.", "info")

    return _back_to_users()


@bp.route("/users/<int:user_id>/active", methods=["POST"])
@admin_required
def set_active(user_id):
    """Deactivate or reactivate another account."""

    _require_valid_id(user_id)

    wanted = request.form.get("active")

    # The page only sends "0" or "1"; anything else is a forged request
    if wanted not in ("0", "1"):
        abort(400)

    try:
        changed, username, stranded = accounts.set_active(
            g.user.id, user_id, wanted == "1"
        )

    except accounts.AccountNotFound:
        abort(404)

    except accounts.AccountError as error:
        flash(str(error), "danger")
        return _back_to_users()

    if not changed:
        flash("No changes to save.", "info")
    elif wanted == "1":
        flash(f"Account {username} reactivated.", "success")
    else:
        flash(
            f"Account {username} deactivated and signed out everywhere. "
            "Its tickets and comments are kept.",
            "success"
        )
        _stranded_warning(username, stranded)

    return _back_to_users()


@bp.route("/users/<int:user_id>/password", methods=["POST"])
@admin_required
def reset_password(user_id):
    """Set a new password for another account."""

    _require_valid_id(user_id)

    password = request.form.get("password", "")
    problem = validation.new_password_problem(
        password, request.form.get("confirm_password", "")
    )

    if problem:
        flash(problem, "danger")
        return _back_to_users()

    try:
        username = accounts.reset_password(g.user.id, user_id, password)

    except accounts.AccountNotFound:
        abort(404)

    except accounts.AccountError as error:
        flash(str(error), "danger")
        return _back_to_users()

    flash(
        f"Password for {username} reset, and all of its sessions ended. "
        "Give the user the new password through a separate, trusted "
        "channel; they must choose a new one when they next sign in.",
        "success"
    )

    return _back_to_users()


@bp.route("/audit")
@admin_required
def audit_log():
    """
    Show the most recent audit events.

    ?kind=accounts or ?kind=logins narrows the list. Any other value is
    ignored (allow list), so it can never reach the SQL.
    """

    kind = request.args.get("kind")

    if kind not in accounts.AUDIT_CATEGORIES:
        kind = None

    return render_template(
        "admin_audit.html",
        events=accounts.list_audit_events(category=kind),
        kind=kind,
        max_events=accounts.MAX_AUDIT_EVENTS
    )
