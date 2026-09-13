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

"""Session message ledger shared by the Pro operator and Flash runner.

Messages are organized into four regions:

- **S (stable prefix)**: the static system message — byte-identical for the
  whole session (the plan+history section is moved out by the template split).
- **F (frozen history)**: restored history and compressed chunks.
- **A (active window)**: raw per-turn messages, append-only. Each committed
  turn contributes its tail observation HumanMessage, the operator AIMessages
  (tool_calls and native thinking preserved by reference), the in-turn
  ToolMessages, and the turn's result message (every turn on the Pro path;
  only when an action failed on the Flash path). The
  scrub edge (:class:`~artemis.agents.flash.context_compressor.ScrubEdgeCompressor`)
  has two depths: a shallow text edge (UI list, plan recitation and ephemeral
  per-turn blocks leave as soon as a newer observation exists) and a
  screenshot edge that follows the occupancy tier (relaxed below the start
  gate, tightened past it), where the screenshot is replaced in place by its
  visual summary. Every rewrite happens once and is never undone; everything
  newer is byte-identical to what the model was first shown. Messages are
  never removed or reordered, so tool-call/response pairs are never split.
- **T (current tail)**: built fresh every turn by the operator; passed to
  :meth:`render` and only enters A when the turn is committed.

All timestamps inside the transcript use the session-start offset ``T+mm:ss``
(byte-stable once frozen); "ago" wording is reserved for the auxiliary agents'
per-call compiled views. When the caller supplies the DataEngine
``session_start`` epoch, the ledger clock is anchored to it so the tail
offsets, the chunk ledger lines and the video analyzer's action timeline all
share one origin.

Profiles: the Pro operator (one committed turn per graph step) and the Flash
runner (one committed turn per reactive turn; a multi-action turn registers
every recorded step id via ``extra_step_keys`` so the chunk ledger lists each
step) build their prompts from the same ledger.
"""

import json
import time
from typing import Any, Callable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

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

#: ``HumanMessage.additional_kwargs`` key listing the indices of content blocks that
#: are only meaningful for the turn they were built for (reminders, hints, user
#: guidance, per-turn notices). The scrub edge deletes them at its text edge, so
#: they never reach the frozen region or a chunk capsule. The indices always
#: describe the message's *current* block layout: a rewrite that removes blocks
#: before the text edge (the screenshot swap when the image edge is shallower)
#: remaps the surviving indices in place, and the key is dropped once the blocks
#: are deleted. ``additional_kwargs`` is never sent to the provider, so the
#: marking costs the model nothing.
EPHEMERAL_BLOCKS_KEY = "ephemeral_blocks"


def mark_ephemeral(message: BaseMessage, indices) -> None:
    """Flag content blocks of ``message`` (by index) as ephemeral, see
    :data:`EPHEMERAL_BLOCKS_KEY`."""
    existing = list(message.additional_kwargs.get(EPHEMERAL_BLOCKS_KEY, []))
    for index in indices:
        index = int(index)
        if index not in existing:
            existing.append(index)
    message.additional_kwargs[EPHEMERAL_BLOCKS_KEY] = existing


def format_session_offset(seconds: float) -> str:
    """Render a session-start offset as ``T+mm:ss`` (minutes never wrap)."""
    total = max(0, int(seconds))
    return f"T+{total // 60:02d}:{total % 60:02d}"


#: Placeholder for an image block that is still inside the scrub edge's
#: pending-grace window when a turn transcript is rendered.
SCREENSHOT_PLACEHOLDER = "[screenshot]"

_ROLE_LABELS = {
    "human": "observation",
    "ai": "operator",
    "tool": "tool result",
    "system": "system",
}


def _render_content_text(content: Any) -> str:
    """Flatten message content to plain text.

    Text blocks pass through verbatim, native thinking blocks are labelled,
    image blocks become :data:`SCREENSHOT_PLACEHOLDER`.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif not isinstance(block, dict):
            parts.append(str(block))
        elif block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif block.get("type") == "thinking":
            parts.append(f"(thinking) {block.get('thinking', '')}")
        elif block.get("type") in ("image_url", "image"):
            parts.append(SCREENSHOT_PLACEHOLDER)
        else:
            parts.append(json.dumps(block, ensure_ascii=False, default=str))
    return "\n".join(part for part in parts if part)


def render_turn_transcript(messages: list[BaseMessage]) -> str:
    """Mechanical plain-text rendering of one committed turn's messages.

    Lossless with respect to what the operator model was shown: every text
    block, native thinking block, tool call (name and arguments) and tool
    result is carried verbatim, in message order. Image blocks — present only
    while a screenshot is still inside the scrub edge's pending-grace window —
    become a placeholder; a resolved visual-transition summary is already
    plain text. The segment capsule uses this transcript as its source when
    compressing the turn.

    A tool result is labelled with its tool's name, resolved through the
    turn's own preceding tool calls when the message carries no name (the Pro
    operator's ToolMessages don't); the opaque call id is shown only when no
    name can be resolved.
    """
    lines: list[str] = []
    names_by_id: dict[str, str] = {}
    for msg in messages:
        kind = str(getattr(msg, "type", "") or "message")
        if isinstance(msg, AIMessage):
            kind = "ai"  # streamed replies report type "AIMessageChunk"
        role = _ROLE_LABELS.get(kind, kind)
        if kind == "tool":
            call_id = getattr(msg, "tool_call_id", None)
            name = getattr(msg, "name", None) or names_by_id.get(str(call_id)) or call_id
            if name:
                role = f"{role} {name}"
            status = getattr(msg, "status", None)
            if status and status != "success":
                role = f"{role} ({status})"
        body = _render_content_text(getattr(msg, "content", None))
        if body:
            lines.append(f"[{role}]\n{body}")
        for call in getattr(msg, "tool_calls", None) or []:
            if not isinstance(call, dict):
                continue
            if call.get("id") and call.get("name"):
                names_by_id[str(call["id"])] = str(call["name"])
            args = json.dumps(call.get("args") or {}, ensure_ascii=False, default=str)
            lines.append(f"[tool call] {call.get('name')}({args})")
    return "\n".join(lines)


#: Tokens charged per image block in the provider-less prompt estimate
#: (Gemini's flat per-image cost; the Flash runner uses the same figure).
IMAGE_BLOCK_TOKENS = 258

#: Characters per token assumed before any calibrated measurement.
DEFAULT_CHARS_PER_TOKEN = 4.0

#: Bounds of the calibrated chars-per-token ratio: ~1 for CJK-heavy prompts,
#: ~4 for English prose; anything outside is a measurement artefact.
_CHARS_PER_TOKEN_BOUNDS = (1.0, 6.0)

#: Smallest text growth (characters) between two consecutive measured prompts
#: for their difference to count as a calibration sample (see
#: :meth:`TranscriptLedger.record_prompt_tokens`). Below this the provider's
#: rounding and a few hundred tokens of turn-to-turn jitter dominate the
#: quotient.
MIN_CALIBRATION_DELTA_CHARS = 2000


def measure_prompt_content(messages: list[Any]) -> tuple[int, int]:
    """Count ``(text_chars, image_count)`` over a prompt's messages.

    The walk covers what the provider tokenizes as text: string content in
    full; in list content the ``text`` blocks by their text, ``image_url`` /
    ``image`` blocks as one image each, any other block by its ``str()``; and
    each message's ``tool_calls`` by the JSON of their arguments (an
    AIMessage's calls are sent back to the model verbatim, and the ledger's
    turn transcripts size them the same way).
    """
    text_chars = 0
    image_count = 0
    for msg in messages:
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            text_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") in ("image_url", "image") or "image_url" in block:
                        image_count += 1
                    elif block.get("type") == "text":
                        text_chars += len(str(block.get("text", "")))
                    else:
                        text_chars += len(str(block))
                else:
                    text_chars += len(str(block))
        elif content is not None:
            text_chars += len(str(content))
        for call in getattr(msg, "tool_calls", None) or []:
            if isinstance(call, dict):
                text_chars += len(
                    json.dumps(call.get("args") or {}, ensure_ascii=False, default=str)
                )
    return text_chars, image_count


def estimate_prompt_tokens(messages: list[Any]) -> int:
    """Provider-less prompt size estimate: ``text_chars // 4`` plus
    :data:`IMAGE_BLOCK_TOKENS` per image block (see
    :func:`measure_prompt_content` for what counts). Used as the context base
    when a provider reports no usage metadata; never below 1."""
    text_chars, image_count = measure_prompt_content(messages)
    return max(1, text_chars // 4 + image_count * IMAGE_BLOCK_TOKENS)


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
        xml_scrub_depth: int | None = None,
        image_scrub_depth_relaxed: int | None = None,
        context_budget_tokens: int | None = None,
        start_ratio: float | None = None,
        clock: Callable[[], float] | None = None,
        session_start: float | None = None,
    ):
        # ``session_start`` is an epoch-seconds origin (the DataEngine's
        # ``session_start_time``); with it the ledger reads the wall clock so
        # every ``T+mm:ss`` label is relative to the recorded session start.
        # Without it the ledger keeps its own monotonic origin.
        if session_start is not None:
            self._clock = clock or time.time
            self._session_start = float(session_start)
        else:
            self._clock = clock or time.monotonic
            self._session_start = self._clock()

        self._static: list[BaseMessage] = []
        self._restored: list[BaseMessage] = []
        self._active: list[BaseMessage] = []
        self._staged: list[BaseMessage] | None = None
        self._turn_count = 0

        # L2/L3 chunk compression. ``_turns`` records each committed
        # turn's message span in ``_active``; a compression event advances
        # ``_active_start`` past whole turns (never splitting tool-call pairs)
        # and replaces the frozen chunk blocks wholesale. The underlying
        # ``_active`` list stays append-only so scrub-edge indices stay valid.
        self._turns: list[dict] = []
        self._chunked_turn_count = 0
        self._active_start = 0
        self._frozen_blocks: list[BaseMessage] = []
        self._chunker: Any | None = None

        # The owning operator's last measured prompt size (tokens). This is
        # the live context base the chunker's start/soft/hard thresholds read;
        # only the operator's own calls write it, so Planner/Checker/lens
        # calls sharing the session can never masquerade as the operator's
        # context (see HistoryChunkManager._context_base_tokens).
        self._last_prompt_tokens: int | None = None
        # Session-calibrated characters-per-token ratio (see
        # record_prompt_tokens): the chunker converts transcript characters
        # to tokens through it, so CJK-heavy sessions (~1 char/token on
        # Gemini) are not under-estimated 4x by the fixed //4 default.
        # Calibrated differentially: ``_calibration_sample`` is the previous
        # measured ``(text_chars, image_count, prompt_tokens)`` and the
        # pooled deltas between consecutive same-image-count prompts give
        # the session's single best ratio.
        self._chars_per_token: float = DEFAULT_CHARS_PER_TOKEN
        self._calibration_sample: tuple[int, int, int] | None = None
        self._pooled_delta_chars = 0
        self._pooled_delta_tokens = 0

        # Whether the last committed turn carried no visible reasoning text (bare
        # tool calls / thinking only). Set by ``commit_staged``; both runners read
        # it to decide whether to attach the shared reasoning reminder.
        self._last_turn_silent: bool = False

        # Text edge (default 1): UI list, plan recitation and ephemeral blocks
        # leave as soon as a newer observation exists; screenshots follow K.
        self._xml_scrub_depth = xml_scrub_depth

        # Occupancy-driven screenshot depth. Below the start gate
        # (``last_prompt_tokens < budget * start_ratio``, or no measurement
        # yet) the scrub edge sits at the relaxed depth so the model keeps
        # more raw screenshots while nothing is being replaced; from the
        # start gate up it tightens to ``image_scrub_depth``. Scrubbing is
        # irreversible, so a change of depth never rewrites a message:
        # tightening scrubs the extra depths once, relaxing lets new images
        # live longer. See :attr:`effective_image_scrub_depth`.
        self._image_scrub_depth = max(1, int(image_scrub_depth))
        self._image_scrub_depth_relaxed = (
            max(self._image_scrub_depth, int(image_scrub_depth_relaxed))
            if image_scrub_depth_relaxed is not None
            else self._image_scrub_depth
        )
        self._context_budget_tokens = int(context_budget_tokens) if context_budget_tokens else None
        self._start_ratio = float(start_ratio) if start_ratio is not None else None

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
    # Context base (operator-measured prompt size)
    # ------------------------------------------------------------------

    @property
    def last_prompt_tokens(self) -> int | None:
        """The operator's last measured prompt size, or None before the
        first measured call (or when the provider reports no usage)."""
        return self._last_prompt_tokens

    @property
    def last_turn_silent(self) -> bool:
        """True when the last committed turn had no visible reasoning text.

        A turn is silent when it carries at least one AI message and none of
        them shows text: string content that is empty or whitespace, or list
        content without a non-empty ``text`` block (native ``thinking`` blocks
        are not visible text). A turn with no AI message at all is not silent.
        """
        return self._last_turn_silent

    @staticmethod
    def _ai_message_has_visible_text(message: BaseMessage) -> bool:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return bool(content.strip())
        if isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    if block.strip():
                        return True
                elif isinstance(block, dict) and block.get("type") == "text":
                    if str(block.get("text") or "").strip():
                        return True
            return False
        return False

    @classmethod
    def _turn_is_silent(cls, messages: list[BaseMessage]) -> bool:
        # Streamed replies are ``AIMessageChunk`` (type "AIMessageChunk"), so
        # the class, not the type string, identifies the model's messages.
        ai_messages = [m for m in messages if isinstance(m, AIMessage)]
        if not ai_messages:
            return False
        return not any(cls._ai_message_has_visible_text(m) for m in ai_messages)

    @property
    def chars_per_token(self) -> float:
        """Calibrated characters-per-token ratio (4.0 until a measured call
        with its messages is recorded)."""
        return self._chars_per_token

    def chars_to_tokens(self, chars: int) -> int:
        """Convert transcript characters to tokens through the calibrated
        ratio (identical to ``chars // 4`` at the default ratio)."""
        return int(max(0, int(chars)) / self._chars_per_token)

    @property
    def occupancy(self) -> float | None:
        """``last_prompt_tokens / context_budget_tokens``, or None when either
        is unknown."""
        if self._last_prompt_tokens is None or not self._context_budget_tokens:
            return None
        return self._last_prompt_tokens / self._context_budget_tokens

    @property
    def below_start_gate(self) -> bool:
        """Whether the measured occupancy is under the start gate. An unknown
        occupancy (no measured call yet) counts as below: a session starts
        empty."""
        if self._start_ratio is None:
            return False
        ratio = self.occupancy
        return ratio is None or ratio < self._start_ratio

    @property
    def effective_image_scrub_depth(self) -> int:
        """The screenshot depth the scrub edge uses at the next render: the
        relaxed depth below the start gate, the tight depth from there up."""
        if self.below_start_gate:
            return self._image_scrub_depth_relaxed
        return self._image_scrub_depth

    def record_prompt_tokens(
        self, prompt_tokens: int | None, messages: list[Any] | None = None
    ) -> None:
        """Record the operator's measured prompt size for one call.

        Called by the ledger's owning operator (Flash runner / Pro operator)
        after each of its own LLM calls; within a multi-call turn the last
        (largest) call wins. Non-positive or missing values are ignored so a
        provider without usage metadata leaves the base unknown rather than
        zero (and the calibration state below is left untouched).

        When ``messages`` (the exact list sent to the model) accompanies a
        measured size, the call also calibrates :attr:`chars_per_token`,
        **differentially**: the ledger remembers the previous measured
        sample ``(text_chars, image_count, prompt_tokens)`` and, when the new
        sample carries the same number of images, grows the text by at least
        :data:`MIN_CALIBRATION_DELTA_CHARS` and costs more tokens, it pools
        the pair's differences (``pooled_chars += Δchars``,
        ``pooled_tokens += Δtokens``) and sets the ratio to
        ``pooled_chars / pooled_tokens``, clamped to [1.0, 6.0]. The pooled
        quotient is the maximum-likelihood single ratio for the session (no
        smoothing constant to tune, no last-pair noise). The reference is
        replaced only when it was consumed, when the image count changed, or
        when the prompt shrank in tokens (a compaction or scrub reset the
        baseline); a sample that moved too little keeps the reference, so
        several small turns accumulate into one usable difference instead of
        being discarded one by one (real sessions often grow < 2000 chars
        per turn and would otherwise never calibrate).

        Why differences: the provider's ``prompt_tokens`` also counts terms
        that are not in ``messages`` or not proportional to their text — the
        bound tool/function schemas, and full-size screenshots that tile to
        ~1100–1800 tokens rather than the flat :data:`IMAGE_BLOCK_TOKENS`.
        A whole-prompt quotient charges all of that to the text, and it
        also averages over the static prefix, whereas the chunker applies
        the ratio to the transcript *tail*, whose density differs. On real
        Gemini Operator traces (10 sessions, 2026-09) the two disagree by
        5-30% with a session-dependent sign, so the size-based chunk
        trigger fired early in some sessions and late in others. Between
        two prompts with the same image count the constant terms cancel and
        the difference measures the tail's own rate. Until the first
        usable difference exists the whole-prompt quotient
        ``text_chars / (prompt_tokens - images * IMAGE_BLOCK_TOKENS)`` is
        used as a fallback; once a pooled difference exists the pooled ratio
        is authoritative and the fallback is no longer applied.
        """
        try:
            value = int(prompt_tokens) if prompt_tokens is not None else 0
        except (TypeError, ValueError):
            return
        if value <= 0:
            return
        self._last_prompt_tokens = value
        if messages is None:
            return
        try:
            text_chars, image_count = measure_prompt_content(messages)
        except Exception as exc:
            logger.debug(f"Prompt calibration skipped: {exc}", exc_info=True)
            return
        if text_chars <= 0:
            return

        low, high = _CHARS_PER_TOKEN_BOUNDS
        previous = self._calibration_sample
        sample = (text_chars, image_count, value)

        if previous is None:
            self._calibration_sample = sample
        else:
            prev_chars, prev_images, prev_tokens = previous
            delta_chars = text_chars - prev_chars
            delta_tokens = value - prev_tokens
            if image_count != prev_images or delta_tokens < 0:
                # Not comparable (image set changed) or the prompt actually
                # shrank (compaction / scrub): start a fresh baseline here.
                self._calibration_sample = sample
            elif delta_chars >= MIN_CALIBRATION_DELTA_CHARS:
                if delta_tokens > 0:
                    self._pooled_delta_chars += delta_chars
                    self._pooled_delta_tokens += delta_tokens
                    pooled = self._pooled_delta_chars / self._pooled_delta_tokens
                    self._chars_per_token = min(high, max(low, pooled))
                    self._calibration_sample = sample
                    return
                self._calibration_sample = sample  # text grew at no cost: unusable
            # else: moved too little to measure yet (in either direction) —
            # keep the reference so the movement accumulates against it.

        if self._pooled_delta_tokens > 0:
            # A pooled difference already exists: it is authoritative, and
            # the whole-prompt quotient (which mis-charges schemas and image
            # tiling to the text) is never applied again.
            return
        text_tokens = max(1, value - image_count * IMAGE_BLOCK_TOKENS)
        self._chars_per_token = min(high, max(low, text_chars / text_tokens))

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
    # F region: restored history
    # ------------------------------------------------------------------

    @property
    def has_restored_history(self) -> bool:
        return bool(self._restored)

    def set_restored_history(self, text: str) -> None:
        """Install the cold-start frozen history block (empty ledger only)."""
        if self._restored:
            raise RuntimeError("TranscriptLedger restored history is already set.")
        if self._active or self._staged or self._turn_count:
            raise RuntimeError("Restored history can only seed an empty ledger (cold start).")
        self._restored = [HumanMessage(content=[{"type": "text", "text": text}])]

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
        extra_step_keys: tuple[str, ...] | list[str] = (),
    ) -> None:
        """Move the staged turn into the active region.

        Args:
            step_key: The DataEngine step id recorded for this turn; keys the
                turn's observation screenshot to its visual-transition summary
                job for the depth-K scrub.
            validator_result: The turn's validator report (``None`` when the
                turn executed no terminal action); appended as a frozen result
                message carrying the ``T+mm:ss`` session offset.
            extra_step_keys: Further DataEngine step ids recorded during the
                same turn (a Flash turn that executed several actions records
                one step per action). They join ``step_key`` in the turn's
                ``step_keys`` so chunk compression lists every step.
        """
        if self._staged is None:
            return
        staged = self._staged
        self._staged = None
        self._last_turn_silent = self._turn_is_silent(staged)

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

        step_keys: list[str] = []
        for key in (step_key, *extra_step_keys):
            if key is not None and str(key) not in step_keys:
                step_keys.append(str(key))
        self._turns.append(
            {
                "step_key": str(step_key) if step_key is not None else None,
                "step_keys": step_keys,
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

                # format_result_clean reports only errors/repairs; a dispatched
                # action renders as the bare status line.
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
                    "text": (f"{EXECUTION_RESULT_MARKER} ({self.elapsed_label()}) ---\n{rendered}"),
                }
            ]
        )

    # ------------------------------------------------------------------
    # F region: chunk compression
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
        """Total characters of the given turns' rendered transcripts (size
        trigger). Measured on the very text the chunk capsule receives, so
        the trigger sizes exactly what gets compressed."""
        return sum(len(self.turn_transcript(turn)) for turn in turns)

    def turn_transcript(self, turn: dict) -> str:
        """Plain-text rendering of one committed turn exactly as it currently
        stands in the active region (i.e. after the scrub edge), see
        :func:`render_turn_transcript`. Chunk compression feeds this to the
        segment capsule so the capsule digests precisely what it replaces."""
        return render_turn_transcript(self._active[turn["start"] : turn["end"]])

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

        Order per turn: the occupancy band is read once (from the operator's
        last measured prompt), the scrub edge advances over the active window
        at that band's screenshot depth, then the chunker runs (a triggered
        compression event deep-mutates the frozen region and advances the F/A
        boundary). Scrub-before-chunk guarantees that the turns a chunk
        closes over — all older than the sliding-window floor — have already
        had their screenshots resolved when the relaxed depth is no deeper
        than the floor plus one. The returned list is a fresh container:
        appending to it (the operator's in-turn tool loop) never mutates the
        ledger regions.
        """
        self._compressor.compress(self._active, image_scrub_depth=self.effective_image_scrub_depth)
        if self._chunker is not None:
            try:
                self._chunker.on_render(self)
            except Exception as e:
                logger.error(f"History chunker render hook failed: {e}")
        return [
            *self._static,
            *self._restored,
            *self._frozen_blocks,
            *self._active[self._active_start :],
            *tail,
        ]
