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

"""Universal Context Compressor for Artemis Flash profile.

Replaces pruned historical screenshots with high-density visual step summaries
when ready, while seamlessly falling back to standard pruned action text if
the background summary is still in flight.
"""

from typing import Any

from langchain_core.messages import BaseMessage

from artemis.agents.flash.summarizer import VisualStepSummarizer
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


def compress_flash_messages(
    messages: list[BaseMessage],
    summarizer: VisualStepSummarizer | None = None,
    prune_history_xml: bool = True,
) -> None:
    """Compresses multi-turn conversation messages in-place.

    Rules:
    1. Keeps the latest screenshot and live UI hierarchy completely intact.
    2. For earlier steps, if a VisualStepSummarizer has completed the step's summary,
       replaces the intermediate image block with the objective summary text.
    3. If the background summary is not ready yet, gracefully falls back to the existing
       behavior (omitting the image block and retaining the original action text).
    4. If prune_history_xml is True, removes heavy outdated UI Element lists from past turns.
    """
    if not messages:
        return

    # 1. Identify the last message containing an active screenshot
    last_img_msg_idx = -1
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        msg_content = getattr(msg, "content", None)
        if isinstance(msg_content, list):
            has_image = any(
                isinstance(block, dict) and block.get("type") in ("image_url", "image")
                for block in msg_content
            )
            if has_image:
                last_img_msg_idx = idx
                break

    # 2. Iterate through historical messages and apply compression/fallback
    tool_step_counter = 0
    for idx in range(len(messages)):
        msg = messages[idx]
        msg_content = getattr(msg, "content", None)
        if not isinstance(msg_content, list):
            continue

        # Keep the latest live screen intact
        if idx == last_img_msg_idx:
            continue

        # Count tool steps strictly corresponding to FlashRunner action turns
        is_tool_msg = getattr(msg, "tool_call_id", None) is not None
        if is_tool_msg:
            tool_step_counter += 1
            current_step_num = tool_step_counter
        else:
            current_step_num = None

        new_blocks: list[dict[str, Any]] = []
        summary_injected = False

        for b in msg_content:
            if not isinstance(b, dict):
                continue
            b_type = b.get("type", "")
            b_text = b.get("text", "")

            # Filter heavy outdated XML hierarchy from past steps to save tokens
            if prune_history_xml and b_type == "text" and "--- UI Element List ---" in b_text:
                continue

            if b_type in ("image_url", "image"):
                # Check if this is a tool execution step and background summarizer has a ready summary
                if (
                    current_step_num
                    and summarizer
                    and summarizer.has_summary(current_step_num)
                    and not summary_injected
                ):
                    summary_text = summarizer.get_summary(current_step_num)
                    if summary_text:
                        new_blocks.append({"type": "text", "text": summary_text})
                        summary_injected = True
                # If summarizer is not ready or initial screen, gracefully omit the image block
                continue

            new_blocks.append(b)

        # Defensive fallback: never leave ToolMessage or HumanMessage content empty
        if not new_blocks:
            new_blocks = [{"type": "text", "text": "Action completed." if is_tool_msg else ""}]

        msg.content = new_blocks
