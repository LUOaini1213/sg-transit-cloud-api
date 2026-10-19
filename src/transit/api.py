"""REST API Lambda behind API Gateway (proxy integration).

Read-only. Every response carries the same security headers, and every input
is validated before it reaches DynamoDB.
"""
import json
import os
import re
from collections import Counter
from decimal import Decimal

from boto3.dynamodb.conditions import Key

from . import common, geohash

ROAD_CATEGORY = {"1": "Expressway", "2": "Major arterial", "3": "Arterial", "4": "Minor arterial",
                 "5": "Small road", "6": "Slip road", "8": "Short tunnel"}
SLOW_BANDS = {"1", "2"}  # below 20 km/h
MAX_RADIUS_M = 500  # a 3x3 block of precision-6 geohash cells always covers this radius
STOP_CODE = re.compile(r"^\d{5}$")
LINK_ID = re.compile(r"^\d{1,12}$")
SNAPSHOT = re.compile(r"^\d{8}_\d{4}$")
SG_LAT, SG_LON = (1.15, 1.48), (103.59, 104.10)

SECURITY_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def _json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError(type(o).__name__)


def respond(status, body, origin=None):
    headers = dict(SECURITY_HEADERS)
    allowed = os.environ.get("CORS_ORIGIN", "")
    if allowed and origin == allowed:
        headers["Access-Control-Allow-Origin"] = allowed
        headers["Vary"] = "Origin"
    return {"statusCode": status, "headers": headers, "body": json.dumps(body, default=_json_default)}


def _table(name):
    return common.resource("dynamodb").Table(common.env(name))


def _float_param(q, name, lo, hi):
    raw = q.get(name)
    if raw is None:
        raise ApiError(400, f"missing query parameter {name}")
    try:
        v = float(raw)
    except ValueError:
        raise ApiError(400, f"{name} must be a number") from None
    if not lo <= v <= hi:
        raise ApiError(400, f"{name} must be between {lo} and {hi}")
    return v


def _int_param(q, name, default, lo, hi):
    raw = q.get(name)
    if raw is None:
        return default
    if not re.fullmatch(r"\d{1,6}", raw):
        raise ApiError(400, f"{name} must be a whole number")
    v = int(raw)
    if not lo <= v <= hi:
        raise ApiError(400, f"{name} must be between {lo} and {hi}")
    return v


def get_stop(params, q):
    code = params.get("code", "")
    if not STOP_CODE.match(code):
        raise ApiError(400, "stop code must be 5 digits")
    item = _table("STOPS_TABLE").get_item(Key={"code": code}).get("Item")
    if not item:
        raise ApiError(404, "stop not found")
    return {k: item[k] for k in ("code", "description", "road", "lat", "lon", "captured_at")}


def nearby_stops(params, q):
    lat = _float_param(q, "lat", *SG_LAT)
    lon = _float_param(q, "lon", *SG_LON)
    radius = _int_param(q, "radius_m", 300, 10, MAX_RADIUS_M)
    table = _table("STOPS_TABLE")
    found = []
    for cell in geohash.neighbours(geohash.encode(lat, lon, 6)):
        kw = {"IndexName": "by_geohash", "KeyConditionExpression": Key("gh6").eq(cell)}
        while True:
            resp = table.query(**kw)
            for it in resp["Items"]:
                d = geohash.haversine_m(lat, lon, float(it["lat"]), float(it["lon"]))
                if d <= radius:
                    found.append({"code": it["code"], "description": it["description"],
                                  "road": it["road"], "distance_m": round(d, 1)})
            if "LastEvaluatedKey" not in resp:
                break
            kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    found.sort(key=lambda s: (s["distance_m"], s["code"]))
    return {"lat": lat, "lon": lon, "radius_m": radius, "count": len(found), "stops": found}


def road_speeds(params, q):
    link = params.get("link_id", "")
    if not LINK_ID.match(link):
        raise ApiError(400, "link_id must be numeric")
    limit = _int_param(q, "limit", 12, 1, 96)
    resp = _table("SPEEDS_TABLE").query(KeyConditionExpression=Key("link_id").eq(link),
                                        ScanIndexForward=False, Limit=limit)
    if not resp["Items"]:
        raise ApiError(404, "no readings for this link")
    first = resp["Items"][0]
    return {"link_id": link, "road": first["road"],
            "category": ROAD_CATEGORY.get(first["category"], first["category"]),
            "readings": [{"captured_at": i["captured_at"], "band": i["band"],
                          "min_kmh": i["min_kmh"], "max_kmh": i["max_kmh"]} for i in resp["Items"]]}


def _metadata_items(table, partition):
    """Query may stop at 1 MB, even without a Limit or a filter."""
    kw = {"KeyConditionExpression": Key("pk").eq(partition), "ConsistentRead": True}
    while True:
        response = table.query(**kw)
        yield from response["Items"]
        if not response.get("LastEvaluatedKey"):
            return
        kw["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def _snapshot_progress(pages):
    """Count persisted page identities, never SQS deliveries or index writes.

    Old summaries lack an expected extent. Conflicting or mixed metadata also
    cannot establish completion, even if the number of returned rows matches.
    """
    completed = [p for p in pages if p.get("write_state", "complete") == "complete"]
    result = {"status": "unknown", "pages": len(completed), "pending_pages": len(pages) - len(completed),
              "expected_pages": None, "expected_records": None}
    if not pages or any("expected_pages" not in p or "expected_records" not in p for p in pages):
        return result
    if any(p.get("write_state") not in {"pending", "complete"} for p in pages):
        return result
    extents = {(p["expected_pages"], p["expected_records"]) for p in pages}
    if len(extents) != 1 or len({p["captured_at"] for p in pages}) != 1:
        return result
    expected_pages, expected_records = extents.pop()
    if any(isinstance(v, bool) or not isinstance(v, (int, Decimal)) or v != int(v)
           for v in (expected_pages, expected_records)) or expected_pages < 1 or expected_records < 0:
        return result
    expected_pages, expected_records = int(expected_pages), int(expected_records)
    result.update(expected_pages=expected_pages, expected_records=expected_records)
    numbers = []
    for page in pages:
        key = page["sk"]
        if not re.fullmatch(r"page#[0-9]+", key):
            return result
        number = int(key[5:])
        if key != f"page#{number:03d}" or number >= expected_pages:
            return result
        numbers.append(number)
    if len(set(numbers)) != len(pages):
        return result
    total = sum(int(p["total"]) for p in completed)
    if len(completed) == expected_pages and total == expected_records:
        result["status"] = "complete"
    elif len(completed) < expected_pages and total <= expected_records:
        result["status"] = "partial"
    return result


def list_snapshots(params, q):
    table = _table("META_TABLE")
    snapshots = []
    for item in _metadata_items(table, "snapshots#TrafficSpeedBands"):
        pages = list(_metadata_items(table, f"TrafficSpeedBands#{item['sk']}"))
        snapshots.append({"snapshot": item["sk"], "captured_at": item["captured_at"],
                          **_snapshot_progress(pages)})
    return {"snapshots": snapshots}


def congestion(params, q):
    snap = params.get("snapshot", "")
    if not SNAPSHOT.match(snap):
        raise ApiError(400, "snapshot must look like YYYYMMDD_HHMM")
    pages = list(_metadata_items(_table("META_TABLE"), f"TrafficSpeedBands#{snap}"))
    if not pages:
        raise ApiError(404, "snapshot not ingested")
    per_cat, totals, reasons = {}, Counter(total=0, accepted=0, rejected=0), Counter()
    for p in pages:
        if p.get("write_state", "complete") != "complete":
            continue
        totals.update({"total": int(p["total"]), "accepted": int(p["accepted"]),
                       "rejected": int(p["rejected"])})
        reasons.update({k: int(v) for k, v in p.get("reasons", {}).items()})
        for cat, bands in p.get("bands", {}).items():
            per_cat.setdefault(cat, Counter()).update({b: int(n) for b, n in bands.items()})
    categories = []
    for cat in sorted(per_cat):
        b = per_cat[cat]
        n = sum(b.values())
        slow = sum(v for k, v in b.items() if k in SLOW_BANDS)
        mean = sum(int(k) * v for k, v in b.items()) / n
        categories.append({"category": cat, "name": ROAD_CATEGORY.get(cat, "Unknown"), "links": n,
                           "mean_band": round(mean, 3), "share_below_20kmh": round(slow / n, 4)})
    return {"snapshot": snap, "captured_at": pages[0]["captured_at"], **_snapshot_progress(pages),
            **totals, "rejected_reasons": dict(reasons), "categories": categories}


def health(params, q):
    return {"status": "ok"}


ROUTES = {
    ("GET", "/health"): health,
    ("GET", "/v1/stops/nearby"): nearby_stops,
    ("GET", "/v1/stops/{code}"): get_stop,
    ("GET", "/v1/roads/{link_id}/speeds"): road_speeds,
    ("GET", "/v1/snapshots"): list_snapshots,
    ("GET", "/v1/snapshots/{snapshot}/congestion"): congestion,
}


def handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    origin = headers.get("origin")
    route = ROUTES.get((event.get("httpMethod"), event.get("resource")))
    if route is None:
        return respond(404, {"error": "not found"}, origin)
    try:
        body = route(event.get("pathParameters") or {}, event.get("queryStringParameters") or {})
        return respond(200, body, origin)
    except ApiError as exc:
        return respond(exc.status, {"error": exc.message}, origin)
    except Exception as exc:
        rid = getattr(context, "aws_request_id", None)
        common.log("api_error", error_type=type(exc).__name__, error=str(exc), request_id=rid)
        return respond(500, {"error": "internal error", "request_id": rid}, origin)
