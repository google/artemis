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

"""Session-level message transcript ledger for the Pro operator (M2).

Implements the four-region discipline of the history-module redesign §3.2:

- **S (stable prefix)**: the static system message — byte-identical for the
  whole session (the plan+history section is moved out by the template split).
- **F (frozen history)**: in M2 only the cold-start restored-history block; L2
  chunks arrive in M3.
- **A (active window)**: raw per-turn messages, append-only. Each committed
  turn contributes its tail observation HumanMessage, the operator AIMessages
  (tool_calls and native thinking preserved by reference), the in-turn
  ToolMessages, and the turn's validator result message. The scrub edge
  (:class:`~artemis.agents.flash.context_compressor.ScrubEdgeCompressor`,
  generalized in M2) strips old UI lists / plan recitations at depth 1 and
  resolves screenshots to visual summaries at depth K — messages are never
  removed or reordered, so tool-call/response pairs are never split.
- **T (current tail)**: built fresh every turn by the operator; passed to
  :meth:`render` and only enters A when the turn is committed.

All timestamps inside the transcript use the session-start offset ``T+mm:ss``
(byte-stable once frozen); "ago" wording is reserved for the auxiliary agents'
per-call compiled views.
"""

import json
import time
from typing import Any, Callable

from langchain_core.messages import BaseMessage, HumanMessage

from artemis.memory.step_memory import StepMemoryService
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

#: Marker for the per-turn task-plan recitation block in the observation tail.
PLAN_RECITATION_MARKER = "--- Task Plan (recited) ---"

#: Marker of the Pro observation's UI element list block (see
#: ``ObservationPromptComponent``).
PRO_UI_LIST_MARKER = "--- Visible UI Elements ---"

#: Marker prefix of a committed turn's validator result message.
EXECUTION_RESULT_MARKER = "--- Action Execution Result"

#: Header prefix of the cold-start restored-history block.
RESTORED_HISTORY_HEADER = "[Restored history]"


def format_session_offset(seconds: float) -> str:
    """Render a session-start offset as ``T+mm:ss`` (minutes never wrap)."""
    total = max(0, int(seconds))
    return f"T+{total // 60:02d}:{total % 60:02d}"


class TranscriptLedger:
    """Append-only four-region message ledger for one Pro session.

    The ledger owns no LLM calls: summary lookups go through the shared
    :class:`StepMemoryService` (``ctx.step_memory``), whose visual-transition
    lens is fed by the Pro SummarizerNode each step.
    """

    def __init__(
        self,
        *,
        step_memory: StepMemoryService | None = None,
        prune_history_xml: bool = True,
        image_scrub_depth: int = 3,
        pending_grace_steps: int = 3,
        xml_scrub_depth: int = 1,
        clock: Callable[[], float] | None = None,
    ):
        self._clock = clock or time.monotonic
        self._session_start = self._clock()

        self._static: list[BaseMessage] = []
        self._restored: list[BaseMessage] = []
        self._active: list[BaseMessage] = []
        self._staged: list[BaseMessage] | None = None
        self._turn_count = 0

        # L2/L3 chunk compression (M3). ``_turns`` records each committed
        # turn's message span in ``_active``; a compression event advances
        # ``_active_start`` past whole turns (never splitting tool-call pairs)
        # and replaces the frozen chunk blocks wholesale. The underlying
        # ``_active`` list stays append-only so scrub-edge indices stay valid.
        self._turns: list[dict] = []
        self._chunked_turn_count = 0
        self._active_start = 0
        self._frozen_blocks: list[BaseMessage] = []
        self._chunker: Any | None = None

        # id(message) -> summary-job key (DataEngine step id). A side map keeps
        # the key out of the serialized message payload entirely.
        self._step_keys: dict[int, str] = {}

        # Imported here, not at module level: context_compressor imports
        # artemis.memory.step_memory, so a top-level import forms a cycle
        # whose failure depends on which side is imported first.
        from artemis.agents.flash.context_compressor import ScrubEdgeCompressor

        self._compressor = ScrubEdgeCompressor(
            summarizer=step_memory,
            prune_history_xml=prune_history_xml,
            image_scrub_depth=image_scrub_depth,
            pending_grace_steps=pending_grace_steps,
            xml_scrub_depth=xml_scrub_depth,
            summary_key_getter=lambda msg: self._step_keys.get(id(msg)),
            strip_markers=(PRO_UI_LIST_MARKER, PLAN_RECITATION_MARKER),
            tail_offset=1,
        )

    # ------------------------------------------------------------------
    # Session clock
    # ------------------------------------------------------------------

    def elapsed_seconds(self) -> float:
        return max(0.0, self._clock() - self._session_start)

    def elapsed_label(self) -> str:
        """The current session offset as ``T+mm:ss``."""
        return format_session_offset(self.elapsed_seconds())

    # ------------------------------------------------------------------
    # S region
    # ------------------------------------------------------------------

    @property
    def has_static_prefix(self) -> bool:
        return bool(self._static)

    def set_static_prefix(self, messages: list[BaseMessage]) -> None:
        """Install the byte-stable system prefix; only settable once."""
        if self._static:
            raise RuntimeError("TranscriptLedger static prefix is already set.")
        self._static = list(messages)

    # ------------------------------------------------------------------
    # F region (M2: cold-start restored history only)
    # ------------------------------------------------------------------

    @property
    def has_restored_history(self) -> bool:
        return bool(self._restored)

    def set_restored_history(self, text: str) -> None:
        """Install the cold-start frozen history block (empty ledger only)."""
        if self._restored:
            raise RuntimeError("TranscriptLedger restored history is already set.")
        if self._active or self._staged or self._turn_count:
            raise RuntimeError(
                "Restored history can only seed an empty ledger (cold start)."
            )
        self._restored = [
            HumanMessage(content=[{"type": "text", "text": text}])
        ]

    # ------------------------------------------------------------------
    # A region (append-only turn commits)
    # ------------------------------------------------------------------

    @property
    def turn_count(self) -> int:
        return self._turn_count

    @property
    def has_staged_turn(self) -> bool:
        return self._staged is not None

    @property
    def active_messages(self) -> tuple[BaseMessage, ...]:
        return tuple(self._active)

    def stage_turn(self, messages: list[BaseMessage]) -> None:
        """Hold a finished turn's messages until the next build commits them.

        Committing is deferred because the turn's DataEngine step id and its
        validator result only exist after the operator returns.
        """
        if self._staged is not None:
            logger.warning(
                "TranscriptLedger: previous staged turn was never committed;"
                " committing it without step metadata."
            )
            self.commit_staged()
        self._staged = [m for m in messages if isinstance(m, BaseMessage)]

    def commit_staged(
        self,
        *,
        step_key: str | None = None,
        validator_result: Any | None = None,
    ) -> None:
        """Move the staged turn into the active region.

        Args:
            step_key: The DataEngine step id recorded for this turn; keys the
                turn's observation screenshot to its visual-transition summary
                job for the depth-K scrub.
            validator_result: The turn's validator report (``None`` when the
                turn executed no terminal action); appended as a frozen result
                message carrying the ``T+mm:ss`` session offset.
        """
        if self._staged is None:
            return
        staged = self._staged
        self._staged = None

        if step_key is not None:
            observation = self._first_image_message(staged)
            if observation is not None:
                self._step_keys[id(observation)] = str(step_key)

        span_start = len(self._active)
        self._active.extend(staged)

        if validator_result is not None:
            result_message = self._build_result_message(validator_result)
            if result_message is not None:
                self._active.append(result_message)

        self._turns.append(
            {
                "step_key": str(step_key) if step_key is not None else None,
                "start": span_start,
                "end": len(self._active),
            }
        )
        self._turn_count += 1

    @staticmethod
    def _first_image_message(messages: list[BaseMessage]) -> BaseMessage | None:
        for msg in messages:
            content = getattr(msg, "content", None)
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("image_url", "image"):
                    return msg
        return None

    def _build_result_message(self, result: Any) -> HumanMessage | None:
        rendered = None
        if isinstance(result, dict):
            status = result.get("status") or "unknown"
            detail = None
            try:
                from artemis.utils.task_tree import format_result_clean

                # format_result_clean reports only errors/repairs; success
                # renders as the bare status line.
                detail = format_result_clean(result)
            except Exception:
                detail = None
            rendered = f"Status: {status}" + (f"\n{detail}" if detail else "")
        else:
            try:
                rendered = json.dumps(result, ensure_ascii=False, default=str)
            except Exception:
                rendered = str(result)
        if not rendered:
            return None
        return HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": (
                        f"{EXECUTION_RESULT_MARKER} ({self.elapsed_label()}) ---\n{rendered}"
                    ),
                }
            ]
        )

    # ------------------------------------------------------------------
    # F region (M3): chunk compression primitives
    # ------------------------------------------------------------------

    def attach_chunker(self, chunker: Any) -> None:
        """Install the L2/L3 :class:`HistoryChunkManager` consulted at render."""
        self._chunker = chunker

    @property
    def chunker(self) -> Any | None:
        return self._chunker

    @property
    def frozen_blocks(self) -> tuple[BaseMessage, ...]:
        return tuple(self._frozen_blocks)

    def unchunked_turns(self) -> list[dict]:
        """Committed turns not yet consumed by a compression event (copies)."""
        return [dict(t) for t in self._turns[self._chunked_turn_count :]]

    def turn_text_chars(self, turns: list[dict]) -> int:
        """Total text characters of the given turns' messages (size trigger)."""
        total = 0
        for turn in turns:
            for msg in self._active[turn["start"] : turn["end"]]:
                content = getattr(msg, "content", None)
                if isinstance(content, str):
                    total += len(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            total += len(block.get("text", ""))
        return total

    def freeze_turns(self, turn_count: int, frozen_blocks: list[BaseMessage]) -> None:
        """Compression event: consume the oldest ``turn_count`` unchunked turns
        and replace the frozen chunk blocks wholesale.

        The boundary always advances to a committed turn's end, so a tool-call
        message and its responses are never split across the F/A boundary.
        """
        if turn_count <= 0:
            self._frozen_blocks = list(frozen_blocks)
            return
        available = self._turns[self._chunked_turn_count :]
        if turn_count > len(available):
            raise ValueError(
                f"freeze_turns({turn_count}) exceeds the {len(available)}"
                " unchunked committed turns."
            )
        self._chunked_turn_count += turn_count
        self._active_start = self._turns[self._chunked_turn_count - 1]["end"]
        self._frozen_blocks = list(frozen_blocks)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self, tail: list[BaseMessage]) -> list[BaseMessage]:
        """Advance compression and return ``S + F + A + tail``.

        Order per turn: the chunker runs first (a triggered compression event
        deep-mutates the frozen region and advances the F/A boundary), then
        the scrub edge advances over the active window. The returned list is a
        fresh container: appending to it (the operator's in-turn tool loop)
        never mutates the ledger regions.
        """
        if self._chunker is not None:
            try:
                self._chunker.on_render(self)
            except Exception as e:
                logger.error(f"History chunker render hook failed: {e}")
        self._compressor.compress(self._active)
        return [
            *self._static,
            *self._restored,
            *self._frozen_blocks,
            *self._active[self._active_start :],
            *tail,
        ]
