data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/build/lambda.zip"
  excludes    = ["**/__pycache__/**"]
}

locals {
  fn_config = {
    ingest  = { handler = "transit.ingest.handler", timeout = 60, memory = 256 }
    process = { handler = "transit.process.handler", timeout = 60, memory = 256 }
    api     = { handler = "transit.api.handler", timeout = 10, memory = 256 }
  }
  fn_env = {
    STOPS_TABLE   = aws_dynamodb_table.stops.name
    SPEEDS_TABLE  = aws_dynamodb_table.speeds.name
    META_TABLE    = aws_dynamodb_table.meta.name
    RAW_BUCKET    = aws_s3_bucket.lake.id
    REPLAY_BUCKET = aws_s3_bucket.replay.id
    QUEUE_URL     = aws_sqs_queue.pages.id
    SOURCE_MODE   = "replay"
    CORS_ORIGIN   = var.cors_origin
    LOG_LEVEL     = "INFO"
  }
}

resource "aws_cloudwatch_log_group" "fn" {
  for_each          = local.functions
  name              = "/aws/lambda/${local.name}-${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "fn" {
  for_each         = local.functions
  function_name    = "${local.name}-${each.key}"
  role             = aws_iam_role.fn[each.key].arn
  runtime          = "python3.12"
  handler          = local.fn_config[each.key].handler
  timeout          = local.fn_config[each.key].timeout
  memory_size      = local.fn_config[each.key].memory
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = local.fn_env
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.fn[each.key].name
  }

  depends_on = [aws_iam_role_policy.fn, aws_cloudwatch_log_group.fn]
}

resource "aws_lambda_event_source_mapping" "pages" {
  event_source_arn        = aws_sqs_queue.pages.arn
  function_name           = aws_lambda_function.fn["process"].arn
  batch_size              = 5
  function_response_types = ["ReportBatchItemFailures"]
}

# ---------- EventBridge schedules ----------
resource "aws_cloudwatch_event_rule" "ingest" {
  for_each = {
    speeds = { schedule = "rate(15 minutes)", dataset = "TrafficSpeedBands" }
    stops  = { schedule = "cron(0 19 * * ? *)", dataset = "BusStops" } # 03:00 SGT daily
  }
  name                = "${local.name}-ingest-${each.key}"
  schedule_expression = each.value.schedule
  state               = var.schedule_enabled ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "ingest" {
  for_each = aws_cloudwatch_event_rule.ingest
  rule     = each.value.name
  arn      = aws_lambda_function.fn["ingest"].arn
  input    = jsonencode({ dataset = each.key == "speeds" ? "TrafficSpeedBands" : "BusStops" })
}

resource "aws_lambda_permission" "events" {
  for_each      = aws_cloudwatch_event_rule.ingest
  statement_id  = "events-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn["ingest"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = each.value.arn
}
