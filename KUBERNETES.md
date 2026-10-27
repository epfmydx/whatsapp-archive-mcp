# Kubernetes setup

This chart packages a single-owner API, a controller, one WhatsApp bridge per
configured account and optional on-demand transcription Jobs. It does not install
PostgreSQL, an S3 service or an RWX storage provisioner.

## Prerequisites

- PostgreSQL application database and a separate session database per bridge.
  Use scoped runtime credentials, not a superuser or automatic database creation.
- Private S3-compatible storage, or a durable shared RWX archive volume.
- HTTPS hostname reachable by your MCP client, with OAuth paths routed to the API.
- Images built from this public edition and available to your cluster.

Build with `docker build -t YOUR_REGISTRY/whatsapp-mcp-app:YOUR_TAG service`,
and equivalently bridge/worker using their directories. The worker image is only
needed when ASR is enabled. Configure `app.image`, `bridge.image`, `worker.image`
with your own references. Pin digests for a reviewed release; set
`requireImageDigest: true` when every configured component has a verified digest.
No private image or release tag from another installation is required.

## Secrets and values

The app Secret named by `app.existingSecret` requires `DATABASE_URL`,
`SESSION_SECRET`, `TOKEN_ENCRYPTION_KEY`, `INTERNAL_TOKEN` and `ADMIN_PASSWORD`.
Use independently generated high-entropy secrets; `TOKEN_ENCRYPTION_KEY` must be
a valid Fernet key. Set `app.adminEmail` explicitly. S3 mode additionally requires
`S3_ACCESS_KEY_ID` and `S3_SECRET_ACCESS_KEY`; disk mode does not.

Each account's `sessionSecretName` must contain `SESSION_DATABASE_URL`. Give each
bridge its own session database/credential. The chart does not allocate database
servers or require the API to have infrastructure-management permissions.
Profile slots are administrator-provisioned; naming one in the UI assigns it.
Keep account IDs, session secrets and spool volumes stable across upgrades.

Use [storage and ASR configuration](docs/configuration.md) to choose a backend.
Copy examples to an ignored `values.production.local.yaml`; do not commit actual
hosts, credentials, bucket details or live data. Configure ingress or HTTPRoute,
trusted proxy peers and network policy for your environment. Generic examples
are not validated infrastructure for your cluster. Callback verification and
later webhook delivery require appropriate outbound HTTPS access; the optional
relay can isolate that access.

```sh
helm lint --strict charts/whatsapp-mcp
helm template whatsapp-mcp charts/whatsapp-mcp \
  --namespace whatsapp-mcp -f values.production.local.yaml > /tmp/whatsapp-mcp-rendered.yaml
# Review the manifest and confirm context, storage and Secret references first.
helm upgrade --install whatsapp-mcp charts/whatsapp-mcp \
  --kube-context YOUR_CONTEXT --namespace whatsapp-mcp --create-namespace \
  -f values.production.local.yaml --wait --timeout 10m
```

Review schema-bootstrap, app/controller/bridge placement and optional worker
placement separately. Worker pool labels and taints belong in infrastructure
values. The worker template checksum changes when active worker configuration
changes. A Pending Job waits for sufficient capacity; the application does not
create nodes or configure cluster autoscaling.

Backend changes require a deliberate data migration and backup plan. Disabling
ASR retains archive/job data; retained model-cache claims may need explicit reuse
when re-enabling. Never delete session state or re-pair just to change a label,
storage setting or release. These instructions have not deployed a public edition.
