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

"""Structured output and thinking tag parser."""

import re
from artemis.llm.schemas import ThinkingBlock


def extract_thinking_tags(text: str) -> tuple[str, list[ThinkingBlock]]:
    """Extracts content inside <thought> or <thinking> tags from LLM response."""
    thinking_blocks: list[ThinkingBlock] = []

    pattern = r"<(?:thought|thinking)>(.*?)</(?:thought|thinking)>"
    matches = re.findall(pattern, text, flags=re.DOTALL)

    for m in matches:
        thinking_blocks.append(ThinkingBlock(content=m.strip(), is_native=False))

    cleaned_text = re.sub(pattern, "", text, flags=re.DOTALL).strip()
    return cleaned_text, thinking_blocks
