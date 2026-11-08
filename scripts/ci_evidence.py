"""Extract the numbers the README quotes from a GitHub Actions log, so they can
be committed under results/ instead of living only in logs and artifacts that
expire.

    gh run view <run_id> --log > log.txt
    python scripts/ci_evidence.py <run_id> <commit_sha> <conclusion> log.txt >> runs.json-lines

Prints one JSON object. Only lines the workflow itself prints are read:
terraform's "Resources: N added", /usr/bin/time's "apply took", the
integration fixture's "ingested N records in S", the mutation summary, the
pytest -rA PASSED/FAILED lines, ZAP's FAIL-NEW/WARN-NEW totals, the ZAP
summary totals and the post-destroy resource count.
"""
import json
import re
import sys

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def extract(text):
    text = ANSI.sub("", text)
    # gh prints "job<TAB>step<TAB>timestamp message"; keep the message only
    lines = [re.sub(r"^[^\t]*\t[^\t]*\t\S+ ?", "", ln) for ln in text.splitlines()]

    def first(pattern, conv=str):
        for ln in lines:
            m = re.search(pattern, ln)
            if m:
                return [conv(g) for g in m.groups()] if len(m.groups()) > 1 else conv(m.group(1))
        return None

    mutants = [tuple(map(int, m.groups())) for ln in lines
               for m in [re.match(r"^(\d+)/(\d+) mutants killed$", ln)] if m]
    zap = [dict(fail_new=int(m.group(1)), warn_new=int(m.group(2))) for ln in lines
           for m in [re.match(r"^FAIL-NEW: (\d+)\s+FAIL-INPROG: \d+\s+WARN-NEW: (\d+)", ln)] if m]
    ingested = first(r"^ingested (\d+) records in ([\d.]+)s$")
    destroy = first(r"^after destroy: functions=(\d+) tables=(\d+)$", int)
    return {
        "resources_added": first(r"Apply complete! Resources: (\d+) added", int),
        "apply_seconds": first(r"^apply took ([\d.]+)s$", float),
        "ingested_records": int(ingested[0]) if ingested else None,
        "drain_seconds": float(ingested[1]) if ingested else None,
        "mutants_killed": list(mutants[0]) if mutants else None,
        "control_mutants_killed": list(mutants[1]) if len(mutants) > 1 else None,
        "iam_check_passed": any(ln.startswith("IAM check passed") for ln in lines),
        "integration_passed": sum(1 for ln in lines if ln.startswith("PASSED tests/integration/")),
        "integration_failed": sum(1 for ln in lines if ln.startswith("FAILED tests/integration/")),
        "zap_scans": zap,
        "zap_totals": first(r"^totals: (.*)$"),
        "after_destroy": {"functions": destroy[0], "tables": destroy[1]} if destroy else None,
    }


def main(run_id, sha, conclusion, log_path):
    with open(log_path, encoding="utf-8", errors="replace") as f:
        ev = extract(f.read())
    print(json.dumps({"run_id": int(run_id), "commit": sha, "conclusion": conclusion, **ev}))


if __name__ == "__main__":
    main(*sys.argv[1:5])
