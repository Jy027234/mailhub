{{- define "mailhub.name" -}}
mailhub
{{- end -}}
{{- define "mailhub.fullname" -}}
{{ .Release.Name }}-mailhub
{{- end -}}
