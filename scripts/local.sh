#!/usr/bin/env bash
# Local loop on Linux/WSL with Docker: up | deploy | test | scan | down
# Needs terraform, and `pip install -r requirements.txt terraform-local awscli-local`.
set -euo pipefail
cd "$(dirname "$0")/.."
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=ap-southeast-1
export AWS_ENDPOINT_URL=http://localhost:4566
case "${1:-}" in
  up)
    docker run -d --name sgtc-localstack -p 127.0.0.1:4566:4566 \
      -v /var/run/docker.sock:/var/run/docker.sock -e DYNAMODB_IN_MEMORY=1 localstack/localstack:4.14.0
    until curl -sf localhost:4566/_localstack/health >/dev/null; do sleep 2; done ;;
  deploy)
    (cd infra && tflocal init -input=false && tflocal apply -auto-approve -var-file=test.tfvars \
       && terraform output -json > /tmp/sgtc-outputs.json) ;;
  test)
    python -m pytest -q
    python scripts/check_iam.py /tmp/sgtc-outputs.json
    STACK_OUTPUTS=/tmp/sgtc-outputs.json python -m pytest tests/integration -q ;;
  scan)
    semgrep scan --metrics=off --error --config p/python --config p/terraform --config p/secrets .
    trivy fs --scanners vuln,misconfig,secret --exit-code 1 . ;;
  down)
    (cd infra && tflocal destroy -auto-approve -var-file=test.tfvars) || true
    docker rm -f sgtc-localstack
    docker ps -aq --filter name=sgtc-localstack-lambda | xargs -r docker rm -f ;;
  *) echo "usage: $0 up|deploy|test|scan|down"; exit 2 ;;
esac
