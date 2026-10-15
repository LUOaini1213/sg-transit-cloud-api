"""Write a plain OpenAPI file for ZAP: point `servers` at the deployed stage
and drop the API Gateway integration extensions.

Usage: python scripts/render_openapi.py <base_url> <out.json>
"""
import json
import pathlib
import sys

import yaml

spec = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] / "infra" / "openapi.yaml").read_text())
spec["servers"] = [{"url": sys.argv[1]}]
for ops in spec["paths"].values():
    for op in ops.values():
        op.pop("x-amazon-apigateway-integration", None)
pathlib.Path(sys.argv[2]).write_text(json.dumps(spec, indent=2))
print(f"{len(spec['paths'])} paths -> {sys.argv[2]}")
