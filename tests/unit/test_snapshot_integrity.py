"""Snapshot completeness across asynchronous delivery and DynamoDB query pages."""
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from conftest import drain
from transit import common, ingest, process
from test_api import call

SNAPSHOT = "20260101_0800"
CAPTURED = "2026-01-01T08:00:00+08:00"
PROGRESS = ("status", "pages", "pending_pages", "expected_pages", "expected_records")


def summary(page, expected_pages=2, snapshot=SNAPSHOT):
    return {"pk": f"TrafficSpeedBands#{snapshot}", "sk": f"page#{page:03d}",
            "captured_at": CAPTURED, "total": 10, "accepted": 10, "rejected": 0,
            "bands": {"1": {"2": 10}}, "expected_pages": expected_pages,
            "expected_records": expected_pages * 10, "write_state": "complete"}


def index(meta, snapshot=SNAPSHOT):
    meta.put_item(Item={"pk": "snapshots#TrafficSpeedBands", "sk": snapshot,
                        "captured_at": CAPTURED})


def progress_from_both(snapshot):
    status, congestion, _ = call("/v1/snapshots/{snapshot}/congestion", {"snapshot": snapshot})
    assert status == 200
    status, listing, _ = call("/v1/snapshots")
    assert status == 200
    entry = next(item for item in listing["snapshots"] if item["snapshot"] == snapshot)
    assert {k: entry[k] for k in PROGRESS} == {k: congestion[k] for k in PROGRESS}
    return congestion


def test_snapshot_index_crosses_actual_query_size_limit(aws):
    meta = boto3.resource("dynamodb").Table("t-meta")
    snapshots = [f"20260101_08{i:02d}" for i in range(6)]
    for snap in snapshots:
        # Each row is below 400 KB, while the partition exceeds Query's 1 MB.
        meta.put_item(Item={"pk": "snapshots#TrafficSpeedBands", "sk": snap,
                            "captured_at": CAPTURED, "extra_metadata": "x" * 200_000})
        meta.put_item(Item=summary(0, 1, snap))
    first = meta.query(KeyConditionExpression=Key("pk").eq("snapshots#TrafficSpeedBands"))
    assert first.get("LastEvaluatedKey") and len(first["Items"]) < len(snapshots)
    status, body, _ = call("/v1/snapshots")
    assert status == 200
    assert [item["snapshot"] for item in body["snapshots"]] == snapshots
    assert all(item["status"] == "complete" for item in body["snapshots"])


def test_summary_crosses_actual_query_size_limit(aws):
    meta = boto3.resource("dynamodb").Table("t-meta")
    index(meta)
    for page in range(6):
        meta.put_item(Item={**summary(page, 6), "extra_metadata": "x" * 200_000})
    first = meta.query(KeyConditionExpression=Key("pk").eq(f"TrafficSpeedBands#{SNAPSHOT}"))
    assert first.get("LastEvaluatedKey") and len(first["Items"]) < 6
    body = progress_from_both(SNAPSHOT)
    assert (body["pages"], body["total"], body["accepted"], body["rejected"]) == (6, 60, 60, 0)
    assert body["status"] == "complete"
    assert body["categories"][0]["links"] == 60
    assert body["categories"][0]["share_below_20kmh"] == 1


def test_public_snapshot_partial_failed_retried_and_complete(replay, manifest):
    snap = "20260929_1735"
    run = ingest.handler({"dataset": "TrafficSpeedBands", "snapshot": snap}, None)
    messages = sorted(drain(replay["sqs"], replay["queue_url"]),
                      key=lambda m: json.loads(m["Body"])["page"])
    records = [{"messageId": m["MessageId"], "body": m["Body"]} for m in messages]
    assert run["pages"] == len(records) == 13
    expected = manifest["snapshots"][snap]["records"]
    assert expected == 6267
    for record in records:
        msg = json.loads(record["body"])
        assert (msg["expected_pages"], msg["expected_records"]) == (13, expected)

    # Last page arrives first, twice. Neither arrival order nor retries can
    # create extra progress or masquerade as a complete snapshot.
    assert process.handler({"Records": [records[-1], records[-1]]}, None) == {"batchItemFailures": []}
    partial = progress_from_both(snap)
    assert (partial["status"], partial["pages"], partial["total"]) == ("partial", 1, 267)
    assert (partial["expected_pages"], partial["expected_records"]) == (13, expected)

    # One page fails while the rest succeeds. A DLQ would leave this exact
    # partial state; a later successful redrive supplies the missing evidence.
    missing = {**records[0], "body": json.dumps({**json.loads(records[0]["body"]),
                                                "raw_key": "raw/TrafficSpeedBands/not-present.json"})}
    result = process.handler({"Records": [missing, *records[1:-1]]}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": missing["messageId"]}]}
    partial = progress_from_both(snap)
    assert (partial["status"], partial["pages"], partial["total"]) == ("partial", 12, expected - 500)

    assert process.handler({"Records": [records[0], records[0]]}, None) == {"batchItemFailures": []}
    complete = progress_from_both(snap)
    assert (complete["status"], complete["pages"], complete["total"]) == ("complete", 13, expected)
    assert complete["accepted"] + complete["rejected"] == expected

    # A conflicting retry cannot shrink the expected extent of an existing
    # page and must be reported as an individual failed delivery.
    conflicting = {**records[0], "body": json.dumps({**json.loads(records[0]["body"]),
                                                    "expected_pages": 1, "expected_records": 500})}
    assert process.handler({"Records": [conflicting]}, None) == {
        "batchItemFailures": [{"itemIdentifier": conflicting["messageId"]}]}
    assert progress_from_both(snap) == complete


@pytest.mark.parametrize("case", ["legacy", "mixed", "conflicting_extent", "wrong_page", "wrong_total",
                                  "conflicting_capture", "legacy_without_state"])
def test_unverifiable_summaries_are_unknown(aws, case):
    meta = boto3.resource("dynamodb").Table("t-meta")
    index(meta)
    pages = [summary(0), summary(1)]
    if case in {"legacy", "mixed"}:
        for page in pages if case == "legacy" else pages[:1]:
            del page["expected_pages"]
            del page["expected_records"]
    elif case == "conflicting_extent":
        pages[1]["expected_pages"] = 3
        pages[1]["expected_records"] = 30
    elif case == "wrong_page":
        pages[1]["sk"] = "page#002"
    elif case == "wrong_total":
        pages[1]["total"] = 9
    elif case == "legacy_without_state":
        for page in pages:
            del page["write_state"]
    else:
        pages[1]["captured_at"] = "2026-01-01T08:01:00+08:00"
    for page in pages:
        meta.put_item(Item=page)
    body = progress_from_both(SNAPSHOT)
    assert body["status"] == "unknown"
    assert body["pages"] == 2


@pytest.mark.parametrize("extent", [{"expected_pages": 1}, {"expected_records": 1},
                                     {"expected_pages": True, "expected_records": 1},
                                     {"expected_pages": 0, "expected_records": 0},
                                     {"expected_pages": 1, "expected_records": -1},
                                     {"expected_pages": 1, "expected_records": "1"}])
def test_bad_snapshot_extent_is_rejected(extent):
    msg = {"dataset": "TrafficSpeedBands", "snapshot": SNAPSHOT, "captured_at": CAPTURED,
           "raw_key": "raw/TrafficSpeedBands/page.json", "page": 0, **extent}
    with pytest.raises(process.BadMessage, match="snapshot extent"):
        process.parse_message(json.dumps(msg))


def speed_record(band=2, link="1"):
    return {"LinkID": link, "RoadName": "A", "RoadCategory": "2", "SpeedBand": band,
            "MinimumSpeed": str((band - 1) * 10), "MaximumSpeed": str(band * 10 - 1),
            "StartLon": "103.8", "StartLat": "1.3", "EndLon": "103.81", "EndLat": "1.31"}


def queue_record(aws, body, key="raw/TrafficSpeedBands/page.json", **overrides):
    aws["s3"].put_object(Bucket="t-lake", Key=key, Body=json.dumps(body).encode())
    msg = {"dataset": "TrafficSpeedBands", "snapshot": SNAPSHOT, "captured_at": CAPTURED,
           "raw_key": key, "page": 0, "expected_pages": 1,
           "expected_records": len(body["value"]), **overrides}
    return {"messageId": "delivery", "body": json.dumps(msg)}


@pytest.mark.parametrize("conflict", ["extent", "capture", "content"])
def test_conflicting_retry_cannot_change_published_readings(aws, conflict):
    record = queue_record(aws, {"value": [speed_record(2)]})
    assert process.handler({"Records": [record]}, None) == {"batchItemFailures": []}
    before = progress_from_both(SNAPSHOT)
    overrides = {"expected_pages": 2, "expected_records": 2} if conflict == "extent" else {}
    if conflict == "capture":
        overrides["captured_at"] = "2026-01-01T08:01:00+08:00"
    bad = queue_record(aws, {"value": [speed_record(7)]}, **overrides)
    assert process.handler({"Records": [bad]}, None) == {
        "batchItemFailures": [{"itemIdentifier": bad["messageId"]}]}
    reading = boto3.resource("dynamodb").Table("t-speeds").get_item(
        Key={"link_id": "1", "captured_at": CAPTURED})["Item"]
    assert reading["band"] == 2
    assert boto3.resource("dynamodb").Table("t-speeds").scan()["Count"] == 1
    assert progress_from_both(SNAPSHOT) == before


def test_incomplete_write_is_partial_and_identical_retry_recovers(aws):
    record = queue_record(aws, {"value": [speed_record(link=str(i)) for i in range(30)]})
    client = common.resource("dynamodb").meta.client
    observations = []

    def interrupt_write(**kwargs):
        # The first 25 data rows have reached DynamoDB, while the page is
        # pending. Simulate the worker failing before the remaining rows.
        observations.append(progress_from_both(SNAPSHOT))
        raise RuntimeError("interrupted after first data batch")

    client.meta.events.register("after-call.dynamodb.BatchWriteItem", interrupt_write)
    try:
        assert process.handler({"Records": [record]}, None) == {
            "batchItemFailures": [{"itemIdentifier": record["messageId"]}]}
    finally:
        client.meta.events.unregister("after-call.dynamodb.BatchWriteItem", interrupt_write)
    assert boto3.resource("dynamodb").Table("t-speeds").scan()["Count"] == 25
    after_failure = progress_from_both(SNAPSHOT)
    assert observations == [after_failure]
    assert (after_failure["status"], after_failure["pages"], after_failure["pending_pages"],
            after_failure["total"], after_failure["categories"]) == ("partial", 0, 1, 0, [])

    assert process.handler({"Records": [record]}, None) == {"batchItemFailures": []}
    recovered = progress_from_both(SNAPSHOT)
    assert (recovered["status"], recovered["pages"], recovered["pending_pages"], recovered["total"]) == (
        "complete", 1, 0, 30)
    assert boto3.resource("dynamodb").Table("t-speeds").scan()["Count"] == 30


@pytest.mark.parametrize("bands", [(2, 7), (2, 2)])
def test_concurrent_page_claims_bind_content_before_writes(aws, monkeypatch, bands):
    # Separate SDK resources per thread, as required by boto3's resource
    # thread-safety contract. Both requests reach their conditional claim at
    # the same barrier; this is actual concurrency, not a serial retry test.
    local = threading.local()

    def resource(name):
        if not hasattr(local, "ddb"):
            local.ddb = boto3.Session().resource(name)
        return local.ddb

    monkeypatch.setattr(common, "resource", resource)
    barrier = threading.Barrier(2)
    msg = {"dataset": "TrafficSpeedBands", "snapshot": SNAPSHOT, "captured_at": CAPTURED,
           "raw_key": "raw/TrafficSpeedBands/page.json", "page": 0,
           "expected_pages": 1, "expected_records": 1}

    def write(band):
        ddb = resource("dynamodb")

        def simultaneous_claim(params, **kwargs):
            item = json.loads(params["body"]).get("Item", {})
            if item.get("write_state", {}).get("S") == "pending":
                barrier.wait(timeout=10)

        ddb.meta.client.meta.events.register("before-call.dynamodb.PutItem", simultaneous_claim)
        body = {"value": [speed_record(band)]}
        items, rejected, page_summary = process.process_page(msg, body)
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        try:
            process._store(msg, items, rejected, page_summary, digest)
            return band, "success"
        except ClientError as exc:
            return band, exc.response["Error"]["Code"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, bands))
    if bands[0] != bands[1]:
        assert sorted(result for _, result in results) == ["ConditionalCheckFailedException", "success"]
    else:
        assert [result for _, result in results] == ["success", "success"]
    winner = next(band for band, result in results if result == "success")
    readings = boto3.resource("dynamodb").Table("t-speeds").scan()["Items"]
    assert len(readings) == 1 and readings[0]["band"] == winner
    published = progress_from_both(SNAPSHOT)
    assert published["status"] == "complete" and published["pending_pages"] == 0
    assert published["categories"][0]["mean_band"] == winner


class MutableSource:
    """Controlled public-data-shaped input; actual S3/SQS/DynamoDB use moto."""

    def __init__(self, body):
        self.body = body

    def snapshots(self):
        return [(SNAPSHOT, {"captured_at": CAPTURED})]

    def pages(self, dataset, snapshot):
        yield 0, self.body


def ingest_one(aws):
    ingest.handler({"dataset": "TrafficSpeedBands", "snapshot": SNAPSHOT}, None)
    messages = drain(aws["sqs"], aws["queue_url"])
    assert len(messages) == 1
    return {"messageId": messages[0]["MessageId"], "body": messages[0]["Body"]}


def test_ingest_preserves_prior_raw_bytes_on_changed_snapshot(aws, monkeypatch):
    source = MutableSource({"value": [speed_record(2)]})
    monkeypatch.setattr(ingest, "_source", lambda: source)
    original = ingest_one(aws)
    old_key = json.loads(original["body"])["raw_key"]
    original_bytes = aws["s3"].get_object(Bucket="t-lake", Key=old_key)["Body"].read()
    assert process.handler({"Records": [original]}, None) == {"batchItemFailures": []}
    status, published, _ = call("/v1/snapshots/{snapshot}/congestion", {"snapshot": SNAPSHOT})
    assert status == 200

    # Reordered JSON keys produce both the same hash-addressed key and exact
    # same stored bytes, not just semantically equivalent JSON at that key.
    source.body = {"value": [dict(reversed(list(speed_record(2).items())))]}
    repeated = ingest_one(aws)
    assert json.loads(repeated["body"])["raw_key"] == old_key
    assert aws["s3"].get_object(Bucket="t-lake", Key=old_key)["Body"].read() == original_bytes
    assert process.handler({"Records": [repeated]}, None) == {"batchItemFailures": []}

    source.body = {"value": [speed_record(7)]}
    changed = ingest_one(aws)
    new_key = json.loads(changed["body"])["raw_key"]
    assert new_key != old_key
    assert aws["s3"].get_object(Bucket="t-lake", Key=old_key)["Body"].read() == original_bytes
    assert process.handler({"Records": [changed]}, None) == {
        "batchItemFailures": [{"itemIdentifier": changed["messageId"]}]}
    assert progress_from_both(SNAPSHOT) == published
    reading = boto3.resource("dynamodb").Table("t-speeds").scan()["Items"]
    assert len(reading) == 1 and reading[0]["band"] == 2
    # The original queued pointer still reads the original content after a
    # changed replay, instead of accidentally consuming the new body.
    assert process.handler({"Records": [original]}, None) == {"batchItemFailures": []}
    assert progress_from_both(SNAPSHOT) == published


def test_old_queued_path_can_upgrade_to_same_content_hashed_path(aws, monkeypatch):
    body = {"value": [speed_record(2)]}
    old_key = f"raw/TrafficSpeedBands/snapshot={SNAPSHOT}/page-000.json"
    old = queue_record(aws, body, key=old_key)
    old_msg = json.loads(old["body"])
    del old_msg["expected_pages"]
    del old_msg["expected_records"]
    old["body"] = json.dumps(old_msg)
    assert process.handler({"Records": [old]}, None) == {"batchItemFailures": []}
    assert progress_from_both(SNAPSHOT)["status"] == "unknown"

    monkeypatch.setattr(ingest, "_source", lambda: MutableSource(body))
    upgraded = ingest_one(aws)
    assert json.loads(upgraded["body"])["raw_key"] != old_key
    assert process.handler({"Records": [upgraded]}, None) == {"batchItemFailures": []}
    published = progress_from_both(SNAPSHOT)
    assert published["status"] == "complete"
    # The old message cannot downgrade a now-known expected extent.
    assert process.handler({"Records": [old]}, None) == {
        "batchItemFailures": [{"itemIdentifier": old["messageId"]}]}
    assert progress_from_both(SNAPSHOT) == published
