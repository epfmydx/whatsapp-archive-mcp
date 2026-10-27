# Archive service

A single-owner Flask API provides login, profile management, OAuth authorization,
read-only MCP, private media access and event subscriptions. PostgreSQL owns
archive metadata, OAuth grants, subscriptions/outbox and transcription job state.

## Read-only MCP contract

The only tools are `list_accounts`, `list_chats`, `search_contacts`,
`search_messages`, `get_message`, `get_media` and `read_image`. All advertise
`readOnlyHint: true`; unsupported/mutation names are rejected by dispatch.
`set_alias`, `remove_alias`, send/reply/reaction/delete/mark-read tools are absent.
There is no hidden write-scope switch. OAuth supports only `whatsapp:read`;
registration, consent, token exchange and refresh reject unsupported scopes.

Read-only concerns the MCP tools, not the service's own database/storage writes.
Owner admin forms manage profiles/access; internal bridge routes ingest records;
event subscription RPC methods manage callback delivery. These control-plane
operations do not provide WhatsApp conversation mutations.

`list_chats`, `search_messages` and `search_contacts` return
`{items, next_cursor, has_more}`. Continue using the returned cursor with the same
account/filter inputs; timestamp-only continuation can miss equal-time messages.
`before` is an optional initial search time bound, not a replacement for cursor.
Name enrichment uses known account-scoped contacts, observed names and identifiers.
Stored local aliases can be resolved for archive compatibility; MCP cannot edit
them. Contact search does not import the full phonebook or enumerate WhatsApp.

Messages include typed content/replies/received reactions/update metadata where
available. Unknown content carries an unavailable reason; unsupported original
payloads may remain privately retained. Deletion hides original text/media in
MCP while retaining recovery records. Do not treat a recent bridge check-in as
proof of complete history or a latest-message time as proof of sync failure.

`get_media` returns a five-minute private link. S3 uses presigned GET; disk uses
an application-signed capability revalidated against retained message/account/
profile and deletion state. `read_image` returns bounded PNG/JPEG/WebP bytes.
Transcription results are optional; disabled ASR does not prevent media reads.

## Authentication and events

The MCP endpoint is `/mcp`. OAuth metadata is at
`/.well-known/oauth-authorization-server`; resource metadata is at
`/.well-known/oauth-protected-resource` and its `/mcp` suffix. Clients can use
manual configuration or dynamic registration at `/oauth/register`. Owner login,
exact redirect validation, PKCE and explicit consent remain required.
No public owner registration is offered. Client-supplied names are unverified.

Connections lists active access grants/subscriptions and supports owner-only
revocation. Every profile and read is account-scoped. Native forms require CSRF.
Private responses are no-store; successful static assets retain edge-cache policy.
Trusted proxy handling must match the actual app-facing gateway; untrusted
forwarded headers do not choose login throttle identity. Optional Cloudflare
origin authentication does not replace OAuth or owner login.

MCP Events subscriptions verify callbacks and enqueue durable deliveries. The API
and controller require outbound callback HTTPS, directly or through the optional
isolated relay. Signed callbacks, bounded SSRF-safe destinations, revocation and
expiry checks remain in place. Sanitized logs omit payloads, full callback URLs,
secrets and raw provider errors. An accepted subscription does not establish
successful future delivery. Tokens/signatures and media URLs are credentials.

## Configuration and checks

See [configuration](../docs/configuration.md) for S3/disk and optional ASR;
[Helm setup](../KUBERNETES.md) for database/Secret/persistent-volume prerequisites.
`ASR_ENABLED=false` prevents new jobs and controller launches while retaining
existing state; callbacks from existing attempts can still finish.

Install `requirements.txt`, then run
`PYTHONPATH=service python -m pytest service/tests -q` from the repository root.
PostgreSQL fixtures are optional locally and must target a disposable database.
No synthetic test result establishes live WhatsApp/OAuth/provider acceptance.
