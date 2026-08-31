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

Replaces historical screenshots with high-density visual step summaries only
after they are ready. Screenshots remain in context while summarization or
retry is still in flight.
"""

from typing import Any

from langchain_core.messages import BaseMessage

from artemis.agents.flash.summarizer import VisualStepSummarizer
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

_HISTORY_SUMMARY_PREFIX = "--- Historical Visual Transition ---\n"
_UI_LIST_MARKER = "--- UI Element List ---"


def _without_historical_ui_list(text: str) -> str:
    """Remove a historical UI list without discarding text before its marker."""
    marker_index = text.find(_UI_LIST_MARKER)
    if marker_index < 0:
        return text
    return text[:marker_index].rstrip()


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
    3. If the background summary is not ready yet, retains the original image until
       a successful summary can replace it.
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

    # 2. Iterate through historical messages and apply compression/fallback.
    # ``tool_call_id`` is the stable identity of an action result. The legacy
    # ordinal is retained only for summaries produced by older callers/tests.
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
        tool_call_id = getattr(msg, "tool_call_id", None)
        is_tool_msg = tool_call_id is not None
        if is_tool_msg:
            tool_step_counter += 1
            legacy_step_num = tool_step_counter
        else:
            legacy_step_num = None

        summary_text = None
        summary_pending = False
        if summarizer and is_tool_msg:
            # New FlashRunner instances key summaries by tool_call_id, so generic
            # tools, no-tool turns, and parallel tool calls cannot shift memories.
            action_key = str(tool_call_id)
            summary_text = summarizer.get_summary(action_key)
            summary_pending = summarizer.is_pending(action_key)
            if (
                summary_text is None
                and not summarizer.has_job(action_key)
                and legacy_step_num is not None
            ):
                summary_text = summarizer.get_summary(legacy_step_num)
                summary_pending = summarizer.is_pending(legacy_step_num)

        new_blocks: list[Any] = []

        for b in msg_content:
            if not isinstance(b, dict):
                # LangChain permits string parts in multimodal content lists.
                # They are lightweight historical evidence and must not vanish.
                new_blocks.append(b)
                continue
            b_type = b.get("type", "")
            b_text = b.get("text", "")

            if b_type == "text" and b_text.startswith(_HISTORY_SUMMARY_PREFIX):
                # Rebuild this generated block on every pass. This makes the
                # compressor idempotent and lets a late summary replace itself.
                continue

            # Filter heavy outdated XML hierarchy from past steps to save tokens.
            # Some adapters combine action text and the hierarchy in one block;
            # retain the useful prefix instead of dropping the entire block.
            if prune_history_xml and b_type == "text" and _UI_LIST_MARKER in b_text:
                retained_text = _without_historical_ui_list(b_text)
                if retained_text:
                    retained_block = dict(b)
                    retained_block["text"] = retained_text
                    new_blocks.append(retained_block)
                continue

            if b_type in ("image_url", "image"):
                # A submitted image remains visible until its summary is ready.
                # This temporarily favors context size over losing visual evidence.
                if summary_pending:
                    new_blocks.append(b)
                continue

            new_blocks.append(b)

        # Do not tie injection to the presence of an image: the image is normally
        # pruned before the background summary finishes. A stable tool-call key lets
        # a later compression pass backfill the summary into the correct result.
        if summary_text:
            new_blocks.append(
                {
                    "type": "text",
                    "text": f"{_HISTORY_SUMMARY_PREFIX}{summary_text}",
                }
            )

        # Defensive fallback: never leave ToolMessage or HumanMessage content empty
        if not new_blocks:
            new_blocks = [{"type": "text", "text": "Action completed." if is_tool_msg else ""}]

        msg.content = new_blocks
