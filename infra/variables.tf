variable "region" {
  type    = string
  default = "ap-southeast-1"
}

variable "project" {
  type    = string
  default = "sg-transit-cloud-api"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "prefix" {
  description = "Short prefix for resource names."
  type        = string
  default     = "sgtc"
}

variable "schedule_enabled" {
  description = "Run the ingestion schedules. Off for tests so runs are deterministic."
  type        = bool
  default     = false
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "cors_origin" {
  description = "Single browser origin allowed to call the API (empty = no CORS header)."
  type        = string
  default     = ""
}

variable "process_timeout_s" {
  description = "Timeout of the processing Lambda."
  type        = number
  default     = 60
}

variable "queue_visibility_timeout_s" {
  description = "Page queue visibility timeout. AWS recommends 6x the consumer timeout; tests shorten both so the DLQ path runs in about a minute."
  type        = number
  default     = 360
  validation {
    condition     = var.queue_visibility_timeout_s >= var.process_timeout_s
    error_message = "Visibility timeout must be at least the processing Lambda timeout."
  }
}
