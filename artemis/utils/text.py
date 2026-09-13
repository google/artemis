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

"""Text extraction and manipulation utilities."""

from typing import Any


def safe_extract_text(content: Any) -> str:
    """Safely extracts a flat string from potentially complex or nested LLM response content.

    Guarantees returning a string object to prevent AttributeError when calling
    string methods.
    """
    if isinstance(content, str):
        return content
    elif isinstance(content, list):
        return "".join(safe_extract_text(item) for item in content)
    elif isinstance(content, dict):
        if "text" in content:
            return safe_extract_text(content["text"])
        return str(content)
    elif content is None:
        return ""
    else:
        return str(content)
