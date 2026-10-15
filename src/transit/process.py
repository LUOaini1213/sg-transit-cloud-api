"""Processing Lambda (SQS event source): validate one raw page, upsert DynamoDB.

Writes use deterministic keys, so a redelivered message rewrites the same
items. Failed messages are reported individually (ReportBatchItemFailures);
after maxReceiveCount the queue moves them to the dead-letter queue.
"""
import datetime as dt
import json
from collections import Counter
from decimal import Decimal

from . import common, geohash
from .quality import validate_speed, validate_stop
from .sources import DATASETS, decode_page

GEOHASH_PRECISION = 6
TTL_DAYS = 30
# Stop taking new records when less than this much time is left, and hand the
# rest back to the queue. Without it a slow batch hits the Lambda timeout and
# every record in it is redelivered, including the ones already written.
SAFETY_MS = 10_000


class BadMessage(Exception):
    pass


def parse_message(body):
    try:
        msg = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise BadMessage(f"body is not JSON: {exc}") from exc
    required = ("dataset", "snapshot", "captured_at", "raw_key", "page")
    if not isinstance(msg, dict) or any(k not in msg for k in required):
        raise BadMessage("missing fields")
    if msg["dataset"] not in DATASETS:
        raise BadMessage("unknown dataset")
    prefix = f"raw/{msg['dataset']}/"
    if not str(msg["raw_key"]).startswith(prefix) or ".." in msg["raw_key"]:
        raise BadMessage("raw_key outside the raw zone")
    if not isinstance(msg["page"], int) or msg["page"] < 0:
        raise BadMessage("bad page number")
    return msg


def _stop_item(rec, captured_at, now_iso):
    return {"code": rec["code"], "description": rec["description"], "road": rec["road"],
            "lat": rec["lat"], "lon": rec["lon"],
            "gh6": geohash.encode(float(rec["lat"]), float(rec["lon"]), GEOHASH_PRECISION),
            "captured_at": captured_at, "updated_at": now_iso}


def _speed_item(rec, snapshot, captured_at, expires_at):
    return {"link_id": rec["link_id"], "captured_at": captured_at, "snapshot": snapshot,
            "band": rec["band"], "min_kmh": rec["min_kmh"], "max_kmh": rec["max_kmh"],
            "road": rec["road"], "category": rec["category"], "expires_at": expires_at}


def process_page(msg, body, now=None):
    """Pure part: returns (items, rejected, summary). No AWS calls."""
    now = now or dt.datetime.now(dt.timezone.utc)
    validate = validate_stop if msg["dataset"] == "BusStops" else validate_speed
    items, rejected, reasons, warnings = [], [], Counter(), Counter()
    bands = {}
    expires_at = int(now.timestamp()) + TTL_DAYS * 86400
    seen = set()
    for raw in body["value"]:
        rec, why, warn = validate(raw if isinstance(raw, dict) else {})
        warnings.update(warn)
        if rec is None:
            reasons.update(why)
            rejected.append({"record": raw, "reasons": why})
            continue
        key = rec.get("code") or rec.get("link_id")
        if key in seen:
            reasons.update(["duplicate_key"])
            rejected.append({"record": raw, "reasons": ["duplicate_key"]})
            continue
        seen.add(key)
        if msg["dataset"] == "BusStops":
            items.append(_stop_item(rec, msg["captured_at"], now.isoformat()))
        else:
            items.append(_speed_item(rec, msg["snapshot"], msg["captured_at"], expires_at))
            cat = bands.setdefault(rec["category"], Counter())
            cat[str(rec["band"])] += 1
    summary = {"total": len(body["value"]), "accepted": len(items), "rejected": len(rejected),
               "reasons": dict(reasons), "warnings": dict(warnings),
               "bands": {c: dict(b) for c, b in bands.items()}}
    return items, rejected, summary


def _store(msg, items, rejected, summary):
    ddb = common.resource("dynamodb")
    table_name = common.env("STOPS_TABLE") if msg["dataset"] == "BusStops" else common.env("SPEEDS_TABLE")
    with ddb.Table(table_name).batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)
    if rejected:
        key = msg["raw_key"].replace("raw/", "rejected/", 1)
        common.client("s3").put_object(Bucket=common.env("RAW_BUCKET"), Key=key,
                                       Body=json.dumps(rejected, default=str).encode(),
                                       ContentType="application/json")
    meta = ddb.Table(common.env("META_TABLE"))
    meta.put_item(Item={"pk": f"{msg['dataset']}#{msg['snapshot']}", "sk": f"page#{msg['page']:03d}",
                        "captured_at": msg["captured_at"], **_ddb_safe(summary)})
    meta.put_item(Item={"pk": f"snapshots#{msg['dataset']}", "sk": msg["snapshot"],
                        "captured_at": msg["captured_at"]})


def _ddb_safe(obj):
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _ddb_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_ddb_safe(v) for v in obj]
    return obj


def handle_record(record):
    msg = parse_message(record["body"])
    raw = common.client("s3").get_object(Bucket=common.env("RAW_BUCKET"), Key=msg["raw_key"])["Body"].read()
    body = decode_page(raw)
    items, rejected, summary = process_page(msg, body)
    _store(msg, items, rejected, summary)
    common.put_metrics(msg["dataset"], RecordsAccepted=summary["accepted"],
                       RecordsRejected=summary["rejected"],
                       RecordsWarned=sum(summary["warnings"].values()))
    common.log("page_processed", dataset=msg["dataset"], snapshot=msg["snapshot"], page=msg["page"],
               accepted=summary["accepted"], rejected=summary["rejected"], reasons=summary["reasons"])


def handler(event, context):
    failures = []
    records = event.get("Records", [])
    for i, record in enumerate(records):
        if context is not None and context.get_remaining_time_in_millis() < SAFETY_MS:
            deferred = [r.get("messageId") for r in records[i:]]
            common.log("batch_deferred", deferred=len(deferred))
            failures += [{"itemIdentifier": m} for m in deferred]
            break
        try:
            handle_record(record)
        except Exception as exc:
            common.log("page_failed", message_id=record.get("messageId"), error=str(exc),
                       error_type=type(exc).__name__)
            failures.append({"itemIdentifier": record.get("messageId")})
    return {"batchItemFailures": failures}
