"""Where DataMall pages come from.

ReplaySource reads committed fixture pages that were captured from the real
API and uploaded to a replay bucket. DataMallHttpSource talks to the live API;
it is unit-tested against a local fake server but is not deployed here, since
the stack runs on LocalStack without a DataMall key.
"""
import gzip
import json
import time
import urllib.parse
import urllib.request

DATASETS = ("BusStops", "TrafficSpeedBands")
PAGE_SIZE = 500


class SourceError(Exception):
    pass


def encode_page(body: dict) -> bytes:
    """Stable bytes for raw-object identity and consumer integrity checks."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def decode_page(raw: bytes) -> dict:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    body = json.loads(raw)
    if not isinstance(body, dict) or not isinstance(body.get("value"), list):
        raise SourceError("page has no 'value' list")
    return body


class ReplaySource:
    """Fixture layout: <prefix>/BusStops/page-NNN.json and
    <prefix>/TrafficSpeedBands/<snapshot>/page-NNN.json.gz."""

    def __init__(self, s3, bucket, prefix="datamall"):
        self.s3, self.bucket, self.prefix = s3, bucket, prefix.strip("/")

    def _keys(self, prefix):
        keys, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = self.s3.list_objects_v2(**kw)
            keys += [o["Key"] for o in resp.get("Contents", [])]
            if not resp.get("IsTruncated"):
                return keys
            token = resp["NextContinuationToken"]

    def manifest(self):
        if not hasattr(self, "_manifest"):
            self._manifest = json.loads(self.s3.get_object(
                Bucket=self.bucket, Key=f"{self.prefix}/manifest.json")["Body"].read())
        return self._manifest

    def snapshots(self):
        return sorted(self.manifest()["snapshots"].items())

    def bus_stops_snapshot(self):
        return self.manifest()["bus_stops_captured"]

    def pages(self, dataset, snapshot=None):
        if dataset not in DATASETS:
            raise SourceError(f"unknown dataset {dataset!r}")
        prefix = f"{self.prefix}/{dataset}/" + (f"{snapshot}/" if snapshot else "")
        keys = sorted(k for k in self._keys(prefix) if "/page-" in k)
        if not keys:
            raise SourceError(f"no pages under {prefix}")
        for i, key in enumerate(keys):
            raw = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            yield i, decode_page(raw)


class DataMallHttpSource:
    """Live DataMall client with $skip pagination and bounded retries."""

    def __init__(self, base_url, account_key, timeout=30, retries=3, backoff=0.5, opener=None):
        if not base_url.startswith("https://") and not base_url.startswith("http://127.0.0.1"):
            raise SourceError("DataMall base URL must be https")
        self.base_url, self.key = base_url.rstrip("/"), account_key
        self.timeout, self.retries, self.backoff = timeout, retries, backoff
        self.open = opener or urllib.request.urlopen

    def _get(self, path, skip):
        url = f"{self.base_url}/{path}?" + urllib.parse.urlencode({"$skip": skip})
        req = urllib.request.Request(url, headers={"AccountKey": self.key, "accept": "application/json"})
        last = None
        for attempt in range(self.retries):
            try:
                with self.open(req, timeout=self.timeout) as resp:
                    return decode_page(resp.read())
            except SourceError:
                raise
            except Exception as exc:  # network errors and 5xx
                last = exc
                time.sleep(self.backoff * (2 ** attempt))
        raise SourceError(f"GET {path} skip={skip} failed after {self.retries} attempts: {last}")

    def pages(self, dataset, snapshot=None):
        path = {"BusStops": "BusStops", "TrafficSpeedBands": "v4/TrafficSpeedBands"}.get(dataset)
        if path is None:
            raise SourceError(f"unknown dataset {dataset!r}")
        skip, i = 0, 0
        while True:
            body = self._get(path, skip)
            yield i, body
            if len(body["value"]) < PAGE_SIZE:
                return
            skip += PAGE_SIZE
            i += 1
