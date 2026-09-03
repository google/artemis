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

"""Context compression shared by Flash and Pro.

:class:`ScrubEdgeCompressor` strips XML at depth 1 and resolves screenshots
at depth K, then freezes the message. A late summary never backfills a frozen
message. FlashRunner uses this implementation; the Pro transcript ledger
reuses it over its active region (see the class docstring).
"""

from typing import Any, Callable

from langchain_core.messages import BaseMessage

from artemis.memory.step_memory import StepMemoryService
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

#: Header written above a resolved visual summary (public: the chunk capsule
#: lens checks it to avoid repeating a summary already present verbatim).
HISTORY_SUMMARY_PREFIX = "--- Historical Visual Transition ---\n"
_HISTORY_SUMMARY_PREFIX = HISTORY_SUMMARY_PREFIX
_UI_LIST_MARKER = "--- UI Element List ---"


def _without_marked_suffix(text: str, markers: tuple[str, ...] = (_UI_LIST_MARKER,)) -> str:
    """Remove a marked heavy block without discarding text before its marker."""
    marker_index = -1
    for marker in markers:
        idx = text.find(marker)
        if idx >= 0 and (marker_index < 0 or idx < marker_index):
            marker_index = idx
    if marker_index < 0:
        return text
    return text[:marker_index].rstrip()


class ScrubEdgeCompressor:
    """Replace older observation details with summaries near the message tail.

    Mutation discipline — every message is touched at most twice, always near
    the tail, and is then frozen forever:

    1. Depth 1 (XML): once a newer observation exists, historical UI Element
       lists are stripped (same semantics as the legacy ``prune_history_xml``).
    2. Depth K (image, ``image_scrub_depth``): the K-th most recent
       image-bearing message has its screenshot resolved — replaced by the
       ready visual summary; if the summary is still pending the image is
       retained for up to ``pending_grace_steps`` further image-depths, after
       which it is replaced by a pending placeholder. A failed summary is
       replaced by an unavailable placeholder immediately. Both placeholders
       carry the DataEngine step reference.
    3. Frozen: after resolution the message is never read or written again —
       a late summary never backfills a frozen message.

    Contract: the message list is append-only (FlashRunner never removes or
    reorders entries), so bookkeeping is index-based and each pass only
    touches the few messages near the scrub edge instead of rescanning the
    whole history.

    The Pro transcript ledger uses this discipline over its active region.
    Its observation messages carry no
    ``tool_call_id``, so ``summary_key_getter`` supplies the summary-job key
    (the DataEngine step id) per message; ``strip_markers`` extends the
    depth-1 strip to the Pro tail blocks (UI element list + plan recitation);
    and ``tail_offset=1`` accounts for the live observation living outside
    the tracked list (the ledger renders it as a separate tail), so depth
    arithmetic and the keep-window stay aligned with the Flash semantics
    where the live observation is *inside* the list. Defaults leave the
    Flash behavior byte-identical.
    """

    def __init__(
        self,
        summarizer: StepMemoryService | None = None,
        *,
        prune_history_xml: bool = True,
        image_scrub_depth: int = 3,
        pending_grace_steps: int = 3,
        xml_scrub_depth: int = 1,
        summary_key_getter: Callable[[BaseMessage], str | None] | None = None,
        strip_markers: tuple[str, ...] = (_UI_LIST_MARKER,),
        tail_offset: int = 0,
    ):
        self._summarizer = summarizer
        self._prune_history_xml = prune_history_xml
        self._image_scrub_depth = max(1, image_scrub_depth)
        self._pending_grace_steps = max(0, pending_grace_steps)
        self._xml_scrub_depth = max(1, xml_scrub_depth)
        self._summary_key_getter = summary_key_getter
        self._strip_markers = tuple(strip_markers) or (_UI_LIST_MARKER,)
        self._tail_offset = max(0, tail_offset)

        self._scanned_until = 0
        self._tool_msg_count = 0
        # Append-only records of image-bearing messages, in message order:
        # {"idx", "key" (str tool_call_id | None), "legacy" (ordinal | None), "is_tool"}
        self._tracked: list[dict[str, Any]] = []
        self._frozen: set[int] = set()
        self._xml_candidates: list[int] = []

    def compress(self, messages: list[BaseMessage]) -> None:
        """Advance the scrub edge over newly appended messages in-place."""
        self._discover(messages)
        if not self._tracked:
            return
        self._scrub_xml(messages)
        self._scrub_images(messages)

    # ------------------------------------------------------------------
    # Discovery (incremental; frozen prefix is never rescanned)
    # ------------------------------------------------------------------

    def _discover(self, messages: list[BaseMessage]) -> None:
        for idx in range(self._scanned_until, len(messages)):
            msg = messages[idx]
            tool_call_id = getattr(msg, "tool_call_id", None)
            if tool_call_id is not None:
                self._tool_msg_count += 1
            content = getattr(msg, "content", None)
            if not isinstance(content, list):
                continue

            has_image = False
            has_xml = False
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in ("image_url", "image"):
                    has_image = True
                elif block.get("type") == "text" and self._has_strip_marker(block.get("text", "")):
                    has_xml = True

            if has_xml:
                self._xml_candidates.append(idx)
            if has_image:
                key = str(tool_call_id) if tool_call_id is not None else None
                if key is None and self._summary_key_getter is not None:
                    try:
                        getter_key = self._summary_key_getter(msg)
                    except Exception:
                        getter_key = None
                    if getter_key is not None:
                        key = str(getter_key)
                self._tracked.append(
                    {
                        "idx": idx,
                        "key": key,
                        "legacy": self._tool_msg_count if tool_call_id is not None else None,
                        "is_tool": tool_call_id is not None,
                    }
                )
        self._scanned_until = len(messages)

    def _has_strip_marker(self, text: str) -> bool:
        return any(marker in text for marker in self._strip_markers)

    # ------------------------------------------------------------------
    # Depth-1 XML scrub
    # ------------------------------------------------------------------

    def _scrub_xml(self, messages: list[BaseMessage]) -> None:
        if not self._prune_history_xml:
            return
        # Keep the UI list only in the newest xml_scrub_depth observation
        # messages; everything older (or non-image XML carriers) is stripped.
        # With a live tail outside the tracked list (tail_offset > 0) the tail
        # itself occupies the newest slots of the keep window.
        keep_count = max(0, self._xml_scrub_depth - self._tail_offset)
        keep_indices = {rec["idx"] for rec in self._tracked[-keep_count:]} if keep_count else set()
        still_pending: list[int] = []
        for idx in self._xml_candidates:
            if idx in keep_indices:
                still_pending.append(idx)
                continue
            if idx in self._frozen:
                continue
            self._strip_xml_blocks(messages[idx])
        self._xml_candidates = still_pending

    def _strip_xml_blocks(self, msg: BaseMessage) -> None:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            return
        new_blocks: list[Any] = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and self._has_strip_marker(block.get("text", ""))
            ):
                retained_text = _without_marked_suffix(block.get("text", ""), self._strip_markers)
                if retained_text:
                    retained_block = dict(block)
                    retained_block["text"] = retained_text
                    new_blocks.append(retained_block)
                continue
            new_blocks.append(block)
        msg.content = new_blocks

    # ------------------------------------------------------------------
    # Depth-K image scrub
    # ------------------------------------------------------------------

    def _scrub_images(self, messages: list[BaseMessage]) -> None:
        total = len(self._tracked)
        scrub_depth = self._image_scrub_depth
        grace_limit = scrub_depth + self._pending_grace_steps

        for pos, rec in enumerate(self._tracked):
            if rec["idx"] in self._frozen:
                continue
            # 1 == most recent image-bearing message; a live tail outside the
            # tracked list (tail_offset > 0) counts as the newest depths.
            depth = total - pos + self._tail_offset
            if depth < scrub_depth:
                continue  # active window
            if self._tail_offset == 0 and pos == total - 1:
                continue  # the live observation is never scrubbed

            summary, pending, failed, step_no = self._lookup(rec)
            if summary is not None:
                replacement = {
                    "type": "text",
                    "text": f"{_HISTORY_SUMMARY_PREFIX}{summary}",
                }
            elif failed:
                replacement = {
                    "type": "text",
                    "text": f"[visual summary unavailable; evidence at DataEngine step {step_no}]",
                }
            elif pending and depth <= grace_limit:
                continue  # grace period: retain the image, revisit next pass
            elif pending:
                replacement = {
                    "type": "text",
                    "text": f"[visual summary pending; evidence at DataEngine step {step_no}]",
                }
            else:
                replacement = None  # no summary job exists; drop the image silently

            self._resolve(messages, rec, replacement)

    def _lookup(self, rec: dict[str, Any]) -> tuple[str | None, bool, bool, int | None]:
        """Mirror the legacy keying: tool_call_id first, ordinal fallback."""
        summarizer = self._summarizer
        if summarizer is None:
            return None, False, False, None

        effective_key: Any = rec["key"]
        if effective_key is None:
            return None, False, False, None

        summary = summarizer.get_summary(effective_key)
        if summary is None and not summarizer.has_job(effective_key) and rec["legacy"] is not None:
            effective_key = rec["legacy"]
            summary = summarizer.get_summary(effective_key)

        failed = summarizer.has_failed(effective_key)
        pending = summarizer.is_pending(effective_key) and not failed
        step_no = summarizer.get_step_number(effective_key)
        if step_no is None:
            step_no = rec["legacy"]
        return summary, pending, failed, step_no

    def _resolve(
        self,
        messages: list[BaseMessage],
        rec: dict[str, Any],
        replacement: dict[str, Any] | None,
    ) -> None:
        """Replace the message's image blocks and freeze it permanently."""
        msg = messages[rec["idx"]]
        content = getattr(msg, "content", None)
        new_blocks: list[Any] = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("image_url", "image"):
                    continue
                if (
                    self._prune_history_xml
                    and isinstance(block, dict)
                    and block.get("type") == "text"
                    and self._has_strip_marker(block.get("text", ""))
                ):
                    # Freezing implies fully scrubbed: no stale UI list survives
                    # even when xml_scrub_depth exceeds the image depth.
                    retained_text = _without_marked_suffix(
                        block.get("text", ""), self._strip_markers
                    )
                    if retained_text:
                        retained_block = dict(block)
                        retained_block["text"] = retained_text
                        new_blocks.append(retained_block)
                    continue
                new_blocks.append(block)

        if replacement is not None:
            new_blocks.append(replacement)

        # Defensive fallback: never leave ToolMessage or HumanMessage content empty
        if not new_blocks:
            new_blocks = [{"type": "text", "text": "Action completed." if rec["is_tool"] else ""}]

        msg.content = new_blocks
        self._frozen.add(rec["idx"])
        if rec["idx"] in self._xml_candidates:
            self._xml_candidates.remove(rec["idx"])
