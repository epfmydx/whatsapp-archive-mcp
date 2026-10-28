# WhatsApp archive bridge

One Go process links to one WhatsApp account using Whatsmeow. It captures available
messages and protocol updates, normalizes supported content and ingests it into
the private archive API. Each bridge has its own PostgreSQL session database and
durable spool. No message-sending HTTP endpoint or MCP write tool is provided.

New registrations advertise OS name **Linux**, version `0.1.0` and companion
platform `DESKTOP`. Existing pairings remain intact. WhatsApp controls the visible
linked-device label; changing this metadata does not establish better stability.

## Storage

`STORAGE_BACKEND=s3` uses `S3_BUCKET`, `S3_REGION`, `S3_ENDPOINT_URL`,
`S3_ADDRESSING_STYLE` and credentials. An empty endpoint selects AWS's regional
endpoint. DO Spaces and MinIO use their respective S3 API endpoint.
`STORAGE_BACKEND=disk` writes verified originals atomically below `MEDIA_ROOT`
(default `/media`) and requires no S3 credentials. All readers need that same
private durable archive. See [configuration](../docs/configuration.md).

Downloads are bounded and verified before publication. The spool preserves
pending records and verified caches across API/storage outages. Independent text
and media lanes prevent slow media from delaying fresh text ingestion. Retry
state, exponential cooldown, recovery reservations and quarantine are durable;
logs use opaque item/attempt IDs and fixed sanitized reasons.

Supported receive-side context includes replies, reactions, edits, deletion
markers and conservative protocol-only classification. Unknown/mixed content
retains private recovery data and explicit unavailable reasons; full history or
recovery of missing originals is not guaranteed. Effective deletion hides content
from normal reads while retained source remains available for operator recovery.

## Own-phone media recovery

When enabled, bounded `SendMediaRetryReceipt` requests ask the account's own phone
for a refreshed path for an existing attachment. This is a WhatsApp protocol
request; it is not a chat-send tool or an arbitrary recipient operation.
`MEDIA_RECOVERY_ENABLED=false` disables these requests. Defaults allow one request
per item cycle, 30 seconds between requests per bridge, a 120-second response
window and eight download attempts per path phase. Missing/expired media can
still fail. Exhausted items remain in non-destructive quarantine for explicit
internal operator review; do not delete the spool or re-pair to clear a backlog.

The unreleased recovery follow-up accepts `/v/`, `/mms/` and `/o1/v/` relative
paths while preserving signed queries and download integrity checks. Recovery
logs add fixed path-family/validation, phone-result, handler-branch and attachment
state classifications without raw paths, payloads or keys. A phone `SUCCESS`
response followed by `recovery_invalid_path` is a local validation failure, not
proof that the phone lacks the file. Existing quarantined media requires explicit
internal operator requeue; an upgrade never triggers a bulk retry.

The new fields are `path_family` (for example `o1_v`), `path_validation`
(`accepted` or a fixed rejection reason), `notification_result` (`success`,
`not_found`, `decryption_error`, `general_error`, `missing` or `other`),
`recovery_branch` and `attachment_state`. Unrelated fields may be omitted;
unallowlisted values become `unknown`. Default zero HTTP/size/duration fields on
quarantine events do not describe a measured media download.

Permanently malformed updates are retained in quarantine with `invalid_metadata`
instead of retried forever. Retained protobufs and attempt counts survive restart.
Encrypted reactions waiting for message secrets remain retryable. Update repair
requires an explicit decoder/operator repair rather than the media requeue route.
After deployment, first test one quarantined media item that still opens on the
paired phone, and check both the refreshed-path outcome and verified archival.

ASR is controlled by API/controller settings and is independent of bridge
archival. Internal status, identity ingestion, pairing and operator routes require
the configured internal token. Never expose that token or port to MCP clients.

## Tests

`go test ./...` and `go vet ./...` use synthetic fixtures. Optional session-database
checks require a disposable PostgreSQL URL; never use an actual paired session DB.
Build provenance and dependency versions are in `go.mod`/`go.sum`; third-party
licenses remain applicable to distributed bridge executables and images.
