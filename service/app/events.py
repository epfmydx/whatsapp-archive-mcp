"""MCP webhook subscriptions and persistent Standard Webhooks outbox."""
import base64
import fcntl
import hashlib
import hmac
import ipaddress
import json
import multiprocessing
import os
import secrets
import socket
import stat
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlsplit
import certifi
import urllib3
from cryptography.fernet import Fernet
from flask import current_app, g, has_request_context
from sqlalchemy import select
from .models import Account, Profile, Client, Subscription, Event, Delivery, Token, now
from .registration import client_is_active
from .permissions import READ_SCOPE
from .profiles import require_account
from .observability import new_correlation_id, log_callback

class CallbackEndpointError(ValueError):
    """A bounded, protocol-visible failure that never exposes network diagnostics."""
    def __init__(self, reason):
        if reason not in ("challenge_failed", "timeout", "connection_failed"):
            raise ValueError("Invalid callback error reason")
        self.reason = reason
        super().__init__("CallbackEndpointError")


REQUEST_DEADLINE = 15
DNS_DEADLINE = 5
TRANSPORT_LOCK_FILE = "/tmp/whatsapp-mcp-webhook-transport.lock"


EVENTS = ("whatsapp.message.received", "whatsapp.transcription.completed", "whatsapp.account.status",
          "whatsapp.message.updated", "whatsapp.message.reaction")


def canonical(value): return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def public_destination(url, resolver=None):
    p = urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment or p.port not in (None, 443):
        raise ValueError("Webhook URL must be HTTPS on port 443 without credentials or fragment")
    try:
        addresses = {x[4][0] for x in (resolver(p.hostname, 443, type=socket.SOCK_STREAM) if resolver else
                                     _bounded_call(_resolve, (p.hostname,), DNS_DEADLINE))}
    except TimeoutError as e: raise CallbackEndpointError("timeout") from e
    except (OSError, UnicodeError) as e: raise CallbackEndpointError("connection_failed") from e
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError("Webhook host must resolve only to public addresses")
    return sorted(addresses)[0]


def secret_bytes(secret):
    if not isinstance(secret, str) or not secret.startswith("whsec_"): raise ValueError("Invalid webhook secret")
    try: value = base64.b64decode(secret[6:], validate=True)
    except Exception as e: raise ValueError("Invalid webhook secret") from e
    if not 24 <= len(value) <= 64: raise ValueError("Webhook secret must contain 24 to 64 bytes")
    return value


def decode_secret(encrypted, encryption_key):
    clear = Fernet(encryption_key.encode()).decrypt(encrypted.encode()).decode()
    # The v1 legacy representation encrypted only the raw whsec_ string.
    value = {"current": clear, "previous": None, "rotate_until": 0} if clear.startswith("whsec_") else json.loads(clear)
    secret_bytes(value["current"])
    if value.get("previous") is not None: secret_bytes(value["previous"])
    return value


def signed_headers(secret, event_id, body, subscription_id, timestamp=None, previous=None):
    timestamp = str(timestamp or now())
    data = event_id.encode()+b"."+timestamp.encode()+b"."+body
    signatures = ["v1,"+base64.b64encode(hmac.new(secret_bytes(key), data, hashlib.sha256).digest()).decode()
                  for key in ([secret, previous] if previous else [secret])]
    return {"webhook-id": event_id, "webhook-timestamp": timestamp, "webhook-signature": " ".join(signatures),
            "X-MCP-Subscription-Id": subscription_id, "Content-Type": "application/json"}


def _resolve(hostname):
    return socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)


def _process_context():
    # Never fork a web worker or inherit SQLAlchemy connections into a child.
    return multiprocessing.get_context("spawn")


def stop_process(process):
    """Reap owned children on success, deadline, and controller shutdown."""
    if process.pid is None:
        process.close()
        return
    if process.is_alive():
        process.terminate()
        process.join(1)
        if process.is_alive():
            process.kill()
    process.join()
    process.close()


def _call_child(connection, operation, args):
    try:
        try:
            result = ("ok", operation(*args))
        except (urllib3.exceptions.TimeoutError, TimeoutError):
            result = ("timeout", None)
        except Exception:
            # Do not send provider exception text or private request data to logs.
            result = ("connection_failed", None)
        connection.send(result)
    finally:
        connection.close()


@contextmanager
def _transport_slot():
    """One DNS/HTTPS child per pod, across Gunicorn processes and threads.

    The empty lock file contains no request data. Use a shared pod-local /tmp,
    reject replaced/unsafe files, and never queue work while capacity is busy.
    O_CLOEXEC prevents spawned children from retaining the parent's admission.
    """
    fd = os.open(TRANSPORT_LOCK_FILE, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise OSError("Unsafe webhook admission file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TimeoutError("Webhook transport capacity unavailable") from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _bounded_call(operation, args, timeout):
    """Bound elapsed time, including trickled reads, with a disposable process.

    Socket/urllib3 read timeouts restart after individual reads. A parent wall
    deadline also covers TLS, writes and a peer sending bytes indefinitely.
    Secrets travel only over multiprocessing's private pipe, never child argv.
    """
    with _transport_slot():
        context = _process_context()
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=_call_child, args=(send, operation, args))
        deadline = time.monotonic() + timeout
        try:
            process.start()
            send.close()
            if not receive.poll(max(0, deadline-time.monotonic())):
                raise TimeoutError("Webhook deadline exceeded")
            try:
                outcome, result = receive.recv()
            except EOFError:
                raise OSError("Webhook transport failed") from None
            if outcome == "timeout": raise TimeoutError("Webhook deadline exceeded")
            if outcome != "ok": raise OSError("Webhook transport failed")
            return result
        finally:
            send.close()
            receive.close()
            if process.pid is not None: stop_process(process)


def pinned_post(url, ip, body, headers):
    """Pinned HTTPS, bounded by elapsed time as well as socket inactivity."""
    if not ipaddress.ip_address(ip).is_global: raise ValueError("Non-public pinned address")
    return _bounded_call(_pinned_post, (url, ip, body, headers), REQUEST_DEADLINE)


def _pinned_post(url, ip, body, headers):
    # Only numeric IPs reach urllib3: no proxies, DNS, redirects or retries.
    p = urlsplit(url)
    pool = urllib3.HTTPSConnectionPool(ip, 443, assert_hostname=p.hostname, server_hostname=p.hostname,
                                      cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(), maxsize=1)
    try:
        response = pool.urlopen("POST", (p.path or "/") + ("?"+p.query if p.query else ""), body=body,
                                headers={**headers, "Host": p.hostname}, timeout=urllib3.Timeout(connect=5, read=10),
                                retries=False, redirect=False, preload_content=False)
        try: return response.status, response.read(4097)
        finally: response.close()
    finally: pool.close()


def emit(db, account_id, name, data):
    event = Event(account_id=account_id, event_type=name, payload=canonical(data))
    db.add(event); db.flush()
    for sub in db.scalars(select(Subscription).where(Subscription.account_id == account_id, Subscription.event_type == name,
                                                   Subscription.active.is_(True), Subscription.expires_at > now())):
        db.add(Delivery(event_id=event.id, subscription_id=sub.id))
    return event


def _send_callback(url, body, headers, operation, correlation_id, *, post=None,
                   relay_config=None, destination_ip=None):
    if post is None and relay_config and relay_config.get("WEBHOOK_RELAY_URL"):
        # Configured relay failures never fall back to direct public egress.
        from .relay_client import send_callback
        return send_callback(relay_config, url, body, headers, operation=operation,
                             correlation_id=correlation_id)
    ip = destination_ip or public_destination(url)
    status, answer = (post or pinned_post)(url, ip, body, headers)
    return status, answer, ip


def subscribe(params, token, post=None):
    name, args, delivery = params.get("name"), params.get("arguments", {}), params.get("delivery", {})
    if name not in EVENTS or not isinstance(args, dict) or set(args) != {"account_id"}: raise ValueError("Invalid event arguments")
    account = require_account(args["account_id"], token.owner_id)
    if not isinstance(delivery, dict) or delivery.get("mode") != "webhook": raise ValueError("Webhook delivery required")
    callback, secret = delivery.get("url", ""), delivery.get("secret", "")
    secret_bytes(secret)
    identity = canonical([token.owner_id, token.client_id, callback, name, args])
    sub_id = hashlib.sha256(identity.encode()).hexdigest()[:36]
    ttl = params.get("ttlMs", 86400000)
    if ttl is None: ttl = 86400000
    if not isinstance(ttl, int) or not 60000 <= ttl <= 7*86400000: raise ValueError("ttlMs must be 60000..604800000")
    correlation_id = getattr(g, "correlation_id", None) if has_request_context() else None
    correlation_id = correlation_id or new_correlation_id()
    started = time.monotonic()
    challenge = secrets.token_urlsafe(32)
    body = canonical({"type": "verification", "challenge": challenge}).encode()
    try:
        status, answer, ip = _send_callback(
            callback, body, signed_headers(secret, "verify_"+secrets.token_hex(16), body, sub_id),
            "verification", correlation_id, post=post, relay_config=current_app.config)
    except (urllib3.exceptions.TimeoutError, TimeoutError) as error:
        log_callback("verification", "error", correlation_id, (time.monotonic()-started)*1000,
                     reason="timeout", component="api")
        raise CallbackEndpointError("timeout") from error
    except (urllib3.exceptions.HTTPError, OSError) as error:
        log_callback("verification", "error", correlation_id, (time.monotonic()-started)*1000,
                     reason="connection_failed", component="api")
        raise CallbackEndpointError("connection_failed") from error
    except ValueError as error:
        log_callback("verification", "error", correlation_id, (time.monotonic()-started)*1000,
                     reason=error.reason if isinstance(error, CallbackEndpointError) else "invalid_destination",
                     component="api")
        raise
    try: verified = 200 <= status < 300 and json.loads(answer).get("challenge") == challenge
    except (ValueError, AttributeError): verified = False
    if not verified:
        log_callback("verification", "error", correlation_id, (time.monotonic()-started)*1000,
                     callback_status=status, reason="challenge_failed", component="api")
        raise CallbackEndpointError("challenge_failed")
    log_callback("verification", "success", correlation_id, (time.monotonic()-started)*1000,
                 callback_status=status, component="api")
    # Callback verification must not hold a subscription row lock.
    sub = g.db.get(Subscription, sub_id, with_for_update=True)
    if sub is None:
        sub = Subscription(id=sub_id, owner_id=token.owner_id, client_id=token.client_id, token_family=token.family,
                           idempotency_key=hashlib.sha256(identity.encode()).hexdigest(), account_id=account.id, event_type=name)
        g.db.add(sub)
    state = {"current": secret, "previous": None, "rotate_until": 0}
    encryption_key = current_app.config["TOKEN_ENCRYPTION_KEY"]
    if sub.secret:
        previous = decode_secret(sub.secret, encryption_key)
        if not hmac.compare_digest(previous["current"], secret):
            state.update(previous=previous["current"], rotate_until=now()+300)
        elif previous.get("rotate_until", 0) > now():
            state = previous
    sub.callback, sub.destination_ip = callback, ip
    sub.secret = Fernet(encryption_key.encode()).encrypt(canonical(state).encode()).decode()
    sub.expires_at, sub.active, sub.token_family = now()+ttl//1000, True, token.family
    return {"id": sub.id, "refreshBefore": datetime.fromtimestamp(sub.expires_at-30, timezone.utc).isoformat().replace("+00:00", "Z"),
            "cursor": None, "truncated": False}


def unsubscribe(params, token):
    identity = canonical([token.owner_id, token.client_id, params.get("delivery", {}).get("url"), params.get("name"), params.get("arguments", {})])
    sub = g.db.get(Subscription, hashlib.sha256(identity.encode()).hexdigest()[:36], with_for_update=True)
    if sub: sub.active = False
    return {}


def deliver(sessions, encryption_key, post=None, allowed_clients=None, allowed_accounts=None,
            dynamic_enabled=True, relay_config=None):
    """Claim and send one item under the independent webhook advisory lock.

    The caller supplies a session factory. Claim attempts and retry time commit
    before I/O, so crashes consume a bounded attempt and cannot lose the outbox.
    No transaction/row lock stays open while an endpoint receives a request.
    """
    with sessions.begin() as db:
        item = db.scalar(select(Delivery).where(Delivery.delivered.is_(False), Delivery.attempts < 8,
                                                Delivery.available_at <= now()).order_by(Delivery.available_at, Delivery.id)
                         .limit(1).with_for_update(skip_locked=True))
        if item is None: return False
        sub = db.get(Subscription, item.subscription_id)
        account = db.get(Account, sub.account_id) if sub else None
        assigned = db.get(Profile, sub.account_id) if sub else None
        client = client_is_active(db, sub.client_id, allowed_clients, dynamic_enabled) if sub else None
        access = db.scalar(select(Token.id).where(Token.family == sub.token_family, Token.client_id == sub.client_id,
                                                  Token.owner_id == sub.owner_id, Token.scope.in_([
                                                      READ_SCOPE]),
                                                  Token.revoked.is_(False), Token.refresh_expires_at > now())) if sub else None
        if (not sub or not sub.active or sub.expires_at <= now() or not access or not client or not client.enabled
                or not account or not assigned or account.owner_id != sub.owner_id
                or (allowed_accounts is not None and sub.account_id not in {account["id"] for account in allowed_accounts})):
            item.delivered = True
            return True
        event = db.get(Event, item.event_id)
        if not event or event.account_id != sub.account_id or event.event_type != sub.event_type:
            item.delivered = True
            return True
        body = canonical({"eventId": event.id, "name": event.event_type,
                          "timestamp": datetime.fromtimestamp(event.created_at, timezone.utc).isoformat().replace("+00:00", "Z"),
                          "data": json.loads(event.payload), "cursor": None}).encode()
        secrets_state = decode_secret(sub.secret, encryption_key)
        previous = secrets_state.get("previous") if secrets_state.get("rotate_until", 0) > now() else None
        headers = signed_headers(secrets_state["current"], event.id, body, sub.id, previous=previous)
        item.attempts += 1
        # A retry cannot become due before the 25s dispatch-process watchdog.
        item.available_at = now()+min(3600, 2**item.attempts*15)
        delivery_id, attempt = item.id, item.attempts
        subscription_id, callback, destination_ip = sub.id, sub.callback, sub.destination_ip
    correlation_id, started = new_correlation_id(), time.monotonic()
    try:
        status, _, _ = _send_callback(callback, body, headers, "delivery", correlation_id,
                                      post=post, relay_config=relay_config, destination_ip=destination_ip)
        log_callback("delivery", "success" if 200 <= status < 300 else "http_failure", correlation_id,
                     (time.monotonic()-started)*1000, callback_status=status, component="controller")
    except Exception as error:
        reason = error.reason if isinstance(error, CallbackEndpointError) else (
            "timeout" if isinstance(error, (TimeoutError, urllib3.exceptions.TimeoutError)) else
            "invalid_destination" if isinstance(error, ValueError) else "connection_failed")
        log_callback("delivery", "error", correlation_id, (time.monotonic()-started)*1000,
                     reason=reason, component="controller")
        status = 503
    with sessions.begin() as db:
        item = db.get(Delivery, delivery_id, with_for_update=True)
        # Fence a late result after a crash/retry claimed a newer attempt.
        if item and item.attempts == attempt and not item.delivered:
            item.delivered = 200 <= status < 300 or status in (410, 413)
            if status == 410:
                sub = db.get(Subscription, subscription_id, with_for_update=True)
                if sub: sub.active = False
    return True
