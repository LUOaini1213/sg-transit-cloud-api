data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  name       = "${var.prefix}-${var.environment}"
  account_id = data.aws_caller_identity.current.account_id
  functions  = toset(["ingest", "process", "api"])
}
