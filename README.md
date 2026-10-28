# WhatsApp Archive MCP

[Source](https://github.com/epfmydx/whatsapp-archive-mcp) · [License](LICENSE)

A self-hosted WhatsApp archive, **purpose-built for read-only MCP access**.
Connect your own WhatsApp accounts, search archived conversations, read available
media and optionally transcribe voice notes on CPU.

## Read-only by design

**This project does not provide WhatsApp write tools and will not add them.**
There are no MCP tools for sending messages/media, replying, reacting, deleting,
marking messages read or changing contacts. The public MCP catalog also excludes
local alias mutation tools. OAuth grants only `whatsapp:read`.

Available tools: `list_accounts`, `list_chats`, `search_contacts`,
`search_messages`, `get_message`, `get_media` and `read_image`.

Read-only describes what an MCP client can do. The service writes its own archive,
and the owner manages profiles and OAuth access through the admin UI. Pairing,
WhatsApp protocol acknowledgments and optional bounded media-recovery requests to
your own paired phone are still necessary transport functions; this is not a
network-silent or zero-write process. Event subscription methods manage callbacks,
not WhatsApp conversations.

## Choose your storage and transcription

| Option | Configuration | Requirement |
| --- | --- | --- |
| AWS S3 | `storage.backend: s3`; AWS region/bucket, optional endpoint | Private bucket and credentials |
| DigitalOcean Spaces | `storage.backend: s3`; Spaces regional endpoint | Private Space and credentials |
| MinIO / compatible S3 | `storage.backend: s3`; custom endpoint | Compatible S3 API and credentials |
| Disk | `storage.backend: disk` | Durable shared filesystem; Kubernetes RWX PVC |
| Optional CPU transcription | `worker.enabled: true` or `false` | ASR workers/cache only when enabled |

ASR is enabled by default. Turn it off to keep archiving audio without queuing new
transcription jobs. Re-enabling it processes retained queued work and future
audio; audio archived while disabled is not automatically backfilled.
S3 remains the default storage backend. See [configuration](docs/configuration.md).
Changing backends does not migrate existing files or object keys.

With ASR enabled, `worker.threads` accepts positive integers without a fixed
server-size ceiling. Set CPU/memory requests and limits through `worker.resources`.
The default remains one thread and 3Gi memory. See [ASR tuning and measured thread
scaling](docs/asr-tuning.md). Build the updated worker image before choosing more
than two threads; earlier worker versions reject them.

## Deploy

1. Build the app, bridge and optional worker images from `service/`, `bridge/`
   and `worker/`. Publish them to a registry you control.
2. Provision PostgreSQL, one session database per bridge, private storage and
   application/session Secrets. No privileged cluster/database account is used
   by the application to provision new runtimes.
3. Install the [Helm chart](charts/whatsapp-mcp) with your own values and image
   references. Follow [Kubernetes setup](KUBERNETES.md).
4. Sign in as the configured owner, create profiles and scan each pairing QR on
   your own phone. Connect an MCP client using OAuth discovery and consent.

New pairings identify the linked device as **Linux**, platform **DESKTOP**.
Existing sessions are preserved; the label displayed on the phone is controlled
by WhatsApp. It is not a connection-stability guarantee.

## Boundaries

This is an unofficial WhatsApp Web integration using Whatsmeow. History and
attachments are limited to what WhatsApp supplies to the linked device; complete
history, unavailable-media recovery and guaranteed real-time delivery are not
promised. Review WhatsApp's applicable terms before use. Only connect accounts
and archive conversations you are authorized to access.

The app is single-owner with multiple profiles, not a multi-tenant hosted service.
Media remains private: S3 downloads use short-lived presigned URLs; disk downloads
use short-lived signed application links. Treat either as a temporary credential.
Keep accounts, buckets/volumes, databases and backups private. Admin pages are
no-store. Never point tests at a live archive.

## Development and licensing

- [Documentation](docs/README.md), [service](service/README.md),
  [bridge](bridge/README.md) and [worker](worker/README.md).
- [Security reporting](SECURITY.md), [contributing](CONTRIBUTING.md) and
  [verification](docs/verification.md).
- Project code: **GPL-3.0-only**, except separately identified third-party work.
  Copyright 2026 EPFMy DigitalX Services. See [LICENSE](LICENSE) and
  [third-party notices](THIRD_PARTY_NOTICES.md). Brand names and marks are not
  granted for implying endorsement of a fork.
- Inspired by [lharries/whatsapp-mcp](https://github.com/lharries/whatsapp-mcp).

Source is maintained at [epfmydx/whatsapp-archive-mcp](https://github.com/epfmydx/whatsapp-archive-mcp).
Build your own images using the pinned dependency records and setup guide. No
prebuilt public image release or deployment is established by this source publication.
