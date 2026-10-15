import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from transit.sources import DataMallHttpSource, ReplaySource, SourceError, decode_page


def test_decode_plain_and_gzip():
    body = {"odata.metadata": "x", "value": [{"a": 1}]}
    raw = json.dumps(body).encode()
    assert decode_page(raw) == body
    assert decode_page(gzip.compress(raw)) == body
    with pytest.raises(SourceError):
        decode_page(b'{"value": 3}')


def test_replay_pages_in_order(replay, manifest):
    src = ReplaySource(replay["s3"], "t-replay")
    snaps = src.snapshots()
    assert [s for s, _ in snaps] == sorted(manifest["snapshots"])
    pages = list(src.pages("TrafficSpeedBands", "20260929_1641"))
    assert [i for i, _ in pages] == list(range(len(pages)))
    assert sum(len(b["value"]) for _, b in pages) == manifest["snapshots"]["20260929_1641"]["records"]
    ids = [int(r["LinkID"]) for _, b in pages for r in b["value"]]
    assert ids == sorted(ids)
    stops = list(src.pages("BusStops"))
    assert sum(len(b["value"]) for _, b in stops) == manifest["bus_stops"]
    assert src.bus_stops_snapshot() == "20260926"


def test_replay_unknown(replay):
    src = ReplaySource(replay["s3"], "t-replay")
    with pytest.raises(SourceError):
        list(src.pages("Carparks"))
    with pytest.raises(SourceError):
        list(src.pages("TrafficSpeedBands", "20990101_0000"))


class FakeDataMall(BaseHTTPRequestHandler):
    records = [{"LinkID": str(i)} for i in range(1200)]
    fail_first = 0
    seen = []

    def do_GET(self):
        FakeDataMall.seen.append((self.path, self.headers.get("AccountKey")))
        if FakeDataMall.fail_first > 0:
            FakeDataMall.fail_first -= 1
            self.send_response(503)
            self.end_headers()
            return
        skip = int(parse_qs(urlparse(self.path).query).get("$skip", ["0"])[0])
        body = json.dumps({"value": self.records[skip:skip + 500]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_server():
    FakeDataMall.seen, FakeDataMall.fail_first = [], 0
    srv = HTTPServer(("127.0.0.1", 0), FakeDataMall)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}/ltaodataservice"
    srv.shutdown()
    srv.server_close()


def test_http_pagination_and_key_header(fake_server):
    src = DataMallHttpSource(fake_server, "k-123", backoff=0)
    pages = list(src.pages("TrafficSpeedBands"))
    assert [len(b["value"]) for _, b in pages] == [500, 500, 200]
    assert [p for p, _ in FakeDataMall.seen] == [
        "/ltaodataservice/v4/TrafficSpeedBands?%24skip=0",
        "/ltaodataservice/v4/TrafficSpeedBands?%24skip=500",
        "/ltaodataservice/v4/TrafficSpeedBands?%24skip=1000"]
    assert {k for _, k in FakeDataMall.seen} == {"k-123"}


def test_http_retries_then_succeeds(fake_server):
    FakeDataMall.fail_first = 2
    src = DataMallHttpSource(fake_server, "k", retries=3, backoff=0)
    assert sum(len(b["value"]) for _, b in src.pages("BusStops")) == 1200


def test_http_gives_up(fake_server):
    FakeDataMall.fail_first = 10
    src = DataMallHttpSource(fake_server, "k", retries=3, backoff=0)
    with pytest.raises(SourceError, match="after 3 attempts"):
        list(src.pages("BusStops"))
    assert len(FakeDataMall.seen) == 3


def test_http_requires_tls():
    with pytest.raises(SourceError):
        DataMallHttpSource("http://datamall2.mytransport.sg/ltaodataservice", "k")
