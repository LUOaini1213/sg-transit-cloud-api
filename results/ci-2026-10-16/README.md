# CI evidence, 2026-10-16

The README's Results numbers come from three GitHub Actions runs of
`.github/workflows/ci.yml` on `main`. Their logs and artifacts expire (the
artifacts on 2027-01-14), so the numbers are kept here.

| Run | Commit | Conclusion | Used for |
|---|---|---|---|
| 38836029252 | 7d9bd87 | failure | apply time, resources, pipeline drain time. The deploy, ingest and destroy steps completed; the job failed later on an integration assertion about gateway-response headers, which LocalStack stores but does not apply (fixed in b0c59ba by checking the stored configuration). |
| 38839029755 | b0c59ba | success | the same, plus integration tests, ZAP and the latency table |
| 38840962290 | 2f6a1e9 | success | the same as 38839029755 except latency |

Files:

- `runs.json`: one object per run, produced by
  `gh run view <run> --log > log.txt` and
  `python scripts/ci_evidence.py <run> <commit> <conclusion> log.txt`.
  It holds the lines the workflow prints: terraform's resource count,
  `apply took`, `ingested N records in S`, the mutation summary, the
  integration tests' PASSED/FAILED lines, ZAP's FAIL-NEW/WARN-NEW counts and
  the post-destroy check.
- `latency.json`: `reports/latency.json` from the `deploy-reports` artifact of
  run 38839029755, unchanged (SHA-256
  `5ab1d652fd283231739f0417d8b7ed1d8bcf754c5d110fbcd170adc2baefd217`).

`python scripts/check_readme.py --check` compares the README against these
files. These runs predate the snapshot `status` field. A later CI run of the
current code can be added here the same way.
