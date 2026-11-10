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

"""The two build scripts must agree, because only one of them can be tested here."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASH = REPO_ROOT / "packaging" / "build.sh"
POWERSHELL = REPO_ROOT / "packaging" / "build.ps1"

SHARED_FLAGS = (
    "--include-package",
    "--include-package-data",
    "--include-data-file",
    "--include-data-files",
    "--nofollow-import-to",
)


def _flags(script: Path) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {flag: set() for flag in SHARED_FLAGS}
    for line in script.read_text(encoding="utf-8").splitlines():
        stripped = line.strip().lstrip('"').lstrip("-")
        candidate = "--" + stripped if not stripped.startswith("-") else stripped
        for flag in SHARED_FLAGS:
            if candidate.startswith(flag + "="):
                found[flag].add(candidate.split("=", 1)[1].split()[0].strip('"`'))
    return found


def test_both_build_scripts_exist():
    assert BASH.is_file()
    assert POWERSHELL.is_file()


def test_both_scripts_pass_the_same_nuitka_includes():
    from_bash = _flags(BASH)
    from_powershell = _flags(POWERSHELL)

    for flag in SHARED_FLAGS:
        assert from_bash[flag] == from_powershell[flag], flag


def test_both_scripts_collect_the_licenses():
    for script in (BASH, POWERSHELL):
        assert "collect_licenses.py" in script.read_text(encoding="utf-8"), script


def test_the_executable_file_stays_at_the_dist_root():
    from_bash = BASH.read_text(encoding="utf-8")
    from_powershell = POWERSHELL.read_text(encoding="utf-8")

    assert "mv " not in from_bash
    assert "Move-Item" not in from_powershell


def test_macos_links_the_public_name_instead_of_moving_the_binary():
    from_bash = BASH.read_text(encoding="utf-8")

    assert "--output-filename=artemis-app" in from_bash
    assert 'ln -sf ../artemis-app "$DIST/bin/artemis"' in from_bash


def test_windows_names_the_binary_artemis():
    from_powershell = POWERSHELL.read_text(encoding="utf-8")

    assert "--output-filename=artemis " in from_powershell or "--output-filename=artemis`" in from_powershell


def test_both_scripts_wipe_the_previous_dist_directory():
    assert "rm -rf" in BASH.read_text(encoding="utf-8")
    assert "Remove-Item" in POWERSHELL.read_text(encoding="utf-8")


def test_the_entry_point_is_shared():
    assert "artemis_cli.py" in BASH.read_text(encoding="utf-8")
    assert "artemis_cli.py" in POWERSHELL.read_text(encoding="utf-8")


def test_both_scripts_let_the_caller_lower_the_parallel_jobs():
    assert "${JOBS:-8}" in BASH.read_text(encoding="utf-8")
    assert "$env:JOBS" in POWERSHELL.read_text(encoding="utf-8")
