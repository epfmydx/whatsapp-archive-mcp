# Public edition implementation contract

Port authorized 2026-10-28 after private acceptance: the owner reported the
private deployment working well, with roughly 2.5× ASR improvement, and requested
the fixes be ported here. Apply the deterministic recovery privacy-test correction
and positive thread counts without a fixed server-size maximum, preserving
read-only MCP, disk/S3 storage, optional ASR, defaults and dependency/model pins.
Record controlled public-audio benchmark evidence separately from operator
feedback. Source chart/appVersion becomes 0.7.3; build matching images yourself.
This authorizes source commits, normal main push and public CI verification.
No private Git history, runtime/archive data, deployment records, registry images,
public release tag, model publication or live rollout is included. The sibling
private checkout and all running deployments remain unchanged.

Publication authorized 2026-10-28: the owner's release request covers committing
and normally pushing the reviewed recovery follow-up to this existing public
source repository and verifying source CI. This supersedes the earlier
commit/push exclusion for this candidate only. Public image/model/chart registry
publication, release tags and deployments remain outside this source pipeline.

Accepted 2026-10-28: apply the media-recovery follow-up to this separate public
edition as source/tests/docs only: relative `/o1/v/` compatibility, sanitized
recovery diagnostics and terminal retention of permanently invalid updates.
Preserve read-only MCP, disk/S3 behavior, optional ASR and all existing data.
No automatic requeue, live phone request, commit/push, release or deployment is
included in this follow-up.

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
