# sg-transit-cloud-api

[![ci](https://github.com/LUOaini1213/sg-transit-cloud-api/actions/workflows/ci.yml/badge.svg)](https://github.com/LUOaini1213/sg-transit-cloud-api/actions/workflows/ci.yml)

A small serverless service on Singapore transport open data, deployed with
Terraform to **[LocalStack](https://github.com/localstack/localstack) 4.14
(community edition), an AWS emulator in Docker, not to a real AWS account**.
It replays LTA DataMall bus stops and road speed bands through S3, SQS, Lambda
and DynamoDB and serves a read-only REST API (nearby-stop search, congestion
per snapshot). In CI, `terraform apply` creates 164 resources, 47,995 replayed
records go through the pipeline (14 rejected by validation), 14 integration
tests pass against the deployed API, and the ZAP active scan found one real
502 (a cold-start timeout), since fixed. Limits: LocalStack community does not
enforce IAM or apply KMS and bucket policies as AWS does, its latency says
nothing about AWS, and the speed bands cover a ~3 km central-area box only. Numbers in
[Results](#results), evidence in [results/](results/ci-2026-10-16/), what
would change on a real account in [docs/REAL_AWS.md](docs/REAL_AWS.md).

## What it does

```
EventBridge schedules (off in tests)
  -> ingest Lambda: DataMall pages -> S3 raw zone, one SQS message per page
  -> SQS (KMS, DLQ after 3 receives)
  -> process Lambda: validate, upsert DynamoDB, rejected records -> S3
  -> API Gateway (REST, OpenAPI) -> api Lambda -> DynamoDB (read only)
CloudWatch: structured JSON logs, custom metrics, 6 alarms -> SNS
```

The data is real but a geographic subset: 4,126 bus stops (stops between
1.24-1.47 N and 103.75-104.05 E) and seven speed-band snapshots of the 6,267
road links that start in a roughly 3 km x 3 km central-area box
(1.28-1.31 N, 103.83-103.86 E), captured on Tuesday 29 Sep 2026 (16:41 to
18:28) from DataMall with a key and committed as replay fixtures, so no key
is needed to run it. Congestion figures therefore describe the central area,
not all of Singapore. Source, licence, table dictionary and quality rules:
[docs/DATA.md](docs/DATA.md).

| Endpoint | Returns |
|---|---|
| `GET /health` | `{"status": "ok"}` |
| `GET /v1/stops/{code}` | one bus stop |
| `GET /v1/stops/nearby?lat=&lon=&radius_m=` | stops within 10-500 m, nearest first (geohash GSI, 9-cell query, exact distance filter) |
| `GET /v1/roads/{link_id}/speeds?limit=` | latest speed-band readings for a road link |
| `GET /v1/snapshots` | ingested speed-band snapshots, each with `status` (`complete`, `partial` or `unknown`) and pages processed out of expected |
| `GET /v1/snapshots/{snapshot}/congestion` | per road category over the central-area links: links, mean band, share below 20 km/h; plus the same `status` |

A snapshot is `complete` when every page that ingest queued has a summary and
the record totals match, `partial` while pages are still queued or after one
was parked in the DLQ (the congestion figures then cover only the processed
pages), and `unknown` for summaries written before ingest recorded the
expected page count.

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
The CI numbers are committed in [results/ci-2026-10-16/](results/ci-2026-10-16/)
(extracted from three CI runs by `scripts/ci_evidence.py`), and
`python scripts/check_readme.py --check`, run in CI, fails if this section or
the first screen drifts from that evidence, the fixtures, the test suite or
[docs/SECURITY.md](docs/SECURITY.md).

| | |
|---|---|
| Resources created by `terraform apply` | 164, in 95-99 s on a GitHub runner (3 runs) |
| Records through the pipeline | 47,995 (4,126 stops + 7 x 6,267 speed readings), queue drained in 48-66 s on a GitHub runner (3 runs) |
| Records rejected by validation | 14 (2 links per snapshot with no reading); all 4,126 stops pass |
| Unit tests | 58 (moto for S3, SQS, DynamoDB, CloudWatch) |
| Integration tests against the deployed stack | 14 |
| Planted bugs caught (mutation check) | 28 of 28, required on every CI run; a control mutant must survive, so the check can fail |
| Semgrep findings | 4 before, 0 after |
| Trivy findings | 29 before (9 high, 6 medium, 14 low), 0 after, 3 accepted with a reason |
| ZAP baseline + OpenAPI active scan | 0 fail, 0 warn in the cited runs, and CI fails on any Medium or High alert not ignored with a reason; one real 502 found and fixed (below) |
| Poison message to DLQ, alarm, SNS notification | checked end to end by an integration test, with test timeouts of 30 s |

API latency from the client on LocalStack, 200 sequential requests per
endpoint after 20 warm-up requests (GitHub runner,
[results/ci-2026-10-16/latency.json](results/ci-2026-10-16/latency.json),
copied from the run's CI artifact):

| Endpoint | p50 | p95 | p99 | errors |
|---|---|---|---|---|
| `/health` | 13.2 ms | 15.1 ms | 17.5 ms | 0/200 |
| `/v1/stops/{code}` | 17.4 ms | 19.7 ms | 21.1 ms | 0/200 |
| `/v1/stops/nearby` (300 m) | 54.6 ms | 63.1 ms | 71.2 ms | 0/200 |
| `/v1/roads/{id}/speeds` | 17.8 ms | 20.3 ms | 22.2 ms | 0/200 |
| `/v1/snapshots/{id}/congestion` | 22.0 ms | 26.4 ms | 29.9 ms | 0/200 |

(CI run 38839029755, commit b0c59ba.)

These are emulator numbers. They are useful for catching regressions between
commits and say nothing about latency on AWS. They were measured before the
snapshot `status` field was added. Congestion now also follows query
pagination, so its row needs a fresh CI run before it describes the current
code; `/v1/snapshots` (not timed here) now reads each snapshot's page
summaries as well.

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
| unit | pytest; README numbers against the committed evidence (`scripts/check_readme.py --check`); mutation check (every planted bug in `scripts/mutate.py` must be caught); control run with an equivalent mutant that must be reported, so the check itself can fail |
| unit-windows | pytest and the README check on Windows |
| sast | Semgrep with `p/python`, `p/terraform`, `p/secrets`; fails on any finding |
| trivy | Trivy for dependencies, IaC misconfiguration and secrets; fails on any finding |
| deploy-localstack | LocalStack as a service container; `tflocal apply`; IAM check on the deployed roles; integration tests; latency run; ZAP baseline and OpenAPI scan (both fail on WARN, and `zap_summary.py` fails on any Medium or High alert not ignored in `zap/rules.tsv`); `tflocal destroy` and a check that nothing is left |

Actions are pinned to commit SHAs; Terraform and Trivy binaries are checked
against their published SHA-256 sums.

## Run it

Unit tests, the planted-bug check and the README check need only Python
3.12, no Docker (CI runs the unit tests and the README check on Ubuntu and
Windows, the planted-bug check on Ubuntu):

```bash
pip install -r requirements.txt
python -m pytest -q                 # unit tests
python scripts/mutate.py            # planted-bug check
python scripts/check_readme.py --check
```

The deployed stack needs Linux or WSL with Docker, Terraform 1.15.9 and the
pinned deploy tools CI uses:

```bash
pip install -r requirements.txt -r requirements-deploy.txt
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
scripts/          check_iam, mutate, bench_api, render_openapi, zap_summary, build_fixtures,
                  check_readme, ci_evidence, local.sh
results/          CI numbers quoted in this README, kept after the logs expire
zap/rules.tsv     ZAP rule overrides, each with its reason
docs/             DATA.md, SECURITY.md, REAL_AWS.md
```

## Licence

Code: MIT. Data: contains information from LTA DataMall, made available under
the terms of the Singapore Open Data Licence.
