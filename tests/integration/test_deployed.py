import importlib.util
import json
import time

import boto3
import pytest
import requests

from conftest import ROOT, fixture_no_reading, invoke, queue_depth, wait_until

spec = importlib.util.spec_from_file_location("check_iam", ROOT / "scripts" / "check_iam.py")
check_iam = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_iam)


def count(table):
    ddb, n, kw = boto3.client("dynamodb"), 0, {}
    while True:
        r = ddb.scan(TableName=table, Select="COUNT", **kw)
        n += r["Count"]
        if "LastEvaluatedKey" not in r:
            return n
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def test_pipeline_counts(out, ingested, manifest):
    assert count(out["tables"]["stops"]) == manifest["bus_stops"]
    expected = sum(s["records"] - fixture_no_reading(k) for k, s in manifest["snapshots"].items())
    assert count(out["tables"]["speeds"]) == expected
    assert queue_depth(out["dlq_url"]) == 0
    print(f"\ningested {sum(r['records'] for r in ingested['runs'])} records in {ingested['seconds']:.1f}s")


def test_raw_and_rejected_zones(out, ingested, manifest):
    s3 = boto3.client("s3")
    keys = [o["Key"] for p in s3.get_paginator("list_objects_v2").paginate(Bucket=out["lake_bucket"])
            for o in p.get("Contents", [])]
    raw = [k for k in keys if k.startswith("raw/")]
    assert len(raw) == sum(r["pages"] for r in ingested["runs"])
    rejected = [k for k in keys if k.startswith("rejected/")]
    n = sum(len(json.loads(s3.get_object(Bucket=out["lake_bucket"], Key=k)["Body"].read())) for k in rejected)
    assert n == sum(fixture_no_reading(s) for s in manifest["snapshots"])


def test_api_over_http(base, ingested, manifest):
    r = requests.get(f"{base}/health", timeout=10)
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    for h, v in {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}.items():
        assert r.headers[h] == v
    r = requests.get(f"{base}/v1/stops/01012", timeout=10)
    assert r.status_code == 200 and r.json()["road"] == "Victoria St"
    r = requests.get(f"{base}/v1/stops/nearby", params={"lat": 1.2966, "lon": 103.8525, "radius_m": 200}, timeout=10)
    assert r.status_code == 200 and r.json()["stops"][0]["code"] == "01012"
    snaps = requests.get(f"{base}/v1/snapshots", timeout=10).json()["snapshots"]
    assert [s["snapshot"] for s in snaps] == sorted(manifest["snapshots"])
    for s in snaps:
        c = requests.get(f"{base}/v1/snapshots/{s['snapshot']}/congestion", timeout=10).json()
        assert s["status"] == c["status"] == "complete"
        assert s["pending_pages"] == c["pending_pages"] == 0
        assert s["pages"] == c["pages"] == c["expected_pages"]
        assert c["expected_records"] == manifest["snapshots"][s["snapshot"]]["records"]
        assert c["total"] == manifest["snapshots"][s["snapshot"]]["records"]
        assert c["rejected"] == fixture_no_reading(s["snapshot"])
    link = requests.get(f"{base}/v1/snapshots/20260929_1753/congestion", timeout=10).json()
    assert link["categories"]


def test_api_errors_over_http(base):
    cases = [("/v1/stops/abc", 400), ("/v1/stops/00000", 404), ("/v1/stops/nearby?lat=1.3", 400),
             ("/v1/stops/nearby?lat=1.3&lon=103.8&radius_m=5000", 400), ("/v1/roads/x/speeds", 400),
             ("/v1/snapshots/latest/congestion", 400)]
    for path, status in cases:
        r = requests.get(base + path, timeout=10)
        assert r.status_code == status, (path, r.status_code, r.text)
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert "Traceback" not in r.text
    # unknown resources and methods are stopped by API Gateway, before Lambda
    assert requests.get(base + "/v1/admin", timeout=10).status_code in (403, 404)
    assert requests.post(base + "/health", timeout=10).status_code in (403, 404, 405)


def test_gateway_responses_declared(out):
    """DEFAULT_4XX/5XX carry the security headers on AWS. LocalStack 4.14
    stores them but does not apply them to its own 403 for unknown routes,
    so this checks the configuration, not the response."""
    items = boto3.client("apigateway").get_gateway_responses(restApiId=out["api_id"])["items"]
    by_type = {i["responseType"]: i.get("responseParameters", {}) for i in items}
    for t in ("DEFAULT_4XX", "DEFAULT_5XX"):
        assert by_type[t]["gatewayresponse.header.X-Content-Type-Options"] == "'nosniff'"
        assert by_type[t]["gatewayresponse.header.Cache-Control"] == "'no-store'"


def test_reingest_is_idempotent(out, ingested):
    before = count(out["tables"]["speeds"])
    invoke(out["functions"]["ingest"], {"dataset": "TrafficSpeedBands", "snapshot": "20260929_1753"})
    wait_until(lambda: queue_depth(out["queue_url"]) == 0, 300, 3, "queue to drain")
    assert count(out["tables"]["speeds"]) == before


def test_custom_metrics(out, ingested, manifest):
    cw = boto3.client("cloudwatch")
    expected = sum(fixture_no_reading(s) for s in manifest["snapshots"])

    def rejected_sum():
        r = cw.get_metric_statistics(Namespace="SgTransit", MetricName="RecordsRejected",
                                     Dimensions=[{"Name": "Dataset", "Value": "TrafficSpeedBands"}],
                                     StartTime=time.time() - 3600, EndTime=time.time() + 60,
                                     Period=3600, Statistics=["Sum"])
        return sum(p["Sum"] for p in r["Datapoints"])
    # Metrics count deliveries, not unique pages: SQS is at-least-once and the
    # idempotency test re-ingests a snapshot. Exact counts come from the meta
    # table (see the congestion checks); here the metric must cover them.
    got = rejected_sum()
    assert got >= expected, got
    print(f"\nRecordsRejected metric sum {got:.0f} for {expected} unique rejected records")


def test_structured_logs(out, ingested):
    logs = boto3.client("logs")
    ev = logs.filter_log_events(logGroupName=f"/aws/lambda/{out['functions']['process']}",
                                filterPattern="page_processed")["events"]
    parsed = [json.loads(e["message"][e["message"].index("{"):]) for e in ev if "{" in e["message"]]
    assert parsed and all({"dataset", "snapshot", "page", "accepted", "rejected"} <= p.keys() for p in parsed)


def test_poison_message_goes_to_dlq_and_alarms(out, ingested):
    sqs, sns, cw = boto3.client("sqs"), boto3.client("sns"), boto3.client("cloudwatch")
    probe = sqs.create_queue(QueueName="itest-alarm-probe")["QueueUrl"]
    probe_arn = sqs.get_queue_attributes(QueueUrl=probe, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    sub = sns.subscribe(TopicArn=out["alarm_topic_arn"], Protocol="sqs", Endpoint=probe_arn)["SubscriptionArn"]
    try:
        sqs.send_message(QueueUrl=out["queue_url"], MessageBody=json.dumps(
            {"dataset": "TrafficSpeedBands", "snapshot": "x", "captured_at": "x",
             "raw_key": "raw/TrafficSpeedBands/does-not-exist.json", "page": 0}))
        msgs = wait_until(lambda: sqs.receive_message(QueueUrl=out["dlq_url"], MaxNumberOfMessages=1,
                                                      VisibilityTimeout=0, AttributeNames=["All"])
                          .get("Messages"), 420, 5, "poison message in DLQ")
        assert int(msgs[0]["Attributes"]["ApproximateReceiveCount"]) >= 1
        alarm = f"sgtc-dev-dlq-not-empty"

        def state():
            a = cw.describe_alarms(AlarmNames=[alarm])["MetricAlarms"]
            return a and a[0]["StateValue"] == "ALARM"
        wait_until(state, 300, 10, "DLQ alarm to fire")

        def notified():
            for m in sqs.receive_message(QueueUrl=probe, MaxNumberOfMessages=10, WaitTimeSeconds=1).get("Messages", []):
                body = json.loads(json.loads(m["Body"])["Message"])
                if body.get("AlarmName") == alarm and body.get("NewStateValue") == "ALARM":
                    return True
            return False
        wait_until(notified, 120, 2, "SNS alarm notification")
    finally:
        sns.unsubscribe(SubscriptionArn=sub)
        sqs.delete_queue(QueueUrl=probe)


def test_iam_least_privilege(out):
    docs = check_iam.fetch_policies({"roles": {"value": out["roles"]}}, boto3.client("iam"))
    for fn in ("ingest", "process", "api"):
        assert check_iam.check_policy(fn, docs[fn]) == [], fn
        assert not docs[fn].get("_attached")


def test_encryption_and_schedules_as_declared(out):
    ddb, sqs, s3, ev = boto3.client("dynamodb"), boto3.client("sqs"), boto3.client("s3"), boto3.client("events")
    for t in out["tables"].values():
        sse = ddb.describe_table(TableName=t)["Table"]["SSEDescription"]
        assert sse["SSEType"] == "KMS" and sse["KMSMasterKeyArn"]
    for q in (out["queue_url"], out["dlq_url"]):
        assert sqs.get_queue_attributes(QueueUrl=q, AttributeNames=["KmsMasterKeyId"])["Attributes"]["KmsMasterKeyId"]
    for b in (out["lake_bucket"], out["replay_bucket"]):
        rule = s3.get_bucket_encryption(Bucket=b)["ServerSideEncryptionConfiguration"]["Rules"][0]
        assert rule["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms"
        assert s3.get_bucket_versioning(Bucket=b)["Status"] == "Enabled"
    rules = ev.list_rules(NamePrefix="sgtc-dev-ingest")["Rules"]
    assert {r["Name"] for r in rules} == {"sgtc-dev-ingest-speeds", "sgtc-dev-ingest-stops"}
    assert {r["State"] for r in rules} == {"DISABLED"}


@pytest.mark.parametrize("fn", ["ingest", "process", "api"])
def test_functions_traced_and_logged(out, fn):
    cfg = boto3.client("lambda").get_function_configuration(FunctionName=out["functions"][fn])
    assert cfg["TracingConfig"]["Mode"] == "Active"
    assert cfg["LoggingConfig"]["LogGroup"] == f"/aws/lambda/{out['functions'][fn]}"
    assert cfg["Runtime"] == "python3.12"
