"""Processing Lambda (SQS event source): validate one raw page, upsert DynamoDB.

Writes use deterministic keys, so a redelivered message rewrites the same
items. Failed messages are reported individually (ReportBatchItemFailures);
after maxReceiveCount the queue moves them to the dead-letter queue.
"""
import datetime as dt
import hashlib
import json
from collections import Counter
from decimal import Decimal

from . import common, geohash
from .quality import validate_speed, validate_stop
from .sources import DATASETS, decode_page, encode_page

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
    if type(msg["page"]) is not int or msg["page"] < 0:
        raise BadMessage("bad page number")
    if "expected_pages" in msg or "expected_records" in msg:
        if (type(msg.get("expected_pages")) is not int or msg["expected_pages"] <= msg["page"]
                or type(msg.get("expected_records")) is not int or msg["expected_records"] < 0):
            raise BadMessage("bad snapshot extent")
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


def _store(msg, items, rejected, summary, content_hash):
    ddb = common.resource("dynamodb")
    meta = ddb.Table(common.env("META_TABLE"))
    extent = {k: msg[k] for k in ("expected_pages", "expected_records") if k in msg}
    values = {":captured": msg["captured_at"], ":raw": msg["raw_key"], ":hash": content_hash}
    no_extent = "attribute_not_exists(expected_pages) AND attribute_not_exists(expected_records)"
    exact_extent = no_extent
    claim_extent = no_extent
    if extent:
        values.update({":pages": msg["expected_pages"], ":records": msg["expected_records"]})
        exact_extent = "expected_pages = :pages AND expected_records = :records"
        claim_extent = f"({no_extent}) OR ({exact_extent})"
    page = {"pk": f"{msg['dataset']}#{msg['snapshot']}", "sk": f"page#{msg['page']:03d}",
            "captured_at": msg["captured_at"], "raw_key": msg["raw_key"],
            "content_sha256": content_hash, **extent}
    # Bind this page to immutable input atomically BEFORE touching data. An
    # identical concurrent delivery can safely repeat the same deterministic
    # writes; a different payload/identity cannot win this condition. Pending
    # remains retryable after a crash, without a lease or a delivery counter.
    # Matching content may migrate an old queued raw path to its hash-addressed
    # path; capture and extent still have to match (or upgrade legacy extent).
    meta.put_item(Item={**page, "write_state": "pending"},
                  ConditionExpression="attribute_not_exists(pk) OR (captured_at = :captured AND "
                                      "(attribute_not_exists(raw_key) OR raw_key = :raw OR content_sha256 = :hash) AND "
                                      "(attribute_not_exists(content_sha256) OR content_sha256 = :hash) AND "
                                      f"({claim_extent}))",
                  ExpressionAttributeValues=values)
    meta.put_item(Item={"pk": f"snapshots#{msg['dataset']}", "sk": msg["snapshot"],
                        "captured_at": msg["captured_at"]})
    table_name = common.env("STOPS_TABLE") if msg["dataset"] == "BusStops" else common.env("SPEEDS_TABLE")
    with ddb.Table(table_name).batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)
    if rejected:
        key = msg["raw_key"].replace("raw/", "rejected/", 1)
        common.client("s3").put_object(Bucket=common.env("RAW_BUCKET"), Key=key,
                                       Body=json.dumps(rejected, default=str).encode(),
                                       ContentType="application/json")
    meta.put_item(Item={**page, "write_state": "complete", **_ddb_safe(summary)},
                  ConditionExpression="content_sha256 = :hash AND captured_at = :captured "
                                      f"AND raw_key = :raw AND ({exact_extent})",
                  ExpressionAttributeValues=values)


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
    content_hash = hashlib.sha256(encode_page(body)).hexdigest()
    _store(msg, items, rejected, summary, content_hash)
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
