"""API routes over a dataset loaded once per module."""
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


def test_get_stop(loaded):
    status, body, _ = call("/v1/stops/{code}", {"code": "01012"})
    assert status == 200 and body["description"] == "Hotel Grand Pacific" and body["road"] == "Victoria St"
    assert call("/v1/stops/{code}", {"code": "00000"})[0] == 404
    for bad in ("1012", "01012' OR 1=1", "", "../../x"):
        assert call("/v1/stops/{code}", {"code": bad})[0] == 400


def test_nearby_matches_brute_force(loaded):
    table = boto3.resource("dynamodb").Table("t-stops")
    stops, kw = [], {}
    while True:
        resp = table.scan(**kw)
        stops += resp["Items"]
        if "LastEvaluatedKey" not in resp:
            break
        kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    for lat, lon, r in [(1.2966, 103.8525, 200), (1.3521, 103.8198, 500), (1.4400, 103.7860, 300),
                        (1.3000, 103.8000, 10)]:
        status, body, _ = call("/v1/stops/nearby", query={"lat": str(lat), "lon": str(lon), "radius_m": str(r)})
        assert status == 200
        brute = sorted((round(geohash.haversine_m(lat, lon, float(s["lat"]), float(s["lon"])), 1), s["code"])
                       for s in stops if geohash.haversine_m(lat, lon, float(s["lat"]), float(s["lon"])) <= r)
        assert [(s["distance_m"], s["code"]) for s in body["stops"]] == brute
        assert body["count"] == len(brute)


def test_nearby_first_result(loaded):
    status, body, _ = call("/v1/stops/nearby", query={"lat": "1.2966", "lon": "103.8525", "radius_m": "200"})
    assert body["stops"][0]["code"] == "01012" and body["stops"][0]["distance_m"] < 50


def test_road_speeds(loaded):
    status, body, _ = call("/v1/roads/{link_id}/speeds", {"link_id": "103000000"}, {"limit": "5"})
    if status == 404:  # pick a link that exists in the fixture
        item = boto3.resource("dynamodb").Table("t-speeds").scan(Limit=1)["Items"][0]
        status, body, _ = call("/v1/roads/{link_id}/speeds", {"link_id": item["link_id"]}, {"limit": "5"})
    assert status == 200 and len(body["readings"]) == 2
    times = [r["captured_at"] for r in body["readings"]]
    assert times == sorted(times, reverse=True)
    assert call("/v1/roads/{link_id}/speeds", {"link_id": "abc"})[0] == 400
    assert call("/v1/roads/{link_id}/speeds", {"link_id": "1"}, {"limit": "0"})[0] == 400
    assert call("/v1/roads/{link_id}/speeds", {"link_id": "999999999999"})[0] == 404


def test_snapshots_and_congestion(loaded, manifest):
    status, body, _ = call("/v1/snapshots")
    assert [s["snapshot"] for s in body["snapshots"]] == ["20260929_1735", "20260929_1753"]
    status, c, _ = call("/v1/snapshots/{snapshot}/congestion", {"snapshot": "20260929_1753"})
    n = manifest["snapshots"]["20260929_1753"]["records"]
    assert status == 200 and c["total"] == n and c["accepted"] == n - 2 and c["rejected"] == 2
    assert c["rejected_reasons"] == {"no_reading": 2}
    assert sum(cat["links"] for cat in c["categories"]) == n - 2
    # recompute one category directly from the speeds table
    items, kw = [], {}
    table = boto3.resource("dynamodb").Table("t-speeds")
    while True:
        resp = table.scan(**kw)
        items += [i for i in resp["Items"] if i["snapshot"] == "20260929_1753"]
        if "LastEvaluatedKey" not in resp:
            break
        kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    for cat in c["categories"]:
        rows = [i for i in items if i["category"] == cat["category"]]
        slow = sum(1 for i in rows if int(i["band"]) <= 2)
        assert cat["links"] == len(rows)
        assert cat["share_below_20kmh"] == round(slow / len(rows), 4)
        assert cat["mean_band"] == round(sum(int(i["band"]) for i in rows) / len(rows), 3)
    assert call("/v1/snapshots/{snapshot}/congestion", {"snapshot": "20200101_0000"})[0] == 404
    assert call("/v1/snapshots/{snapshot}/congestion", {"snapshot": "latest"})[0] == 400


