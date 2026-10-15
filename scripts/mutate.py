"""Planted-bug check for the unit tests.

Each mutant is one small source edit that a reviewer would call a bug. For
every mutant the source tree is copied, the edit applied, and the unit tests
run; the mutant is "killed" if the tests fail. The run fails if any mutant
survives, and also if a mutant's search text is missing (so the list cannot
silently go stale).

`--control` adds an equivalent mutant (a comment-only change) that must
survive. It shows the harness reports a survivor and exits non-zero, i.e.
the check itself can fail.

Usage: python scripts/mutate.py [--control] [--only NAME] [--jobs N]
"""
import argparse
import concurrent.futures as cf
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]

MUTANTS = [
    # (name, file, original text, mutated text)
    ("stop-code-4-digits", "src/transit/quality.py", r'STOP_CODE = re.compile(r"^\d{5}$")', r'STOP_CODE = re.compile(r"^\d{4,5}$")'),
    ("band-0-valid", "src/transit/quality.py", "VALID_BANDS = range(1, 9)", "VALID_BANDS = range(0, 9)"),
    ("min-gt-max-allowed", "src/transit/quality.py", "elif lo < 0 or lo > hi:", "elif lo < 0:"),
    ("cross-border-rejected", "src/transit/quality.py", 'warnings.append("outside_sg_bbox")\n    desc', 'reasons.append("outside_sg_bbox")\n    desc'),
    ("no-reading-kept", "src/transit/quality.py", 'reasons.append("no_reading")', 'pass'),
    ("geohash-bit-width", "src/transit/geohash.py", "if bits == 5:", "if bits == 4:"),
    ("neighbours-one-sided", "src/transit/geohash.py", "for j in (-1, 0, 1):", "for j in (0, 1):"),
    ("radius-cap-raised", "src/transit/api.py", "MAX_RADIUS_M = 500", "MAX_RADIUS_M = 5000"),
    ("slow-band-set", "src/transit/api.py", 'SLOW_BANDS = {"1", "2"}', 'SLOW_BANDS = {"1"}'),
    ("nearby-unsorted", "src/transit/api.py", 'found.sort(key=lambda s: (s["distance_m"], s["code"]))', 'pass'),
    ("nosniff-dropped", "src/transit/api.py", '    "X-Content-Type-Options": "nosniff",\n', ''),
    ("cors-reflects-any", "src/transit/api.py", "if allowed and origin == allowed:", "if allowed and origin:"),
    ("error-detail-leak", "src/transit/api.py", '{"error": "internal error", "request_id": rid}', '{"error": str(exc), "request_id": rid}'),
    ("speeds-oldest-first", "src/transit/api.py", "ScanIndexForward=False", "ScanIndexForward=True"),
    ("raw-zone-check-off", "src/transit/process.py", "if not str(msg[\"raw_key\"]).startswith(prefix) or \"..\" in msg[\"raw_key\"]:", "if False:"),
    ("duplicates-kept", "src/transit/process.py", "if key in seen:", "if False:"),
    ("deadline-check-off", "src/transit/process.py", "if context is not None and context.get_remaining_time_in_millis() < SAFETY_MS:", "if False:"),
    ("failures-swallowed", "src/transit/process.py", 'failures.append({"itemIdentifier": record.get("messageId")})', 'pass'),
    ("cursor-no-wrap", "src/transit/ingest.py", "(names.index(item[\"snapshot\"]) + 1) % len(names)", "min(names.index(item[\"snapshot\"]) + 1, len(names) - 1)"),
    ("sqs-batch-11", "src/transit/ingest.py", "for i in range(0, len(messages), 10):\n        chunk = messages[i:i + 10]", "for i in range(0, len(messages), 11):\n        chunk = messages[i:i + 11]"),
    ("no-retries", "src/transit/sources.py", "for attempt in range(self.retries):", "for attempt in range(1):"),
    ("pagination-off-by-one", "src/transit/sources.py", "if len(body[\"value\"]) < PAGE_SIZE:", "if len(body[\"value\"]) <= PAGE_SIZE:"),
    ("tls-not-enforced", "src/transit/sources.py", 'if not base_url.startswith("https://") and', 'if False and'),
    ("iam-wildcard-blind", "scripts/check_iam.py", 'if a == "*" or a.endswith(":*") or "*" in a:', 'if a == "*":'),
    ("iam-readonly-blind", "scripts/check_iam.py", 'if role == "api":', 'if role == "none":'),
]
CONTROL = ("control-comment-only", "src/transit/api.py", "MAX_RADIUS_M = 500", "MAX_RADIUS_M = 500  # unchanged")


def run_mutant(m, pytest_args):
    name, rel, old, new = m
    with tempfile.TemporaryDirectory(prefix=f"mut-{name}-") as tmp:
        tmp = pathlib.Path(tmp)
        for part in ("src", "tests", "fixtures", "scripts", "pytest.ini"):
            src = ROOT / part
            (shutil.copytree if src.is_dir() else shutil.copy)(src, tmp / part)
        target = tmp / rel
        text = target.read_text()
        if text.count(old) != 1:
            return name, "STALE", 0.0
        target.write_text(text.replace(old, new))
        t0 = time.time()
        r = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *pytest_args],
                           cwd=tmp, capture_output=True, text=True)
        return name, ("killed" if r.returncode != 0 else "SURVIVED"), time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--control", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args()
    mutants = [m for m in MUTANTS if not args.only or m[0] == args.only]
    if args.control:
        mutants = [CONTROL] + mutants

    base = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=ROOT,
                          capture_output=True, text=True)
    if base.returncode != 0:
        print(base.stdout[-2000:])
        print("baseline tests fail; fix them before mutation testing")
        return 2

    with cf.ThreadPoolExecutor(args.jobs) as ex:
        results = list(ex.map(lambda m: run_mutant(m, []), mutants))
    width = max(len(r[0]) for r in results)
    for name, status, secs in results:
        print(f"{name:<{width}}  {status:<8}  {secs:6.1f}s")
    killed = sum(1 for _, s, _ in results if s == "killed")
    bad = [r for r in results if r[1] != "killed"]
    print(f"\n{killed}/{len(results)} mutants killed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
