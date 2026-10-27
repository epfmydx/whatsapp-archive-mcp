"""Optional Cloudflare origin verification for owner-facing endpoints."""
import hmac
from ipaddress import ip_address

from flask import abort, request

from .proxy import single_ip, trusted_peer


def single_header(name):
    values = request.headers.getlist(name)
    if len(values) != 1 or any(char in values[0] for char in (",", "\r", "\n")):
        return ""
    return values[0]


def install_admin_origin_guard(app):
    @app.before_request
    def verify_admin_origin():
        if not app.config["CLOUDFLARE_PROXY_ENABLED"]:
            return
        view = app.view_functions.get(request.endpoint)
        if request.endpoint not in ("login", "authorize") and not getattr(view, "_owner_only", False):
            return
        if not trusted_peer(request.environ.get("REMOTE_ADDR", ""), app.config["TRUSTED_PROXY_CIDRS"]):
            abort(403, "cloudflare_proxy_required")
        supplied = single_header("X-WhatsApp-Origin")
        if not hmac.compare_digest(supplied.encode(), app.config["CLOUDFLARE_ORIGIN_SECRET"].encode()):
            abort(403, "cloudflare_proxy_required")
        if single_header("X-Forwarded-Proto") != "https":
            abort(403, "cloudflare_https_required")
        try:
            address = single_ip(single_header("CF-Connecting-IP"))
            parsed = ip_address(address)
            if not parsed.is_global or parsed.is_multicast:
                raise ValueError()
        except ValueError:
            abort(400, "invalid_cloudflare_client_ip")
        request.environ["whatsapp.cloudflare_client_ip"] = address
