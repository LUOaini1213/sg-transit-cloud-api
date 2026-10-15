"""Ingest -> SQS -> process, on moto."""
import gzip
import json

import boto3
import pytest

from conftest import drain
from transit import ingest, process
from transit.sources import SourceError


def run_ingest(event):
    return ingest.handler(event, None)


def as_records(msgs):
    return [{"messageId": m["MessageId"], "body": m["Body"]} for m in msgs]


def test_ingest_speed_snapshot(replay, manifest):
    out = run_ingest({"dataset": "TrafficSpeedBands", "snapshot": "20260929_1753"})
    n = manifest["snapshots"]["20260929_1753"]["records"]
    assert out["records"] == n and out["pages"] == -(-n // 500)
    msgs = drain(replay["sqs"], replay["queue_url"])
    assert len(msgs) == out["pages"]
    bodies = sorted((json.loads(m["Body"]) for m in msgs), key=lambda b: b["page"])
    assert [b["page"] for b in bodies] == list(range(out["pages"]))
    assert all(b["captured_at"] == "2026-09-29T17:53:00+08:00" for b in bodies)
    keys = {o["Key"] for o in replay["s3"].list_objects_v2(Bucket="t-lake")["Contents"]}
    assert keys == {b["raw_key"] for b in bodies}
    assert all(k.startswith("raw/TrafficSpeedBands/snapshot=20260929_1753/") for k in keys)


def test_cursor_advances_and_wraps(replay, manifest):
    names = sorted(manifest["snapshots"])
    seen = [run_ingest({"dataset": "TrafficSpeedBands"})["snapshot"] for _ in range(len(names) + 2)]
    assert seen == names + names[:2]


def test_ingest_rejects_bad_events(replay):
    for ev in ([], {"dataset": "Carparks"}, {"dataset": "TrafficSpeedBands", "snapshot": "../x"},
               {"dataset": "TrafficSpeedBands", "snapshot": "20990101_0000"}):
        with pytest.raises(SourceError):
            run_ingest(ev)
    assert drain(replay["sqs"], replay["queue_url"]) == []


def test_ingest_refuses_live_mode(replay, monkeypatch):
    monkeypatch.setenv("SOURCE_MODE", "live")
    with pytest.raises(SourceError):
        run_ingest({"dataset": "BusStops"})


def test_end_to_end_stops_and_speeds(replay, manifest):
    run_ingest({"dataset": "BusStops"})
    run_ingest({"dataset": "TrafficSpeedBands", "snapshot": "20260929_1753"})
    msgs = drain(replay["sqs"], replay["queue_url"])
    res = process.handler({"Records": as_records(msgs)}, None)
    assert res == {"batchItemFailures": []}
    ddb = boto3.client("dynamodb")
    assert ddb.scan(TableName="t-stops", Select="COUNT")["Count"] == manifest["bus_stops"]
    n = manifest["snapshots"]["20260929_1753"]["records"]
    assert ddb.scan(TableName="t-speeds", Select="COUNT")["Count"] == n - 2
    rejected = [o["Key"] for o in replay["s3"].list_objects_v2(Bucket="t-lake", Prefix="rejected/")["Contents"]]
    body = [r for k in rejected for r in json.loads(replay["s3"].get_object(Bucket="t-lake", Key=k)["Body"].read())]
    assert len(body) == 2 and all(r["reasons"] == ["no_reading"] for r in body)


def test_redelivery_is_idempotent(replay, manifest):
    run_ingest({"dataset": "TrafficSpeedBands", "snapshot": "20260929_1641"})
    msgs = drain(replay["sqs"], replay["queue_url"])
    process.handler({"Records": as_records(msgs)}, None)
    process.handler({"Records": as_records(msgs)}, None)  # same messages again
    run_ingest({"dataset": "TrafficSpeedBands", "snapshot": "20260929_1641"})  # and a re-run
    process.handler({"Records": as_records(drain(replay["sqs"], replay["queue_url"]))}, None)
    ddb = boto3.client("dynamodb")
    accepted = manifest["snapshots"]["20260929_1641"]["records"] - 2
    assert ddb.scan(TableName="t-speeds", Select="COUNT")["Count"] == accepted
    pages = ddb.query(TableName="t-meta", KeyConditionExpression="pk = :p",
                      ExpressionAttributeValues={":p": {"S": "TrafficSpeedBands#20260929_1641"}})["Items"]
    assert len(pages) == len(msgs)


def test_partial_batch_failure(replay):
    run_ingest({"dataset": "TrafficSpeedBands", "snapshot": "20260929_1717"})
    good = as_records(drain(replay["sqs"], replay["queue_url"]))[:2]
    bad = [
        {"messageId": "not-json", "body": "{oops"},
        {"messageId": "outside", "body": json.dumps({**json.loads(good[0]["body"]), "raw_key": "secrets/x.json"})},
        {"messageId": "traversal", "body": json.dumps({**json.loads(good[0]["body"]),
                                                       "raw_key": "raw/TrafficSpeedBands/../../x"})},
        {"messageId": "missing", "body": json.dumps({**json.loads(good[0]["body"]),
                                                     "raw_key": "raw/TrafficSpeedBands/none.json"})},
    ]
    res = process.handler({"Records": good + bad}, None)
    assert sorted(f["itemIdentifier"] for f in res["batchItemFailures"]) == sorted(r["messageId"] for r in bad)


def test_process_page_counts_and_duplicates():
    msg = {"dataset": "TrafficSpeedBands", "snapshot": "20260929_1753",
           "captured_at": "2026-09-29T17:53:00+08:00", "raw_key": "raw/TrafficSpeedBands/x", "page": 0}
    rec = {"LinkID": "1", "RoadName": "A", "RoadCategory": "2", "SpeedBand": 2, "MinimumSpeed": "10",
           "MaximumSpeed": "19", "StartLon": "103.8", "StartLat": "1.3", "EndLon": "103.81", "EndLat": "1.31"}
    body = {"value": [rec, {**rec, "LinkID": "2", "SpeedBand": 5, "MinimumSpeed": "40", "MaximumSpeed": "49"},
                      {**rec}, {**rec, "LinkID": "3", "SpeedBand": 0, "MinimumSpeed": "", "MaximumSpeed": ""},
                      "not-a-dict"]}
    items, rejected, summary = process.process_page(msg, body)
    assert [i["link_id"] for i in items] == ["1", "2"]
    assert summary["total"] == 5 and summary["accepted"] == 2 and summary["rejected"] == 3
    assert summary["reasons"]["duplicate_key"] == 1 and summary["reasons"]["no_reading"] == 1
    assert summary["bands"] == {"2": {"2": 1, "5": 1}}


def test_gzip_raw_object_is_accepted(replay):
    body = {"value": [{"BusStopCode": "99999", "RoadName": "R", "Description": "D",
                       "Latitude": 1.3, "Longitude": 103.8}]}
    replay["s3"].put_object(Bucket="t-lake", Key="raw/BusStops/snapshot=1/page-000.json",
                            Body=gzip.compress(json.dumps(body).encode()))
    msg = {"dataset": "BusStops", "snapshot": "1", "captured_at": "x",
           "raw_key": "raw/BusStops/snapshot=1/page-000.json", "page": 0}
    assert process.handler({"Records": [{"messageId": "m", "body": json.dumps(msg)}]}, None) == {"batchItemFailures": []}
    item = boto3.resource("dynamodb").Table("t-stops").get_item(Key={"code": "99999"})["Item"]
    assert item["gh6"] == "w21z6h"


class Clock:
    """Lambda context whose remaining time drops by `step_ms` per call."""

    def __init__(self, start_ms, step_ms):
        self.left, self.step = start_ms, step_ms

    def get_remaining_time_in_millis(self):
        left = self.left
        self.left -= self.step
        return left


def test_deadline_hands_back_unstarted_records(replay):
    run_ingest({"dataset": "TrafficSpeedBands", "snapshot": "20260929_1810"})
    recs = as_records(drain(replay["sqs"], replay["queue_url"]))[:5]
    # 25 s left, 8 s per check: records 0 and 1 run (25, 17 s left), the rest are deferred
    res = process.handler({"Records": recs}, Clock(25_000, 8_000))
    assert [f["itemIdentifier"] for f in res["batchItemFailures"]] == [r["messageId"] for r in recs[2:]]
    pages = boto3.client("dynamodb").query(
        TableName="t-meta", KeyConditionExpression="pk = :p",
        ExpressionAttributeValues={":p": {"S": "TrafficSpeedBands#20260929_1810"}})["Items"]
    assert len(pages) == 2
    # with plenty of time the whole batch goes through
    assert process.handler({"Records": recs}, Clock(900_000, 1))["batchItemFailures"] == []


def test_raw_zone_is_enforced_even_if_object_exists(replay):
    """A message pointing outside raw/ is refused before any read, even when
    the object exists and holds a valid page."""
    page = {"value": [{"BusStopCode": "88888", "RoadName": "R", "Description": "D",
                       "Latitude": 1.3, "Longitude": 103.8}]}
    replay["s3"].put_object(Bucket="t-lake", Key="rejected/BusStops/x.json", Body=json.dumps(page).encode())
    msg = {"dataset": "BusStops", "snapshot": "1", "captured_at": "x", "page": 0}
    for key in ("rejected/BusStops/x.json", "raw/BusStops/../../rejected/BusStops/x.json", "raw/TrafficSpeedBands/x"):
        with pytest.raises(process.BadMessage):
            process.parse_message(json.dumps({**msg, "raw_key": key}))
        res = process.handler({"Records": [{"messageId": "m", "body": json.dumps({**msg, "raw_key": key})}]}, None)
        assert res == {"batchItemFailures": [{"itemIdentifier": "m"}]}
    assert "Item" not in boto3.resource("dynamodb").Table("t-stops").get_item(Key={"code": "88888"})
