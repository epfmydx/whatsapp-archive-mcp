# Storage and optional transcription

S3-compatible storage and disk use the same archived object-key contract.
Set one backend consistently on API, bridge and worker. Existing archive metadata
is retained; changing the setting alone does not copy or migrate archived media.

## S3 / DigitalOcean Spaces / MinIO

Use `storage.backend: s3` (default). Helm's `s3` values map to these environment
variables; credentials belong in the existing app Secret, never a values file.

| Helm value | Environment variable | AWS example / purpose |
| --- | --- | --- |
| `s3.endpointUrl` | `S3_ENDPOINT_URL` | Empty uses the AWS regional endpoint |
| `s3.publicEndpointUrl` | `S3_PUBLIC_ENDPOINT_URL` | Optional client-reachable endpoint for signed downloads |
| `s3.region` | `S3_REGION` | `ap-southeast-1` |
| `s3.bucket` | `S3_BUCKET` | Your private bucket/Space |
| `s3.addressingStyle` | `S3_ADDRESSING_STYLE` | `virtual` for AWS/Spaces, `path` commonly for MinIO |
| Secret `S3_ACCESS_KEY_ID` | `S3_ACCESS_KEY_ID` | Scoped storage access key |
| Secret `S3_SECRET_ACCESS_KEY` | `S3_SECRET_ACCESS_KEY` | Storage secret key |

For Spaces use `https://REGION.digitaloceanspaces.com`, the Space name and its
region. For MinIO use your API endpoint (not the console), bucket and region.
If internal and external endpoints differ, both must refer to the same objects;
the download endpoint must be reachable by the MCP client. Do not expose buckets
publicly. Signed downloads last five minutes; proxy/CDN policies must preserve
signatures and private responses.

## Disk

```yaml
storage:
  backend: disk
  disk:
    existingClaim: archive-rwx
worker:
  enabled: false
```

`STORAGE_BACKEND=disk` and `MEDIA_ROOT=/media/archive` are passed to Helm workloads,
inside the shared volume mounted at `/media`. The bridge creates and owns this
private child; it does not try to chmod the root-owned PVC mount itself. The
API and optional worker mount the archive read-only; bridges mount it writable.
The controller/schema bootstrap do not need media access. Disk mode does not
require S3 credentials.

For Kubernetes, supply a persistent **ReadWriteMany** claim accessible from all
relevant nodes, or configure `storage.disk.storageClass` and `size` to create one.
A local per-node path or a single-node block volume is not a substitute for this
multi-bridge/multi-node contract. Outside Kubernetes, mount the same persistent
directory into API, bridge and optional worker containers; standalone
`MEDIA_ROOT` defaults to `/media` and must be owned by the service UID. Use the
shared runtime UID 10001 and private file permissions. Do not mount the archive
inside web static directories. Do not use symlinked roots or paths.

Disk mode requires POSIX no-follow path operations, atomic hard-link publication,
fsync and private permissions. The chart sets `fsGroupChangePolicy: OnRootMismatch`
to avoid normal recursive kubelet permission changes on remount. Confirm your
RWX provisioner/CSI driver honors compatible ownership and permission semantics;
a driver that rewrites existing private files can break verified duplicate reads.
The chart setting is not evidence of live-volume acceptance.

The API's signed disk-download link expires after 300 seconds. Each download
checks the retained message, profile assignment and effective deletion state.
The disk archive is retained when a message is hidden after deletion, but that
message's original media must no longer be served through MCP or signed links.
Back up the volume with its database; archive files are not disposable cache.
Helm retention annotations protect created archive/cache PVCs from normal Helm
deletion. They do not replace backup or prevent explicit administrative deletion.

## Optional ASR

```yaml
worker:
  enabled: false
```

Equivalent environment setting: `ASR_ENABLED=false` on API/controller. Default is
true. Accepted values are booleans or the strings `true`, `false`, `1`, `0`.
Changing environment configuration requires restarting/rolling out the affected
processes. Off means no new transcription jobs from ingestion and no controller
launch/relaunch of queued or missing worker Jobs. Text/audio/media archival,
OAuth, MCP reads and webhook delivery continue.

Existing queued/running database state is retained. Already running workers may
finish and submit authenticated results. Kubernetes reconciliation/terminal
cleanup is suspended while disabled and resumes when enabled again. Audio
archived while disabled is not automatically queued by replay or re-enabling.
The chart omits the worker template and model-cache claim while disabled;
previously created kept cache claims remain. No ASR node pool is required when off.

With ASR enabled, CPU Jobs run on demand with one active job across profiles.
Requests must fit a node's allocatable resources. Performance, RAM and language
accuracy depend on the model, audio and hardware; cached synthetic measurements
are not an acceptance guarantee. No GPU or cloud inference API is required.

Source chart 0.7.3 accepts positive `worker.threads` integers without the former
two-thread maximum. CPU and memory remain `worker.resources.requests` and
`worker.resources.limits`, with no application RAM ceiling. Defaults remain one
thread, CPU request `1000m` / limit `1500m`, and memory request/limit `3Gi`.
Use a matching updated worker image; older images still reject larger counts.
Existing Jobs retain their settings; new Jobs use the changed template.
See [ASR tuning](asr-tuning.md) for allocation guidance and benchmark limits.
