"""Print ZAP findings by risk from one or more JSON reports (-J output)."""
import json
import sys
from collections import Counter

RISK = {"3": "High", "2": "Medium", "1": "Low", "0": "Informational"}

total = Counter()
for path in sys.argv[1:]:
    rep = json.load(open(path))
    print(f"== {path}")
    for site in rep.get("site", []):
        for a in site.get("alerts", []):
            risk = RISK.get(str(a.get("riskcode")), a.get("riskcode"))
            total[risk] += 1
            print(f"  [{risk}] {a['pluginid']} {a['name']} ({a.get('count', len(a.get('instances', [])))} instances)")
print("totals:", dict(total) or "no alerts")
