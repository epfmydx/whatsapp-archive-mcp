{{- define "wa.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- define "wa.fullname" -}}
{{- default (printf "%s-%s" .Release.Name (include "wa.name" .)) .Values.fullnameOverride | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- define "wa.image" -}}
{{- $repository := required "image.repository is required" .image.repository -}}
{{- $digest := default "" .image.digest -}}
{{- if .root.Values.imageRegistry -}}
{{- $repository = printf "%s/%s" (.root.Values.imageRegistry | trimSuffix "/") $repository -}}
{{- end -}}
{{- if $digest -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" $digest) -}}
{{- fail "image.digest must be a full sha256 digest" -}}
{{- end -}}
{{- printf "%s@%s" $repository $digest -}}
{{- else -}}
{{- if .root.Values.requireImageDigest -}}
{{- fail "requireImageDigest requires a digest for every workload image" -}}
{{- end -}}
{{- printf "%s:%s" $repository (required "image.tag is required without a digest" .image.tag) -}}
{{- end -}}
{{- end -}}
{{- define "wa.labels" -}}
app.kubernetes.io/name: {{ include "wa.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
{{- define "wa.security" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
fsGroupChangePolicy: OnRootMismatch
seccompProfile: {type: RuntimeDefault}
{{- end -}}
{{- define "wa.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities: {drop: [ALL]}
{{- end -}}
{{- define "wa.sharedEnv" -}}
- name: INTERNAL_TOKEN
  valueFrom: {secretKeyRef: {name: {{ .Values.app.existingSecret }}, key: INTERNAL_TOKEN}}
- {name: STORAGE_BACKEND, value: {{ .Values.storage.backend | quote }}}
- {name: MEDIA_ROOT, value: {{ ternary "/media/archive" "/media" (eq .Values.storage.backend "disk") | quote }}}
- {name: ASR_ENABLED, value: {{ .Values.worker.enabled | toString | quote }}}
{{- if eq .Values.storage.backend "s3" }}
- name: S3_ACCESS_KEY_ID
  valueFrom: {secretKeyRef: {name: {{ .Values.app.existingSecret }}, key: S3_ACCESS_KEY_ID}}
- name: S3_SECRET_ACCESS_KEY
  valueFrom: {secretKeyRef: {name: {{ .Values.app.existingSecret }}, key: S3_SECRET_ACCESS_KEY}}
- {name: S3_ENDPOINT_URL, value: {{ .Values.s3.endpointUrl | quote }}}
- {name: S3_PUBLIC_ENDPOINT_URL, value: {{ default .Values.s3.endpointUrl .Values.s3.publicEndpointUrl | default "" | quote }}}
- {name: S3_BUCKET, value: {{ .Values.s3.bucket | quote }}}
- {name: S3_REGION, value: {{ .Values.s3.region | quote }}}
- {name: S3_ADDRESSING_STYLE, value: {{ .Values.s3.addressingStyle | quote }}}
{{- end }}
- {name: APP_INTERNAL_URL, value: {{ printf "http://%s:8000" (include "wa.fullname" .) | quote }}}
{{- end -}}
{{- define "wa.appEnv" -}}
{{ include "wa.sharedEnv" . }}
{{- range $key := list "DATABASE_URL" "SESSION_SECRET" "TOKEN_ENCRYPTION_KEY" "ADMIN_PASSWORD" }}
- name: {{ $key }}
  valueFrom: {secretKeyRef: {name: {{ $.Values.app.existingSecret }}, key: {{ $key }}}}
{{- end }}
- {name: APP_BASE_URL, value: {{ .Values.app.baseUrl | quote }}}
- {name: ADMIN_EMAIL, value: {{ .Values.app.adminEmail | quote }}}
- {name: OAUTH_CLIENTS_JSON, value: {{ .Values.app.oauthClients | toJson | quote }}}
- {name: OAUTH_DYNAMIC_REGISTRATION_ENABLED, value: {{ .Values.app.oauthDynamicRegistration | toString | quote }}}
- {name: LOGIN_PROXY_MODE, value: {{ .Values.app.loginProxy.mode | quote }}}
{{- if eq .Values.app.loginProxy.mode "signed" }}
- name: LOGIN_PROXY_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ default .Values.app.existingSecret .Values.app.loginProxy.secretName }}
      key: {{ .Values.app.loginProxy.secretKey }}
{{- end }}
{{- $accounts := list -}}
{{- $urls := dict -}}
{{- range .Values.accounts -}}
{{- $accounts = append $accounts (dict "id" .id "label" .label "profileSlot" (default false .profileSlot)) -}}
{{- $_ := set $urls .id (printf "http://%s-bridge-%s:8080" (include "wa.fullname" $) .id) -}}
{{- end }}
- {name: ACCOUNTS_JSON, value: {{ $accounts | toJson | quote }}}
- {name: BRIDGE_URLS_JSON, value: {{ $urls | toJson | quote }}}
{{ include "wa.databaseEnv" . }}
{{- with .Values.app.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "wa.accountsJSON" -}}
{{- $accounts := list -}}
{{- range .Values.accounts -}}
{{- $accounts = append $accounts (dict "id" .id "label" .label "profileSlot" (default false .profileSlot)) -}}
{{- end -}}
{{- $accounts | toJson -}}
{{- end -}}

{{- define "wa.webhookRelayTokenEnv" -}}
- name: WEBHOOK_RELAY_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.webhookRelay.existingSecret }}
      key: {{ .Values.webhookRelay.secretKey }}
{{- end -}}

{{- define "wa.webhookRelayClientEnv" -}}
{{- if .Values.webhookRelay.enabled }}
- {name: WEBHOOK_RELAY_URL, value: {{ printf "http://%s-webhook-relay:8001" (include "wa.fullname" .) | quote }}}
{{ include "wa.webhookRelayTokenEnv" . }}
{{- end }}
{{- end -}}

{{- define "wa.httpAppEnv" -}}
{{ include "wa.appEnv" . }}
{{ include "wa.webhookRelayClientEnv" . }}
- {name: CLOUDFLARE_PROXY_ENABLED, value: {{ .Values.cloudflareProxy.enabled | toString | quote }}}
{{- if .Values.cloudflareProxy.enabled }}
- name: CLOUDFLARE_ORIGIN_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ required "cloudflareProxy.originSecretName is required when enabled" .Values.cloudflareProxy.originSecretName }}
      key: {{ required "cloudflareProxy.originSecretKey is required when enabled" .Values.cloudflareProxy.originSecretKey }}
{{- end }}
{{- end -}}

{{- define "wa.archiveClaim" -}}
{{- default (printf "%s-archive" (include "wa.fullname" .)) .Values.storage.disk.existingClaim -}}
{{- end -}}
{{- define "wa.archiveVolume" -}}
{{- if eq .Values.storage.backend "disk" }}
- name: archive
  persistentVolumeClaim:
    claimName: {{ include "wa.archiveClaim" . }}
{{- end }}
{{- end -}}
{{- define "wa.archiveReadMount" -}}
{{- if eq .Values.storage.backend "disk" }}
- {name: archive, mountPath: /media, readOnly: true}
{{- end }}
{{- end -}}
