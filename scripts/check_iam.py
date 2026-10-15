"""Least-privilege check for the deployed Lambda roles.

LocalStack (community) does not enforce IAM, so a role that is too broad
would still "work" there. This script reads the inline policies back from the
deployed stack and fails on:
  * wildcard actions ("*" or "service:*"), NotAction or NotResource
  * Resource "*" outside a short allow-list of actions that have no
    resource ARN, and PutMetricData without a namespace condition
  * any action that is not in the expected set for that role
  * any write action on the API role (the API is read-only)

Usage: python scripts/check_iam.py <outputs.json>   (terraform output -json)
"""
import json
import sys

STAR_OK = {"cloudwatch:PutMetricData", "xray:PutTraceSegments", "xray:PutTelemetryRecords"}
EXPECTED = {
    "ingest": {"logs:CreateLogStream", "logs:PutLogEvents", "s3:ListBucket", "s3:GetObject", "s3:PutObject",
               "sqs:SendMessage", "dynamodb:GetItem", "dynamodb:PutItem", "cloudwatch:PutMetricData",
               "kms:Decrypt", "kms:GenerateDataKey", "xray:PutTraceSegments", "xray:PutTelemetryRecords"},
    "process": {"logs:CreateLogStream", "logs:PutLogEvents", "sqs:ReceiveMessage", "sqs:DeleteMessage",
                "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility", "s3:GetObject", "s3:PutObject",
                "dynamodb:PutItem", "dynamodb:BatchWriteItem", "cloudwatch:PutMetricData",
                "kms:Decrypt", "kms:GenerateDataKey", "xray:PutTraceSegments", "xray:PutTelemetryRecords"},
    "api": {"logs:CreateLogStream", "logs:PutLogEvents", "dynamodb:GetItem", "dynamodb:Query",
            "kms:Decrypt", "xray:PutTraceSegments", "xray:PutTelemetryRecords"},
}
WRITE_PREFIXES = ("Put", "Delete", "Update", "BatchWrite", "Send", "Create", "Generate")


def _list(v):
    return v if isinstance(v, list) else [v]


def check_policy(role, doc):
    """Return a list of problems for one role's policy document."""
    problems, actions = [], set()
    for st in _list(doc.get("Statement", [])):
        sid = st.get("Sid", "?")
        if st.get("Effect") != "Allow":
            continue
        if "NotAction" in st or "NotResource" in st:
            problems.append(f"{role}/{sid}: NotAction/NotResource")
        acts = _list(st.get("Action", []))
        res = _list(st.get("Resource", []))
        for a in acts:
            if a == "*" or a.endswith(":*") or "*" in a:
                problems.append(f"{role}/{sid}: wildcard action {a}")
        actions.update(acts)
        if any(r == "*" for r in res):
            extra = set(acts) - STAR_OK
            if extra:
                problems.append(f"{role}/{sid}: Resource '*' for {sorted(extra)}")
            if "cloudwatch:PutMetricData" in acts and "cloudwatch:namespace" not in json.dumps(st.get("Condition", {})):
                problems.append(f"{role}/{sid}: PutMetricData without namespace condition")
        for r in res:
            if r != "*" and (r.endswith(":*:*") or r.endswith(":table/*") or r in ("arn:aws:s3:::*",)):
                problems.append(f"{role}/{sid}: broad resource {r}")
        if any(a.startswith("kms:") for a in acts) and "kms:ViaService" not in json.dumps(st.get("Condition", {})):
            problems.append(f"{role}/{sid}: KMS use without kms:ViaService condition")
    unexpected = actions - EXPECTED.get(role, set())
    if unexpected:
        problems.append(f"{role}: unexpected actions {sorted(unexpected)}")
    if role == "api":
        writes = [a for a in actions if (a == "*" or a.split(":")[-1].startswith(WRITE_PREFIXES))
                  and not a.startswith(("logs:", "xray:"))]
        if writes:
            problems.append(f"api: write actions on a read-only API {sorted(writes)}")
    return problems


def fetch_policies(outputs, iam):
    roles = outputs["roles"]["value"]
    docs = {}
    for fn, name in roles.items():
        for pname in iam.list_role_policies(RoleName=name)["PolicyNames"]:
            doc = iam.get_role_policy(RoleName=name, PolicyName=pname)["PolicyDocument"]
            docs[fn] = json.loads(doc) if isinstance(doc, str) else doc
        attached = iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]
        if attached:
            docs.setdefault(fn, {"Statement": []})
            docs[fn].setdefault("_attached", [a["PolicyArn"] for a in attached])
    return docs


def main(path):
    import boto3
    outputs = json.load(open(path))
    docs = fetch_policies(outputs, boto3.client("iam"))
    problems = []
    for fn in EXPECTED:
        if fn not in docs:
            problems.append(f"{fn}: no inline policy found")
            continue
        if docs[fn].get("_attached"):
            problems.append(f"{fn}: managed policies attached {docs[fn]['_attached']}")
        problems += check_policy(fn, docs[fn])
    for fn, doc in docs.items():
        n = sum(len(_list(s.get("Action", []))) for s in _list(doc.get("Statement", [])))
        print(f"{fn}: {len(_list(doc.get('Statement', [])))} statements, {n} actions")
    if problems:
        print("IAM check FAILED:\n  " + "\n  ".join(problems))
        return 1
    print("IAM check passed: no wildcards, every action expected, API role read-only")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
