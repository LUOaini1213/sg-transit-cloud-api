# Deploying to a real AWS account

Everything in this repository has only been deployed to LocalStack 4.14
(community edition), an AWS API emulator running in Docker. It has not been
deployed to a real AWS account. This page lists what would change, and what
LocalStack does not prove.

## What changes

1. **Provider.** Run `terraform` instead of `tflocal`. tflocal only writes a
   temporary provider override that points every service at
   `http://localhost:4566`; the configuration itself is plain AWS.
2. **Provider version.** `infra/versions.tf` pins `hashicorp/aws` to 6.57.1
   because 6.58.0 waits for a REST API status that LocalStack 4.14 does not
   return. On AWS, move to the current 6.x release.
3. **State.** Add an S3 backend with a lock (S3 native locking or a DynamoDB
   lock table), encrypted with a KMS key, in a separate state account or
   bucket. Today state is local and thrown away after each CI run.
4. **Credentials in CI.** Use GitHub OIDC to assume a deploy role
   (`aws-actions/configure-aws-credentials`, pinned to a commit SHA) instead of
   long-lived keys. The deploy role needs IAM, Lambda, API Gateway, DynamoDB,
   S3, SQS, SNS, KMS, Events and Logs permissions scoped to the `sgtc-*`
   names.
5. **Alarm tags.** Alarms use an untagged provider alias only to work around
   a LocalStack serializer bug; on AWS they can use the default provider.
6. **Data source.** Set `SOURCE_MODE=live` and wire the live client
   (`transit.sources.DataMallHttpSource`, already unit-tested against a fake
   server) to a DataMall AccountKey kept in Secrets Manager. That needs a
   secret, `secretsmanager:GetSecretValue` on that one secret ARN for the
   ingest role, and the key itself from a DataMall account. The replay bucket
   and fixture upload would then be removed.
7. **Schedules.** `schedule_enabled = true` turns on the two EventBridge
   rules (speed bands every 15 minutes, bus stops daily at 03:00 SGT).
8. **Timeouts.** Use the defaults (`process_timeout_s = 60`,
   `queue_visibility_timeout_s = 360`, six times the consumer timeout as AWS
   recommends). `test.tfvars` shortens both so the DLQ path runs in about a
   minute in CI.
9. **Alarm delivery.** Subscribe an on-call email or chat webhook to the
   `sgtc-<env>-alarms` SNS topic.
10. **Edge.** Put AWS WAF (managed rule groups plus a rate rule) in front of
    the API stage, and decide on API keys or a usage plan if the API is not
    meant to be public.

## What LocalStack did not prove

- **IAM is not enforced** in the community edition. A policy that is too
  narrow would still work there. `scripts/check_iam.py` checks the deployed
  policies for breadth, but only a real account shows that they are also
  sufficient (for example the KMS `kms:ViaService` conditions).
- **KMS, S3 bucket policies (TLS-only), point-in-time recovery, X-Ray tracing
  and access logging** are accepted and read back by the integration tests,
  but LocalStack does not apply them the way AWS does.
- **Latency and throughput** numbers are LocalStack numbers. Lambda cold
  starts, DynamoDB latency and API Gateway overhead on AWS are different.
- **Alarm evaluation.** The DLQ alarm fired and reached SNS on LocalStack, but
  LocalStack does not evaluate alarms on extended statistics (the p95 latency
  alarm), so that one is only created, never exercised.
- **Cost.** Nothing here was billed. On-demand DynamoDB, one KMS key, Lambda,
  API Gateway REST and CloudWatch Logs would be the main line items.
