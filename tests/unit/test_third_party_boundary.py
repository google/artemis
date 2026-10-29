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

"""Tests for the imports between ``artemis`` and ``third_party/mobile_use``.

The two packages import each other. Every name imported across them, in either
direction, must exist and be public, so renaming or removing it on one side
fails here instead of at runtime.
"""

import ast
import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
THIRD_PARTY_DIRS = [ROOT / "third_party"]
ARTEMIS_DIRS = [ROOT / "artemis", ROOT / "apps" / "admin_console", ROOT / "mcp_server"]


def _py_files(dirs: list[Path]):
    for base in dirs:
        for path in sorted(base.rglob("*.py")):
            if "node_modules" not in path.parts and "__pycache__" not in path.parts:
                yield path


def _cross_imports(dirs: list[Path], target_prefix: str) -> list[tuple[str, str, str | None]]:
    """Return (location, module, name) for imports of ``target_prefix`` modules."""
    found: list[tuple[str, str, str | None]] = []
    for path in _py_files(dirs):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            loc = f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module == target_prefix or node.module.startswith(target_prefix + "."):
                    found.extend((loc, node.module, alias.name) for alias in node.names)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == target_prefix or alias.name.startswith(target_prefix + "."):
                        found.append((loc, alias.name, None))
    return found


# Imports of artemis from third_party.
THIRD_PARTY_TO_ARTEMIS = _cross_imports(THIRD_PARTY_DIRS, "artemis")
# Imports of third_party from artemis.
ARTEMIS_TO_THIRD_PARTY = _cross_imports(ARTEMIS_DIRS, "third_party")


def _is_private(dotted: str) -> bool:
    return any(part.startswith("_") and not part.startswith("__") for part in dotted.split("."))


def test_boundary_scan_found_imports():
    # Fails if the scan finds nothing, e.g. after a directory move.
    assert THIRD_PARTY_TO_ARTEMIS
    assert ARTEMIS_TO_THIRD_PARTY


@pytest.mark.parametrize(
    ("loc", "module", "name"),
    THIRD_PARTY_TO_ARTEMIS + ARTEMIS_TO_THIRD_PARTY,
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_cross_boundary_import_resolves_and_is_public(loc, module, name):
    full = module if name is None else f"{module}.{name}"
    assert not _is_private(full), (
        f"{loc}: imports private '{full}' across the artemis/third_party boundary; "
        "make it public on the defining side."
    )
    mod = importlib.import_module(module)
    if name is None or name == "*":
        return
    if not hasattr(mod, name):
        # `from pkg import submodule`
        importlib.import_module(f"{module}.{name}")


_THIRD_PARTY_HEADER_MARKERS = ("derived from", "originally from", "minitap")


def _leading_comment_block(path: Path) -> str:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            break
        lines.append(line)
    return "\n".join(lines).lower()


def test_first_party_files_carry_no_third_party_header():
    # Code that needs an upstream copyright header belongs in third_party/.
    offenders = [
        str(path.relative_to(ROOT))
        for path in _py_files(ARTEMIS_DIRS + [ROOT / "tests", ROOT / "scripts"])
        if any(marker in _leading_comment_block(path) for marker in _THIRD_PARTY_HEADER_MARKERS)
    ]
    assert not offenders, f"third-party attribution header outside third_party/: {offenders}"
