# Public edition implementation contract

Accepted 2026-10-27: prepare a separate, sanitized public source edition; preserve the private repository and all deployments. Use Linux/DESKTOP for new linked-device registrations. The MCP catalog is intentionally read-only with no write tools, including local alias mutation tools. Preserve private archive/consent/event security boundaries.

Storage must support S3-compatible services (AWS S3, DigitalOcean Spaces, MinIO) and a disk backend. Disk mode on Kubernetes needs durable storage shared between bridge writers, API readers and optional transcription workers; do not silently rely on ephemeral files or unrelated per-node paths. Authenticated MCP may produce short-lived private media download links.

ASR is optional: a validated setting must stop new transcription work when off while keeping text/media archival and event callbacks functioning. Existing archive/transcript/job state must be retained; do not silently backfill when re-enabled.

The owner selected GPL-3.0-only for project-authored code with third-party
notices. The owner supplied `git@github.com:epfmydx/whatsapp-archive-mcp.git` and authorized
the first public source commit and normal main push. Publish only this sanitized
fresh-history copy. No release tag, registry/image/model publication, pairing,
live data access or development/production deployment is authorized. The existing
private implementation and runtime remain separate. Public CI results must be
verified before claiming provider-hosted acceptance.
