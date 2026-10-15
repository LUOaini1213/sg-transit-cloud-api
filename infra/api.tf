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
    redeploy = sha1(aws_api_gateway_rest_api.api.body)
  }
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "v1" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  deployment_id = aws_api_gateway_deployment.api.id
  stage_name    = var.environment
}

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
