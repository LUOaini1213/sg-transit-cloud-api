# One role per function. Each policy names the exact resources it touches;
# scripts/check_iam.py fails the pipeline if a wildcard action or resource
# slips in (cloudwatch:PutMetricData is the one exception: it has no resource
# ARN, so it is scoped with a namespace condition instead).

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "fn" {
  for_each           = local.functions
  name               = "${local.name}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

locals {
  log_arn = { for f in local.functions : f => "${aws_cloudwatch_log_group.fn[f].arn}:*" }
}

data "aws_iam_policy_document" "ingest" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [local.log_arn["ingest"]]
  }
  statement {
    sid       = "ListReplay"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.replay.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["datamall/*"]
    }
  }
  statement {
    sid       = "ReadReplay"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.replay.arn}/datamall/*"]
  }
  statement {
    sid       = "WriteRaw"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.lake.arn}/raw/*"]
  }
  statement {
    sid       = "Queue"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.pages.arn]
  }
  statement {
    sid       = "RunState"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.meta.arn]
  }
  statement {
    sid       = "Metrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["SgTransit"]
    }
  }
}

data "aws_iam_policy_document" "process" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [local.log_arn["process"]]
  }
  statement {
    sid       = "Consume"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"]
    resources = [aws_sqs_queue.pages.arn]
  }
  statement {
    sid       = "ReadRaw"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.lake.arn}/raw/*"]
  }
  statement {
    sid       = "WriteRejected"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.lake.arn}/rejected/*"]
  }
  statement {
    sid       = "Upsert"
    actions   = ["dynamodb:PutItem", "dynamodb:BatchWriteItem"]
    resources = [aws_dynamodb_table.stops.arn, aws_dynamodb_table.speeds.arn, aws_dynamodb_table.meta.arn]
  }
  statement {
    sid       = "Metrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["SgTransit"]
    }
  }
}

data "aws_iam_policy_document" "api" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [local.log_arn["api"]]
  }
  statement {
    sid       = "ReadStops"
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.stops.arn, "${aws_dynamodb_table.stops.arn}/index/by_geohash"]
  }
  statement {
    sid       = "ReadSpeedsAndMeta"
    actions   = ["dynamodb:Query"]
    resources = [aws_dynamodb_table.speeds.arn, aws_dynamodb_table.meta.arn]
  }
}

locals {
  policy_json = {
    ingest  = data.aws_iam_policy_document.ingest.json
    process = data.aws_iam_policy_document.process.json
    api     = data.aws_iam_policy_document.api.json
  }
}

resource "aws_iam_role_policy" "fn" {
  for_each = local.functions
  name     = "${local.name}-${each.key}"
  role     = aws_iam_role.fn[each.key].id
  policy   = local.policy_json[each.key]
}
