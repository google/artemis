"""Fail CI when exception-handler or type-ignore counts exceed the baseline.

Lower the committed baseline when removing existing debt.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (ROOT / "artemis", ROOT / "mcp_server", ROOT / "apps" / "admin_console")
BASELINE_PATH = ROOT / ".quality-baseline.json"


def _exception_names(node: ast.expr | None) -> set[str]:
    if node is None:
        return {"bare-except"}
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, ast.Tuple):
        names: set[str] = set()
        for child in node.elts:
            names.update(_exception_names(child))
        return names
    return set()


def collect_metrics() -> dict[str, int]:
    metrics = {
        "broad_exception_handlers": 0,
        "silent_broad_exception_handlers": 0,
        "type_ignore_comments": 0,
    }
    for source_root in SOURCE_ROOTS:
        for path in source_root.rglob("*.py"):
            text = path.read_text(encoding="utf-8-sig")
            metrics["type_ignore_comments"] += len(re.findall(r"#\s*type:\s*ignore\b", text))
            try:
                tree = ast.parse(text, filename=str(path))
            except SyntaxError as exc:
                raise RuntimeError(f"Cannot inspect invalid Python file {path}: {exc}") from exc
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                names = _exception_names(node.type)
                if not names.intersection({"Exception", "BaseException", "bare-except"}):
                    continue
                metrics["broad_exception_handlers"] += 1
                if node.body and all(isinstance(statement, ast.Pass) for statement in node.body):
                    metrics["silent_broad_exception_handlers"] += 1
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print-current", action="store_true", help="print metrics as JSON")
    args = parser.parse_args()

    current = collect_metrics()
    if args.print_current:
        print(json.dumps(current, indent=2, sort_keys=True))
        return 0

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    regressions = {
        name: (baseline[name], value)
        for name, value in current.items()
        if value > int(baseline[name])
    }
    if regressions:
        for name, (allowed, actual) in regressions.items():
            print(f"quality regression: {name} increased from {allowed} to {actual}")
        return 1

    print("quality ratchet passed")
    for name, value in current.items():
        improvement = int(baseline[name]) - value
        suffix = f" ({improvement} below baseline)" if improvement else ""
        print(f"  {name}: {value}{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
