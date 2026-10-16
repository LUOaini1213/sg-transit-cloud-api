# Security checks and findings

Three scanners run in CI on every push. The "before" column is the first
working version (commit `3d700dd`), scanned with the same tools and rule
packs; "after" is the current tree.

| Scanner | What it looks at | Before | After |
|---|---|---|---|
| Semgrep 1.181 (`p/python`, `p/terraform`, `p/secrets`) | Python source and Terraform | 4 (3 warning, 1 info) | 0 |
| Trivy 0.74 (`vuln`, `misconfig`, `secret`) | Terraform, pinned Python dependencies, secrets | 29 misconfigurations (9 high, 6 medium, 14 low) | 0, with 3 accepted in place (below) |
| OWASP ZAP 2.17 baseline + OpenAPI scan | the deployed API on LocalStack | 2 low, 4 informational alert types | see the README for the current run |

## What was fixed

Semgrep:
- `aws-dynamodb-table-unencrypted` x3: the three tables now use the stack's
  customer managed KMS key.
- `aws-lambda-x-ray-tracing-not-active`: tracing is `Active` on all three
  functions.

Trivy (IDs are Trivy's AWS check IDs):
- AWS-0086/0087/0091/0093/0094 (S3 public access block, 4 high + 1 low):
  the first version created the blocks with `for_each` over a map of bucket
  IDs, which Trivy could not link to the buckets. Each bucket now has its own
  block, so the control can be checked statically.
- AWS-0095 (SNS unencrypted), AWS-0096 x2 (SQS unencrypted), AWS-0132 x2
  (S3 without a customer managed key), AWS-0025 x3 (DynamoDB without a
  customer managed key), AWS-0017 x3 (log groups without a customer managed
  key): one KMS key with rotation, used by all of them. The Lambda roles may
  use it only through S3, SQS and DynamoDB (`kms:ViaService`).
- AWS-0024 x3: DynamoDB point-in-time recovery on.
- AWS-0090 x2: S3 versioning on, with noncurrent versions expiring after 30
  days.
- AWS-0089 x2: S3 access logging to a dedicated log bucket.
- AWS-0001: API Gateway access logs to a KMS-encrypted log group (with the
  account-level CloudWatch role API Gateway needs).
- AWS-0003, AWS-0066 x3: X-Ray tracing on the stage and the functions.

Added without a scanner asking:
- TLS-only bucket policies on all three buckets.
- Gateway responses (`DEFAULT_4XX`, `DEFAULT_5XX`) carry the same security
  headers as the Lambda responses, so a 403 for an unknown route is not a bare
  response.

## Accepted findings (suppressed in place, with a comment)

| ID | Resource | Why |
|---|---|---|
| AWS-0089 | the access-log bucket | a log bucket cannot log to itself |
| AWS-0132 | the access-log bucket | S3 server access logs cannot be delivered to an SSE-KMS bucket; it uses SSE-S3 |
| AWS-0190 | API stage method settings | response caching is off: the data changes every 15 minutes and a cache cluster is billed per hour |

## ZAP

The API returns, on every response: `Content-Type: application/json`,
`Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, a
`default-src 'none'` CSP with `frame-ancestors 'none'`, HSTS,
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
`Cross-Origin-Resource-Policy: same-origin` and a restrictive
`Permissions-Policy`. These were in the first version, so ZAP's first run had
little to report on the API itself:

- 10036, server version in the `Server` header (low): the value is
  `TwistedWeb/25.5.0`, LocalStack's own web server. It is set to IGNORE in
  `zap/rules.tsv` with that reason.
- 90004, Cross-Origin-Resource-Policy missing (low): only on
  `http://localhost:4566/`, the LocalStack root, not on the API. The
  emulator's root paths are out of scope in `zap/rules.tsv`.
- Informational: cacheable content on the same LocalStack root paths, and
  "client error response" for the 400/404 answers the API scan provokes on
  purpose (reported as informational; server errors are not downgraded).

One finding from the active scan was real. On 2026-10-16 the API scan got a
`502` from `GET /v1/snapshots` (ZAP 100000, "server error response", low).
The API Lambda log showed two invocations ending at exactly 10,000 ms: a
cold start of a fresh LocalStack Lambda container ran into the function's
10 s timeout, and API Gateway turned that into a 502. The integration tests
had hit the same thing on their first request. The API function timeout is
now 29 s (the API Gateway integration limit), the integration tests warm the
function once before timing requests, and rule 100000 is no longer
downgraded in `zap/rules.tsv`, so any 5xx during a scan fails the job.

The API scan (`zap-api-scan.py` with the OpenAPI file rendered from
`infra/openapi.yaml`) runs ZAP's active rules (injection, path traversal,
SSTI and others) against every documented operation. Any WARN or FAIL that
is not listed in `zap/rules.tsv` fails the job.

## Other controls

- **IAM.** One role per function, one inline policy each, specific ARNs.
  `scripts/check_iam.py` reads the deployed policies back and fails on any
  wildcard action, `Resource "*"` outside three actions that have no resource
  ARN, a missing `kms:ViaService` or namespace condition, any action outside
  the expected set, or any write on the API role. Its unit tests plant nine
  kinds of over-permission and check that each is reported by its own rule.
- **Input validation.** Every path and query parameter is matched against a
  pattern or a numeric range before it reaches DynamoDB; errors are JSON
  with a message, and unexpected exceptions return only a request ID.
- **Queue messages** are pointers into the raw zone. The process Lambda
  refuses a key outside `raw/<dataset>/` or containing `..`, even if the
  object exists.
- **CORS** is off unless `cors_origin` is set, and then only that one origin
  is echoed.
- **Supply chain.** GitHub Actions are pinned to commit SHAs; Terraform and
  Trivy are downloaded from their release pages and checked against the
  published SHA-256 sums; Python dependencies are pinned.

## Limits

LocalStack community does not enforce IAM, KMS or bucket policies, so these
checks show what is declared, not that AWS would enforce it. See
[REAL_AWS.md](REAL_AWS.md).
