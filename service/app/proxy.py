"""Separate scheme trust from explicitly configured login source identity."""
import hmac
from ipaddress import ip_address, ip_network
from werkzeug.middleware.proxy_fix import ProxyFix


def trusted_peer(value, cidrs):
    try:
        peer = ip_address(value)
        return any(peer in ip_network(cidr, strict=True) for cidr in cidrs)
    except ValueError:
        return False


def single_ip(value):
    # Never accept a chain, port, scoped address or alternative header syntax.
    if not isinstance(value, str) or not value or len(value) > 45 or value != value.strip() or "%" in value:
        raise ValueError("Invalid login source identity")
    try:
        address = ip_address(value)
    except ValueError:
        raise ValueError("Invalid login source identity") from None
    return str(getattr(address, "ipv4_mapped", None) or address)


def login_source(environ, config):
    if config.get("CLOUDFLARE_PROXY_ENABLED"):
        address = single_ip(environ.get("whatsapp.cloudflare_client_ip", ""))
        parsed = ip_address(address)
        if not parsed.is_global or parsed.is_multicast:
            raise ValueError("Invalid login source identity")
        return address
    peer = environ.get("REMOTE_ADDR", "")
    if not trusted_peer(peer, config["TRUSTED_PROXY_CIDRS"]):
        return single_ip(peer)
    mode = config["LOGIN_PROXY_MODE"]
    if mode == "signed":
        supplied = environ.get("HTTP_X_ARCHIVE_PROXY_TOKEN", "")
        expected = config["LOGIN_PROXY_SECRET"]
        if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise ValueError("Invalid login source identity")
        return single_ip(environ.get("HTTP_X_ARCHIVE_CLIENT_IP", ""))
    if mode == "envoy":
        # Cilium Envoy must overwrite this header with its observed source IP;
        # trusting that edge behavior is an explicit deployment prerequisite.
        return single_ip(environ.get("HTTP_X_ENVOY_EXTERNAL_ADDRESS", ""))
    # A known proxy cannot safely provide a source in direct/unconfigured mode.
    raise ValueError("Login source identity is not configured")


class TrustedSchemeProxy:
    def __init__(self, application, cidrs):
        self.application = application
        self.networks = tuple(ip_network(value, strict=True) for value in cidrs)
        self.scheme_proxy = ProxyFix(application, x_for=0, x_proto=1, x_host=0, x_port=0, x_prefix=0)

    def __call__(self, environ, start_response):
        try:
            peer = ip_address(environ.get("REMOTE_ADDR", ""))
            trusted = any(peer in network for network in self.networks)
        except ValueError:
            trusted = False
        return (self.scheme_proxy if trusted else self.application)(environ, start_response)
