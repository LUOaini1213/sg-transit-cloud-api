output "api_id" { value = aws_api_gateway_rest_api.api.id }
output "stage" { value = aws_api_gateway_stage.v1.stage_name }
output "invoke_url" { value = aws_api_gateway_stage.v1.invoke_url }
output "functions" { value = { for k, f in aws_lambda_function.fn : k => f.function_name } }
output "queue_url" { value = aws_sqs_queue.pages.id }
output "dlq_url" { value = aws_sqs_queue.dlq.id }
output "lake_bucket" { value = aws_s3_bucket.lake.id }
output "replay_bucket" { value = aws_s3_bucket.replay.id }
output "tables" {
  value = {
    stops  = aws_dynamodb_table.stops.name
    speeds = aws_dynamodb_table.speeds.name
    meta   = aws_dynamodb_table.meta.name
  }
}
output "alarm_topic_arn" { value = aws_sns_topic.alarms.arn }
output "roles" { value = { for k, r in aws_iam_role.fn : k => r.name } }
