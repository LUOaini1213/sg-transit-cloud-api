"""A snapshot whose pages are only partly processed must not look complete.

The page summaries in the meta table appear one by one as the process Lambda
works through the queue, and a page that fails three times stays in the DLQ.
The snapshot endpoints have to say how many pages they are aggregating out of
how many ingest queued.
"""
import json

import boto3

from conftest import drain
from transit import api, ingest, process
from transit.process import BadMessage, parse_message

SNAP = "20260929_1753"


def call(resource, path_params=None):
    ev = {"httpMethod": "GET", "resource": resource, "pathParameters": path_params,
          "queryStringParameters": None, "headers": {}}
    r = api.handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


def as_records(msgs):
    return [{"messageId": m["MessageId"], "body": m["Body"]} for m in msgs]


def by_page(msgs):
    return sorted(msgs, key=lambda m: json.loads(m["Body"])["page"])


class OneItemPerQuery:
    """Wraps a table so every Query returns one item and a LastEvaluatedKey."""

    def __init__(self, table):
        self.table = table

    def query(self, **kw):
        return self.table.query(Limit=1, **kw)


def test_partial_then_complete(replay, manifest, monkeypatch):
    n = manifest["snapshots"][SNAP]["records"]
    out = ingest.handler({"dataset": "TrafficSpeedBands", "snapshot": SNAP}, None)
    assert out["pages"] == 13 and out["records"] == n
    msgs = by_page(drain(replay["sqs"], replay["queue_url"]))
    assert all(json.loads(m["Body"])["expected_pages"] == 13 for m in msgs)
    assert all(json.loads(m["Body"])["expected_records"] == n for m in msgs)

    # 5 of 13 pages processed
    assert process.handler({"Records": as_records(msgs[:5])}, None) == {"batchItemFailures": []}
    status, body = call("/v1/snapshots")
    assert status == 200
    assert body["snapshots"] == [{"snapshot": SNAP, "captured_at": "2026-09-29T17:53:00+08:00",
                                  "status": "partial", "pages": 5, "expected_pages": 13,
                                  "expected_records": n}]
    status, c = call("/v1/snapshots/{snapshot}/congestion", {"snapshot": SNAP})
    assert status == 200
    assert (c["status"], c["pages"], c["expected_pages"], c["expected_records"]) == ("partial", 5, 13, n)
    assert c["total"] == 5 * 500 < n

    # the rest, plus a redelivery of an early page: still 13 distinct pages
    process.handler({"Records": as_records(msgs[5:] + msgs[:1])}, None)
    status, c = call("/v1/snapshots/{snapshot}/congestion", {"snapshot": SNAP})
    assert (c["status"], c["pages"], c["total"]) == ("complete", 13, n)
    listing = call("/v1/snapshots")[1]
    assert listing["snapshots"][0]["status"] == "complete"

    # the meta-table queries follow LastEvaluatedKey: one item per response
    # gives the same answers
    real = api._table
    monkeypatch.setattr(api, "_table", lambda name: OneItemPerQuery(real(name)))
    paged = call("/v1/snapshots/{snapshot}/congestion", {"snapshot": SNAP})[1]
    assert paged == c and paged["pages"] == 13 and paged["status"] == "complete"
    assert call("/v1/snapshots")[1] == listing


def test_summaries_without_extent_are_unknown(aws):
    """Rows written before ingest recorded the extent cannot prove completeness."""
    meta = boto3.resource("dynamodb").Table("t-meta")
    meta.put_item(Item={"pk": "snapshots#TrafficSpeedBands", "sk": SNAP, "captured_at": "x"})
    meta.put_item(Item={"pk": f"TrafficSpeedBands#{SNAP}", "sk": "page#000", "captured_at": "x",
                        "total": 500, "accepted": 500, "rejected": 0, "bands": {"1": {"3": 500}}})
    snap = call("/v1/snapshots")[1]["snapshots"][0]
    assert (snap["status"], snap["pages"], snap["expected_pages"]) == ("unknown", 1, None)


def test_disagreeing_or_inconsistent_extents_are_unknown():
    page = {"total": 500, "expected_pages": 2, "expected_records": 1000}
    assert api.snapshot_status([page, {**page, "expected_pages": 3}])["status"] == "unknown"
    # all pages present but the record totals do not add up
    assert api.snapshot_status([page, {**page, "total": 400}])["status"] == "unknown"
    assert api.snapshot_status([page, page])["status"] == "complete"
    assert api.snapshot_status([page])["status"] == "partial"
    assert api.snapshot_status([])["status"] == "unknown"


def test_bad_extent_is_rejected():
    base = {"dataset": "TrafficSpeedBands", "snapshot": SNAP, "captured_at": "x",
            "raw_key": f"raw/TrafficSpeedBands/snapshot={SNAP}/page-003.json", "page": 3}
    assert parse_message(json.dumps(base))["page"] == 3  # messages from before the change
    assert parse_message(json.dumps({**base, "expected_pages": 4, "expected_records": 1700}))
    for bad in ({"expected_pages": 3, "expected_records": 1700},     # page 3 of 3 does not exist
                {"expected_pages": "13", "expected_records": 1700},
                {"expected_pages": 13},
                {"expected_pages": 13, "expected_records": -1},
                {"expected_pages": True, "expected_records": 1700}):
        try:
            parse_message(json.dumps({**base, **bad}))
        except BadMessage:
            continue
        raise AssertionError(f"accepted {bad}")
