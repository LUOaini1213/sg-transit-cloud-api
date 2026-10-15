import contextlib
import json
import os
import pathlib
import sys

import boto3
import pytest
from moto import mock_aws

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
FIXTURES = ROOT / "fixtures" / "datamall"

os.environ.setdefault("LOG_LEVEL", "WARNING")
from transit import common  # noqa: E402

NAMES = {
    "STOPS_TABLE": "t-stops", "SPEEDS_TABLE": "t-speeds", "META_TABLE": "t-meta",
    "RAW_BUCKET": "t-lake", "REPLAY_BUCKET": "t-replay", "SOURCE_MODE": "replay",
}


def load_page(path):
    from transit.sources import decode_page
    return decode_page(pathlib.Path(path).read_bytes())


@contextlib.contextmanager
def _account(mp):
    """Moto-backed account with the same tables, buckets and queues as infra/."""
    for k, v in {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
                 "AWS_DEFAULT_REGION": "ap-southeast-1"}.items():
        mp.setenv(k, v)
    mp.delenv("AWS_ENDPOINT_URL", raising=False)
    for k, v in NAMES.items():
        mp.setenv(k, v)
    mp.delenv("CORS_ORIGIN", raising=False)
    with mock_aws():
        common.reset_clients()
        ddb = boto3.client("dynamodb")
        ddb.create_table(
            TableName="t-stops", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "code", "AttributeType": "S"},
                                  {"AttributeName": "gh6", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "code", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[{
                "IndexName": "by_geohash",
                "KeySchema": [{"AttributeName": "gh6", "KeyType": "HASH"},
                              {"AttributeName": "code", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "INCLUDE",
                               "NonKeyAttributes": ["description", "road", "lat", "lon"]}}])
        for name, hk, rk in (("t-speeds", "link_id", "captured_at"), ("t-meta", "pk", "sk")):
            ddb.create_table(
                TableName=name, BillingMode="PAY_PER_REQUEST",
                AttributeDefinitions=[{"AttributeName": hk, "AttributeType": "S"},
                                      {"AttributeName": rk, "AttributeType": "S"}],
                KeySchema=[{"AttributeName": hk, "KeyType": "HASH"},
                           {"AttributeName": rk, "KeyType": "RANGE"}])
        s3 = boto3.client("s3")
        for b in ("t-lake", "t-replay"):
            s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": "ap-southeast-1"})
        sqs = boto3.client("sqs")
        url = sqs.create_queue(QueueName="t-pages")["QueueUrl"]
        mp.setenv("QUEUE_URL", url)
        yield {"s3": s3, "sqs": sqs, "ddb": ddb, "queue_url": url}
        common.reset_clients()


def _upload_fixtures(s3):
    for f in FIXTURES.rglob("*"):
        if f.is_file():
            s3.put_object(Bucket="t-replay", Key=f"datamall/{f.relative_to(FIXTURES).as_posix()}",
                          Body=f.read_bytes())


@pytest.fixture
def aws(monkeypatch):
    with _account(monkeypatch) as acct:
        yield acct


@pytest.fixture
def replay(aws):
    """Upload the committed fixtures to the replay bucket, as terraform does."""
    _upload_fixtures(aws["s3"])
    return aws


@pytest.fixture(scope="module")
def loaded():
    """One account per test module with bus stops and two speed snapshots
    pushed through ingest and process (slow on moto, so built once)."""
    from transit import ingest, process
    with pytest.MonkeyPatch.context() as mp, _account(mp) as acct:
        _upload_fixtures(acct["s3"])
        ingest.handler({"dataset": "BusStops"}, None)
        for snap in ("20260929_1735", "20260929_1753"):
            ingest.handler({"dataset": "TrafficSpeedBands", "snapshot": snap}, None)
        msgs = drain(acct["sqs"], acct["queue_url"])
        res = process.handler({"Records": [{"messageId": m["MessageId"], "body": m["Body"]} for m in msgs]}, None)
        assert res["batchItemFailures"] == []
        acct["mp"] = mp
        yield acct


@pytest.fixture(scope="session")
def manifest():
    return json.loads((FIXTURES / "manifest.json").read_text())


def drain(sqs, url):
    out = []
    while True:
        resp = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10)
        msgs = resp.get("Messages", [])
        if not msgs:
            return out
        for m in msgs:
            out.append(m)
            sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])

