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

"""File and configuration loading utilities for ARTEMIS."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import IO, Any


def strip_json_comments(text: str) -> str:
    """Removes single-line (//) and multi-line (/* ... */) comments from JSONC text.

    Args:
        text: Raw JSON with comments string.

    Returns:
        Clean JSON string with comments stripped.
    """
    pattern = r"//.*?$|/\*.*?\*/"
    return re.sub(pattern, "", text, flags=re.MULTILINE | re.DOTALL)


def load_jsonc(source: str | Path | IO[str] | IO[bytes]) -> dict[str, Any]:
    """Loads and parses a JSONC (JSON with Comments) document.

    Args:
        source: File-like object, Path, or string path to parse.

    Returns:
        Parsed dictionary.
    """
    if isinstance(source, (str, Path)):
        raw_content = Path(source).read_text(encoding="utf-8")
    else:
        content = source.read()
        raw_content = content.decode("utf-8") if isinstance(content, bytes) else content

    cleaned = strip_json_comments(raw_content)
    return json.loads(cleaned)
