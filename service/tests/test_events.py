import base64
import hashlib
import hmac
import json
import socket
import pytest
from flask import g
from sqlalchemy import select
from app.events import public_destination, signed_headers, subscribe, unsubscribe, emit, deliver
from app.models import Token, Subscription, Delivery, now
from app.auth import digest


def resolution(ip): return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip,443))]

@pytest.mark.parametrize("url,ip", [("http://public.test", "8.8.8.8"), ("https://127.0.0.1", "127.0.0.1"), ("https://public.test", "169.254.169.254"), ("https://public.test", "10.0.0.1"), ("https://public.test", "::1"), ("https://user:pass@public.test", "8.8.8.8"), ("https://public.test:8443", "8.8.8.8")])
def test_callback_ssrf_rejected(url, ip):
    with pytest.raises(ValueError): public_destination(url, lambda *a,**kw: resolution(ip))


def test_dns_mixed_public_private_rejected():
    with pytest.raises(ValueError): public_destination("https://public.test", lambda *a,**kw: resolution("8.8.8.8")+resolution("127.0.0.1"))


def test_standard_webhooks_signature():
    secret = "whsec_"+base64.b64encode(b"s"*32).decode()
    body = b'{"test":true}'
    headers = signed_headers(secret, "event-id", body, "sub-id", 123)
    expected = base64.b64encode(hmac.new(b"s"*32, b"event-id.123."+body, hashlib.sha256).digest()).decode()
    assert headers["webhook-signature"] == "v1,"+expected
    assert headers["X-MCP-Subscription-Id"] == "sub-id"


def test_subscription_verification_idempotency_outbox_revocation(application, monkeypatch):
    monkeypatch.setattr("app.events.public_destination", lambda url: "8.8.8.8")
    secret = "whsec_"+base64.b64encode(b"s"*32).decode()
    params = {"name": "whatsapp.message.received", "arguments": {"account_id": "personal"}, "delivery": {"mode": "webhook", "url": "https://callback.test/events", "secret": secret}}
    seen = []
    def post(url, ip, body, headers):
        data = json.loads(body); seen.append((data,headers))
        return 200, json.dumps({"challenge": data.get("challenge")}).encode()
    with application.test_request_context():
        g.db = application.extensions["db_session"]()
        token = Token(client_id="desktop", owner_id="owner", access_hash=digest("access"), refresh_hash=digest("refresh"), family="family", resource=application.config["RESOURCE"], scope="whatsapp:read", expires_at=now()+900, refresh_expires_at=now()+10000)
        g.db.add(token); g.db.flush()
        sub = subscribe(params, token, post=post)
        assert subscribe(params, token, post=post)["id"] == sub["id"]
        assert secret not in g.db.get(Subscription, sub["id"]).secret
        emit(g.db, "personal", params["name"], {"account_id": "personal", "id": "message-id"})
        g.db.commit()
        deliver(application.extensions["db_session"], application.config["TOKEN_ENCRYPTION_KEY"], post=post)
        g.db.expire_all()
        assert g.db.scalar(select(Delivery)).delivered
        event, headers = seen[-1]
        assert event["eventId"] == headers["webhook-id"]
        assert event["data"]["id"] == "message-id"
        emit(g.db, "personal", params["name"], {"account_id": "personal", "id": "message-id2"})
        token.revoked = True; g.db.commit()
        count = len(seen)
        deliver(application.extensions["db_session"], application.config["TOKEN_ENCRYPTION_KEY"], post=post)
        assert len(seen) == count
        assert unsubscribe(params, token) == {}
        assert not g.db.get(Subscription, sub["id"]).active
        g.db.close()


def test_failed_verification_never_activates(application, monkeypatch):
    monkeypatch.setattr("app.events.public_destination", lambda url: "8.8.8.8")
    with application.test_request_context():
        g.db = application.extensions["db_session"]()
        token = Token(client_id="desktop", owner_id="owner", family="x")
        params = {"name": "whatsapp.message.received", "arguments": {"account_id": "personal"}, "delivery": {"mode": "webhook", "url": "https://callback.test", "secret": "whsec_"+base64.b64encode(b"s"*32).decode()}}
        with pytest.raises(ValueError): subscribe(params, token, post=lambda *a: (302,b'{}'))
        assert list(g.db.scalars(select(Subscription))) == []
        g.db.close()


def test_null_ttl_grants_finite_lifetime_and_dual_secret_rotation(application, monkeypatch):
    from app.events import decode_secret
    from datetime import datetime
    monkeypatch.setattr("app.events.public_destination", lambda url: "8.8.8.8")
    old = "whsec_"+base64.b64encode(b"o"*32).decode()
    new = "whsec_"+base64.b64encode(b"n"*32).decode()
    params = {"name": "whatsapp.message.received", "arguments": {"account_id": "personal"}, "delivery": {"mode": "webhook", "url": "https://callback.test/events", "secret": old}, "ttlMs": None}
    def verify(url, ip, body, headers): return 200, json.dumps({"challenge": json.loads(body)["challenge"]}).encode()
    observed = []
    def received(url, ip, body, headers):
        observed.append((body, headers)); return 200, b'{}'
    with application.test_request_context():
        g.db = application.extensions["db_session"]()
        token = Token(client_id="desktop", owner_id="owner", access_hash=digest("access"), refresh_hash=digest("refresh"), family="family", resource=application.config["RESOURCE"], scope="whatsapp:read", expires_at=now()+900, refresh_expires_at=now()+10000)
        g.db.add(token); g.db.flush()
        granted = subscribe(params, token, post=verify)
        assert datetime.fromisoformat(granted["refreshBefore"].replace("Z", "+00:00")).timestamp() < now()+86401
        sub = g.db.get(Subscription, granted["id"])
        assert now() < sub.expires_at <= now()+86400
        params["delivery"]["secret"] = new
        assert subscribe(params, token, post=verify)["id"] == granted["id"]
        key = application.config["TOKEN_ENCRYPTION_KEY"]
        state = decode_secret(sub.secret, key)
        assert state["current"] == new and state["previous"] == old
        emit(g.db, "personal", params["name"], {"account_id": "personal", "id": "rotation-message"})
        g.db.commit(); deliver(application.extensions["db_session"], key, post=received)
        body, headers = observed[-1]
        signatures = headers["webhook-signature"].split(" ")
        assert len(signatures) == 2
        for secret in (new,old):
            expected = signed_headers(secret, headers["webhook-id"], body, sub.id, int(headers["webhook-timestamp"]))["webhook-signature"]
            assert expected in signatures
        monkeypatch.setattr("app.events.now", lambda: state["rotate_until"]+1)
        emit(g.db, "personal", params["name"], {"account_id": "personal", "id": "after-rotation"})
        g.db.commit(); deliver(application.extensions["db_session"], key, post=received)
        body, headers = observed[-1]
        assert len(headers["webhook-signature"].split(" ")) == 1
        assert headers["webhook-signature"] == signed_headers(new, headers["webhook-id"], body, sub.id, int(headers["webhook-timestamp"]))["webhook-signature"]
        g.db.close()


@pytest.mark.parametrize("reason", ["challenge_failed", "timeout", "connection_failed"])
def test_callback_failure_protocol_error(client, monkeypatch, reason):
    from test_auth import authorize, exchange
    from test_api import rpc
    monkeypatch.setattr("app.events.public_destination", lambda url: "8.8.8.8")
    def post(*args):
        if reason == "timeout": raise TimeoutError("private network diagnostics")
        if reason == "connection_failed": raise OSError("private network diagnostics")
        return 200, b'{"challenge":"wrong"}'
    monkeypatch.setattr("app.events.pinned_post", post)
    access = exchange(client, authorize(client)).json["access_token"]
    params = {"name": "whatsapp.message.received", "arguments": {"account_id": "personal"}, "delivery": {"mode": "webhook", "url": "https://callback.test", "secret": "whsec_"+base64.b64encode(b"s"*32).decode()}}
    response = rpc(client, access, "events/subscribe", params)
    assert response.status_code == 200
    assert response.json["error"] == {"code": -32015, "message": "CallbackEndpointError", "data": {"reason": reason}}
    assert "private" not in json.dumps(response.json)


def test_legacy_encrypted_secret_decodes(application):
    from cryptography.fernet import Fernet
    from app.events import decode_secret
    key = application.config["TOKEN_ENCRYPTION_KEY"]
    secret = "whsec_"+base64.b64encode(b"s"*32).decode()
    encrypted = Fernet(key.encode()).encrypt(secret.encode()).decode()
    assert decode_secret(encrypted, key) == {"current": secret, "previous": None, "rotate_until": 0}


def test_pinned_post_elapsed_deadline_stops_trickled_body(monkeypatch):
    """Individual reads stay below 10s; the whole response still stops promptly."""
    import multiprocessing
    import time
    from app import events
    children_before = {p.pid for p in multiprocessing.active_children()}
    class Response:
        status = 200
        def read(self, count):
            for _ in range(count): time.sleep(.02)
            return b'x'*count
        def close(self): pass
    class Pool:
        def __init__(self, *args, **kwargs): pass
        def urlopen(self, *args, **kwargs): return Response()
        def close(self): pass
    # Fork is used only by this synthetic test to inject an in-memory transport.
    # Production always uses spawn and never inherits application connections.
    monkeypatch.setattr(events, '_process_context', lambda: multiprocessing.get_context('fork'))
    monkeypatch.setattr(events.urllib3, 'HTTPSConnectionPool', Pool)
    monkeypatch.setattr(events, 'REQUEST_DEADLINE', .25)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        events.pinned_post('https://callback.test/events', '8.8.8.8', b'{}', {})
    assert time.monotonic()-started < 2
    assert {p.pid for p in multiprocessing.active_children()} == children_before


def test_deadline_process_uses_spawn_and_returns_without_leaking_children():
    import multiprocessing
    from app.events import _bounded_call
    before = {p.pid for p in multiprocessing.active_children()}
    assert _bounded_call(abs, (-3,), 10) == 3
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_pinned_transport_keeps_tls_host_and_disables_redirects_and_retries(monkeypatch):
    from app import events
    seen = {}
    class Response:
        status = 200
        def read(self, count):
            assert count == 4097
            return b'{}'
        def close(self): seen['response_closed'] = True
    class Pool:
        def __init__(self, *args, **kwargs): seen.update(address=args, tls=kwargs)
        def urlopen(self, *args, **kwargs):
            seen.update(request=args, options=kwargs)
            return Response()
        def close(self): seen['pool_closed'] = True
    monkeypatch.setattr(events.urllib3, 'HTTPSConnectionPool', Pool)
    assert events._pinned_post('https://callback.test/events?a=1', '8.8.8.8', b'{}', {}) == (200, b'{}')
    assert seen['address'] == ('8.8.8.8', 443)
    assert seen['tls']['assert_hostname'] == seen['tls']['server_hostname'] == 'callback.test'
    assert seen['tls']['cert_reqs'] == 'CERT_REQUIRED'
    assert seen['options']['headers']['Host'] == 'callback.test'
    assert seen['options']['retries'] is False and seen['options']['redirect'] is False
    assert seen['response_closed'] and seen['pool_closed']


def outbox(application, monkeypatch):
    monkeypatch.setattr('app.events.public_destination', lambda url: '8.8.8.8')
    with application.test_request_context():
        g.db = application.extensions['db_session']()
        token = Token(client_id='desktop', owner_id='owner', access_hash=digest('outbox-access'),
                      refresh_hash=digest('outbox-refresh'), family='outbox-family', resource=application.config['RESOURCE'],
                      scope='whatsapp:read', expires_at=now()+900, refresh_expires_at=now()+10000)
        g.db.add(token); g.db.flush()
        params = {'name': 'whatsapp.message.received', 'arguments': {'account_id': 'personal'},
                  'delivery': {'mode': 'webhook', 'url': 'https://callback.test/events',
                               'secret': 'whsec_'+base64.b64encode(b's'*32).decode()}}
        def verify(url, ip, body, headers):
            return 200, json.dumps({'challenge': json.loads(body)['challenge']}).encode()
        subscribe(params, token, post=verify)
        emit(g.db, 'personal', params['name'], {'account_id': 'personal', 'id': 'synthetic-message'})
        g.db.commit(); g.db.close()
    return application.extensions['db_session'], application.config['TOKEN_ENCRYPTION_KEY']


def test_outbox_claim_commits_before_io_and_crash_retries_are_bounded(application, monkeypatch):
    sessions, key = outbox(application, monkeypatch)
    sent = []
    def crash(url, ip, body, headers):
        # A second session sees a durable reservation before network I/O starts.
        with sessions() as db:
            item = db.scalar(select(Delivery))
            assert item.attempts == 1 and item.available_at > now()
        sent.append(headers['webhook-id'])
        raise SystemExit('synthetic process interruption')
    with pytest.raises(SystemExit): deliver(sessions, key, post=crash)
    assert not deliver(sessions, key, post=lambda *args: pytest.fail('reserved claim was redelivered'))
    with sessions.begin() as db:
        item = db.scalar(select(Delivery)); item.available_at = now()-1
    def receive(url, ip, body, headers):
        assert headers['webhook-id'] == sent[0]
        with sessions() as db:
            assert db.scalar(select(Delivery)).attempts == 2
        return 503, b'{}'
    deliver(sessions, key, post=receive)
    for _ in range(6):
        with sessions.begin() as db: db.scalar(select(Delivery)).available_at = now()-1
        deliver(sessions, key, post=lambda *args: (503, b'{}'))
    with sessions.begin() as db:
        item = db.scalar(select(Delivery)); assert item.attempts == 8
        item.available_at = now()-1
    assert not deliver(sessions, key, post=lambda *args: pytest.fail('exhausted delivery retried'))


@pytest.mark.parametrize('status', [200, 410, 413])
def test_outbox_terminal_status_and_short_transactions(application, monkeypatch, status):
    sessions, key = outbox(application, monkeypatch)
    def post(*args):
        # A second writer can commit while the callback is in flight.
        with sessions.begin() as db:
            sub = db.scalar(select(Subscription)); sub.expires_at += 1
        return status, b'{}'
    deliver(sessions, key, post=post)
    with sessions() as db:
        assert db.scalar(select(Delivery)).delivered
        assert db.scalar(select(Subscription)).active == (status != 410)


def test_removed_client_cannot_receive_outbox(application, monkeypatch):
    sessions, key = outbox(application, monkeypatch)
    deliver(sessions, key, allowed_clients=[], post=lambda *args: pytest.fail('removed client received event'))
    with sessions() as db: assert db.scalar(select(Delivery)).delivered


@pytest.mark.parametrize("change", ["removed", "unassigned", "other_owner", "mismatched_event"])
def test_outbox_rechecks_profile_authorization(application, monkeypatch, change):
    from app.models import Account, Event, Owner, Profile
    sessions, key = outbox(application, monkeypatch)
    with sessions.begin() as db:
        if change == "unassigned":
            db.delete(db.get(Profile, "personal"))
        elif change == "other_owner":
            db.add(Owner(id="someone_else", email="synthetic@example.test", password_hash="unused"))
            db.flush()
            db.get(Account, "personal").owner_id = "someone_else"
        elif change == "mismatched_event":
            db.scalar(select(Event)).event_type = "whatsapp.transcript.completed"
    deliver(sessions, key, allowed_accounts=[] if change == "removed" else application.config["ACCOUNTS"],
            post=lambda *args: pytest.fail("unauthorized profile received callback"))
    with sessions() as db: assert db.scalar(select(Delivery)).delivered


def test_disabled_persisted_client_cannot_receive_outbox(application, monkeypatch):
    from app.models import Client
    sessions, key = outbox(application, monkeypatch)
    with sessions.begin() as db: db.get(Client, 'desktop').redirect_uris = ''
    deliver(sessions, key, allowed_clients=application.config['OAUTH_CLIENTS'],
            post=lambda *args: pytest.fail('disabled client received event'))
    with sessions() as db: assert db.scalar(select(Delivery)).delivered


def test_late_result_cannot_override_newer_attempt_or_disable_subscription(application, monkeypatch):
    sessions, key = outbox(application, monkeypatch)
    def late_first_attempt(*args):
        # Simulate lease/lock loss and a retry finishing while the original
        # callback is still in flight. A stale 410 must not disable the renewed
        # subscription or complete the newer failed attempt.
        with sessions.begin() as db:
            db.scalar(select(Delivery)).available_at = now()-1
        assert deliver(sessions, key, post=lambda *args: (503, b'{}'))
        return 410, b'{}'
    assert deliver(sessions, key, post=late_first_attempt)
    with sessions() as db:
        item = db.scalar(select(Delivery))
        assert item.attempts == 2 and not item.delivered
        assert db.scalar(select(Subscription)).active


def test_dns_deadline_is_bounded_and_uses_public_error_category(monkeypatch):
    import multiprocessing
    import time
    from app import events
    def blocked(hostname): time.sleep(30)
    monkeypatch.setattr(events, '_process_context', lambda: multiprocessing.get_context('fork'))
    monkeypatch.setattr(events, '_resolve', blocked)
    monkeypatch.setattr(events, 'DNS_DEADLINE', .2)
    start = time.monotonic()
    with pytest.raises(events.CallbackEndpointError) as result:
        events.public_destination('https://callback.test')
    assert result.value.reason == 'timeout'
    assert time.monotonic()-start < 2


def test_transport_capacity_shared_across_processes_and_threads(monkeypatch, tmp_path):
    import multiprocessing
    import time
    from concurrent.futures import ThreadPoolExecutor
    from app import events
    context = multiprocessing.get_context('fork')
    monkeypatch.setattr(events, '_process_context', lambda: context)
    lock_file = tmp_path/'transport.lock'
    monkeypatch.setattr(events, 'TRANSPORT_LOCK_FILE', str(lock_file))
    ready, release = context.Event(), context.Event()
    def blocked():
        ready.set()
        assert release.wait(5)
        return 7
    def first_call(): assert events._bounded_call(blocked, (), 8) == 7
    def unexpected(): pytest.fail('busy admission started a transport')
    first = context.Process(target=first_call)
    first.start()
    try:
        assert ready.wait(3)
        start = time.monotonic()
        # Distinct threads in another process must both reject immediately.
        with ThreadPoolExecutor(max_workers=2) as executor:
            calls = [executor.submit(events._bounded_call, unexpected, (), 8) for _ in range(2)]
            for call in calls:
                with pytest.raises(TimeoutError, match='capacity unavailable'): call.result(timeout=1)
        assert time.monotonic()-start < 1
        assert lock_file.read_bytes() == b''
        assert lock_file.stat().st_mode & 0o777 == 0o600
        release.set()
        first.join(3)
        assert first.exitcode == 0
    finally:
        release.set()
        events.stop_process(first)
    # The same admission slot is available after the first child was reaped.
    assert events._bounded_call(abs, (-3,), 3) == 3


def test_transport_capacity_released_after_deadline(monkeypatch, tmp_path):
    import multiprocessing
    import time
    from app import events
    monkeypatch.setattr(events, '_process_context', lambda: multiprocessing.get_context('fork'))
    monkeypatch.setattr(events, 'TRANSPORT_LOCK_FILE', str(tmp_path/'transport.lock'))
    with pytest.raises(TimeoutError): events._bounded_call(time.sleep, (30,), .1)
    assert events._bounded_call(abs, (-3,), 3) == 3


@pytest.mark.parametrize('unsafe', ['symlink', 'mode', 'directory'])
def test_transport_admission_rejects_unsafe_lock_file(monkeypatch, tmp_path, unsafe):
    from app import events
    lock_file = tmp_path/'transport.lock'
    if unsafe == 'symlink': lock_file.symlink_to(tmp_path/'target')
    elif unsafe == 'directory': lock_file.mkdir()
    else:
        lock_file.touch(mode=0o644)
    monkeypatch.setattr(events, 'TRANSPORT_LOCK_FILE', str(lock_file))
    with pytest.raises(OSError):
        events._bounded_call(abs, (-3,), 3)


def test_dns_and_https_share_admission_and_public_timeout_category(monkeypatch, tmp_path):
    from app import events
    monkeypatch.setattr(events, 'TRANSPORT_LOCK_FILE', str(tmp_path/'transport.lock'))
    monkeypatch.setattr(events, '_process_context', lambda: pytest.fail('busy admission created a child'))
    with events._transport_slot():
        with pytest.raises(events.CallbackEndpointError) as result:
            events.public_destination('https://callback.test')
        assert result.value.reason == 'timeout'
        with pytest.raises(TimeoutError, match='capacity unavailable'):
            events.pinned_post('https://callback.test', '8.8.8.8', b'{}', {})
