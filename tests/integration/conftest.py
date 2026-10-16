"""Integration tests against the stack deployed to LocalStack by terraform.

Needs: LocalStack on AWS_ENDPOINT_URL (default http://localhost:4566) and
STACK_OUTPUTS pointing at `terraform output -json`.
"""
import gzip
import json
import os
import pathlib
import time

import boto3
import requests
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "datamall"
ENDPOINT = os.environ.setdefault("AWS_ENDPOINT_URL", "http://localhost:4566")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")


@pytest.fixture(scope="session")
def out():
    raw = json.loads(pathlib.Path(os.environ["STACK_OUTPUTS"]).read_text())
    return {k: v["value"] for k, v in raw.items()}


@pytest.fixture(scope="session")
def base(out, ingested):
    url = f"{ENDPOINT}/_aws/execute-api/{out['api_id']}/{out['stage']}"
    # The first call after a long ingest can hit a Lambda cold start on
    # LocalStack (a new container), which takes longer than the 10 s request
    # timeout the tests use. Warm the function once with a generous timeout.
    requests.get(f"{url}/health", timeout=120)
    return url


@pytest.fixture(scope="session")
def manifest():
    return json.loads((FIXTURES / "manifest.json").read_text())


def fixture_no_reading(snapshot):
    """Count rows the API marks as 'no reading' straight from the fixture files
    (band 0 with empty speeds), independent of the validator under test."""
    n = 0
    for f in sorted((FIXTURES / "TrafficSpeedBands" / snapshot).glob("page-*.json.gz")):
        for r in json.loads(gzip.decompress(f.read_bytes()))["value"]:
            n += r["SpeedBand"] == 0 and r["MinimumSpeed"] == "" and r["MaximumSpeed"] == ""
    return n


def invoke(fn, payload):
    lam = boto3.client("lambda")
    r = lam.invoke(FunctionName=fn, Payload=json.dumps(payload).encode())
    body = json.loads(r["Payload"].read())
    assert "FunctionError" not in r, body
    return body


def queue_depth(url):
    a = boto3.client("sqs").get_queue_attributes(
        QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])
    return int(a["Attributes"]["ApproximateNumberOfMessages"]) + int(a["Attributes"]["ApproximateNumberOfMessagesNotVisible"])


def wait_until(pred, timeout, step=2.0, what="condition"):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


@pytest.fixture(scope="session")
def ingested(out, manifest):
    """Ingest bus stops and every speed snapshot once, wait for the queue to drain."""
    t0 = time.time()
    runs = [invoke(out["functions"]["ingest"], {"dataset": "BusStops"})]
    for snap in sorted(manifest["snapshots"]):
        runs.append(invoke(out["functions"]["ingest"], {"dataset": "TrafficSpeedBands", "snapshot": snap}))
    wait_until(lambda: queue_depth(out["queue_url"]) == 0, 600, 3, "page queue to drain")
    return {"runs": runs, "seconds": time.time() - t0}
