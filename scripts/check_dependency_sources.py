#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Reject private dependency sources before installing project dependencies.

Uses only the Python 3.12+ standard library so CI can run it before uv sync.
"""

import json
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    errors: list[str] = []

    def check_url(label: str, url: str, host: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != host:
            errors.append(f"{label}: expected an HTTPS URL on {host}")

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    workspace_sources = {
        "artemis": {"editable": "."},
        "artemis-client": {"editable": "packages/artemis-client"},
    }
    for package in lock["package"]:
        label = f"uv.lock: {package['name']}=={package['version']}"
        source = package["source"]
        if source != workspace_sources.get(package["name"]):
            if source != {"registry": "https://pypi.org/simple"}:
                errors.append(f"{label}: expected the public PyPI registry")
        artifacts = list(package.get("wheels", []))
        if "sdist" in package:
            artifacts.append(package["sdist"])
        for artifact in artifacts:
            check_url(f"{label}: artifact", artifact.get("url", ""), "files.pythonhosted.org")

    npm_lock = json.loads((ROOT / "apps/showcase_ui/package-lock.json").read_text(encoding="utf-8"))
    for name, package in npm_lock["packages"].items():
        if "resolved" in package:
            check_url(f"package-lock.json: {name}", package["resolved"], "registry.npmjs.org")

    if errors:
        print("Dependency source check failed:\n" + "\n".join(errors), file=sys.stderr)
        return 1
    print("Dependency sources use public PyPI and npm registries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
