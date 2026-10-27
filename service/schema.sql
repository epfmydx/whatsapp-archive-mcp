-- Reference DDL; use python -m app.init_db for explicit additive bootstrap.

CREATE TABLE oauth_clients (
	client_id VARCHAR(200) NOT NULL,
	name VARCHAR(200) NOT NULL,
	redirect_uris TEXT NOT NULL,
	PRIMARY KEY (client_id)
);

CREATE TABLE owner (
	id VARCHAR(50) NOT NULL,
	email VARCHAR(254) NOT NULL,
	password_hash TEXT NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (email)
);

CREATE TABLE rate_limits (
	key VARCHAR(100) NOT NULL,
	"window" INTEGER NOT NULL,
	count INTEGER NOT NULL,
	PRIMARY KEY (key)
);

CREATE TABLE accounts (
	id VARCHAR(80) NOT NULL,
	owner_id VARCHAR(50) NOT NULL,
	label VARCHAR(200) NOT NULL,
	status VARCHAR(30) NOT NULL,
	updated_at INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(owner_id) REFERENCES owner (id)
);

CREATE TABLE oauth_codes (
	code VARCHAR(128) NOT NULL,
	client_id VARCHAR(200) NOT NULL,
	owner_id VARCHAR(50) NOT NULL,
	redirect_uri TEXT NOT NULL,
	scope VARCHAR(80) NOT NULL,
	challenge VARCHAR(128) NOT NULL,
	method VARCHAR(10) NOT NULL,
	created_at INTEGER NOT NULL,
	PRIMARY KEY (code),
	FOREIGN KEY(client_id) REFERENCES oauth_clients (client_id),
	FOREIGN KEY(owner_id) REFERENCES owner (id)
);

CREATE TABLE oauth_dynamic_clients (
	client_id VARCHAR(200) NOT NULL,
	created_at INTEGER NOT NULL,
	PRIMARY KEY (client_id),
	FOREIGN KEY(client_id) REFERENCES oauth_clients (client_id)
);

CREATE TABLE oauth_tokens (
	id VARCHAR(36) NOT NULL,
	client_id VARCHAR(200) NOT NULL,
	owner_id VARCHAR(50) NOT NULL,
	access_hash VARCHAR(64) NOT NULL,
	refresh_hash VARCHAR(64) NOT NULL,
	family VARCHAR(36) NOT NULL,
	resource TEXT NOT NULL,
	scope VARCHAR(80) NOT NULL,
	expires_at INTEGER NOT NULL,
	refresh_expires_at INTEGER NOT NULL,
	revoked BOOLEAN NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(client_id) REFERENCES oauth_clients (client_id),
	FOREIGN KEY(owner_id) REFERENCES owner (id),
	UNIQUE (access_hash),
	UNIQUE (refresh_hash)
);

CREATE TABLE account_health (
	account_id VARCHAR(80) NOT NULL,
	last_bridge_seen_at INTEGER,
	last_message_ingested_at INTEGER,
	whatsapp_connected BOOLEAN,
	whatsapp_authenticated BOOLEAN,
	pending_count INTEGER,
	oldest_pending_at VARCHAR(50),
	error VARCHAR(100),
	PRIMARY KEY (account_id),
	FOREIGN KEY(account_id) REFERENCES accounts (id)
);

CREATE TABLE account_queue_health (
	account_id VARCHAR(80) NOT NULL,
	retry_health TEXT,
	queue_health TEXT,
	PRIMARY KEY (account_id),
	FOREIGN KEY(account_id) REFERENCES accounts (id)
);

CREATE TABLE event_subscriptions (
	id VARCHAR(36) NOT NULL,
	owner_id VARCHAR(50) NOT NULL,
	client_id VARCHAR(200) NOT NULL,
	token_family VARCHAR(36) NOT NULL,
	idempotency_key VARCHAR(200) NOT NULL,
	callback TEXT NOT NULL,
	destination_ip VARCHAR(60) NOT NULL,
	secret TEXT NOT NULL,
	account_id VARCHAR(80) NOT NULL,
	event_type VARCHAR(80) NOT NULL,
	expires_at INTEGER NOT NULL,
	active BOOLEAN NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (owner_id, client_id, idempotency_key),
	FOREIGN KEY(owner_id) REFERENCES owner (id),
	FOREIGN KEY(account_id) REFERENCES accounts (id)
);

CREATE TABLE events (
	id VARCHAR(36) NOT NULL,
	account_id VARCHAR(80) NOT NULL,
	event_type VARCHAR(80) NOT NULL,
	payload TEXT NOT NULL,
	created_at INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(account_id) REFERENCES accounts (id)
);

CREATE TABLE identity_aliases (
	account_id VARCHAR(80) NOT NULL,
	target_type VARCHAR(20) NOT NULL,
	target_id VARCHAR(300) NOT NULL,
	alias VARCHAR(200) NOT NULL,
	updated_at INTEGER NOT NULL,
	PRIMARY KEY (account_id, target_type, target_id),
	FOREIGN KEY(account_id) REFERENCES accounts (id)
);

CREATE TABLE identity_links (
	account_id VARCHAR(80) NOT NULL,
	lid VARCHAR(300) NOT NULL,
	pn VARCHAR(300) NOT NULL,
	observed_at VARCHAR(50) NOT NULL,
	PRIMARY KEY (account_id, lid),
	FOREIGN KEY(account_id) REFERENCES accounts (id)
);

CREATE TABLE identity_names (
	account_id VARCHAR(80) NOT NULL,
	jid VARCHAR(300) NOT NULL,
	kind VARCHAR(30) NOT NULL,
	name VARCHAR(200) NOT NULL,
	observed_at VARCHAR(50) NOT NULL,
	PRIMARY KEY (account_id, jid, kind),
	FOREIGN KEY(account_id) REFERENCES accounts (id)
);

CREATE TABLE messages (
	id VARCHAR(36) NOT NULL,
	account_id VARCHAR(80) NOT NULL,
	message_id VARCHAR(300) NOT NULL,
	chat_jid VARCHAR(300) NOT NULL,
	chat_name TEXT NOT NULL,
	sender_jid VARCHAR(300) NOT NULL,
	sender_name TEXT NOT NULL,
	timestamp VARCHAR(50) NOT NULL,
	is_from_me BOOLEAN NOT NULL,
	text TEXT NOT NULL,
	media TEXT,
	PRIMARY KEY (id),
	UNIQUE (account_id, chat_jid, message_id),
	FOREIGN KEY(account_id) REFERENCES accounts (id)
);

CREATE TABLE profiles (
	account_id VARCHAR(80) NOT NULL,
	creation_key VARCHAR(36) NOT NULL,
	created_at INTEGER NOT NULL,
	PRIMARY KEY (account_id),
	FOREIGN KEY(account_id) REFERENCES accounts (id),
	UNIQUE (creation_key)
);

CREATE TABLE event_outbox (
	id VARCHAR(36) NOT NULL,
	event_id VARCHAR(36) NOT NULL,
	subscription_id VARCHAR(36) NOT NULL,
	attempts INTEGER NOT NULL,
	available_at INTEGER NOT NULL,
	delivered BOOLEAN NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (event_id, subscription_id),
	FOREIGN KEY(event_id) REFERENCES events (id),
	FOREIGN KEY(subscription_id) REFERENCES event_subscriptions (id)
);

CREATE TABLE transcription_jobs (
	id VARCHAR(36) NOT NULL,
	message_id VARCHAR(36) NOT NULL,
	state VARCHAR(20) NOT NULL,
	slot INTEGER,
	attempt INTEGER NOT NULL,
	attempt_token TEXT,
	attempt_secret TEXT,
	job_name VARCHAR(100),
	updated_at INTEGER NOT NULL,
	available_at INTEGER NOT NULL,
	result TEXT,
	error VARCHAR(200),
	PRIMARY KEY (id),
	UNIQUE (message_id),
	FOREIGN KEY(message_id) REFERENCES messages (id),
	UNIQUE (slot)
);

CREATE TABLE worker_attempts (
	name VARCHAR(100) NOT NULL,
	job_id VARCHAR(36) NOT NULL,
	terminal BOOLEAN NOT NULL,
	cleanup_scheduled BOOLEAN NOT NULL,
	PRIMARY KEY (name),
	FOREIGN KEY(job_id) REFERENCES transcription_jobs (id)
);

CREATE INDEX ix_oauth_tokens_family ON oauth_tokens (family);

CREATE INDEX ix_identity_links_account_pn ON identity_links (account_id, pn);

CREATE INDEX ix_messages_account_id ON messages (account_id);

CREATE INDEX ix_messages_chat_jid ON messages (chat_jid);

CREATE INDEX ix_messages_timestamp ON messages (timestamp);

CREATE INDEX ix_transcription_jobs_state ON transcription_jobs (state);

CREATE INDEX ix_worker_attempts_job_id ON worker_attempts (job_id);
