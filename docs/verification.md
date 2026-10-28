# Public edition verification — 27 October 2026

## Recovery compatibility follow-up — 28 October 2026 (unreleased)

Baseline `e0f59ae6ae493cc0d129a5e8b5188c66560aadf9`, plus local bridge/tests/docs
changes. The shared recovery patch supports `/o1/v/` with signed-byte/integrity
preservation, adds fixed diagnostic classifications and retains permanent invalid
updates without automatic retries. Missing-secret reactions remain retryable.

Full bridge `go test -count=1 ./...` and `go vet ./...` passed in an offline
disposable Go 1.27.3 Alpine container with cached dependencies. Synthetic tests
cover encrypted responses, unsafe path rejection, notification outcomes,
diagnostic privacy, durable quarantine/restart and retained payload/attempt state.
Optional live session-database fixtures were not enabled. Service health tests:
**60 passed**. Shared bridge patch equality, whitespace and modified local
documentation links were checked.

A fresh GPT-6.1 Sol reviewer at high effort inspected the integrated source,
tests and operating guidance in both editions; no actionable findings remained.

No source publication, tag, image/chart release, deployment or live phone request
was performed. Existing quarantined media is not automatically retried. Follow
the [bridge's single-item test guidance](../bridge/README.md#own-phone-media-recovery)
after a separately authorized deployment; synthetic checks do not establish
actual paired-phone or CDN recovery.

## Initial preparation snapshot

This is a local public-edition preparation snapshot based on a reviewed private
0.7.0 implementation. Only selected tracked source files were exported. Git
history, runtime state, credentials, archive experiments and private deployment
reports were excluded. At the local preparation checkpoint, the new Git repository had no commits,
tags or remote; no source or runtime artifact had been published. The owner
subsequently authorized the first main push to the separate public repository
`epfmydx/whatsapp-archive-mcp`; its publication/CI evidence is recorded separately.
No container/model image, registry release or deployment is part of this action. The original private checkout remains unchanged.

## Accepted scope

- Linux/DESKTOP linked-device metadata for new pairings, preserving sessions.
- Seven read-only MCP tools; no alias/send/reply/reaction/delete/mark-read tools.
  Only `whatsapp:read` OAuth scope. Unsupported tool calls and write scopes fail.
- Configurable AWS S3, DO Spaces, MinIO or private disk archive storage.
- Optional ASR: disabled ingestion creates no new jobs, controller launches none,
  prior job state/callbacks remain, and webhook dispatch continues.
- GPL-3.0-only project code, with third-party notice/license snapshots.

## Executed local checks

- Full service suite: **642 passed, 14 skipped**, using isolated synthetic SQLite
  fixtures. Optional PostgreSQL integration fixtures were not enabled locally.
- Subsequent generic-namespace/controller/ASR checks: **49 passed**. They cover
  disabled ingestion, no automatic backfill, retained queues/running attempts,
  callbacks, webhook ticks and no Kubernetes initialization when ASR is off.
- Worker: **15 tests passed**, including **33 pytest subtests** in the worker
  qualification pass; the parent also ran unittest discovery successfully.
- Bridge: full `go test ./...` and `go vet ./...` passed in an offline disposable
  `golang:1.27.3-alpine` compiler with a read-only dependency cache. No live session
  database or WhatsApp connection was used.
- A separate non-root filesystem smoke invoked actual `initializeDisk` as UID
  10001 against root-owned `/media` with group 10001 and mode 2770. The old
  mount-root path correctly failed; the `/media/archive` owned-child configuration
  initialized successfully with private directory modes. This is not a live PVC.
- The chart test suite and strict Helm lint passed. It covers storage selection,
  RWX archive mounts and read-only readers, no S3 credentials in disk mode,
  disabled-ASR resources, PVC retention, controller checksum, generic placement,
  AWS empty-endpoint and optional-public-endpoint fallback, and schema rejection.
  Exact final chart counts are recorded at the end of this file.
- The parent ran read-only catalog/dispatch/OAuth/auth/pagination checks:
  **41 passed**. Existing internal alias-resolution fixtures remain for archive
  compatibility, but no public MCP mutation entry is offered.

## Review and repairs

Implementation used GPT-6.1 Sol at high effort for disk source and optional
ASR/chart wiring. A fresh GPT-6.1 Sol reviewer independently ran 142 focused tests
and identified deployment/configuration defects which were repaired and rechecked:
root-owned PVC startup, recursive fsGroup permission drift, empty AWS endpoints
and the optional public-endpoint default. Helm now uses an owned `/media/archive`
child and `fsGroupChangePolicy: OnRootMismatch`. Default S3 endpoints are empty;
custom endpoints are explicit and the public endpoint can inherit the private one.

## Public-export checks

The text/filename scan found no private admin email, user filesystem path,
installation host, private registry reference, old custom device label, tracked database,
media, credential file or Git history in the intended source export. Recognizable
private-key/AWS-key/GitHub-token markers were also checked. This is a targeted
scan, not a formal secret-detection or complete legal/security certification.
DigitalX brand/domain references and third-party copyright identities are intended.
Temporary compiler binaries/fixtures and caches remain ignored under `.local/`.
Local Markdown file targets and whitespace were checked after integration.

## Licensing and remaining proof boundaries

The owner selected GPL-3.0. Full texts/indices accompany the source. All 32 pinned
Go module versions have copied top-level notices. Matching service-environment
Python license metadata was copied for 41 of 44 pinned package/version entries;
three older worker AWS SDK versions are explicitly marked unavailable locally
in the index. The source pins remain unchanged. A source-notice inventory is not
an image SBOM or complete corresponding-source package. Before distributing
binaries/images/weights, review all transitive/base-image/FFmpeg/model obligations
and supply the required notices and corresponding source/build material.

Live PostgreSQL/S3/Spaces interoperability, actual RWX provisioner/CSI permission
behavior, concurrent volume use on different nodes, network-policy acceptance,
physical-device/browser/screen-reader acceptance, pairing, real callback delivery,
actual ASR accuracy/memory and public GitHub CI have not been requalified by these
synthetic checks. Disk mode requires a compatible POSIX persistent filesystem;
backend switches need an explicit data-migration/backup plan. The public source repository is `epfmydx/whatsapp-archive-mcp`. GitHub private
vulnerability reporting remains an owner-managed setting; the security policy
provides a fallback when its private reporting form is unavailable.

## Final chart and workflow checkpoint

The final chart suite passed **41 tests and 118 subtests**; strict Helm lint
passed. The new public CI workflow passed offline actionlint. The independent
reviewer rechecked all reported repairs and found no remaining functional
issues. Source/notice scans and local Markdown targets were rechecked after
integration. Public CI had not run at this local qualification checkpoint. The supplied public
repository will run its source-only workflow after the first push.

## Initial public source publication — 27 October 2026

- Repository: [epfmydx/whatsapp-archive-mcp](https://github.com/epfmydx/whatsapp-archive-mcp),
  verified public and empty before its initial push.
- Initial root commit: `e5090856155a658aa1373ba198f88218093dc07f`.
  It has no parent: private Git history was not exported. Source, documentation
  and licenses were scanned/reviewed as 248 regular text files before publication.
- Commit identity uses the maintainer's verified GitHub no-reply address. The
  source tree contains generic owner configuration and the public repository's
  Go module path. The original private checkout remained clean and unchanged.
- [Public CI run 40533375487](https://github.com/epfmydx/whatsapp-archive-mcp/actions/runs/40533375487)
  completed successfully on that exact source commit. Its test job passed:
  642 service tests with 14 optional fixtures skipped in the first pass;
  41 chart tests with 118 subtests; all 14 disposable PostgreSQL integration
  fixtures in the dedicated pass; 15 worker unittest cases; bridge tests and vet.
  Strict Helm lint also passed. No live archive or paired session was used.
- GitHub recognized the repository license as GPL-3.0. Third-party notices retain
  their own terms; the distribution obligations/limits above still apply.
- No release tag, public binary/container/model image, registry artifact or
  Kubernetes deployment was created. Private vulnerability reporting remains an
  owner-managed setting with the safe fallback described in `SECURITY.md`.
