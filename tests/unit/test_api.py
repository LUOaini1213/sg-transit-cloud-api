import json
from decimal import Decimal

import boto3
import pytest

from transit import api, geohash

HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
           "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"}


def call(resource, path_params=None, query=None, headers=None, method="GET"):
    ev = {"httpMethod": method, "resource": resource, "pathParameters": path_params,
          "queryStringParameters": query, "headers": headers or {}}
    r = api.handler(ev, type("Ctx", (), {"aws_request_id": "req-1"})())
    for k, v in HEADERS.items():
        assert r["headers"][k] == v, (resource, k)
    return r["statusCode"], json.loads(r["body"]), r["headers"]


def test_health(aws):
    assert call("/health")[:2] == (200, {"status": "ok"})


def test_unknown_route_and_method(aws):
    assert call("/v1/nope")[0] == 404
    assert call("/health", method="POST")[0] == 404


def test_nearby_validation(aws):
    bad = [{}, {"lat": "1.3"}, {"lat": "x", "lon": "103.8"}, {"lat": "51.5", "lon": "-0.1"},
           {"lat": "1.3", "lon": "103.8", "radius_m": "501"}, {"lat": "1.3", "lon": "103.8", "radius_m": "-5"},
           {"lat": "1.3", "lon": "103.8", "radius_m": "1e3"}, {"lat": "nan", "lon": "103.8"}]
    for q in bad:
        status, body, _ = call("/v1/stops/nearby", query=q)
        assert status == 400 and "error" in body, q


def test_congestion_math_on_hand_example(aws):
    meta = boto3.resource("dynamodb").Table("t-meta")
    for page, bands in ((0, {"1": {"1": 2, "3": 2}}), (1, {"1": {"2": 1, "8": 1}, "5": {"4": 3}})):
        meta.put_item(Item={"pk": "TrafficSpeedBands#20260101_0800", "sk": f"page#{page:03d}",
                            "captured_at": "2026-01-01T08:00:00+08:00", "total": 7 if page else 4,
                            "accepted": 6 if page else 4, "rejected": 1 if page else 0,
                            "reasons": {"no_reading": 1} if page else {}, "bands": bands})
    status, c, _ = call("/v1/snapshots/{snapshot}/congestion", {"snapshot": "20260101_0800"})
    assert status == 200 and (c["total"], c["accepted"], c["rejected"]) == (11, 10, 1)
    exp = {cat["category"]: cat for cat in c["categories"]}
    # category 1: bands 1,1,3,3,2,8 -> 3 of 6 below 20 km/h, mean 18/6 = 3
    assert exp["1"]["links"] == 6 and exp["1"]["share_below_20kmh"] == 0.5 and exp["1"]["mean_band"] == 3.0
    assert exp["5"]["share_below_20kmh"] == 0.0 and exp["5"]["name"] == "Small road"


def test_cors_only_for_configured_origin(aws, monkeypatch):
    monkeypatch.setenv("CORS_ORIGIN", "https://app.example")
    _, _, h = call("/health", headers={"Origin": "https://app.example"})
    assert h["Access-Control-Allow-Origin"] == "https://app.example"
    _, _, h = call("/health", headers={"origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in h


def test_internal_error_hides_detail(aws, monkeypatch):
    monkeypatch.delenv("STOPS_TABLE")
    status, body, _ = call("/v1/stops/{code}", {"code": "01012"})
    assert status == 500 and body == {"error": "internal error", "request_id": "req-1"}


def test_decimal_encoding():
    assert json.loads(api.respond(200, {"a": Decimal("2"), "b": Decimal("1.5")})["body"]) == {"a": 2, "b": 1.5}
