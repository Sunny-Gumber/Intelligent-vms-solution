{{- define "intelligent-vms.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "intelligent-vms.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "intelligent-vms.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "intelligent-vms.labels" -}}
app.kubernetes.io/name: {{ include "intelligent-vms.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end }}

{{- define "intelligent-vms.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "intelligent-vms.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}
