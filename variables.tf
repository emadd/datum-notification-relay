variable "environment" {
  description = "Deployment environment name, used to namespace resources (dev/staging/prod)."
  type        = string
  default     = "dev"
}

variable "apns_team_id" {
  description = "Apple Developer Team ID (safe to be public -- see NOTIFICATION-SERVER-INFRA.md §8)."
  type        = string
  default     = "3YLWGYTJST"
}

variable "apns_key_id" {
  description = "APNs Auth Key Key ID (safe to be public)."
  type        = string
  default     = "T763J5X2R6"
}

variable "apns_bundle_id" {
  description = "The client app's bundle id, used as the apns-topic header."
  type        = string
  default     = "com.madsen.datum"
}

variable "apns_use_sandbox" {
  description = <<-EOT
    Which APNs host run_due_jobs sends to. true/false forces sandbox/production
    outright; null (the default) derives it from `environment`: every
    environment except "prod" sends to the sandbox host, since a Debug/
    devicectl-signed build carries `aps-environment: development` (Xcode/App
    Store Connect only flips it to "production" for a distribution-signed
    archive), so a non-prod environment's registered device tokens are
    sandbox-issued. Override only if a non-prod environment is deliberately
    paired with distribution-signed builds, or vice versa.
  EOT
  type        = bool
  default     = null
  nullable    = true
}

variable "due_job_check_interval_minutes" {
  description = "How often the run-due-jobs Lambda is invoked by EventBridge."
  type        = number
  default     = 5
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for both Lambda functions."
  type        = number
  default     = 14
}
