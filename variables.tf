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
  description = "True to send pushes to APNs' sandbox host instead of production."
  type        = bool
  default     = false
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
