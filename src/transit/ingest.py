"""Ingestion Lambda: pull DataMall pages, land them in S3, queue one message per page.

The message carries only a pointer to the raw object (claim-check pattern), so
page size never runs into the 256 KB SQS limit.
"""
import datetime as dt
import json
import uuid

from . import common
from .sources import DATASETS, ReplaySource, SourceError

SGT = dt.timezone(dt.timedelta(hours=8))


def _source():
    mode = common.env("SOURCE_MODE")
    if mode != "replay":
        # The live client (sources.DataMallHttpSource) needs a DataMall key from
        # Secrets Manager; that wiring is described in docs/REAL_AWS.md.
        raise SourceError(f"SOURCE_MODE={mode!r} is not deployed in this stack")
    return ReplaySource(common.client("s3"), common.env("REPLAY_BUCKET"))


def _next_snapshot(src, table):
    """Replay cursor: each scheduled run takes the next captured snapshot, wrapping around."""
    snaps = src.snapshots()
    item = table.get_item(Key={"pk": "cursor", "sk": "TrafficSpeedBands"}).get("Item")
    names = [s for s, _ in snaps]
    nxt = 0 if not item or item["snapshot"] not in names else (names.index(item["snapshot"]) + 1) % len(names)
    return snaps[nxt]


def parse_event(event):
    """Accept a direct invoke {"dataset": ..., "snapshot": ...} or an EventBridge
    scheduled event whose constant input has the same shape."""
    if not isinstance(event, dict):
        raise SourceError("event must be an object")
    dataset = event.get("dataset")
    if dataset not in DATASETS:
        raise SourceError(f"dataset must be one of {DATASETS}")
    snapshot = event.get("snapshot")
    if snapshot is not None and (not isinstance(snapshot, str) or not snapshot.replace("_", "").isdigit()):
        raise SourceError("snapshot must look like YYYYMMDD_HHMM")
    return dataset, snapshot


def handler(event, context):
    dataset, snapshot = parse_event(event)
    raw_bucket, queue_url = common.env("RAW_BUCKET"), common.env("QUEUE_URL")
    meta = common.resource("dynamodb").Table(common.env("META_TABLE"))
    src = _source()
    s3, sqs = common.client("s3"), common.client("sqs")

    snaps = dict(src.snapshots())
    if dataset == "TrafficSpeedBands":
        if snapshot is None:
            snapshot, info = _next_snapshot(src, meta)
        elif snapshot not in snaps:
            raise SourceError(f"unknown snapshot {snapshot}")
        else:
            info = snaps[snapshot]
        captured_at = info["captured_at"]
    else:
        snapshot = src.bus_stops_snapshot()
        captured_at = f"{snapshot[:4]}-{snapshot[4:6]}-{snapshot[6:8]}T00:00:00+08:00"

    run_id = uuid.uuid4().hex
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    messages, records = [], 0
    for page_no, body in src.pages(dataset, snapshot if dataset == "TrafficSpeedBands" else None):
        key = f"raw/{dataset}/snapshot={snapshot}/page-{page_no:03d}.json"
        s3.put_object(Bucket=raw_bucket, Key=key, Body=json.dumps(body).encode(),
                      ContentType="application/json")
        records += len(body["value"])
        messages.append({"run_id": run_id, "dataset": dataset, "snapshot": snapshot,
                         "captured_at": captured_at, "raw_key": key, "page": page_no})

    for i in range(0, len(messages), 10):
        chunk = messages[i:i + 10]
        resp = sqs.send_message_batch(QueueUrl=queue_url, Entries=[
            {"Id": str(j), "MessageBody": json.dumps(m)} for j, m in enumerate(chunk)])
        if resp.get("Failed"):
            raise SourceError(f"SQS rejected {len(resp['Failed'])} messages: {resp['Failed'][0]}")

    meta.put_item(Item={"pk": f"run#{dataset}", "sk": f"{started}#{run_id}", "run_id": run_id,
                        "snapshot": snapshot, "pages": len(messages), "records": records,
                        "status": "queued"})
    if dataset == "TrafficSpeedBands":
        meta.put_item(Item={"pk": "cursor", "sk": dataset, "snapshot": snapshot})
    common.put_metrics(dataset, PagesIngested=len(messages), RecordsIngested=records)
    common.log("ingest_done", dataset=dataset, snapshot=snapshot, run_id=run_id,
               pages=len(messages), records=records)
    return {"run_id": run_id, "dataset": dataset, "snapshot": snapshot,
            "pages": len(messages), "records": records}
