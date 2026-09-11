{{/* Shared names, labels and environment for the MailHub chart. */}}

{{- define "mailhub.name" -}}
{{- default "mailhub" .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mailhub.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "mailhub.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "mailhub.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "mailhub.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: mailhub
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "mailhub.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mailhub.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "mailhub.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "mailhub.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "mailhub.image" -}}
{{- printf "%s@%s" (required "values.image.repository is required" .Values.image.repository) (required "values.image.digest is required (immutable digest releases only)" .Values.image.digest) -}}
{{- end -}}

{{/*
Fail closed on combinations that would weaken a production release.  These are
belt-and-braces: values.schema.json already constrains the same fields.
*/}}
{{- define "mailhub.validate" -}}
{{- if .Values.runtime.allowSandbox -}}
{{- fail "runtime.allowSandbox must stay false: a released chart must never register the sandbox connector" -}}
{{- end -}}
{{- if or .Values.runtime.outboundEnabled .Values.runtime.ruleAutomationEnabled -}}
{{- if not (and (hasKey .Values.externalSecret.keys "killSwitchEndpoint") .Values.externalSecret.keys.killSwitchEndpoint) -}}
{{- fail "runtime.outboundEnabled/runtime.ruleAutomationEnabled require a non-empty externalSecret.keys.killSwitchEndpoint (host-owned four-eyes switch)" -}}
{{- end -}}
{{- end -}}
{{- if .Values.providers.imap.enabled -}}
{{- if not .Values.providers.imap.host -}}
{{- fail "providers.imap.host is required when providers.imap.enabled is true" -}}
{{- end -}}
{{- if not .Values.providers.imap.smtpHost -}}
{{- fail "providers.imap.smtpHost is required when providers.imap.enabled is true" -}}
{{- end -}}
{{- if .Values.providers.imap.smtpSendEnabled -}}
{{- if not .Values.runtime.outboundEnabled -}}
{{- fail "providers.imap.smtpSendEnabled requires runtime.outboundEnabled (sending stays doubly gated)" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if .Values.autoscaling.enabled -}}
{{- if lt (int .Values.autoscaling.maxReplicas) (int .Values.autoscaling.minReplicas) -}}
{{- fail "autoscaling.maxReplicas must be >= autoscaling.minReplicas" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Secret-backed Host Port environment. */}}
{{- define "mailhub.secretEnv" -}}
{{- $secret := .Values.externalSecret.name -}}
{{- $optional := .Values.externalSecret.optionalKeys -}}
{{- range $logical, $key := .Values.externalSecret.keys }}
- name: {{ $key }}
  valueFrom:
    secretKeyRef:
      name: {{ $secret }}
      key: {{ $key }}
      {{- if has $logical $optional }}
      optional: true
      {{- end }}
{{- end }}
{{- end -}}

{{/* Full container environment: runtime switches, providers, then Host Ports. */}}
{{- define "mailhub.env" -}}
- name: PYTHONDONTWRITEBYTECODE
  value: "1"
- name: PYTHONUNBUFFERED
  value: "1"
- name: MAILHUB_ENV
  value: production
- name: MAILHUB_ALLOW_SANDBOX
  value: {{ .Values.runtime.allowSandbox | quote }}
- name: MAILHUB_OUTBOUND_ENABLED
  value: {{ .Values.runtime.outboundEnabled | quote }}
- name: MAILHUB_JOB_LEASE_SECONDS
  value: {{ .Values.runtime.jobLeaseSeconds | quote }}
- name: MAILHUB_SMTP_MAX_SEND_BYTES
  value: {{ .Values.runtime.smtpMaxSendBytes | quote }}
- name: MAILHUB_RULE_AUTOMATION_ENABLED
  value: {{ .Values.runtime.ruleAutomationEnabled | quote }}
- name: MAILHUB_IMAP_ENABLED
  value: {{ .Values.providers.imap.enabled | quote }}
{{- if .Values.providers.imap.enabled }}
- name: MAILHUB_IMAP_HOST
  value: {{ .Values.providers.imap.host | quote }}
- name: MAILHUB_IMAP_PORT
  value: {{ .Values.providers.imap.port | quote }}
- name: MAILHUB_IMAP_FOLDER
  value: {{ .Values.providers.imap.folder | quote }}
- name: MAILHUB_SMTP_HOST
  value: {{ .Values.providers.imap.smtpHost | quote }}
- name: MAILHUB_SMTP_PORT
  value: {{ .Values.providers.imap.smtpPort | quote }}
{{- end }}
- name: MAILHUB_SMTP_SEND_ENABLED
  value: {{ .Values.providers.imap.smtpSendEnabled | quote }}
- name: MAILHUB_GMAIL_ENABLED
  value: {{ .Values.providers.gmail.enabled | quote }}
- name: MAILHUB_MICROSOFT_GRAPH_ENABLED
  value: {{ .Values.providers.microsoftGraph.enabled | quote }}
{{- include "mailhub.secretEnv" . }}
{{- with .Values.api.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}
