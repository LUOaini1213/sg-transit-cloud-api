# ---------- S3: replay (stand-in for the upstream API), data lake, access logs ----------
resource "aws_s3_bucket" "replay" {
  bucket        = "${local.name}-replay-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket" "lake" {
  bucket        = "${local.name}-lake-${local.account_id}"
  force_destroy = true
}

# Server access logs for the two data buckets. AWS does not deliver access
# logs to a bucket that uses SSE-KMS, so this one uses SSE-S3, and it does not
# log to itself. Both are accepted findings (docs/SECURITY.md).
#trivy:ignore:AVD-AWS-0089
resource "aws_s3_bucket" "logs" {
  bucket        = "${local.name}-access-logs-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "replay" {
  bucket                  = aws_s3_bucket.replay.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "lake" {
  bucket                  = aws_s3_bucket.lake.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "replay" {
  bucket = aws_s3_bucket.replay.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true
  }
}

#trivy:ignore:AVD-AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "replay" {
  bucket = aws_s3_bucket.replay.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_versioning" "lake" {
  bucket = aws_s3_bucket.lake.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_versioning" "logs" {
  bucket = aws_s3_bucket.logs.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_logging" "replay" {
  bucket        = aws_s3_bucket.replay.id
  target_bucket = aws_s3_bucket.logs.id
  target_prefix = "replay/"
}

resource "aws_s3_bucket_logging" "lake" {
  bucket        = aws_s3_bucket.lake.id
  target_bucket = aws_s3_bucket.logs.id
  target_prefix = "lake/"
}

# TLS-only access to every bucket.
locals {
  bucket_arns = { replay = aws_s3_bucket.replay.arn, lake = aws_s3_bucket.lake.arn, logs = aws_s3_bucket.logs.arn }
}

data "aws_iam_policy_document" "tls_only" {
  for_each = local.bucket_arns
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [each.value, "${each.value}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "tls_only" {
  for_each = local.bucket_arns
  bucket   = split(":::", each.value)[1]
  policy   = data.aws_iam_policy_document.tls_only[each.key].json
  depends_on = [
    aws_s3_bucket_public_access_block.replay,
    aws_s3_bucket_public_access_block.lake,
    aws_s3_bucket_public_access_block.logs,
  ]
}

resource "aws_s3_bucket_lifecycle_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id
  rule {
    id     = "expire-raw"
    status = "Enabled"
    filter {
      prefix = "raw/"
    }
    expiration {
      days = 90
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
  rule {
    id     = "expire-rejected"
    status = "Enabled"
    filter {
      prefix = "rejected/"
    }
    expiration {
      days = 180
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    id     = "expire-logs"
    status = "Enabled"
    filter {}
    expiration {
      days = 365
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# Captured DataMall pages, replayed by the ingestion Lambda.
resource "aws_s3_object" "fixture" {
  for_each     = fileset("${path.module}/../fixtures", "datamall/**")
  bucket       = aws_s3_bucket.replay.id
  key          = each.value
  source       = "${path.module}/../fixtures/${each.value}"
  source_hash  = filemd5("${path.module}/../fixtures/${each.value}")
  content_type = endswith(each.value, ".gz") ? "application/gzip" : "application/json"
  depends_on   = [aws_s3_bucket_server_side_encryption_configuration.replay]
}

# ---------- DynamoDB ----------
resource "aws_dynamodb_table" "stops" {
  name         = "${local.name}-stops"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "code"

  attribute {
    name = "code"
    type = "S"
  }
  attribute {
    name = "gh6"
    type = "S"
  }

  global_secondary_index {
    name               = "by_geohash"
    hash_key           = "gh6"
    range_key          = "code"
    projection_type    = "INCLUDE"
    non_key_attributes = ["description", "road", "lat", "lon"]
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.data.arn
  }
  point_in_time_recovery {
    enabled = true
  }
}

resource "aws_dynamodb_table" "speeds" {
  name         = "${local.name}-speeds"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "link_id"
  range_key    = "captured_at"

  attribute {
    name = "link_id"
    type = "S"
  }
  attribute {
    name = "captured_at"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.data.arn
  }
  point_in_time_recovery {
    enabled = true
  }
}

resource "aws_dynamodb_table" "meta" {
  name         = "${local.name}-meta"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.data.arn
  }
  point_in_time_recovery {
    enabled = true
  }
}

# ---------- SQS: page queue with a dead-letter queue ----------
resource "aws_sqs_queue" "dlq" {
  name                              = "${local.name}-pages-dlq"
  message_retention_seconds         = 1209600
  kms_master_key_id                 = aws_kms_key.data.arn
  kms_data_key_reuse_period_seconds = 300
}

resource "aws_sqs_queue" "pages" {
  name                              = "${local.name}-pages"
  visibility_timeout_seconds        = var.queue_visibility_timeout_s
  message_retention_seconds         = 345600
  kms_master_key_id                 = aws_kms_key.data.arn
  kms_data_key_reuse_period_seconds = 300
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3
  })
}

# A redrive-allow policy on the DLQ (only the pages queue may use it) belongs
# here on AWS. It is left out because LocalStack 4.14 never reports the
# attribute as removed, so `terraform destroy` waits 20 minutes and fails.
