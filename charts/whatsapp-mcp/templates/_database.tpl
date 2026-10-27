{{- define "wa.databaseEnv" -}}
{{- if .Values.databaseCA.existingConfigMap }}
- {name: PGSSLROOTCERT, value: /etc/postgresql/ca.crt}
{{- end }}
{{- end -}}

{{- define "wa.databaseMount" -}}
{{- if .Values.databaseCA.existingConfigMap }}
- {name: database-ca, mountPath: /etc/postgresql, readOnly: true}
{{- end }}
{{- end -}}

{{- define "wa.databaseVolume" -}}
{{- if .Values.databaseCA.existingConfigMap }}
- name: database-ca
  configMap:
    name: {{ .Values.databaseCA.existingConfigMap | quote }}
    items:
      - {key: {{ .Values.databaseCA.key | quote }}, path: ca.crt}
{{- end }}
{{- end -}}
