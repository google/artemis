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

:class:`ScrubEdgeCompressor` maintains two edges over the observation history:
a shallow text edge (``xml_scrub_depth``, default 1: as soon as a newer
observation exists) where the heavy text blocks and the per-turn ephemeral
blocks leave, and the screenshot edge at depth K where the image is resolved
in place into its visual summary. A message past both edges is frozen; a late
summary never backfills a frozen message. FlashRunner uses this implementation;
the Pro transcript ledger reuses it over its active region (see the class
docstring).
"""

from typing import Any, Callable

from langchain_core.messages import BaseMessage

from artemis.memory.step_memory import StepMemoryService
from artemis.memory.transcript import EPHEMERAL_BLOCKS_KEY
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

#: Header written above a resolved visual summary (public: the chunk capsule
#: lens checks it to avoid repeating a summary already present verbatim).
HISTORY_SUMMARY_PREFIX = "--- Historical Visual Transition ---\n"
_HISTORY_SUMMARY_PREFIX = HISTORY_SUMMARY_PREFIX
_UI_LIST_MARKER = "--- UI Element List ---"

#: Text of the label block both observation shapes place directly above the
#: screenshot. It only makes sense next to an image, so it leaves together
#: with the image it labels (the visual summary carries its own header).
SCREENSHOT_LABEL = "--- Current Screenshot ---"

#: Sentinel: the screenshot stays for now (summary pending inside the grace
#: window); the message is revisited for the image swap only.
_KEEP_IMAGE = object()


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
    """Replace older observation details with summaries at two scrub edges.

    Depth 1 == the most recent image-bearing message. Messages newer than an
    edge are byte-identical to what the model was first shown. Each message
    is edited at most twice and is then frozen:

    1. Text edge (depth > ``xml_scrub_depth``, default 1, i.e. as soon as a
       newer observation exists), one pass, exactly once per message: the
       heavy blocks behind ``strip_markers`` (the UI element list; for Pro
       also the plan recitation) are cut at their marker with the text before
       the marker kept, and every content block whose index is listed under
       :data:`~artemis.memory.transcript.EPHEMERAL_BLOCKS_KEY` in the
       message's ``additional_kwargs`` (reminders, hints, per-turn notices)
       is removed. Only the live observation therefore ever carries an
       indexed element list: a stale ``[n]`` index from an older screen can
       never be picked up as a target.
    2. Screenshot edge (depth >= ``image_scrub_depth``), IN PLACE: the ready
       visual summary block takes the image block's position and the
       :data:`SCREENSHOT_LABEL` block above it is removed. A failed summary
       becomes an unavailable placeholder immediately; a summary still
       pending keeps the image (and its label) for up to
       ``pending_grace_steps`` further depths, after which a pending
       placeholder replaces it. Both placeholders carry the DataEngine step
       reference. An image with no summary job at all is dropped silently.
    3. Frozen: after the image is resolved the message is never read or
       written again; a late summary never backfills a frozen message.

    Contract: the message list is append-only (FlashRunner never removes or
    reorders entries), so bookkeeping is index-based and each pass only
    touches the few messages near the scrub edge instead of rescanning the
    whole history.

    The Pro transcript ledger uses this discipline over its active region.
    Its observation messages carry no ``tool_call_id``, so
    ``summary_key_getter`` supplies the summary-job key (the DataEngine step
    id) per message; ``strip_markers`` extends the strip to the Pro tail
    blocks (UI element list + plan recitation); and ``tail_offset=1``
    accounts for the live observation living outside the tracked list (the
    ledger renders it as a separate tail), so depth arithmetic stays aligned
    with the Flash semantics where the live observation is *inside* the
    list.
    """

    def __init__(
        self,
        summarizer: StepMemoryService | None = None,
        *,
        prune_history_xml: bool = True,
        image_scrub_depth: int = 3,
        pending_grace_steps: int = 3,
        xml_scrub_depth: int | None = 1,
        summary_key_getter: Callable[[BaseMessage], str | None] | None = None,
        strip_markers: tuple[str, ...] = (_UI_LIST_MARKER,),
        tail_offset: int = 0,
    ):
        self._summarizer = summarizer
        self._prune_history_xml = prune_history_xml
        self._image_scrub_depth = max(1, image_scrub_depth)
        self._pending_grace_steps = max(0, pending_grace_steps)
        # Text edge: the heavy text blocks and ephemeral blocks leave once a
        # message is deeper than this (None keeps the default of 1).
        self._xml_scrub_depth = max(1, int(xml_scrub_depth)) if xml_scrub_depth is not None else 1
        self._summary_key_getter = summary_key_getter
        self._strip_markers = tuple(strip_markers) or (_UI_LIST_MARKER,)
        self._tail_offset = max(0, tail_offset)

        self._scanned_until = 0
        self._tool_msg_count = 0
        # Append-only records of image-bearing messages, in message order:
        # {"idx", "key" (str tool_call_id | None), "legacy" (ordinal | None), "is_tool"}
        self._tracked: list[dict[str, Any]] = []
        self._frozen: set[int] = set()
        # Messages whose one-time text edits (strip + ephemeral deletion) are
        # done; a message inside the pending-grace window sits here unfrozen.
        self._text_scrubbed: set[int] = set()
        # Strip-marker carriers without an image (e.g. a step whose screenshot
        # failed); they are stripped when the edge passes their position.
        self._xml_candidates: list[int] = []

    def compress(
        self, messages: list[BaseMessage], *, image_scrub_depth: int | None = None
    ) -> None:
        """Advance the scrub edge over newly appended messages in-place.

        ``image_scrub_depth`` overrides the constructor depth for this pass
        (the transcript ledger passes its occupancy-driven depth). Because a
        scrubbed message is frozen, a deeper pass after a shallower one never
        backfills anything: relaxing the depth only lets *new* images live
        longer, tightening it scrubs the extra depths once.
        """
        self._discover(messages)
        if not self._tracked and not self._xml_candidates:
            return
        self._scrub_edge(messages, image_scrub_depth)

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
            elif has_xml:
                self._xml_candidates.append(idx)
        self._scanned_until = len(messages)

    def _has_strip_marker(self, text: str) -> bool:
        return any(marker in text for marker in self._strip_markers)

    # ------------------------------------------------------------------
    # The edges: text strip + ephemeral deletion (shallow), image swap (K)
    # ------------------------------------------------------------------

    def _scrub_edge(
        self, messages: list[BaseMessage], image_scrub_depth: int | None = None
    ) -> None:
        total = len(self._tracked)
        scrub_depth = max(1, int(image_scrub_depth or self._image_scrub_depth))
        grace_limit = scrub_depth + self._pending_grace_steps
        text_depth = self._xml_scrub_depth

        for pos, rec in enumerate(self._tracked):
            if rec["idx"] in self._frozen:
                continue
            # 1 == most recent image-bearing message; a live tail outside the
            # tracked list (tail_offset > 0) counts as the newest depths.
            depth = total - pos + self._tail_offset
            if self._tail_offset == 0 and pos == total - 1:
                continue  # the live observation is never scrubbed
            past_text_edge = depth > text_depth
            past_image_edge = depth >= scrub_depth
            if not past_text_edge and not past_image_edge:
                continue  # newer than both edges: byte-identical
            if not past_image_edge:
                # Past the text edge only: strip once, keep the screenshot.
                if rec["idx"] not in self._text_scrubbed:
                    self._rewrite(
                        messages[rec["idx"]],
                        rec["idx"],
                        replacement=_KEEP_IMAGE,
                        is_tool=rec["is_tool"],
                    )
                continue

            summary, pending, failed, step_no = self._lookup(rec)
            replacement: Any
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
                replacement = _KEEP_IMAGE  # grace: text edits now, image swap later
            elif pending:
                replacement = {
                    "type": "text",
                    "text": f"[visual summary pending; evidence at DataEngine step {step_no}]",
                }
            else:
                replacement = None  # no summary job exists; drop the image silently

            self._resolve(messages, rec, replacement, text_edits=past_text_edge)

        if self._xml_candidates:
            # Imageless strip-marker carriers (a step whose screenshot failed)
            # follow the same text edge, counted over every observation —
            # image-bearing or not — plus the live tail: with the default
            # depth 1 the list leaves as soon as any newer observation exists,
            # so only the live observation ever carries an indexed element
            # list. A carrier that is itself the newest observation stays.
            observations = sorted({rec["idx"] for rec in self._tracked} | set(self._xml_candidates))
            remaining: list[int] = []
            for idx in self._xml_candidates:
                newer = sum(1 for other in observations if other > idx) + self._tail_offset
                if newer < text_depth:
                    remaining.append(idx)
                    continue
                if idx not in self._frozen:
                    self._rewrite(messages[idx], idx, replacement=None, is_tool=False)
                    self._frozen.add(idx)
            self._xml_candidates = remaining

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
        replacement: Any,
        *,
        text_edits: bool = True,
    ) -> None:
        """Rewrite one image-bearing message at the screenshot edge; freeze it
        unless the screenshot is kept for the pending-grace window. With a text
        edge deeper than the screenshot edge the text edits wait for their own
        edge (``text_edits=False``); the message is then frozen only once both
        have happened."""
        idx = rec["idx"]
        self._rewrite(
            messages[idx],
            idx,
            replacement=replacement,
            is_tool=rec["is_tool"],
            text_edits=text_edits,
        )
        if not text_edits:
            return  # text edge still ahead: revisit for the strip
        if replacement is not _KEEP_IMAGE:
            self._frozen.add(idx)

    def _rewrite(
        self,
        msg: BaseMessage,
        idx: int,
        *,
        replacement: Any,
        is_tool: bool,
        text_edits: bool = True,
    ) -> None:
        """The single-pass edit of one message (see the class docstring).

        ``replacement`` is the block that takes the first image's place (a
        summary or placeholder), ``None`` to drop the image, or
        :data:`_KEEP_IMAGE` to leave the image and its label in place. The
        text edits (strip + ephemeral deletion) run only the first time the
        message is rewritten and consume the ephemeral indices exactly once.
        Any rewrite that happens *before* that first edit (the image swap when
        the screenshot edge is shallower than the text edge, or a summary
        arriving inside the grace window) can remove the label and the image
        and so shift every later block; the surviving ephemeral indices are
        then remapped to the new layout in ``additional_kwargs`` so the later
        text edit still deletes the blocks that were marked, never a
        neighbour that slid into their old position.
        """
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            self._text_scrubbed.add(idx)
            return

        first_edit = idx not in self._text_scrubbed and text_edits
        ephemeral: set[int] = set()
        if first_edit:
            ephemeral = self._ephemeral_indices(msg)
        keep_image = replacement is _KEEP_IMAGE

        new_blocks: list[Any] = []
        # old position -> new position of every block that survives as itself
        # (a replacement block takes the image's slot but is not the image).
        position_map: dict[int, int] = {}
        image_placed = False
        for position, block in enumerate(content):
            if position in ephemeral:
                continue
            if isinstance(block, dict):
                kind = block.get("type")
                if kind in ("image_url", "image"):
                    if keep_image:
                        position_map[position] = len(new_blocks)
                        new_blocks.append(block)
                    elif replacement is not None and not image_placed:
                        new_blocks.append(replacement)  # in place of the image
                        image_placed = True
                    continue
                if kind == "text":
                    text = block.get("text", "")
                    if not keep_image and text.strip() == SCREENSHOT_LABEL:
                        continue  # the label leaves with the image it labelled
                    if first_edit and self._prune_history_xml and self._has_strip_marker(text):
                        retained_text = _without_marked_suffix(text, self._strip_markers)
                        if retained_text:
                            retained_block = dict(block)
                            retained_block["text"] = retained_text
                            position_map[position] = len(new_blocks)
                            new_blocks.append(retained_block)
                        continue
            position_map[position] = len(new_blocks)
            new_blocks.append(block)

        # Defensive fallback: never leave ToolMessage or HumanMessage content empty
        if not new_blocks:
            new_blocks = [{"type": "text", "text": "Action dispatched." if is_tool else ""}]
            position_map = {}

        msg.content = new_blocks
        if first_edit:
            self._text_scrubbed.add(idx)
            if ephemeral:
                # Consumed: the indices no longer describe the block layout.
                msg.additional_kwargs.pop(EPHEMERAL_BLOCKS_KEY, None)
        else:
            self._remap_ephemeral(msg, position_map)

    @staticmethod
    def _remap_ephemeral(msg: BaseMessage, position_map: dict[int, int]) -> None:
        """Rewrite the not-yet-consumed ephemeral indices of ``msg`` through
        ``position_map`` (old block position -> new block position); indices of
        blocks that did not survive are dropped and the key is removed when
        nothing survives. Only ``additional_kwargs`` is touched, the content
        blocks sent to the provider stay exactly as assembled."""
        kwargs = getattr(msg, "additional_kwargs", None)
        if not isinstance(kwargs, dict) or EPHEMERAL_BLOCKS_KEY not in kwargs:
            return
        remapped: list[int] = []
        for value in kwargs.get(EPHEMERAL_BLOCKS_KEY) or []:
            try:
                old_position = int(value)
            except (TypeError, ValueError):
                continue
            new_position = position_map.get(old_position)
            if new_position is not None and new_position not in remapped:
                remapped.append(new_position)
        if remapped:
            kwargs[EPHEMERAL_BLOCKS_KEY] = remapped
        else:
            kwargs.pop(EPHEMERAL_BLOCKS_KEY, None)

    @staticmethod
    def _ephemeral_indices(msg: BaseMessage) -> set[int]:
        kwargs = getattr(msg, "additional_kwargs", None)
        if not isinstance(kwargs, dict):
            return set()
        indices: set[int] = set()
        for value in kwargs.get(EPHEMERAL_BLOCKS_KEY) or []:
            try:
                indices.add(int(value))
            except (TypeError, ValueError):
                continue
        return indices
