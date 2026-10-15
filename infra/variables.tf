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
