resource "aws_api_gateway_rest_api" "api" {
  name = "${local.name}-api"
  body = templatefile("${path.module}/openapi.yaml", {
    api_uri = aws_lambda_function.fn["api"].invoke_arn
  })
  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_api_gateway_deployment" "api" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  triggers = {
    redeploy = sha1(join(",", [aws_api_gateway_rest_api.api.body,
    jsonencode(aws_api_gateway_gateway_response.defaults)]))
  }
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/aws/apigateway/${local.name}-api/access"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.data.arn
}

resource "aws_api_gateway_stage" "v1" {
  rest_api_id          = aws_api_gateway_rest_api.api.id
  deployment_id        = aws_api_gateway_deployment.api.id
  stage_name           = var.environment
  xray_tracing_enabled = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId     = "$context.requestId", ip = "$context.identity.sourceIp",
      method        = "$context.httpMethod", path = "$context.resourcePath",
      status        = "$context.status", latencyMs = "$context.responseLatency",
      integrationMs = "$context.integrationLatency", ua = "$context.identity.userAgent"
    })
  }
  depends_on = [aws_api_gateway_account.this, aws_iam_role_policy_attachment.apigw_logs]
}

# API Gateway needs an account-level role to write access logs to CloudWatch.
data "aws_iam_policy_document" "apigw_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["apigateway.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "apigw_logs" {
  name               = "${local.name}-apigw-logs"
  assume_role_policy = data.aws_iam_policy_document.apigw_trust.json
}

resource "aws_iam_role_policy_attachment" "apigw_logs" {
  role       = aws_iam_role.apigw_logs.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

resource "aws_api_gateway_account" "this" {
  cloudwatch_role_arn = aws_iam_role.apigw_logs.arn
}

# Response caching (AVD-AWS-0190) is left off: the data changes every 15
# minutes and a cache cluster is billed per hour. See docs/SECURITY.md.
#trivy:ignore:AVD-AWS-0190
resource "aws_api_gateway_method_settings" "all" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  stage_name  = aws_api_gateway_stage.v1.stage_name
  method_path = "*/*"
  settings {
    metrics_enabled        = true
    throttling_rate_limit  = 50
    throttling_burst_limit = 100
  }
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "apigateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn["api"].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/GET/*"
}

# Responses that API Gateway generates itself (unknown route, throttling,
# integration failure) carry the same security headers as the Lambda's.
locals {
  gateway_headers = {
    "gatewayresponse.header.X-Content-Type-Options"       = "'nosniff'"
    "gatewayresponse.header.Cache-Control"                = "'no-store'"
    "gatewayresponse.header.Content-Security-Policy"      = "'default-src ''none''; frame-ancestors ''none'''"
    "gatewayresponse.header.Strict-Transport-Security"    = "'max-age=31536000; includeSubDomains'"
    "gatewayresponse.header.Cross-Origin-Resource-Policy" = "'same-origin'"
  }
}

resource "aws_api_gateway_gateway_response" "defaults" {
  for_each            = toset(["DEFAULT_4XX", "DEFAULT_5XX"])
  rest_api_id         = aws_api_gateway_rest_api.api.id
  response_type       = each.key
  response_parameters = local.gateway_headers
  response_templates = {
    "application/json" = "{\"error\": $context.error.messageString}"
  }
}
