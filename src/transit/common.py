"""Shared helpers: config from environment, structured logging, metrics."""
import json
import logging
import os
import sys
import time

import boto3

NAMESPACE = "SgTransit"

_log = logging.getLogger("transit")
if not _log.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(message)s"))
    _log.addHandler(h)
_log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
_log.propagate = False


def log(event, **fields):
    """One JSON object per line so CloudWatch Logs Insights can query fields."""
    _log.info(json.dumps({"ts": round(time.time(), 3), "event": event, **fields}, default=str))


def env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing environment variable {name}")
    return value


_clients = {}


def client(service):
    if service not in _clients:
        _clients[service] = boto3.client(service)
    return _clients[service]


def resource(service):
    key = f"resource:{service}"
    if key not in _clients:
        _clients[key] = boto3.resource(service)
    return _clients[key]


def reset_clients():
    _clients.clear()


def put_metrics(dataset, **values):
    data = [{"MetricName": k, "Value": float(v), "Unit": "Count",
             "Dimensions": [{"Name": "Dataset", "Value": dataset}]} for k, v in values.items()]
    try:
        client("cloudwatch").put_metric_data(Namespace=NAMESPACE, MetricData=data)
    except Exception as exc:  # metrics must never fail the pipeline
        log("metric_put_failed", error=str(exc))
