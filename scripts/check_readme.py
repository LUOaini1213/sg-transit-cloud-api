"""Check every number in the README's first screen and Results section against
something committed: the fixtures, the code, the test suite, docs/SECURITY.md
or the CI evidence in results/.

Usage: python scripts/check_readme.py --check     (exit 1 on any mismatch)
       python scripts/check_readme.py              (print the table, exit 0)
"""
import argparse
import functools
import importlib.util
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "datamall"
EVIDENCE = ROOT / "results" / "ci-2026-10-16"
sys.path.insert(0, str(ROOT / "src"))

LATENCY_ROWS = {"/health": "health", "/v1/stops/{code}": "stop", "/v1/stops/nearby": "nearby_300m",
                "/v1/roads/{id}/speeds": "road_speeds", "/v1/snapshots/{id}/congestion": "congestion"}


def n(x):
    """4,126 -> '4,126' formatting as the README writes numbers."""
    return f"{x:,}"


def fixture_counts():
    from transit.process import process_page
    from transit.sources import decode_page
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    stops_rejected = 0
    for f in sorted((FIXTURES / "BusStops").glob("page-*.json")):
        stops_rejected += process_page({"dataset": "BusStops", "snapshot": "x", "captured_at": "x"},
                                       decode_page(f.read_bytes()))[2]["rejected"]
    speed_rejected = {}
    for snap in manifest["snapshots"]:
        speed_rejected[snap] = sum(
            process_page({"dataset": "TrafficSpeedBands", "snapshot": snap, "captured_at": "x"},
                         decode_page(f.read_bytes()))[2]["rejected"]
            for f in sorted((FIXTURES / "TrafficSpeedBands" / snap).glob("page-*.json.gz")))
    return manifest, stops_rejected, speed_rejected


@functools.lru_cache(maxsize=None)
def collected(path):
    r = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts=",
                        "-p", "no:cacheprovider", path], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"pytest collection failed for {path}:\n{r.stdout[-2000:]}{r.stderr[-2000:]}")
    return sum(1 for ln in r.stdout.splitlines() if "::" in ln)


def mutant_count():
    spec = importlib.util.spec_from_file_location("mutate", ROOT / "scripts" / "mutate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return len(mod.MUTANTS)


def expected_values():
    """(label, regex with one group per value, expected tuple of strings)."""
    manifest, stops_rejected, speed_rejected = fixture_counts()
    snaps = manifest["snapshots"]
    per_snap = {v["records"] for v in snaps.values()}
    assert len(per_snap) == 1, "snapshots differ in size; the README assumes one link count"
    links = per_snap.pop()
    total = manifest["bus_stops"] + len(snaps) * links
    times = sorted(s[-4:] for s in snaps)
    rejected = sum(speed_rejected.values())
    rej_per_snap = set(speed_rejected.values())
    lat0, lat1, lon0, lon1 = manifest["bbox_links"]
    slat0, slat1, slon0, slon1 = manifest["bbox_stops"]

    runs = json.loads((EVIDENCE / "runs.json").read_text(encoding="utf-8"))
    latency = json.loads((EVIDENCE / "latency.json").read_text(encoding="utf-8"))
    ok_runs = [r for r in runs if r["conclusion"] == "success"]
    resources = {r["resources_added"] for r in runs}
    assert len(resources) == 1, f"runs disagree on resource count: {resources}"
    apply_s = [r["apply_seconds"] for r in runs]
    drain_s = [r["drain_seconds"] for r in runs]
    assert all(r["ingested_records"] == total for r in runs), "a run ingested a different record count"
    zap_clean = all(s["fail_new"] == 0 and s["warn_new"] == 0 for r in ok_runs for s in r["zap_scans"])
    latency_run = next(r for r in runs if r["run_id"] == 38839029755)

    sec = (ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8")
    semgrep_before = re.search(r"\| Semgrep [^|]*\|[^|]*\| (\d+) \(", sec).group(1)
    trivy = re.search(r"\| Trivy [^|]*\|[^|]*\| (\d+) misconfigurations \((\d+) high, (\d+) medium, (\d+) low\)", sec)
    accepted = sec[sec.index("## Accepted findings"):sec.index("## ZAP")]
    n_accepted = sum(1 for ln in accepted.splitlines() if ln.startswith("| AWS-"))

    checks = [
        ("intro summary", r"`terraform apply` creates (\d+) resources, ([\d,]+) replayed records go through "
                          r"the pipeline \((\d+) rejected by validation\), (\d+) integration tests pass",
         (str(next(iter(resources))), n(total), str(rejected), str(collected("tests/integration")))),
        ("bus stops (first screen)", r"geographic subset: ([\d,]+) bus stops", (n(manifest["bus_stops"]),)),
        ("stop bbox", r"stops between ([\d.]+)-([\d.]+) N and ([\d.]+)-([\d.]+) E\)",
         tuple(map(str, (slat0, slat1, slon0, slon1)))),
        ("snapshot count", r"and (\w+) speed-band snapshots", ({7: "seven"}.get(len(snaps), str(len(snaps))),)),
        ("links per snapshot", r"of the ([\d,]+) road links", (n(links),)),
        ("link bbox", r"central-area box \(([\d.]+)-([\d.]+) N, ([\d.]+)-([\d.]+) E\)",
         tuple(map(str, (lat0, lat1, lon0, lon1)))),
        ("capture window", r"(\d\d):(\d\d) to (\d\d):(\d\d)\)",
         (times[0][:2], times[0][2:], times[-1][:2], times[-1][2:])),
        ("resources", r"\| Resources created by `terraform apply` \| (\d+), in (\d+)-(\d+) s",
         (str(resources.pop()), str(round(min(apply_s))), str(round(max(apply_s))))),
        ("records through the pipeline",
         r"\| Records through the pipeline \| ([\d,]+) \(([\d,]+) stops \+ (\d+) x ([\d,]+) speed readings\), "
         r"queue drained in (\d+)-(\d+) s",
         (n(total), n(manifest["bus_stops"]), str(len(snaps)), n(links),
          str(round(min(drain_s))), str(round(max(drain_s))))),
        ("rejected", r"\| Records rejected by validation \| (\d+) \((\d+) links per snapshot with no reading\); "
                     r"all ([\d,]+) stops pass",
         (str(rejected), str(rej_per_snap.pop()) if len(rej_per_snap) == 1 else "varies",
          n(manifest["bus_stops"]) if stops_rejected == 0 else "not all")),
        ("unit tests", r"\| Unit tests \| (\d+) ", (str(collected("tests/unit")),)),
        ("integration tests", r"\| Integration tests against the deployed stack \| (\d+) ",
         (str(collected("tests/integration")),)),
        ("integration tests passed in the cited runs", r"\| Integration tests against the deployed stack \| (\d+) ",
         tuple({str(r["integration_passed"]) for r in ok_runs})),
        ("mutants", r"\| Planted bugs caught \(mutation check\) \| (\d+) of (\d+)",
         (str(mutant_count()), str(mutant_count()))),
        ("semgrep", r"\| Semgrep findings \| (\d+) before, 0 after", (semgrep_before,)),
        ("trivy", r"\| Trivy findings \| (\d+) before \((\d+) high, (\d+) medium, (\d+) low\), 0 after, "
                  r"(\d+) accepted", trivy.groups() + (str(n_accepted),)),
        ("zap", r"\| ZAP baseline \+ OpenAPI active scan \| (0) fail, (0) warn",
         ("0", "0") if zap_clean else ("nonzero", "nonzero")),
        ("latency source", r"\(CI run (\d+), commit ([0-9a-f]{7})",
         (str(latency_run["run_id"]), latency_run["commit"][:7])),
    ]
    for path, key in LATENCY_ROWS.items():
        row = latency[key]
        checks.append((f"latency {path}",
                       r"\| `" + re.escape(path) + r"`[^|]*\| ([\d.]+) ms \| ([\d.]+) ms \| ([\d.]+) ms \| (\d+)/(\d+) \|",
                       tuple(f"{row[k]:.1f}" for k in ("p50_ms", "p95_ms", "p99_ms")) + (str(row["errors"]), str(row["n"]))))
    return checks


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--readme", default=str(ROOT / "README.md"))
    args = ap.parse_args(argv)
    # one line per table row, but prose may wrap anywhere
    readme = re.sub(r"[ \t]*\n[ \t]*(?!\|)", " ", pathlib.Path(args.readme).read_text(encoding="utf-8"))
    bad = 0
    for label, pattern, want in expected_values():
        m = re.search(pattern, readme)
        got = m.groups() if m else None
        ok = got is not None and tuple(got) == tuple(want)
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'}  {label}: README {got if m else 'pattern not found'}, evidence {tuple(want)}")
    print(f"\n{bad} mismatch(es)")
    return 1 if (bad and args.check) else 0


if __name__ == "__main__":
    sys.exit(main())
