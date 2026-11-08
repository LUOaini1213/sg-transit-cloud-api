"""Print ZAP findings by risk from one or more JSON reports (-J output) and
fail on any Medium or High alert that zap/rules.tsv does not mark IGNORE.

ZAP's own exit code already fails on FAIL and WARN rules; this is a second
gate on the reports themselves, so a scan run with -I, or an alert raised by a
rule left at its default level, cannot pass silently.

Usage: python scripts/zap_summary.py [--rules zap/rules.tsv] report.json [...]
"""
import argparse
import json
import pathlib
import sys
from collections import Counter

RISK = {"3": "High", "2": "Medium", "1": "Low", "0": "Informational"}
GATE = 2  # Medium and above
ROOT = pathlib.Path(__file__).resolve().parents[1]


def ignored_rules(path):
    """Plugin ids marked IGNORE in a ZAP rules file (tab separated: id, action, note)."""
    out = set()
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1].strip() == "IGNORE":
            out.add(parts[0].strip())
    return out


def summarise(reports, ignore):
    """Returns (counts by risk, alerts that break the gate)."""
    total, blocking = Counter(), []
    for path in reports:
        rep = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        print(f"== {path}")
        for site in rep.get("site", []):
            for a in site.get("alerts", []):
                code = str(a.get("riskcode"))
                risk = RISK.get(code, code)
                total[risk] += 1
                n = a.get("count", len(a.get("instances", [])))
                print(f"  [{risk}] {a['pluginid']} {a['name']} ({n} instances)")
                if code.isdigit() and int(code) >= GATE and str(a["pluginid"]) not in ignore:
                    blocking.append(f"{a['pluginid']} {a['name']} [{risk}] in {path}")
    return total, blocking


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", default=str(ROOT / "zap" / "rules.tsv"))
    ap.add_argument("reports", nargs="+")
    args = ap.parse_args(argv)
    total, blocking = summarise(args.reports, ignored_rules(args.rules))
    print("totals:", dict(total) or "no alerts")
    for b in blocking:
        print("BLOCKING:", b)
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
