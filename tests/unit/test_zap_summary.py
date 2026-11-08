"""The ZAP report gate: Medium and High alerts fail unless rules.tsv ignores them."""
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("zap_summary", ROOT / "scripts" / "zap_summary.py")
zap_summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zap_summary)


def report(tmp_path, *alerts):
    p = tmp_path / f"zap-{len(list(tmp_path.iterdir()))}.json"
    p.write_text(json.dumps({"site": [{"alerts": [
        {"pluginid": pid, "name": f"rule {pid}", "riskcode": str(risk), "count": "1"} for pid, risk in alerts]}]}))
    return str(p)


def test_low_and_info_pass(tmp_path):
    assert zap_summary.main([report(tmp_path, ("10036", 1), ("10049", 0))]) == 0


def test_medium_or_high_fails(tmp_path):
    assert zap_summary.main([report(tmp_path, ("10049", 0)), report(tmp_path, ("10038", 2))]) == 1
    assert zap_summary.main([report(tmp_path, ("40018", 3))]) == 1


def test_ignored_rule_does_not_fail(tmp_path):
    rules = tmp_path / "rules.tsv"
    rules.write_text("10038\tIGNORE\taccepted, see docs/SECURITY.md\n10020\tWARN\tkept\n")
    assert zap_summary.main(["--rules", str(rules), report(tmp_path, ("10038", 2))]) == 0
    assert zap_summary.main(["--rules", str(rules), report(tmp_path, ("10020", 2))]) == 1


def test_repo_rules_file_is_read():
    assert zap_summary.ignored_rules(ROOT / "zap" / "rules.tsv") == {"10036"}
