# ---------- S3: replay (stand-in for the upstream API) and the data lake ----------
resource "aws_s3_bucket" "replay" {
  bucket        = "${local.name}-replay-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket" "lake" {
  bucket        = "${local.name}-lake-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "all" {
  for_each                = { replay = aws_s3_bucket.replay.id, lake = aws_s3_bucket.lake.id }
  bucket                  = each.value
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "all" {
  for_each = { replay = aws_s3_bucket.replay.id, lake = aws_s3_bucket.lake.id }
  bucket   = each.value
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
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
  }
}

# Captured DataMall pages, replayed by the ingestion Lambda.
resource "aws_s3_object" "fixture" {
  for_each     = fileset("${path.module}/../fixtures", "datamall/**")
  bucket       = aws_s3_bucket.replay.id
  key          = each.value
  source       = "${path.module}/../fixtures/${each.value}"
  etag         = filemd5("${path.module}/../fixtures/${each.value}")
  content_type = endswith(each.value, ".gz") ? "application/gzip" : "application/json"
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
}

# ---------- SQS: page queue with a dead-letter queue ----------
resource "aws_sqs_queue" "dlq" {
  name                      = "${local.name}-pages-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "pages" {
  name                       = "${local.name}-pages"
  visibility_timeout_seconds = 360 # 6 x the process Lambda timeout, as AWS recommends
  message_retention_seconds  = 345600
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.pages.arn]
  })
}
