"""Latency of the deployed API, measured from the client.

Sequential requests after a warm-up, so the numbers are per-request latency
through LocalStack's API Gateway and Lambda emulation. They say nothing about
AWS latency; they are here to catch regressions between commits.

Usage: python scripts/bench_api.py <base_url> [n_per_endpoint] [out.json]
"""
import gzip
import json
import pathlib
import statistics
import sys
import time

import requests

ENDPOINTS = {
    "health": "/health",
    "stop": "/v1/stops/01012",
    "nearby_300m": "/v1/stops/nearby?lat=1.2966&lon=103.8525&radius_m=300",
    "road_speeds": None,  # filled in from the fixture
    "congestion": "/v1/snapshots/20260929_1753/congestion",
}


def pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def main(base, n=200, out=None):
    s = requests.Session()
    # first link id in the 17:53 snapshot fixture
    f = sorted((pathlib.Path(__file__).resolve().parents[1] / "fixtures/datamall/TrafficSpeedBands/20260929_1753").glob("*.gz"))[0]
    link = json.loads(gzip.decompress(f.read_bytes()))["value"][0]["LinkID"]
    ENDPOINTS["road_speeds"] = f"/v1/roads/{link}/speeds?limit=12"
    results = {}
    for name, path in ENDPOINTS.items():
        for _ in range(20):  # warm-up: keep the Lambda container hot
            s.get(base + path, timeout=30)
        lat, errors = [], 0
        for _ in range(n):
            t0 = time.perf_counter()
            r = s.get(base + path, timeout=30)
            lat.append((time.perf_counter() - t0) * 1000)
            errors += r.status_code != 200
        results[name] = {"n": n, "errors": errors, "p50_ms": round(pct(lat, 50), 1),
                         "p95_ms": round(pct(lat, 95), 1), "p99_ms": round(pct(lat, 99), 1),
                         "mean_ms": round(statistics.mean(lat), 1)}
        print(f"{name:<12} n={n} errors={errors} p50={results[name]['p50_ms']}ms "
              f"p95={results[name]['p95_ms']}ms p99={results[name]['p99_ms']}ms")
    if out:
        json.dump(results, open(out, "w"), indent=2)
    return 1 if any(r["errors"] for r in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 200,
                  sys.argv[3] if len(sys.argv) > 3 else None))
