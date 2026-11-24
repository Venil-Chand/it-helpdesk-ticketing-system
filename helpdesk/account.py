"""
Account blueprint: things every signed-in user does to their own account.

Routes:
    /account/password  GET, POST  endpoint "account.change_password"

The POST is rate limited by create_app() (security.apply_route_limits),
because a stolen session could otherwise be used to guess the current
password quickly.
"""

from flask import (
    Blueprint,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import accounts
import validation

from . import clock
from .auth import login_required

bp = Blueprint("account", __name__, url_prefix="/account")


def _render_form(status=200):
    return render_template(
        "change_password.html",
        min_password_length=validation.MIN_PASSWORD_LENGTH,
        max_password_length=validation.MAX_PASSWORD_LENGTH
    ), status


@bp.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    """
    Change your own password.

    Every other session of the account ends. This browser gets a fresh
    session for the new version, so the user stays signed in here.
    """

    if request.method == "GET":
        return _render_form()

    new_password = request.form.get("new_password", "")

    problem = validation.new_password_problem(
        new_password, request.form.get("confirm_password", "")
    )

    if problem is None:
        try:
            new_version = accounts.change_own_password(
                g.user.id,
                session.get("session_version"),
                request.form.get("current_password", ""),
                new_password
            )

        except accounts.AccountError as error:
            problem = str(error)

    if problem:
        flash(problem, "danger")
        return _render_form(status=400)

    # A brand-new session: the old cookie (and any copy of it) is invalid
    user_id = g.user.id
    session.clear()
    session.permanent = True
    session["user_id"] = user_id
    session["session_version"] = new_version
    session["issued_at"] = clock.current_time()

    flash(
        "Password changed. You have been signed out on all other devices.",
        "success"
    )

    return redirect(url_for("dashboard.index"))
