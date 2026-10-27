import time
import uuid
from sqlalchemy import String, Text, Integer, Boolean, UniqueConstraint, ForeignKey, Index, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from authlib.oauth2.rfc6749 import ClientMixin, AuthorizationCodeMixin, TokenMixin


def now():
    return int(time.time())


def uid():
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Owner(Base):
    __tablename__ = "owner"
    id: Mapped[str] = mapped_column(String(50), primary_key=True, default="owner")
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    def get_user_id(self): return self.id


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owner.id"), default="owner")
    label: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="disconnected")
    updated_at: Mapped[int] = mapped_column(Integer, default=now)


class Profile(Base):
    """Owner assignment of an existing runtime; never owns session credentials."""
    __tablename__ = "profiles"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    creation_key: Mapped[str] = mapped_column(String(36), unique=True)
    created_at: Mapped[int] = mapped_column(Integer, default=now)


class IdentityName(Base):
    __tablename__ = "identity_names"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    jid: Mapped[str] = mapped_column(String(300), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    observed_at: Mapped[str] = mapped_column(String(50))


class IdentityLink(Base):
    __tablename__ = "identity_links"
    __table_args__ = (Index("ix_identity_links_account_pn", "account_id", "pn"),)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    lid: Mapped[str] = mapped_column(String(300), primary_key=True)
    pn: Mapped[str] = mapped_column(String(300))
    observed_at: Mapped[str] = mapped_column(String(50))


class IdentityAlias(Base):
    __tablename__ = "identity_aliases"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    target_type: Mapped[str] = mapped_column(String(20), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(300), primary_key=True)
    alias: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[int] = mapped_column(Integer, default=now)


class AccountHealth(Base):
    __tablename__ = "account_health"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    last_bridge_seen_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_message_ingested_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    whatsapp_connected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    whatsapp_authenticated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    pending_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    oldest_pending_at: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error: Mapped[str | None] = mapped_column(String(100), nullable=True)


class AccountQueueHealth(Base):
    """Validated aggregate snapshots; additive to the existing health schema."""
    __tablename__ = "account_queue_health"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    retry_health: Mapped[str | None] = mapped_column(Text, nullable=True)
    queue_health: Mapped[str | None] = mapped_column(Text, nullable=True)


class Client(Base, ClientMixin):
    __tablename__ = "oauth_clients"
    client_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    redirect_uris: Mapped[str] = mapped_column(Text)
    @property
    def enabled(self): return bool(self.redirect_uris.strip())
    def get_client_id(self): return self.client_id
    def get_default_redirect_uri(self): return next(iter(self.redirect_uris.splitlines()), None)
    def check_redirect_uri(self, uri): return uri in self.redirect_uris.splitlines()
    def check_client_secret(self, secret): return False
    def check_endpoint_auth_method(self, method, endpoint): return method == "none"
    def check_response_type(self, response_type): return response_type == "code"
    def check_grant_type(self, grant_type): return grant_type in ("authorization_code", "refresh_token")
    def get_allowed_scope(self, scope):
        from .permissions import normalize_scope
        return normalize_scope(scope) or None


class DynamicClient(Base):
    """Additive registration provenance; retained after owner disconnection."""
    __tablename__ = "oauth_dynamic_clients"
    client_id: Mapped[str] = mapped_column(ForeignKey("oauth_clients.client_id"), primary_key=True)
    created_at: Mapped[int] = mapped_column(Integer, default=now)


class Code(Base, AuthorizationCodeMixin):
    __tablename__ = "oauth_codes"
    code: Mapped[str] = mapped_column(String(128), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("oauth_clients.client_id"))
    owner_id: Mapped[str] = mapped_column(ForeignKey("owner.id"))
    redirect_uri: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(80), default="whatsapp:read")
    challenge: Mapped[str] = mapped_column(String(128))
    method: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[int] = mapped_column(Integer, default=now)
    def is_expired(self): return self.created_at + 120 < now()
    def get_redirect_uri(self): return self.redirect_uri
    def get_scope(self): return self.scope
    def get_auth_time(self): return self.created_at
    def get_nonce(self): return None
    def get_acr(self): return None
    def get_amr(self): return None


class Token(Base, TokenMixin):
    __tablename__ = "oauth_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    client_id: Mapped[str] = mapped_column(ForeignKey("oauth_clients.client_id"))
    owner_id: Mapped[str] = mapped_column(ForeignKey("owner.id"))
    access_hash: Mapped[str] = mapped_column(String(64), unique=True)
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True)
    family: Mapped[str] = mapped_column(String(36), default=uid, index=True)
    resource: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(80))
    expires_at: Mapped[int] = mapped_column(Integer)
    refresh_expires_at: Mapped[int] = mapped_column(Integer)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    def check_client(self, client): return self.client_id == client.client_id
    def get_scope(self): return self.scope
    def get_expires_in(self): return max(0, self.expires_at - now())
    def is_expired(self): return self.expires_at <= now()
    def is_revoked(self): return self.revoked


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("account_id", "chat_jid", "message_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    message_id: Mapped[str] = mapped_column(String(300))
    chat_jid: Mapped[str] = mapped_column(String(300), index=True)
    chat_name: Mapped[str] = mapped_column(Text, default="")
    sender_jid: Mapped[str] = mapped_column(String(300))
    sender_name: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[str] = mapped_column(String(50), index=True)
    is_from_me: Mapped[bool] = mapped_column(Boolean, default=False)
    text: Mapped[str] = mapped_column(Text, default="")
    media: Mapped[str | None] = mapped_column(Text, nullable=True)


class MessageMetadata(Base):
    """Original typed envelope, separate from the immutable legacy archive."""
    __tablename__ = "message_metadata"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    chat_jid: Mapped[str] = mapped_column(String(300), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(300), primary_key=True)
    envelope: Mapped[str] = mapped_column(Text)


class MessageState(Base):
    """Effective view and tombstone, including updates received before originals."""
    __tablename__ = "message_state"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    chat_jid: Mapped[str] = mapped_column(String(300), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(300), primary_key=True)
    effective_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    envelope: Mapped[str | None] = mapped_column(Text, nullable=True)
    edit_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_at: Mapped[str | None] = mapped_column(String(50), nullable=True)
    edit_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deleted_at: Mapped[str | None] = mapped_column(String(50), nullable=True)
    delete_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MessageUpdate(Base):
    __tablename__ = "message_updates"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chat_jid: Mapped[str] = mapped_column(String(300))
    message_id: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[str] = mapped_column(Text)


class MessageReaction(Base):
    __tablename__ = "message_reactions"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    chat_jid: Mapped[str] = mapped_column(String(300), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(300), primary_key=True)
    actor_jid: Mapped[str] = mapped_column(String(300), primary_key=True)
    actor_is_from_me: Mapped[bool] = mapped_column(Boolean)
    emoji: Mapped[str] = mapped_column(String(64))
    timestamp: Mapped[str] = mapped_column(String(50))
    event_id: Mapped[str] = mapped_column(String(64))


class Job(Base):
    __tablename__ = "transcription_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"), unique=True)
    state: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    # SQL NULL permits many inactive jobs; unique 1 permits only one global active job.
    slot: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    attempt_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[int] = mapped_column(Integer, default=now)
    available_at: Mapped[int] = mapped_column(Integer, default=now)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)


class WorkerAttempt(Base):
    __tablename__ = "worker_attempts"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("transcription_jobs.id"), index=True)
    terminal: Mapped[bool] = mapped_column(Boolean, default=False)
    cleanup_scheduled: Mapped[bool] = mapped_column(Boolean, default=False)


class Subscription(Base):
    __tablename__ = "event_subscriptions"
    __table_args__ = (UniqueConstraint("owner_id", "client_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owner.id"))
    client_id: Mapped[str] = mapped_column(String(200))
    token_family: Mapped[str] = mapped_column(String(36))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    callback: Mapped[str] = mapped_column(Text)
    destination_ip: Mapped[str] = mapped_column(String(60))
    secret: Mapped[str] = mapped_column(Text)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    event_type: Mapped[str] = mapped_column(String(80))
    expires_at: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=False)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    event_type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(Integer, default=now)


class Delivery(Base):
    __tablename__ = "event_outbox"
    __table_args__ = (UniqueConstraint("event_id", "subscription_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"))
    subscription_id: Mapped[str] = mapped_column(ForeignKey("event_subscriptions.id"))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[int] = mapped_column(Integer, default=now)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)


class RateLimit(Base):
    __tablename__ = "rate_limits"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    window: Mapped[int] = mapped_column(Integer)
    count: Mapped[int] = mapped_column(Integer, default=0)


def database(url):
    engine = create_engine(url, pool_pre_ping=True, hide_parameters=True)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def unicode_search_lower(connection, _):
            connection.create_function("wa_lower", 1, lambda value: value.lower() if isinstance(value, str) else None,
                                       deterministic=True)
    return engine, sessionmaker(engine, expire_on_commit=False)
