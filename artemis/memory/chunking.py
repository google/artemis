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

"""L2/L3 history compression: segment chunks, eras, and the emergency snapshot.

Implements §3.3 of the history-module redesign (M3). A **HistoryChunk** is a
*segmented ledger*, never a fused capsule: compression shrinks each step's
width, never the step count. Every chunk renders as three bands:

- **① Synopsis & effects** (LLM, :class:`StepCapsuleLens`): three-question
  prose (doing / did / effect — the effect must name the notes left behind)
  plus structured fields (verified_facts / unresolved / failed_paths /
  important_entities / entry_state / exit_state).
- **② Compressed step summary** (same LLM call): interval narrative whose
  union must cover the segment's step range seamlessly (machine-checked; a
  gap forces regeneration, bounded retries degrade to pending).
- **③ Per-step action ledger** (mechanical, zero-distortion,
  :func:`build_action_ledger`): one line per step — step number, ``T+mm:ss``
  session offset, ``format_action_clean`` semantic action, controller/
  validator result phrase; Failure-Analyzer recovery actions as indented
  sub-lines; user-injected instructions verbatim on their own never-evicted
  lines.

Triggers (:class:`HistoryChunkManager`): milestone switch (sole fact source:
the *stamped* ``subgoal_hash`` changing between consecutive recorded steps;
plan-write completions only queue an unconfirmed boundary hint), segment size
(steps / estimated source tokens), and the measured-token soft threshold.
The hard threshold collapses the frozen region to the L3 session snapshot
(chunk headers set-merged; a minimal per-step index survives). Chunk-count
overflow folds the oldest chunks into eras (① set-merged, ② degraded to
segment title lines, ③ kept); era overflow compresses the oldest eras into
the extreme-layer **period paragraph** (§10 decision 5, final user review
2026-09-01): one header line carrying the step range and start/end session
offsets plus a mechanically assembled one-paragraph synopsis (from the member
chunks' band-① fields, no extra LLM call), a ``recall_history`` guidance
line, and the verbatim never-evict user-injection lines. Step-level
addressability degrades to *period* addressability at this layer only; the
full per-step ledger stays recallable from the DataEngine.

Async discipline — **ready-gated swap** (user decision 2026-09-01, revising
the original M3 freeze-then-harvest form): a boundary/size/soft trigger only
*closes* a segment and dispatches its capsule; the original messages stay in
the transcript verbatim (lossless-pending, carried from the image layer to the
segment layer). The swap — freezing the segment's turns and rendering the
chunk block — happens at the first render at or after the capsule is ready.
Rationale: a header-less chunk has no ``verified_facts``, and a model that
sees "what happened" without "what was established" re-doubts verified facts
and loops. Degradation ladder: capsule generation failure (bounded retries
exhausted) keeps the original text and re-dispatches (the lens itself falls
back to a secondary model per attempt); soft-threshold pressure alone still
keeps the original text (correctness over tokens); only the hard threshold
force-swaps everything closed — pending chunks included — into the L3
snapshot. Checkpoint annotations are independent of the header (they bump the
DB version immediately) and render in pending blocks too; frozen text still
only re-renders at swap events, so it stays byte-stable in between.
"""

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Callable

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from artemis.memory.step_memory import JobKey, StepLens, StepMemoryService
from artemis.memory.transcript import format_session_offset
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

#: Recall guidance line rendered under an extreme-layer period paragraph
#: (an era whose per-step ledger overflowed to recall-only; §3.3).
RECALL_GUIDANCE_TEMPLATE = (
    "  (Step-level ledger via recall_history for steps {start}–{end})"
)

CHUNK_PENDING_NOTE = (
    "①/② capsule pending (background generation); the mechanical"
    " ledger below is complete. Original step records remain recallable"
    " via recall_history."
)

_NOTE_TOOLS = ("save_note", "update_note", "append_note")


# ---------------------------------------------------------------------------
# Band ③ — mechanical per-step action ledger
# ---------------------------------------------------------------------------


def _result_phrase(result: Any) -> str:
    """Controller/validator result phrase for one ledger line (mechanical)."""
    if not isinstance(result, dict) or not result:
        return "no terminal action"
    from artemis.utils.task_tree import format_result_clean

    detail = None
    try:
        detail = format_result_clean(result)
    except Exception:
        detail = None
    if detail:
        return detail
    status = result.get("status")
    if status == "success":
        return "executed"
    return str(status) if status else "executed"


def _action_phrase(step: dict) -> str:
    """Semantic action phrase for one step (``format_action_clean``)."""
    from artemis.utils.task_tree import format_action_clean

    action = step.get("action_taken")
    text = format_action_clean(action)
    if isinstance(action, list) and len(action) > 1:
        text += f" (+{len(action) - 1} more actions)"
    return text


def _fa_recovery_lines(step: dict) -> list[str]:
    """Failure-Analyzer device actions of a step, rendered mechanically."""
    from artemis.utils.task_tree import format_tool_call_clean

    lines: list[str] = []
    for event in step.get("interleaved_events") or []:
        name = event.get("name") or ""
        if (
            event.get("type") == "tool_call"
            and name.startswith("_exec_")
            and name != "report_failure_analysis"
        ):
            try:
                rendered = format_tool_call_clean(
                    name, event.get("args") or {}, event.get("result")
                )
            except Exception:
                rendered = None
            if rendered:
                lines.append(rendered)
    return lines


def step_offset_label(step: dict, session_start: float | None) -> str:
    """``T+mm:ss`` session-start offset of a step (byte-stable once frozen)."""
    ts = step.get("timestamp")
    if session_start is None or not isinstance(ts, (int, float)):
        return "T+??:??"
    return format_session_offset(float(ts) - float(session_start))


def injected_instruction_line(step: dict) -> str | None:
    """The never-evict verbatim user-injection line for a step, if any."""
    instr = (step.get("extra_metadata") or {}).get("injected_instruction")
    if not instr:
        return None
    return f'  User @ Step {step.get("step_number")}: "{instr}"'


def build_action_ledger(
    steps: list[dict],
    session_start: float | None,
    *,
    minimal: bool = False,
) -> str:
    """Assemble band ③ mechanically (1:1, never elided, no LLM involved).

    ``minimal=True`` renders the L3 minimum-width index (step number + offset
    + action phrase). User-injected instruction lines are preserved verbatim
    at every width — they are never evicted at any compression level.
    """
    lines: list[str] = []
    for step in steps:
        number = step.get("step_number")
        offset = step_offset_label(step, session_start)
        action = _action_phrase(step)
        if minimal:
            lines.append(f"- Step {number} ({offset}): {action}")
        else:
            lines.append(f"- Step {number} ({offset}): {action} -> {_result_phrase(step.get('last_execution_result'))}")
            for recovery in _fa_recovery_lines(step):
                lines.append(f"    FA: {recovery}")
        user_line = injected_instruction_line(step)
        if user_line:
            lines.append(user_line)
    return "\n".join(lines)


def extract_note_writes(step: dict) -> list[dict[str, str]]:
    """Notes written during a step (band ① input; task_plan writes excluded)."""
    writes: list[dict[str, str]] = []
    for event in step.get("interleaved_events") or []:
        if event.get("type") != "tool_call" or event.get("name") not in _NOTE_TOOLS:
            continue
        args = event.get("args") or {}
        key = args.get("key")
        if not key or key == "task_plan":
            continue
        gist = args.get("content") or args.get("replacement") or ""
        writes.append({"tool": event["name"], "key": str(key), "gist": str(gist)[:300]})
    return writes


# ---------------------------------------------------------------------------
# Band ①+② — StepCapsuleLens (single LLM call) + machine-checked coverage
# ---------------------------------------------------------------------------


def validate_interval_coverage(intervals: Any, start_step: int, end_step: int) -> bool:
    """Band ② hard constraint: the interval union covers [start, end] seamlessly."""
    if not isinstance(intervals, list) or not intervals:
        return False
    expected = start_step
    for interval in intervals:
        if not isinstance(interval, dict):
            return False
        try:
            s = int(interval.get("start_step"))
            e = int(interval.get("end_step"))
        except (TypeError, ValueError):
            return False
        if s != expected or e < s or not str(interval.get("text") or "").strip():
            return False
        expected = e + 1
    return expected == end_step + 1


_CAPSULE_REQUIRED_KEYS = ("doing", "did", "effect", "intervals")
_CAPSULE_LIST_KEYS = ("verified_facts", "unresolved", "failed_paths", "important_entities")


def collect_note_keys(payload: dict[str, Any]) -> list[str]:
    """Deduplicated note keys written during the segment (payload order)."""
    keys: list[str] = []
    seen: set[str] = set()
    for step in payload.get("steps") or []:
        for write in step.get("note_writes") or []:
            key = str(write.get("key") or "").strip()
            if key and key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    return match.group(1) if match else text


class StepCapsuleLens(StepLens):
    """Chunk-level lens producing bands ①+② in one call (§5).

    The payload carries the segment's per-step visual summaries, exact
    actions, validator results, reasoning excerpts, notes writes, and
    injected instructions. The rendered summary string is the JSON capsule;
    an attempt whose interval union fails the mechanical coverage check
    returns ``None`` so the bounded service retry regenerates it, and on
    exhaustion the chunk simply stays pending (③ is independently usable).
    """

    name = "step_capsule"

    _PROMPT_PATH = Path(__file__).parent / "step_capsule.md"

    def __init__(
        self,
        model_name: str | None = None,
        llm: Any | None = None,
        *,
        ctx: Any = None,
        fallback_model_name: str | None = None,
        fallback_llm: Any | None = None,
    ):
        self._model_name = model_name or "gemini-3.7-flash"
        self._llm = llm
        self._ctx = ctx
        # Availability hardening: `chunking.model` is a dedicated model with no
        # gateway behind it — when that one endpoint is down (e.g. a day-long
        # 503), every capsule dies and chunk headers stay pending forever. A
        # configured fallback model turns a provider outage into a degraded
        # attempt instead of a dead loop.
        self._fallback_model_name = (
            fallback_model_name if fallback_model_name != self._model_name else None
        )
        self._fallback_llm = fallback_llm
        try:
            self._prompt = self._PROMPT_PATH.read_text(encoding="utf-8")
        except Exception:
            self._prompt = (
                "You compress one segment of executed steps into a JSON capsule with"
                " keys doing/did/effect/entry_state/exit_state/verified_facts/"
                "unresolved/failed_paths/important_entities/intervals. Never use"
                " verdict words (successfully, completed, failed, ...); intervals"
                " must cover the step range seamlessly. Return only JSON."
            )

    def _get_llm(self):
        if self._llm is None:
            from artemis.services.llm import get_google_llm

            self._llm = get_google_llm(model_name=self._model_name, temperature=0.0)
        return self._llm

    def _get_fallback_llm(self):
        if self._fallback_llm is None and self._fallback_model_name:
            from artemis.services.llm import get_google_llm

            self._fallback_llm = get_google_llm(
                model_name=self._fallback_model_name, temperature=0.0
            )
        return self._fallback_llm

    @property
    def has_fallback(self) -> bool:
        return self._fallback_llm is not None or bool(self._fallback_model_name)

    @property
    def model_name(self) -> str:
        return self._model_name

    def build_messages(self, payload: dict[str, Any]) -> list[BaseMessage]:
        start = payload.get("start_step")
        end = payload.get("end_step")
        label = payload.get("milestone_label")
        header = [f"SEGMENT: Steps {start}–{end}"]
        if label:
            header.append(f"MILESTONE: {label}")

        blocks: list[str] = ["\n".join(header)]
        for step in payload.get("steps", []):
            part = [
                f"## Step {step.get('step_number')} ({step.get('offset')})",
                f"Action: {step.get('action')}",
                f"Recorded result: {step.get('outcome')}",
            ]
            if step.get("visual_summary"):
                part.append(f"Visual transition: {step['visual_summary']}")
            if step.get("thinking_excerpt"):
                part.append(f"Reasoning excerpt: {step['thinking_excerpt']}")
            for write in step.get("note_writes", []):
                part.append(
                    f"Note written ({write['tool']} -> {write['key']}): {write['gist']}"
                )
            if step.get("injected_instruction"):
                part.append(
                    f'User injected instruction: "{step["injected_instruction"]}"'
                )
            for fa in step.get("fa_actions", []):
                part.append(f"Failure-Analyzer recovery: {fa}")
            blocks.append("\n".join(part))

        return [
            SystemMessage(content=self._prompt),
            HumanMessage(content="\n\n".join(blocks)),
        ]

    def parse_capsule(self, text: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Parse + machine-check one capsule; None on any violation."""
        try:
            parsed = json.loads(_strip_code_fences(text))
        except Exception:
            return None
        if not isinstance(parsed, dict):
            return None
        for key in _CAPSULE_REQUIRED_KEYS:
            if key not in parsed:
                return None
        for key in _CAPSULE_LIST_KEYS:
            value = parsed.get(key)
            parsed[key] = [str(v) for v in value] if isinstance(value, list) else []
        parsed.setdefault("entry_state", "")
        parsed.setdefault("exit_state", "")
        if not validate_interval_coverage(
            parsed.get("intervals"), int(payload["start_step"]), int(payload["end_step"])
        ):
            logger.warning(
                "StepCapsuleLens: interval union does not cover Steps"
                f" {payload['start_step']}–{payload['end_step']}; regenerating."
            )
            return None
        # Notes linkage is a machine check, same rank as ② coverage (user
        # decision 2026-09-01): every note key written during the segment must
        # surface in the band-① text, or the capsule regenerates.
        note_keys = collect_note_keys(payload)
        if note_keys:
            searchable = " ".join(
                [
                    str(parsed.get(field) or "")
                    for field in ("doing", "did", "effect", "entry_state", "exit_state")
                ]
                + [v for key in _CAPSULE_LIST_KEYS for v in parsed.get(key) or []]
            )
            missing = [key for key in note_keys if key not in searchable]
            if missing:
                logger.warning(
                    f"StepCapsuleLens: capsule omits note key(s) {missing}"
                    " written during the segment; regenerating."
                )
                return None
        return parsed

    async def _invoke(self, llm: Any, messages: list[BaseMessage], model_label: str):
        response = await asyncio.wait_for(llm.ainvoke(messages), timeout=90.0)
        # Raw-model bypass metering (gateway-wrapped models meter themselves);
        # capsule prompts are small and must not clobber the session's
        # last_prompt_tokens (the compaction thresholds' live context base).
        try:
            from artemis.services.llm import RobustChatModelWrapper
            from artemis.services.token_meter import record_llm_usage

            if not isinstance(llm, RobustChatModelWrapper):
                engine = getattr(self._ctx, "data_engine", None) if self._ctx else None
                record_llm_usage(
                    engine,
                    response,
                    source=f"lens:step_capsule:{model_label}",
                    update_last_prompt=False,
                )
        except Exception:
            pass
        return response

    async def render(self, key: JobKey, payload: dict[str, Any]) -> str | None:
        messages = self.build_messages(payload)
        if self.has_fallback:
            from artemis.services.llm import with_fallback

            response = await with_fallback(
                lambda: self._invoke(self._get_llm(), messages, self._model_name),
                lambda: self._invoke(
                    self._get_fallback_llm(),
                    messages,
                    self._fallback_model_name or "fallback",
                ),
                none_should_fallback=False,
            )
        else:
            response = await self._invoke(self._get_llm(), messages, self._model_name)
        content = getattr(response, "content", "")
        if isinstance(content, list):
            content = "".join(
                b.get("text", "") for b in content if isinstance(b, dict) and "text" in b
            )
        parsed = self.parse_capsule(str(content), payload)
        if parsed is None:
            return None
        return json.dumps(parsed, ensure_ascii=False)


class ChunkCapsuleService(StepMemoryService):
    """Background runtime for chunk capsules (bands ①②) on the shared skeleton."""

    def __init__(self, ctx: Any, lens: StepCapsuleLens, **kwargs):
        super().__init__(ctx, lens=lens, **kwargs)

    @property
    def lens(self) -> StepCapsuleLens:
        return self._lens  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# In-memory chunk / era mirrors
# ---------------------------------------------------------------------------


@dataclass
class ChunkState:
    """In-memory mirror of one chunk (authoritative for rendering & versions)."""

    ordinal: int
    start_step_number: int
    end_step_number: int
    start_step_id: str | None
    end_step_id: str | None
    source_step_ids: list[str]
    subgoal_hash: str | None
    milestone_label: str | None
    start_offset: str
    end_offset: str
    band3: str
    minimal_index: str
    user_lines: list[str]
    version: int = 1
    status: str = "pending"
    band1: dict[str, Any] = field(default_factory=dict)
    band2: str | None = None
    annotations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def capsule_key(self) -> str:
        return f"chunk:{self.start_step_number}-{self.end_step_number}"

    @property
    def step_range_label(self) -> str:
        return f"Steps {self.start_step_number}–{self.end_step_number}"


@dataclass
class EraState:
    """A group of merged chunks: ① set-merged, ② title lines, ③ retained."""

    ordinal: int
    chunks: list[ChunkState]
    recall_only: bool = False

    @property
    def start_step_number(self) -> int:
        return self.chunks[0].start_step_number

    @property
    def end_step_number(self) -> int:
        return self.chunks[-1].end_step_number


def merge_structured_fields(band1_list: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Order-preserving set-merge of the chunks' structured fields (no re-summarizing)."""
    merged: dict[str, list[str]] = {key: [] for key in _CAPSULE_LIST_KEYS}
    for band1 in band1_list:
        for key in _CAPSULE_LIST_KEYS:
            for value in band1.get(key) or []:
                if value not in merged[key]:
                    merged[key].append(value)
    return merged


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_band2(band1: dict[str, Any]) -> str:
    lines = []
    for interval in band1.get("intervals") or []:
        s, e = interval.get("start_step"), interval.get("end_step")
        span = f"Step {s}" if s == e else f"Steps {s}–{e}"
        lines.append(f"  - {span}: {interval.get('text')}")
    return "\n".join(lines)


def _render_annotations(chunk: ChunkState) -> list[str]:
    if not chunk.annotations:
        return []
    lines = ["  Post-hoc check results:"]
    for ann in chunk.annotations:
        lines.append(
            f"    - [{ann.get('kind', 'check')}] '{ann.get('item_text', '')}'"
            f" → {ann.get('status', '?')} ({ann.get('evidence', '')})"
        )
    return lines


def render_chunk_block(chunk: ChunkState) -> str:
    """Full three-band chunk block, strictly in §3.3 order (① → ② → ③)."""
    milestone = f' | Milestone "{chunk.milestone_label}"' if chunk.milestone_label else ""
    header = (
        f"[Chunk {chunk.ordinal}{milestone} | {chunk.step_range_label}"
        f" | {chunk.start_offset} → {chunk.end_offset}]"
    )
    parts = [header, ""]
    if chunk.status == "ready" and chunk.band1:
        b1 = chunk.band1
        parts.append("① Synopsis & effects")
        parts.append(f"  What this segment was doing: {b1.get('doing', '')}")
        parts.append(f"  What was actually done: {b1.get('did', '')}")
        parts.append(f"  Effects / left behind: {b1.get('effect', '')}")
        parts.append(
            f"  Entry: {b1.get('entry_state', '')}    Exit: {b1.get('exit_state', '')}"
        )
        parts.append(f"  Verified: {'; '.join(b1.get('verified_facts') or []) or '-'}")
        parts.append(f"  Unresolved: {'; '.join(b1.get('unresolved') or []) or '-'}")
        parts.append(f"  Failed paths: {'; '.join(b1.get('failed_paths') or []) or '-'}")
        parts.append(f"  Entities: {'; '.join(b1.get('important_entities') or []) or '-'}")
        parts.extend(_render_annotations(chunk))
        parts.append("")
        parts.append("② Compressed step summary")
        parts.append(chunk.band2 or render_band2(chunk.band1))
    else:
        parts.append(CHUNK_PENDING_NOTE)
        parts.extend(_render_annotations(chunk))
    parts.append("")
    parts.append("③ Step action ledger")
    parts.append(chunk.band3)
    return "\n".join(parts)


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")

#: Note references inside a band-① ``effect`` (note keys are file-shaped:
#: ``notes/<key>`` paths or ``*.md`` files) — mechanical extraction only.
_NOTE_REF_RE = re.compile(r"\bnotes/[\w\-.]+|\b[\w\-./]+\.md\b")

#: Cap on merged verified_facts quoted inside a period paragraph.
_PERIOD_FACTS_LIMIT = 4


def _first_sentence(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    return _SENTENCE_SPLIT_RE.split(text, 1)[0].strip().rstrip(".。")


def render_era_period_paragraph(era: EraState) -> str:
    """One-paragraph period synopsis for a recall-only era (§3.3 extreme layer).

    Mechanically assembled from the member chunks' band-① fields — never an
    extra LLM call: the first non-empty ``doing``, each chunk's first ``did``
    sentence chained in order (a band-①-less/pending chunk falls back to its
    milestone label or step range), the merged note references found in the
    ``effect`` texts, and the first few merged ``verified_facts``. When no
    chunk has a band ① at all, it degrades to the milestone-label list with
    step ranges — the paragraph is never empty.
    """
    ready = [c for c in era.chunks if c.band1]
    if not ready:
        labels = [
            f"{c.milestone_label or 'segment'} ({c.step_range_label})"
            for c in era.chunks
        ]
        return "Milestones: " + "; ".join(labels) + "."

    sentences: list[str] = []
    doing = next(
        (
            str(c.band1.get("doing") or "").strip()
            for c in era.chunks
            if c.band1 and str(c.band1.get("doing") or "").strip()
        ),
        "",
    )
    if doing:
        sentences.append(doing if doing.endswith((".", "!", "?", "。", "！", "？")) else doing + ".")

    did_parts: list[str] = []
    for chunk in era.chunks:
        did = _first_sentence(str(chunk.band1.get("did") or "")) if chunk.band1 else ""
        if not did:
            did = chunk.milestone_label or (
                f"steps {chunk.start_step_number}–{chunk.end_step_number}"
            )
        did_parts.append(did)
    sentences.append("Did: " + "; ".join(did_parts) + ".")

    note_refs: list[str] = []
    for chunk in ready:
        for ref in _NOTE_REF_RE.findall(str(chunk.band1.get("effect") or "")):
            if ref not in note_refs:
                note_refs.append(ref)
    if note_refs:
        sentences.append("Notes left: " + ", ".join(note_refs) + ".")

    facts = merge_structured_fields([c.band1 for c in ready])["verified_facts"]
    if facts:
        sentences.append("Verified: " + "; ".join(facts[:_PERIOD_FACTS_LIMIT]) + ".")
    return " ".join(sentences)


def render_era_block(era: EraState) -> str:
    """Era block: ① merged headers, ② degraded to titles, ③ per-chunk ledgers.

    A recall-only era instead renders as the extreme-layer *period paragraph*
    (§10 decision 5, final review 2026-09-01): step range + start/end session
    offsets in the header (period addressability — the coarse foreign key for
    video alignment), a mechanically assembled synopsis, the recall guidance
    line, and the verbatim never-evict user-injection lines.
    """
    if era.recall_only:
        lines = [
            (
                f"[Era {era.ordinal} | Steps {era.start_step_number}–{era.end_step_number}"
                f" | {era.chunks[0].start_offset} → {era.chunks[-1].end_offset}]"
                f" {render_era_period_paragraph(era)}"
            ),
            RECALL_GUIDANCE_TEMPLATE.format(
                start=era.start_step_number, end=era.end_step_number
            ),
        ]
        # Never-evict: user-injected instruction lines survive even recall-only.
        for chunk in era.chunks:
            lines.extend(chunk.user_lines)
        return "\n".join(lines)

    merged = merge_structured_fields([c.band1 for c in era.chunks if c.band1])
    entry = next((c.band1.get("entry_state") for c in era.chunks if c.band1), "") or ""
    exit_state = ""
    for chunk in reversed(era.chunks):
        if chunk.band1:
            exit_state = chunk.band1.get("exit_state") or ""
            break

    parts = [
        (
            f"[Era {era.ordinal} | Steps {era.start_step_number}–{era.end_step_number}"
            f" | {era.chunks[0].start_offset} → {era.chunks[-1].end_offset}"
            f" | merged from {len(era.chunks)} chunks]"
        ),
        "",
        "① Merged synopsis (structured fields, set-merged)",
        f"  Entry: {entry}    Exit: {exit_state}",
        f"  Verified: {'; '.join(merged['verified_facts']) or '-'}",
        f"  Unresolved: {'; '.join(merged['unresolved']) or '-'}",
        f"  Failed paths: {'; '.join(merged['failed_paths']) or '-'}",
        f"  Entities: {'; '.join(merged['important_entities']) or '-'}",
        "",
        "② Segment titles",
    ]
    for chunk in era.chunks:
        title = chunk.milestone_label
        if not title and chunk.band1:
            title = str(chunk.band1.get("did") or "").split(".")[0]
        parts.append(f"  - {chunk.step_range_label}: {title or 'segment'}")
        parts.extend(_render_annotations(chunk))
    parts.append("")
    parts.append("③ Step action ledger")
    for chunk in era.chunks:
        parts.append(chunk.band3)
    return "\n".join(parts)


def render_l3_snapshot(
    eras: list[EraState],
    chunks: list[ChunkState],
    *,
    goal: str | None,
    plan_text: str | None,
) -> str:
    """L3 emergency snapshot: merged knowledge plane + minimal per-step index.

    The chunk headers are mechanically set-merged (no summary-of-summary); the
    per-step skeleton survives at minimal width so step-level time perception
    never breaks (§3.3 L3).
    """
    all_chunks = [c for era in eras for c in era.chunks] + list(chunks)
    merged = merge_structured_fields([c.band1 for c in all_chunks if c.band1])
    exit_state = ""
    for chunk in reversed(all_chunks):
        if chunk.band1:
            exit_state = chunk.band1.get("exit_state") or ""
            break

    start = all_chunks[0].start_step_number if all_chunks else 0
    end = all_chunks[-1].end_step_number if all_chunks else 0
    parts = [
        f"[Session snapshot | Steps {start}–{end} | hard-threshold fallback]",
        f"Overall goal: {goal or '-'}",
        "Plan state:",
        (plan_text or "-").rstrip(),
        f"Verified facts: {'; '.join(merged['verified_facts']) or '-'}",
        f"Unresolved: {'; '.join(merged['unresolved']) or '-'}",
        f"Failed paths: {'; '.join(merged['failed_paths']) or '-'}",
        f"Important entities: {'; '.join(merged['important_entities']) or '-'}",
        f"Device/app state: {exit_state or '-'}",
        "",
        "--- Step index (minimal width, per chunk) ---",
    ]
    for chunk in all_chunks:
        parts.append(f"[{chunk.step_range_label}]")
        parts.append(chunk.minimal_index)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# HistoryChunkManager — triggers, compression events, F-region rendering
# ---------------------------------------------------------------------------


class HistoryChunkManager:
    """Owner of L2/L3 compression policy over a :class:`TranscriptLedger`.

    Fed by the graph (stamped subgoal hashes, plan-write boundary hints,
    checkpoint harvests) and consulted by the ledger at render time
    (:meth:`on_render`). All deep mutations of the frozen region happen inside
    a compression event; between events the frozen blocks are byte-stable.
    """

    def __init__(
        self,
        *,
        engine: Any = None,
        ctx: Any = None,
        chunking_config: Any = None,
        transcript_config: Any = None,
        capsule_service: StepMemoryService | None = None,
        meter_getter: Callable[[], int | None] | None = None,
        goal: str | None = None,
    ):
        self._engine = engine
        self._ctx = ctx
        self._goal = goal

        cc = chunking_config
        self._max_steps = int(getattr(cc, "max_steps", 12) or 12)
        self._target_source_tokens = int(getattr(cc, "target_source_tokens", 2000) or 2000)
        self._model_name = getattr(cc, "model", None) or "gemini-3.7-flash"
        self._max_chunks = int(getattr(cc, "max_chunks", 8) or 8)
        # Independent era cap (M5): None follows max_chunks (pre-M5 behavior).
        self._max_eras = int(getattr(cc, "max_eras", None) or self._max_chunks)

        tc = transcript_config
        self._budget = int(getattr(tc, "context_budget_tokens", 80000) or 80000)
        self._soft_ratio = float(getattr(tc, "soft_ratio", 0.7) or 0.7)
        self._hard_ratio = float(getattr(tc, "hard_ratio", 0.9) or 0.9)
        self._min_active_steps = int(getattr(tc, "min_active_steps", 5) or 5)

        self._meter_getter = meter_getter
        self._capsule_service = capsule_service or self._build_capsule_service(ctx)

        # Trigger state (fed from the graph).
        self._step_hashes: dict[str, str] = {}
        self._last_hash: str | None = None
        self._boundary_hint_pending = False

        # Mirrors. ``_chunks``/``_eras`` hold SWAPPED chunks (their turns are
        # frozen out of the transcript); ``_awaiting`` holds closed segments
        # whose original turns still live in the transcript until their
        # capsule header is ready (ready-gated swap). An entry's ``chunk`` is
        # None when the segment had no step records — it can then only leave
        # the queue through the hard-threshold emergency swap.
        self._chunks: list[ChunkState] = []
        self._eras: list[EraState] = []
        self._awaiting: list[dict[str, Any]] = []
        self._chunk_counter = 0
        self._era_counter = 0

    def _build_capsule_service(self, ctx: Any) -> StepMemoryService:
        kwargs: dict[str, Any] = {}
        try:
            from artemis.config import load_agent_config

            runtime = load_agent_config().memory.runtime
            kwargs = {
                "retry_limit": runtime.retry_limit,
                "max_concurrency": runtime.max_concurrency,
                "flush_timeout_s": runtime.flush_timeout_s,
            }
        except Exception:
            pass
        lens = StepCapsuleLens(
            self._model_name,
            ctx=ctx,
            fallback_model_name=self._resolve_capsule_fallback_model(ctx),
        )
        return ChunkCapsuleService(ctx, lens, **kwargs)

    def _resolve_capsule_fallback_model(self, ctx: Any) -> str | None:
        """Fallback model for capsule generation when `chunking.model` is down.

        Resolved from the LLM config's summarizer role (which inherits the
        global default fallback unless overridden). Only same-provider (google)
        fallbacks apply — the capsule lens rides the raw google model path.
        """
        try:
            llm_cfg = getattr(ctx, "llm_config", None) if ctx is not None else None
            if llm_cfg is None:
                from artemis.config.llm import get_default_llm_config

                llm_cfg = get_default_llm_config()
            fallback = getattr(getattr(llm_cfg, "summarizer", None), "fallback", None)
            provider = str(getattr(fallback, "provider", "") or "")
            model = getattr(fallback, "model", None)
            if model and provider in ("google", "gemini") and model != self._model_name:
                return str(model)
        except Exception:
            pass
        return None

    @property
    def capsule_service(self) -> StepMemoryService:
        return self._capsule_service

    @property
    def chunks(self) -> tuple[ChunkState, ...]:
        return tuple(self._chunks)

    @property
    def eras(self) -> tuple[EraState, ...]:
        return tuple(self._eras)

    @property
    def awaiting_chunks(self) -> tuple[ChunkState, ...]:
        """Closed-but-unswapped chunks (their original turns are still live)."""
        return tuple(e["chunk"] for e in self._awaiting if e["chunk"] is not None)

    @property
    def boundary_hint_pending(self) -> bool:
        return self._boundary_hint_pending

    # ------------------------------------------------------------------
    # Graph-fed trigger events
    # ------------------------------------------------------------------

    def queue_boundary_hint(self) -> None:
        """A plan write completed a top-level milestone: queue an *unconfirmed*
        boundary. Only the next stamped step confirms it (a hash change); a
        stamp without a hash change (e.g. the write was vetoed and rolled
        back) discards the hint — pseudo-switch protection (§3.3)."""
        self._boundary_hint_pending = True

    def on_step_stamped(self, step_id: str, subgoal_hash: str | None) -> None:
        """Record one executed step's stamped subgoal hash (the sole milestone
        fact source: a change between consecutive stamps IS the switch)."""
        stamped = subgoal_hash or "default"
        self._step_hashes[str(step_id)] = stamped
        if self._last_hash is not None and stamped != self._last_hash:
            if self._boundary_hint_pending:
                logger.info("HistoryChunkManager: queued boundary confirmed by stamp change.")
            self._boundary_hint_pending = False
        elif self._boundary_hint_pending:
            logger.info(
                "HistoryChunkManager: queued boundary NOT confirmed by the next"
                " stamped step (plan write likely rolled back); discarding."
            )
            self._boundary_hint_pending = False
        self._last_hash = stamped

    def annotate_from_checkpoint(
        self, checkpoint_id: str, verdicts: list[dict[str, Any]]
    ) -> bool:
        """Post-hoc annotation: a harvested checkpoint verdict landed after its
        segment was chunked. The matching chunk gains an annotation and a
        version bump (DB immediately; the frozen text re-renders only at the
        next compression event)."""
        if not verdicts:
            return False
        aliases = self._subgoal_aliases(checkpoint_id)
        annotated = False
        for chunk in self._all_chunks():
            if chunk.subgoal_hash and chunk.subgoal_hash in aliases:
                chunk.annotations.extend(verdicts)
                chunk.version += 1
                self._persist(chunk)
                annotated = True
        return annotated

    def _subgoal_aliases(self, checkpoint_id: str) -> set[str]:
        aliases = {checkpoint_id}
        try:
            from artemis.utils.task_tree import get_all_subgoal_aliases

            base_dir = getattr(self._engine, "base_dir", None)
            if base_dir:
                aliases |= set(get_all_subgoal_aliases(checkpoint_id, base_dir))
        except Exception:
            pass
        return aliases

    def _all_chunks(self) -> list[ChunkState]:
        return (
            [c for era in self._eras for c in era.chunks]
            + self._chunks
            + [e["chunk"] for e in self._awaiting if e["chunk"] is not None]
        )

    # ------------------------------------------------------------------
    # Render-time compression events
    # ------------------------------------------------------------------

    def on_render(self, ledger) -> None:
        """Evaluate triggers and, when one fires, run one compression event."""
        try:
            self._on_render_inner(ledger)
        except Exception as e:
            logger.error(f"History chunk compression event failed: {e}")

    def _on_render_inner(self, ledger) -> None:
        base_tokens = self._context_base_tokens()
        soft = base_tokens is not None and base_tokens >= self._budget * self._soft_ratio
        hard = base_tokens is not None and base_tokens >= self._budget * self._hard_ratio

        closed_any = self._close_new_segments(ledger, soft)
        if closed_any or soft or hard:
            # Failure rung of the degradation ladder: exhausted capsules are
            # re-dispatched (the lens retries a fallback model per attempt);
            # the original text stays until one attempt lands. Cadence is
            # bounded to trigger/pressure renders, never every render.
            self._redispatch_failed_capsules()
        self._harvest_capsules()
        self._swap_ready_segments(ledger, hard=hard)

    def _close_new_segments(self, ledger, soft: bool) -> bool:
        """Trigger evaluation: close due segments and dispatch their capsules.

        Closing NEVER freezes turns (ready-gated swap): the segment's original
        messages stay live in the transcript; a closed segment joins the
        ``_awaiting`` queue until its capsule header is ready.
        """
        turns = ledger.unchunked_turns()
        if len(turns) <= self._min_active_steps:
            return False  # sliding-window floor: nothing is eligible
        eligible_count = len(turns) - self._min_active_steps
        claimed = sum(len(e["turns"]) for e in self._awaiting)
        if eligible_count <= claimed:
            return False
        remaining = turns[claimed:]
        remaining_eligible = eligible_count - claimed

        segments = self._partition(remaining)
        if not segments:
            return False

        # Milestone trigger: a *complete* closed segment — one that ended (a
        # later segment follows, or its hash differs from the current stamp)
        # and has fully aged past the sliding-window floor — closes whole
        # (§3.3: the previous segment becomes ONE HistoryChunk; the floor only
        # delays the event, it never splits the segment). The trailing segment
        # is subject to the size/soft triggers over its eligible portion only.
        selected: list[tuple[str | None, list[dict]]] = []
        open_hash = self._last_hash

        consumed_prefix = 0
        tail_segment: tuple[str | None, list[dict]] | None = None
        for idx, segment in enumerate(segments):
            seg_hash, seg_turns = segment
            is_last = idx == len(segments) - 1
            closed = (not is_last) or seg_hash != open_hash
            if closed and consumed_prefix + len(seg_turns) <= remaining_eligible:
                selected.append(segment)
                consumed_prefix += len(seg_turns)
                continue
            tail_segment = segment
            break

        milestone_event = bool(selected)

        tail_portion: list[dict] = []
        if tail_segment is not None:
            tail_portion = tail_segment[1][: max(0, remaining_eligible - consumed_prefix)]

        size_event = False
        if tail_segment is not None and tail_portion:
            tail_chars = ledger.turn_text_chars(tail_portion)
            size_event = (
                len(tail_segment[1]) >= self._max_steps
                or (tail_chars // 4) >= self._target_source_tokens
            )

        if size_event:
            selected.append((tail_segment[0], tail_portion))
        elif not milestone_event and soft and tail_portion:
            # Soft threshold with nothing else due: close the oldest open
            # segment's eligible portion (bounded by the chunk size cap).
            selected.append((tail_segment[0], tail_portion[: self._max_steps]))

        if not selected:
            return False

        steps_by_id = self._load_steps_by_id()
        for seg_hash, seg_turns in selected:
            for slice_turns in self._slices(seg_turns):
                chunk = self._create_chunk(slice_turns, seg_hash, steps_by_id)
                self._awaiting.append({"chunk": chunk, "turns": list(slice_turns)})
        logger.info(
            f"History segments closed (ready-gated): {len(self._awaiting)} awaiting"
            " capsule headers; original turns retained until ready."
        )
        return True

    def _redispatch_failed_capsules(self) -> None:
        for entry in self._awaiting:
            chunk = entry["chunk"]
            if chunk is None or chunk.status != "pending":
                continue
            key = chunk.capsule_key
            try:
                if not self._capsule_service.has_failed(key):
                    continue
                payload = self._capsule_service.get_job_payload(key)
                if payload is None:
                    continue
                logger.info(
                    f"Re-dispatching failed capsule {key}; original text is"
                    " retained until a capsule lands."
                )
                self._capsule_service.submit(key, payload)
            except Exception as e:
                logger.error(f"Capsule re-dispatch for {key} failed: {e}")

    def _swap_ready_segments(self, ledger, *, hard: bool) -> None:
        """Swap the ready prefix of awaiting segments into the frozen region.

        Swaps consume the transcript's oldest unchunked turns, so only a
        contiguous READY prefix may swap — an older pending segment keeps its
        (and every younger segment's) original text live. The hard threshold
        is the sole exception: everything closed force-swaps, pending chunks
        included, and the frozen region renders as the L3 snapshot.
        """
        if not self._awaiting:
            return
        swap: list[dict[str, Any]] = []
        if hard:
            swap, self._awaiting = self._awaiting, []
        else:
            while self._awaiting:
                chunk = self._awaiting[0]["chunk"]
                if chunk is None or chunk.status != "ready":
                    break
                swap.append(self._awaiting.pop(0))
        if not swap:
            return

        for entry in swap:
            if entry["chunk"] is not None:
                self._chunks.append(entry["chunk"])
        self._fold_eras()
        blocks = self._render_frozen_blocks(hard=hard)
        consumed = sum(len(e["turns"]) for e in swap)
        ledger.freeze_turns(consumed, blocks)
        logger.info(
            f"History compression swap: froze {consumed} turns"
            f" ({len(self._chunks)} chunks, {len(self._eras)} eras,"
            f" {len(self._awaiting)} still awaiting, hard={hard})."
        )

    def _partition(self, eligible: list[dict]) -> list[tuple[str | None, list[dict]]]:
        """Split eligible turns into consecutive same-hash segments."""
        segments: list[tuple[str | None, list[dict]]] = []
        current_hash: str | None = None
        current: list[dict] = []
        for turn in eligible:
            key = turn.get("step_key")
            stamped = self._step_hashes.get(str(key)) if key is not None else None
            if stamped is None:
                stamped = current_hash  # unknown stamps join the running segment
            if current and stamped == current_hash:
                current.append(turn)
            else:
                if current:
                    segments.append((current_hash, current))
                current_hash = stamped
                current = [turn]
        if current:
            segments.append((current_hash, current))
        return segments

    def _slices(self, seg_turns: list[dict]) -> list[list[dict]]:
        return [
            seg_turns[i : i + self._max_steps]
            for i in range(0, len(seg_turns), self._max_steps)
        ]

    def _context_base_tokens(self) -> int | None:
        if self._meter_getter is not None:
            try:
                return self._meter_getter()
            except Exception:
                return None
        try:
            from artemis.services.token_meter import get_meter

            session_id = getattr(self._engine, "current_session_id", None)
            if not session_id:
                return None
            last = get_meter(session_id).last_prompt_tokens
            return last or None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Chunk creation
    # ------------------------------------------------------------------

    def _load_steps_by_id(self) -> dict[str, dict]:
        steps: list[dict] = []
        try:
            if self._engine is not None:
                steps = self._engine.get_agent_friendly_steps() or []
        except Exception as e:
            logger.error(f"Failed to load steps for chunking: {e}")
        return {str(s.get("step_id")): s for s in steps}

    def _session_start(self) -> float | None:
        return getattr(self._engine, "session_start_time", None)

    def _create_chunk(
        self,
        slice_turns: list[dict],
        seg_hash: str | None,
        steps_by_id: dict[str, dict],
    ) -> ChunkState | None:
        step_keys = [str(t.get("step_key")) for t in slice_turns if t.get("step_key")]
        steps = [steps_by_id[k] for k in step_keys if k in steps_by_id]
        steps.sort(key=lambda s: s.get("step_number") or 0)
        if not steps:
            logger.warning(
                "History chunk skipped: no DataEngine step records for the"
                f" selected turns ({len(slice_turns)} turns)."
            )
            return None

        session_start = self._session_start()
        start_step, end_step = steps[0], steps[-1]
        self._chunk_counter += 1
        chunk = ChunkState(
            ordinal=self._chunk_counter,
            start_step_number=int(start_step.get("step_number")),
            end_step_number=int(end_step.get("step_number")),
            start_step_id=str(start_step.get("step_id")),
            end_step_id=str(end_step.get("step_id")),
            source_step_ids=[str(s.get("step_id")) for s in steps],
            subgoal_hash=seg_hash,
            milestone_label=self._resolve_label(seg_hash),
            start_offset=step_offset_label(start_step, session_start),
            end_offset=step_offset_label(end_step, session_start),
            band3=build_action_ledger(steps, session_start),
            minimal_index=build_action_ledger(steps, session_start, minimal=True),
            user_lines=[
                line for line in (injected_instruction_line(s) for s in steps) if line
            ],
        )
        # Ready gating: the caller queues the chunk as awaiting — it only
        # enters self._chunks (and the frozen region) once its capsule is
        # ready, or through the hard-threshold emergency swap.
        self._persist(chunk)
        self._dispatch_capsule(chunk, steps, session_start)
        return chunk

    def _dispatch_capsule(
        self, chunk: ChunkState, steps: list[dict], session_start: float | None
    ) -> None:
        payload_steps = []
        for step in steps:
            payload_steps.append(
                {
                    "step_number": step.get("step_number"),
                    "offset": step_offset_label(step, session_start),
                    "action": _action_phrase(step),
                    "outcome": _result_phrase(step.get("last_execution_result")),
                    "visual_summary": step.get("summary"),
                    "thinking_excerpt": (step.get("operator_raw_thinking") or "")[:1200]
                    or None,
                    "note_writes": extract_note_writes(step),
                    "injected_instruction": (step.get("extra_metadata") or {}).get(
                        "injected_instruction"
                    ),
                    "fa_actions": _fa_recovery_lines(step),
                }
            )
        payload = {
            "start_step": chunk.start_step_number,
            "end_step": chunk.end_step_number,
            "step_number": chunk.start_step_number,  # service log display
            "milestone_label": chunk.milestone_label,
            "steps": payload_steps,
        }
        try:
            self._capsule_service.submit(chunk.capsule_key, payload)
        except Exception as e:
            logger.error(f"Failed to dispatch chunk capsule {chunk.capsule_key}: {e}")

    def _resolve_label(self, seg_hash: str | None) -> str | None:
        if not seg_hash or seg_hash == "default":
            return None
        try:
            from artemis.utils.notes import get_note_file_path
            from artemis.utils.plan_grammar import parse_plan, subgoal_hash
            from artemis.utils.task_tree import get_all_subgoal_aliases

            base_dir = getattr(self._engine, "base_dir", None)
            if not base_dir:
                return None
            plan_path = get_note_file_path(base_dir, "task_plan")
            if not plan_path.exists():
                return None
            for item in parse_plan(plan_path.read_text(encoding="utf-8")).items:
                item_hash = subgoal_hash(item.text)
                if item_hash == seg_hash or seg_hash in get_all_subgoal_aliases(
                    item_hash, base_dir
                ):
                    return item.text
        except Exception:
            return None
        return None

    # ------------------------------------------------------------------
    # Capsule harvest / persistence
    # ------------------------------------------------------------------

    def _harvest_capsules(self) -> None:
        """Fold ready capsule results into pending chunk mirrors (event-time)."""
        for chunk in self._all_chunks():
            if chunk.status != "pending":
                continue
            raw = self._capsule_service.get_summary(chunk.capsule_key)
            if not raw:
                continue
            try:
                band1 = json.loads(raw)
            except Exception:
                continue
            chunk.band1 = band1
            chunk.band2 = render_band2(band1)
            chunk.status = "ready"
            chunk.version += 1
            self._persist(chunk)

    def _persist(self, chunk: ChunkState) -> None:
        try:
            if self._engine is None or not hasattr(self._engine, "record_history_chunk"):
                return
            band1 = dict(chunk.band1)
            if chunk.annotations:
                band1["annotations"] = chunk.annotations
            self._engine.record_history_chunk(
                start_step_number=chunk.start_step_number,
                end_step_number=chunk.end_step_number,
                version=chunk.version,
                status=chunk.status,
                start_step_id=chunk.start_step_id,
                end_step_id=chunk.end_step_id,
                source_step_ids=chunk.source_step_ids,
                subgoal_hash=chunk.subgoal_hash,
                band1=band1,
                band2=chunk.band2,
                band3=chunk.band3,
                rendered_text=render_chunk_block(chunk),
            )
        except Exception as e:
            logger.error(f"Failed to persist history chunk: {e}")

    # ------------------------------------------------------------------
    # Era folding + frozen-region rendering
    # ------------------------------------------------------------------

    def _fold_eras(self) -> None:
        overflow = len(self._chunks) - self._max_chunks
        if overflow > 0:
            folded, self._chunks = self._chunks[:overflow], self._chunks[overflow:]
            self._era_counter += 1
            self._eras.append(EraState(ordinal=self._era_counter, chunks=folded))
        era_overflow = len(self._eras) - self._max_eras
        if era_overflow > 0:
            for era in self._eras[:era_overflow]:
                if not era.recall_only:
                    era.recall_only = True
                    logger.info(
                        f"Era {era.ordinal} overflowed to the recall-only period"
                        f" paragraph (Steps {era.start_step_number}–"
                        f"{era.end_step_number})."
                    )

    def _render_frozen_blocks(self, *, hard: bool) -> list[BaseMessage]:
        if not self._chunks and not self._eras:
            return []
        if hard:
            plan_text = self._read_plan_text()
            text = render_l3_snapshot(
                self._eras, self._chunks, goal=self._goal, plan_text=plan_text
            )
            return [HumanMessage(content=[{"type": "text", "text": text}])]
        blocks: list[BaseMessage] = []
        for era in self._eras:
            blocks.append(
                HumanMessage(content=[{"type": "text", "text": render_era_block(era)}])
            )
        for chunk in self._chunks:
            blocks.append(
                HumanMessage(content=[{"type": "text", "text": render_chunk_block(chunk)}])
            )
        return blocks

    def _read_plan_text(self) -> str | None:
        try:
            from artemis.utils.notes import get_note_file_path

            base_dir = getattr(self._engine, "base_dir", None)
            if not base_dir:
                return None
            path = get_note_file_path(base_dir, "task_plan")
            return path.read_text(encoding="utf-8") if path.exists() else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Draining
    # ------------------------------------------------------------------

    async def flush(self, timeout_seconds: float | None = None) -> None:
        """Drain in-flight capsule jobs, then persist any harvested results.

        Frozen transcript text is deliberately NOT re-rendered here (deep
        mutations only happen at compression events); this keeps the DB copy
        complete at session end.
        """
        try:
            await self._capsule_service.flush(timeout_seconds)
        except Exception:
            pass
        try:
            self._harvest_capsules()
        except Exception as e:
            logger.debug(f"Chunk capsule harvest at flush skipped: {e}")
