resource "aws_sns_topic" "alarms" {
  name              = "${local.name}-alarms"
  kms_master_key_id = aws_kms_key.data.arn
}

locals {
  alarms = {
    dlq-not-empty = {
      namespace = "AWS/SQS", metric = "ApproximateNumberOfMessagesVisible", stat = "Maximum",
      dims      = { QueueName = aws_sqs_queue.dlq.name }, threshold = 0, period = 60,
      text      = "Pages failed processing three times and are parked in the DLQ."
    }
    process-errors = {
      namespace = "AWS/Lambda", metric = "Errors", stat = "Sum",
      dims      = { FunctionName = aws_lambda_function.fn["process"].function_name }, threshold = 0, period = 300,
      text      = "The processing Lambda raised errors."
    }
    ingest-errors = {
      namespace = "AWS/Lambda", metric = "Errors", stat = "Sum",
      dims      = { FunctionName = aws_lambda_function.fn["ingest"].function_name }, threshold = 0, period = 900,
      text      = "An ingestion run failed."
    }
    api-5xx = {
      namespace = "AWS/ApiGateway", metric = "5XXError", stat = "Sum",
      dims      = { ApiName = aws_api_gateway_rest_api.api.name, Stage = aws_api_gateway_stage.v1.stage_name },
      threshold = 5, period = 300, text = "The API returned more than five 5xx responses in 5 minutes."
    }
    rejected-records = {
      namespace = "SgTransit", metric = "RecordsRejected", stat = "Sum",
      dims      = { Dataset = "TrafficSpeedBands" }, threshold = 300, period = 900,
      text      = "More than 300 speed-band records failed validation in 15 minutes (about 5% of one snapshot)."
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "a" {
  provider            = aws.untagged
  for_each            = local.alarms
  alarm_name          = "${local.name}-${each.key}"
  alarm_description   = each.value.text
  namespace           = each.value.namespace
  metric_name         = each.value.metric
  statistic           = each.value.stat
  dimensions          = each.value.dims
  period              = each.value.period
  evaluation_periods  = 1
  threshold           = each.value.threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "api_latency" {
  provider            = aws.untagged
  alarm_name          = "${local.name}-api-latency-p95"
  alarm_description   = "API p95 latency above 1 s over 5 minutes."
  namespace           = "AWS/ApiGateway"
  metric_name         = "Latency"
  extended_statistic  = "p95"
  dimensions          = { ApiName = aws_api_gateway_rest_api.api.name, Stage = aws_api_gateway_stage.v1.stage_name }
  period              = 300
  evaluation_periods  = 1
  threshold           = 1000
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}
