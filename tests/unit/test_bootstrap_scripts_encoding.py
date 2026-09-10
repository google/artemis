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

"""Keep bootstrap scripts ASCII for Windows PowerShell 5.1 compatibility.

BOM-less scripts are decoded using the system ANSI code page. UTF-8 symbols
can become quote delimiters under cp1252 and cause parse errors.
"""

from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
POWERSHELL_SCRIPTS = sorted(SCRIPTS_DIR.glob("*.ps1"))


def test_powershell_scripts_are_discovered():
    assert POWERSHELL_SCRIPTS, f"no .ps1 scripts found under {SCRIPTS_DIR}"


@pytest.mark.parametrize("script", POWERSHELL_SCRIPTS, ids=lambda p: p.name)
def test_powershell_script_is_pure_ascii(script):
    raw = script.read_bytes()
    offending = []
    line_no = 1
    for index, byte in enumerate(raw):
        if byte == 0x0A:
            line_no += 1
        elif byte > 0x7F:
            offending.append((line_no, index, hex(byte)))

    assert not offending, (
        f"{script.name} contains non-ASCII bytes at (line, offset, byte) "
        f"{offending[:10]}; Windows PowerShell 5.1 decodes BOM-less .ps1 files "
        "with the system ANSI code page. Use ASCII status labels."
    )
