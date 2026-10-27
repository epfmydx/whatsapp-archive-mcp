"""Owner-managed names and assignments for administrator-provisioned runtimes."""
import io
import unicodedata
import uuid

import qrcode
import requests
from flask import abort, current_app, g, redirect, render_template, request, send_file, url_for
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError

from .auth import check_csrf, csrf, owner_required
from .models import Account, Message, Owner, Profile, now, uid


def configured_ids():
    return [account["id"] for account in current_app.config["ACCOUNTS"]]


def assigned_accounts(owner="owner"):
    return list(g.db.scalars(select(Account).join(Profile, Profile.account_id == Account.id).where(
        Account.owner_id == owner, Account.id.in_(configured_ids())).order_by(Profile.created_at, Account.id)))


def require_account(account_id, owner="owner"):
    if not isinstance(account_id, str) or account_id not in configured_ids():
        raise ValueError("Unknown account")
    account = g.db.get(Account, account_id)
    if not account or account.owner_id != owner or g.db.get(Profile, account_id) is None:
        raise ValueError("Unknown account")
    return account


def available_accounts():
    pool_ids = [account["id"] for account in current_app.config["ACCOUNTS"] if account.get("profileSlot", False)]
    available = {account.id: account for account in g.db.scalars(select(Account).where(
        Account.owner_id == "owner", Account.id.in_(pool_ids),
        ~select(Profile.account_id).where(Profile.account_id == Account.id).exists()))}
    # Administrator order is stable and does not expose runtime selection to a form.
    return [available[account_id] for account_id in pool_ids if account_id in available]


def profile_name(value):
    if not isinstance(value, str):
        raise ValueError("Enter a profile name.")
    if any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in value):
        raise ValueError("Use a profile name without control characters.")
    name = value.strip()
    if not 1 <= len(name) <= 80:
        raise ValueError("Enter a profile name between 1 and 80 characters.")
    return name


def creation_key(value):
    try:
        key = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("This form has expired. Open Add profile and try again.") from None
    return key


def browser_account(account_id):
    try:
        return require_account(account_id)
    except ValueError:
        abort(404, "Profile not found")


def form_context(**extra):
    return dict(capacity=len(configured_ids()), available_count=len(available_accounts()), csrf_token=csrf(), **extra)


def profile_health(account):
    from .health import account_health
    return account_health(account)


def profile_summaries(accounts):
    """Build bounded, account-scoped archive totals and bridge health for the owner list."""
    ids = [account.id for account in accounts]
    if not ids:
        return {}
    totals = g.db.execute(select(
        Message.account_id,
        func.count(Message.id),
        func.sum(case(((Message.media.is_not(None)) & (Message.media != "{}") & (Message.media != "null"), 1), else_=0)),
    ).where(Message.account_id.in_(ids)).group_by(Message.account_id)).all()
    totals_by_account = {account_id: (int(messages or 0), int(media or 0))
                         for account_id, messages, media in totals}
    result = {}
    for account in accounts:
        health = profile_health(account)
        messages, media = totals_by_account.get(account.id, (0, 0))
        result[account.id] = {"health": health, "message_count": messages, "media_count": media}
    return result


def install_profiles(app):
    @app.get("/")
    @owner_required
    def home():
        profiles = assigned_accounts()
        return render_template("profiles/index.html", profiles=profiles,
                               profile_stats=profile_summaries(profiles), **form_context())

    @app.get("/profiles/new")
    @owner_required
    def profile_new():
        return render_template("profiles/new.html", **form_context(
            creation_key=uid(), form_name="", error=None))

    @app.post("/profiles")
    @owner_required
    def profile_create():
        check_csrf()
        key, name = request.form.get("creation_key", ""), request.form.get("name", "")

        def failure(error, status=400):
            return render_template("profiles/new.html", **form_context(
                creation_key=key, form_name=name, error=error)), status

        try:
            key = creation_key(key)
        except ValueError as error:
            return failure(str(error))
        # One owner row serializes allocation across all application replicas.
        if g.db.get(Owner, "owner", with_for_update=True) is None:
            abort(403)
        existing = g.db.scalar(select(Profile).where(Profile.creation_key == key))
        if existing:
            try:
                account = require_account(existing.account_id)
            except ValueError:
                return failure("This profile is unavailable. Please contact the administrator.", 409)
            return redirect(url_for("profile_detail", account_id=account.id), code=303)
        try:
            label = profile_name(name)
        except ValueError as error:
            return failure(str(error))
        available = available_accounts()
        if not available:
            return failure("All profile spaces are in use. Ask the administrator to add another.", 409)
        account = available[0]
        account.label, account.updated_at = label, now()
        g.db.add(Profile(account_id=account.id, creation_key=key))
        try:
            g.db.flush()
        except IntegrityError:
            # Unique account/key constraints also protect deployments using a DB
            # without row locks; an uncertain response can replay the same UUID.
            g.db.rollback()
            existing = g.db.scalar(select(Profile).where(Profile.creation_key == key))
            if existing:
                try:
                    account = require_account(existing.account_id)
                except ValueError:
                    return failure("This profile is unavailable. Please contact the administrator.", 409)
            else:
                return failure("Another profile was just added. Please try again.", 409)
        return redirect(url_for("profile_detail", account_id=account.id), code=303)

    @app.get("/profiles/<account_id>")
    @owner_required
    def profile_detail(account_id):
        account = browser_account(account_id)
        return render_template("profiles/detail.html", profile=account, status_text=account.status.replace("_", " ").capitalize(),
                               csrf_token=csrf(), error=None, form_name=account.label,
                               notice="Profile name updated." if request.args.get("renamed") == "1" else None,
                               health_info=profile_health(account))

    @app.post("/profiles/<account_id>/rename")
    @owner_required
    def profile_rename(account_id):
        check_csrf()
        account = browser_account(account_id)
        name = request.form.get("name", "")
        try:
            account.label = profile_name(name)
        except ValueError as error:
            return render_template("profiles/detail.html", profile=account,
                                   status_text=account.status.replace("_", " ").capitalize(),
                                   csrf_token=csrf(), error=str(error), form_name=name, notice=None,
                                   health_info=profile_health(account)), 400
        account.updated_at = now()
        return redirect(url_for("profile_detail", account_id=account.id, renamed="1"), code=303)

    @app.get("/accounts/<account_id>/qr.png")
    @owner_required
    def qr(account_id):
        browser_account(account_id)
        url = app.config["BRIDGE_URLS"].get(account_id)
        if not url:
            abort(503, "Bridge unavailable")
        try:
            response = requests.get(url.rstrip("/")+"/qr", headers={"Authorization": "Bearer "+app.config["INTERNAL_TOKEN"]},
                                    timeout=(3, 5), allow_redirects=False)
            if response.status_code != 200:
                abort(503, "Bridge unavailable")
            data = response.json()
            code = data.get("code") if isinstance(data, dict) else None
            if not isinstance(code, str) or not 1 <= len(code) <= 8192:
                abort(404, "No current pairing code")
        except (requests.RequestException, ValueError):
            abort(503, "Bridge unavailable")
        output = io.BytesIO()
        qrcode.make(code).save(output, format="PNG")
        output.seek(0)
        return send_file(output, mimetype="image/png")
