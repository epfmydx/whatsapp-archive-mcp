"""Keep private responses uncached and leave public static TTLs to the edge."""
from flask import Flask, request, session
from flask.sessions import SecureCookieSessionInterface


def public_static_response(response):
    return (getattr(response, "_archive_static_file", False)
            and request.endpoint == "static" and request.method in ("GET", "HEAD")
            and response.status_code in (200, 304) and not session.modified
            and not response.headers.getlist("Set-Cookie"))


class StaticAssetFlask(Flask):
    def send_static_file(self, filename):
        response = super().send_static_file(filename)
        # Mark an actually served file, not any early response on /static/.
        response._archive_static_file = True
        return response


class StaticAssetSessionInterface(SecureCookieSessionInterface):
    def save_session(self, app, session, response):
        if public_static_response(response):
            # A public asset must not refresh an owner's cookie or vary by it.
            return
        super().save_session(app, session, response)


def apply_cache_policy(response):
    if public_static_response(response):
        # Flask's default file response says no-cache. Keep validators, but let
        # the CDN choose freshness without an origin TTL or cache prohibition.
        response.headers.pop("Cache-Control", None)
        response.headers.pop("Expires", None)
    else:
        response.headers["Cache-Control"] = "no-store"
