# sg-transit-cloud-api

A small serverless service on Singapore transport open data: it ingests LTA
DataMall bus stops and road speed bands, validates them, stores them in
DynamoDB, and serves a read-only REST API with nearby-stop search and a
congestion summary per snapshot.

**Everything here is deployed to [LocalStack](https://github.com/localstack/localstack)
4.14 (community edition), an AWS API emulator running in Docker, not to a
real AWS account.** Terraform uses the normal AWS provider; `tflocal` points
it at the emulator. What would change for a real account, and what the
emulator does not prove, is in [docs/REAL_AWS.md](docs/REAL_AWS.md).

## What it does

```
EventBridge schedules (off in tests)
  -> ingest Lambda: DataMall pages -> S3 raw zone, one SQS message per page
  -> SQS (KMS, DLQ after 3 receives)
  -> process Lambda: validate, upsert DynamoDB, rejected records -> S3
  -> API Gateway (REST, OpenAPI) -> api Lambda -> DynamoDB (read only)
CloudWatch: structured JSON logs, custom metrics, 6 alarms -> SNS
```

The data is real: 4,126 bus stops and seven speed-band snapshots (6,267 road
links each, Tuesday 29 Sep 2026, 16:41 to 18:28) captured from DataMall with a
key and committed as replay fixtures, so no key is needed to run it. Source,
licence, table dictionary and quality rules: [docs/DATA.md](docs/DATA.md).

| Endpoint | Returns |
|---|---|
| `GET /health` | `{"status": "ok"}` |
| `GET /v1/stops/{code}` | one bus stop |
| `GET /v1/stops/nearby?lat=&lon=&radius_m=` | stops within 10-500 m, nearest first (geohash GSI, 9-cell query, exact distance filter) |
| `GET /v1/roads/{link_id}/speeds?limit=` | latest speed-band readings for a road link |
| `GET /v1/snapshots` | ingested speed-band snapshots |
| `GET /v1/snapshots/{snapshot}/congestion` | per road category: links, mean band, share below 20 km/h |

## Infrastructure (Terraform, `infra/`)

- Three Lambda functions (Python 3.12, standard library plus the runtime's
  boto3), one IAM role each with a single inline least-privilege policy.
- S3 replay, lake (raw/ and rejected/ zones, lifecycle) and access-log buckets;
  three DynamoDB tables (on-demand, TTL on readings, point-in-time recovery);
  SQS queue with DLQ; SNS alarm topic.
- One customer managed KMS key with rotation for S3, DynamoDB, SQS, SNS and
  CloudWatch Logs.
- API Gateway REST API built from `infra/openapi.yaml`, with access logs,
  X-Ray, throttling and security headers on gateway responses.
- Alarms: DLQ not empty, process errors, ingest errors, API 5xx, rejected
  records, API p95 latency.

## Results

All numbers come from runs of the code in this repository, on LocalStack.

| | |
|---|---|
| Resources created by `terraform apply` | 164, in 97-99 s on a GitHub runner |
| Records through the pipeline | 47,995 (4,126 stops + 7 x 6,267 speed readings), queue drained in 66 s on a GitHub runner (164-482 s on a loaded laptop running WSL) |
| Records rejected by validation | 14 (2 links per snapshot with no reading); all 4,126 stops pass |
| Unit tests | 50 (moto for S3, SQS, DynamoDB, CloudWatch) |
| Integration tests against the deployed stack | 14 |
| Planted bugs caught (mutation check) | 25 of 25; the first round caught 22, and the three survivors led to stronger tests |
| Semgrep findings | 4 before, 0 after |
| Trivy findings | 29 before (9 high, 6 medium, 14 low), 0 after, 3 accepted with a reason |
| ZAP baseline + OpenAPI active scan | 0 fail, 0 warn after; one real 502 found and fixed (below) |
| Poison message to DLQ, alarm, SNS notification | about 2 minutes with test timeouts of 30 s |

API latency from the client on LocalStack, 200 sequential requests per
endpoint after 20 warm-up requests (GitHub runner, from `reports/latency.json`
in the CI artifacts):

LATENCY_TABLE

These are emulator numbers. They are useful for catching regressions between
commits and say nothing about latency on AWS.

### Things the checks found

- **Redelivered batches.** On a loaded machine some process batches ran into
  the Lambda timeout and SQS redelivered them whole, including pages already
  written. Writes use deterministic keys, so the tables stayed exact, but the
  `RecordsRejected` metric read 20 for 14 rejected records. The handler now
  checks the remaining time before each record and hands the rest back as
  partial batch failures; the latest run read 16 for 14. Metrics count
  deliveries, so exact counts come from the per-page summaries in DynamoDB.
- **A 502 in the active scan.** ZAP got a 502 from `GET /v1/snapshots`. The
  log showed a cold start of a new LocalStack Lambda container reaching the
  API function's 10 s timeout. The timeout is now 29 s (the API Gateway
  limit) and ZAP no longer downgrades server-error alerts.
- **Mutation survivors.** Three planted bugs passed the first test suite: a
  disabled raw-zone check (the test message pointed at a missing object, so it
  failed for the wrong reason) and two disabled IAM rules (other rules happened
  to flag the same policies). The tests now assert the specific reason.
- **LocalStack gaps worked around**, each commented in the code: AWS provider
  pinned to 6.57.1 (6.58 waits for a REST API status LocalStack does not
  return); alarms created without tags (tagged alarms break LocalStack's
  `DescribeAlarms` serializer); no redrive-allow policy on the DLQ (its
  removal never completes, so destroy hangs); DynamoDB run in memory
  (`DYNAMODB_IN_MEMORY=1`; on disk, each 25-item batch write took about 5 s
  on this laptop).

Details of every scanner finding and how it was fixed or accepted:
[docs/SECURITY.md](docs/SECURITY.md).

## CI (`.github/workflows/ci.yml`)

| Job | Steps |
|---|---|
| unit | pytest; mutation check (all 25 planted bugs must be caught); control run with an equivalent mutant that must be reported, so the check itself can fail |
| sast | Semgrep with `p/python`, `p/terraform`, `p/secrets`; fails on any finding |
| trivy | Trivy for dependencies, IaC misconfiguration and secrets; fails on any finding |
| deploy-localstack | LocalStack as a service container; `tflocal apply`; IAM check on the deployed roles; integration tests; latency run; ZAP baseline and OpenAPI scan; `tflocal destroy` and a check that nothing is left |

Actions are pinned to commit SHAs; Terraform and Trivy binaries are checked
against their published SHA-256 sums.

## Run it

Linux or WSL with Docker, Terraform 1.15 and Python 3.12:

```bash
pip install -r requirements.txt terraform-local awscli-local
python -m pytest -q                 # unit tests, no Docker needed
python scripts/mutate.py            # planted-bug check
scripts/local.sh up                 # LocalStack 4.14 in Docker
scripts/local.sh deploy             # tflocal apply, outputs to /tmp/sgtc-outputs.json
scripts/local.sh test               # unit, IAM check, integration
scripts/local.sh down               # destroy and remove the containers
```

Ingest and query by hand:

```bash
awslocal lambda invoke --function-name sgtc-dev-ingest \
  --payload '{"dataset":"TrafficSpeedBands","snapshot":"20260929_1753"}' /dev/stdout
API=$(cd infra && terraform output -raw api_id)
curl "http://localhost:4566/_aws/execute-api/$API/dev/v1/snapshots/20260929_1753/congestion"
```

## Layout

```
src/transit/      ingest.py, process.py, api.py, quality.py, sources.py, geohash.py
infra/            Terraform and the OpenAPI definition
fixtures/         captured DataMall pages (replay source)
tests/unit/       moto-based tests
tests/integration tests against the deployed stack
scripts/          check_iam, mutate, bench_api, render_openapi, zap_summary, build_fixtures, local.sh
zap/rules.tsv     ZAP rule overrides, each with its reason
docs/             DATA.md, SECURITY.md, REAL_AWS.md
```

## Licence

Code: MIT. Data: contains information from LTA DataMall, made available under
the terms of the Singapore Open Data Licence.
