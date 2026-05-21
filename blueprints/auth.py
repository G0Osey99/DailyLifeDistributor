"""Login/logout routes and the login_required decorator."""
from __future__ import annotations

from functools import wraps

from flask import (
    Blueprint, redirect, render_template, request, session, url_for,
)

from core import auth

bp = Blueprint("auth", __name__)

_SESSION_KEY = "authenticated"


def is_authenticated() -> bool:
    return bool(session.get(_SESSION_KEY))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@bp.route("/login", methods=["GET"])
def login():
    if is_authenticated():
        return redirect(url_for("scan.index"))
    return render_template("login.html", error=None)


@bp.route("/login", methods=["POST"])
def login_submit():
    ip = request.remote_addr or "unknown"
    if auth.is_locked(ip):
        return render_template(
            "login.html",
            error="Too many failed attempts. Try again later.",
        ), 429
    password = request.form.get("password", "")
    if auth.verify_password(password):
        auth.clear_failures(ip)
        session[_SESSION_KEY] = True
        session.permanent = True
        nxt = request.args.get("next") or url_for("scan.index")
        if not nxt.startswith("/"):  # only relative redirects (no open redirect)
            nxt = url_for("scan.index")
        return redirect(nxt)
    auth.record_failure(ip)
    return render_template("login.html", error="Incorrect password."), 401


@bp.route("/logout", methods=["POST"])
def logout():
    session.pop(_SESSION_KEY, None)
    return redirect(url_for("auth.login"))
