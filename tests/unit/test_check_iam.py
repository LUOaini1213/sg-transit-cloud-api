import copy
import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location(
    "check_iam", pathlib.Path(__file__).resolve().parents[2] / "scripts" / "check_iam.py")
check_iam = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_iam)

KEY = "arn:aws:kms:ap-southeast-1:000000000000:key/1234"
VIA = {"StringEquals": {"kms:ViaService": ["dynamodb.ap-southeast-1.amazonaws.com"]}}
API_DOC = {"Version": "2012-10-17", "Statement": [
    {"Sid": "Logs", "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
     "Resource": "arn:aws:logs:ap-southeast-1:000000000000:log-group:/aws/lambda/sgtc-dev-api:*"},
    {"Sid": "ReadStops", "Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:Query"],
     "Resource": ["arn:aws:dynamodb:ap-southeast-1:000000000000:table/sgtc-dev-stops",
                  "arn:aws:dynamodb:ap-southeast-1:000000000000:table/sgtc-dev-stops/index/by_geohash"]},
    {"Sid": "DataKey", "Effect": "Allow", "Action": "kms:Decrypt", "Resource": KEY, "Condition": VIA},
    {"Sid": "Tracing", "Effect": "Allow", "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
     "Resource": "*"},
]}


def test_clean_policy_passes():
    assert check_iam.check_policy("api", API_DOC) == []


def mutate(fn):
    doc = copy.deepcopy(API_DOC)
    fn(doc["Statement"])
    return check_iam.check_policy("api", doc)


def test_each_planted_over_permission_is_flagged():
    # (mutation, text the specific rule must report)
    planted = {
        "service wildcard": (lambda s: s[1].update(Action=["dynamodb:*"]), "wildcard action dynamodb:*"),
        "global wildcard": (lambda s: s[1].update(Action="*"), "wildcard action *"),
        "partial wildcard": (lambda s: s[1].update(Action=["dynamodb:Get*"]), "wildcard action dynamodb:Get*"),
        "resource star": (lambda s: s[1].update(Resource="*"), "Resource '*'"),
        "all tables": (lambda s: s[1].update(Resource="arn:aws:dynamodb:ap-southeast-1:000000000000:table/*"),
                       "broad resource"),
        "write on read-only API": (lambda s: s[1]["Action"].append("dynamodb:PutItem"), "write actions"),
        "unexpected service": (lambda s: s.append({"Sid": "X", "Effect": "Allow", "Action": "sqs:SendMessage",
                                                   "Resource": "arn:aws:sqs:ap-southeast-1:0:q"}),
                               "unexpected actions"),
        "NotAction": (lambda s: s.append({"Sid": "N", "Effect": "Allow", "NotAction": "iam:*", "Resource": KEY}),
                      "NotAction"),
        "KMS without ViaService": (lambda s: s[2].pop("Condition"), "kms:ViaService"),
    }
    for name, (fn, expect) in planted.items():
        problems = mutate(fn)
        assert any(expect in p for p in problems), (name, problems)


def test_write_rule_independent_of_expected_set(monkeypatch):
    # an action that is "expected" but is a write must still be refused on the API role
    monkeypatch.setitem(check_iam.EXPECTED, "api", check_iam.EXPECTED["api"] | {"dynamodb:DeleteItem"})
    problems = mutate(lambda s: s[1]["Action"].append("dynamodb:DeleteItem"))
    assert problems == ["api: write actions on a read-only API ['dynamodb:DeleteItem']"]


def test_metric_data_needs_namespace_condition():
    st = {"Sid": "Metrics", "Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*"}
    assert any("namespace" in p for p in check_iam.check_policy("ingest", {"Statement": [st]}))
    st["Condition"] = {"StringEquals": {"cloudwatch:namespace": ["SgTransit"]}}
    assert check_iam.check_policy("ingest", {"Statement": [st]}) == []


def test_deny_statements_are_ignored():
    doc = copy.deepcopy(API_DOC)
    doc["Statement"].append({"Effect": "Deny", "Action": "*", "Resource": "*"})
    assert check_iam.check_policy("api", doc) == []
