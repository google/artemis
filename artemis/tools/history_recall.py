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

"""recall_history: deterministic lossless lookup into cold history (M4).

Implements the sister design's §10 as adopted by the history-module redesign
§3.5: one unified tool over the DataEngine's raw records — step summaries,
history chunks (bands ①②③), exact actions and validator results, operator
thinking, notes, XML/OCR text (which also carries package/activity strings),
Failure-Analyzer traces, and image references. Phase one is keyword + step
range filtering over SQLite-backed records in Python — no vector database, no
model call.

Hard boundaries (config ``agent.memory.recall``):

- at most ``max_results`` results (default 5);
- one response is capped at ``max_text_tokens`` estimated tokens (char/4);
- every result carries a step number / step id;
- ``include_details`` output is clamped per field;
- ``include_images`` returns only the screenshots that actually exist on disk
  for a single step (data URLs), never more than ``max_image_steps`` steps;
- large raw sources return only excerpts around the match plus a reference.

An era whose ledger overflowed to a ``[Era N | Steps a–b: ledger via
recall_history]`` marker line is re-entered here: recalling that step range
returns the full-width ``build_action_ledger`` rows for it.
"""

import base64
import json
from typing import Any

from pydantic import BaseModel, Field

from artemis.core.tool_declaration import ToolDeclaration
from artemis.data_engine.trace import trace_langchain_tool
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.tools.tool_wrapper import ToolWrapper
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

#: How many most-recent steps get their screenshot XML/OCR blobs scanned
#: (per-image JSON loads are the expensive part of the sweep).
XML_SCAN_CAP = 150

#: Character clamp for one matched excerpt.
EXCERPT_CHARS = 240
DETAIL_EXCERPT_CHARS = 600


class RecallHistoryArgs(BaseModel):
    """Arguments schema for the recall_history tool."""

    query: str = Field(
        ...,
        description=(
            "Keywords to search for across the whole execution history"
            " (summaries, actions, validator results, reasoning, notes,"
            " on-screen XML/OCR text, package/activity names). Case-"
            "insensitive; every whitespace-separated term is matched"
            " independently. May be empty when step_range is given (returns"
            " the per-step action ledger of that range)."
        ),
    )
    step_range: list[int] | None = Field(
        default=None,
        description=(
            "Optional [start, end] step-number range (inclusive) to restrict"
            " the search — e.g. the range shown in a '[... ledger via"
            " recall_history]' marker line. Also returns the full per-step"
            " action ledger of the range."
        ),
    )
    include_details: bool = Field(
        default=False,
        description=(
            "Include clamped per-result details (exact action, validator"
            " result, reasoning excerpt) instead of the one-line form."
        ),
    )
    include_images: bool = Field(
        default=False,
        description=(
            "Return the stored screenshots of the single best-matching step"
            " (only images that actually exist; at most one step)."
        ),
    )
    max_results: int = Field(
        default=5,
        ge=1,
        description="Maximum results to return (server-side cap still applies).",
    )


def _terms(query: str) -> list[str]:
    return [t for t in (query or "").lower().split() if t]


def _estimate_tokens(text: str) -> int:
    return len(text) // 4


def _clamp(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _excerpt(text: str, terms: list[str], limit: int) -> str:
    """A window of ``text`` around the first term hit (flattened whitespace)."""
    flat = " ".join((text or "").split())
    lower = flat.lower()
    pos = -1
    for term in terms:
        pos = lower.find(term)
        if pos >= 0:
            break
    if pos < 0:
        return _clamp(flat, limit)
    start = max(0, pos - limit // 3)
    window = flat[start : start + limit]
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + limit < len(flat) else ""
    return f"{prefix}{window.strip()}{suffix}"


def _score(haystack: str, terms: list[str]) -> int:
    lower = haystack.lower()
    return sum(1 for term in terms if term in lower)


def _events_text(step: dict) -> str:
    parts: list[str] = []
    for event in step.get("interleaved_events") or []:
        etype = event.get("type")
        if etype == "tool_call":
            parts.append(str(event.get("name") or ""))
            parts.append(json.dumps(event.get("args") or {}, ensure_ascii=False, default=str))
            result = event.get("result")
            if result is not None:
                parts.append(str(result)[:2000])
        elif etype and "thought" in etype:
            parts.append(str(event.get("content") or ""))
    return "\n".join(parts)


def _step_haystack(step: dict) -> str:
    fields = [
        str(step.get("summary") or ""),
        json.dumps(step.get("action_taken"), ensure_ascii=False, default=str)
        if step.get("action_taken")
        else "",
        json.dumps(step.get("last_execution_result"), ensure_ascii=False, default=str)
        if step.get("last_execution_result")
        else "",
        str(step.get("operator_raw_thinking") or ""),
        _events_text(step),
        str((step.get("extra_metadata") or {}).get("injected_instruction") or ""),
        # M5: the foreground app package stamped by record_step joins the
        # search surface, so package-name queries no longer depend on the
        # XML-blob scan (capped to recent steps) alone.
        str((step.get("extra_metadata") or {}).get("foreground_app") or ""),
    ]
    return "\n".join(f for f in fields if f)


def _screen_text(engine: Any, step: dict) -> str:
    """Raw XML/OCR text of a step's pre-action screenshot (package/activity
    strings live inside the XML blob)."""
    image_name = step.get("pre_image_name") or step.get("post_image_name")
    if not image_name:
        return ""
    try:
        record = engine.storage.get_image(image_name)
    except Exception:
        return ""
    if record is None:
        return ""
    parts = []
    for blob in (record.ui_tree, record.ocr_result):
        if blob:
            parts.append(
                blob if isinstance(blob, str) else json.dumps(blob, ensure_ascii=False, default=str)
            )
    return "\n".join(parts)


def _session_start(engine: Any) -> float | None:
    return getattr(engine, "session_start_time", None)


def _step_label(step: dict, session_start: float | None) -> str:
    from artemis.memory.chunking import step_offset_label

    return f"Step {step.get('step_number')} ({step_offset_label(step, session_start)})"


def _note_writer_steps(steps: list[dict], key: str) -> list[int]:
    writers: list[int] = []
    for step in steps:
        for event in step.get("interleaved_events") or []:
            if (
                event.get("type") == "tool_call"
                and event.get("name") in ("save_note", "update_note", "append_note")
                and (event.get("args") or {}).get("key") == key
            ):
                number = step.get("step_number")
                if isinstance(number, int):
                    writers.append(number)
    return writers


def search_history(
    engine: Any,
    *,
    query: str,
    step_range: list[int] | None = None,
    include_details: bool = False,
    include_images: bool = False,
    max_results: int = 5,
    recall_config: Any = None,
) -> str | list[dict]:
    """Deterministic history search; returns text or content blocks (images)."""
    cfg_max_results = int(getattr(recall_config, "max_results", 5) or 5)
    cfg_max_tokens = int(getattr(recall_config, "max_text_tokens", 2000) or 2000)
    cfg_max_image_steps = int(getattr(recall_config, "max_image_steps", 1))

    max_results = max(1, min(int(max_results or cfg_max_results), cfg_max_results))
    char_budget = cfg_max_tokens * 4

    try:
        steps: list[dict] = engine.get_agent_friendly_steps() or []
    except Exception as e:
        return f"recall_history failed to load history: {e}"

    range_start = range_end = None
    if step_range:
        try:
            range_start, range_end = int(step_range[0]), int(step_range[-1])
            if range_start > range_end:
                range_start, range_end = range_end, range_start
        except (TypeError, ValueError, IndexError):
            range_start = range_end = None

    def in_range(number: Any) -> bool:
        if range_start is None:
            return True
        return isinstance(number, int) and range_start <= number <= range_end

    scoped_steps = [s for s in steps if in_range(s.get("step_number"))]
    terms = _terms(query)
    session_start = _session_start(engine)

    if not steps:
        return "recall_history: no recorded steps yet."
    if step_range and not scoped_steps:
        return f"recall_history: no recorded steps in range {range_start}–{range_end}."

    sections: list[str] = []

    # --- Range ledger: the explicit re-entry point for 'ledger via
    # recall_history' guidance lines and recall-only era period paragraphs —
    # full-width build_action_ledger rows.
    if range_start is not None and scoped_steps:
        from artemis.memory.chunking import build_action_ledger

        ledger = build_action_ledger(scoped_steps, session_start)
        sections.append(
            f"Action ledger for Steps {range_start}–{range_end}"
            f" ({len(scoped_steps)} recorded steps):\n{ledger}"
        )

    # --- Keyword search over steps.
    results: list[tuple[int, int, str]] = []  # (score, step_number, rendered)
    if terms:
        xml_eligible = {id(s) for s in scoped_steps[-XML_SCAN_CAP:]}
        for step in scoped_steps:
            haystack = _step_haystack(step)
            screen = _screen_text(engine, step) if id(step) in xml_eligible else ""
            score = _score(haystack, terms)
            screen_score = _score(screen, terms) if screen else 0
            total = score + screen_score
            if total <= 0:
                continue
            excerpt_limit = DETAIL_EXCERPT_CHARS if include_details else EXCERPT_CHARS
            source = haystack if score >= screen_score else screen
            body = _excerpt(source, terms, excerpt_limit)
            lines = [f"[{_step_label(step, session_start)} | id {step.get('step_id')}]"]
            summary = step.get("summary")
            if summary:
                lines.append(f"  Summary: {_clamp(str(summary), EXCERPT_CHARS)}")
            lines.append(f"  Match: {body}")
            if include_details:
                action = step.get("action_taken")
                if action:
                    lines.append(
                        "  Action: "
                        + _clamp(
                            json.dumps(action, ensure_ascii=False, default=str),
                            DETAIL_EXCERPT_CHARS,
                        )
                    )
                result = step.get("last_execution_result")
                if result:
                    lines.append(
                        "  Result: "
                        + _clamp(
                            json.dumps(result, ensure_ascii=False, default=str),
                            DETAIL_EXCERPT_CHARS,
                        )
                    )
                thinking = step.get("operator_raw_thinking")
                if thinking:
                    lines.append(f"  Reasoning: {_clamp(str(thinking), DETAIL_EXCERPT_CHARS)}")
            number = step.get("step_number") or 0
            results.append((total, number, "\n".join(lines)))

        # --- History chunks (bands ①②③ / rendered text).
        try:
            chunk_rows = engine.get_history_chunks() or []
        except Exception:
            chunk_rows = []
        for row in chunk_rows:
            start_n = getattr(row, "start_step_number", None)
            end_n = getattr(row, "end_step_number", None)
            if range_start is not None and not (
                isinstance(start_n, int)
                and isinstance(end_n, int)
                and start_n <= range_end
                and end_n >= range_start
            ):
                continue
            text = getattr(row, "rendered_text", None) or "\n".join(
                filter(None, [getattr(row, "band2", None), getattr(row, "band3", None)])
            )
            score = _score(text or "", terms)
            if score <= 0:
                continue
            body = _excerpt(text, terms, DETAIL_EXCERPT_CHARS if include_details else EXCERPT_CHARS)
            rendered = (
                f"[History chunk | Steps {start_n}–{end_n} | status"
                f" {getattr(row, 'status', '?')}]\n  Match: {body}"
            )
            results.append((score, int(end_n or 0), rendered))

        # --- Notes (filesystem; attributed to their writing steps).
        try:
            from artemis.utils.notes import list_notes_keys, read_note_content

            base_dir = getattr(engine, "base_dir", None)
            note_keys = list_notes_keys(base_dir) if base_dir else []
        except Exception:
            note_keys = []
        for key in note_keys:
            try:
                content = read_note_content(base_dir, key)
            except Exception:
                continue
            score = _score(content, terms) + _score(key, terms)
            if score <= 0:
                continue
            writers = _note_writer_steps(steps, key)
            if (
                range_start is not None
                and writers
                and not any(range_start <= n <= range_end for n in writers)
            ):
                continue
            anchor = writers[-1] if writers else (steps[-1].get("step_number") or 0)
            origin = f"last written at Step {writers[-1]}" if writers else f"as of Step {anchor}"
            body = _excerpt(
                content, terms, DETAIL_EXCERPT_CHARS if include_details else EXCERPT_CHARS
            )
            results.append((score, int(anchor), f"[Note '{key}' | {origin}]\n  Match: {body}"))

    results.sort(key=lambda r: (-r[0], -r[1]))
    kept = results[:max_results]
    dropped = len(results) - len(kept)

    if kept:
        header = f"Top {len(kept)} match(es) for {terms}:"
        if dropped > 0:
            header += f" ({dropped} more not shown; narrow the query or step_range)"
        sections.append(header + "\n\n" + "\n".join(r[2] for r in kept))
    elif terms:
        sections.append(f"No matches for {terms} in the searched history.")

    text = "\n\n".join(sections)
    if _estimate_tokens(text) > cfg_max_tokens:
        text = (
            text[:char_budget].rstrip()
            + "\n… (response truncated at the recall token budget; narrow the"
            " query or step_range)"
        )

    if not include_images or cfg_max_image_steps < 1:
        return text

    # --- Image recall: real stored screenshots of ONE step only.
    target_step: dict | None = None
    if kept:
        # The best step-shaped result; fall back to the newest scoped step.
        best_numbers = [r[1] for r in kept]
        for number in best_numbers:
            target_step = next((s for s in scoped_steps if s.get("step_number") == number), None)
            if target_step:
                break
    if target_step is None and scoped_steps:
        target_step = scoped_steps[-1]
    if target_step is None:
        return text

    blocks: list[dict] = [{"type": "text", "text": text}]
    added = 0
    for which, name in (
        ("pre-action", target_step.get("pre_image_name")),
        ("post-action", target_step.get("post_image_name")),
    ):
        if not name:
            continue
        try:
            path = engine.get_image_path(name)
            if not path or not path.exists():
                continue
            encoded = base64.b64encode(path.read_bytes()).decode("utf-8")
        except Exception:
            continue
        blocks.append(
            {
                "type": "text",
                "text": (f"--- {which} screenshot of Step {target_step.get('step_number')} ---"),
            }
        )
        blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            }
        )
        added += 1
    if added == 0:
        blocks.append(
            {
                "type": "text",
                "text": (
                    f"(no stored screenshots exist for Step {target_step.get('step_number')})"
                ),
            }
        )
    return blocks


RECALL_HISTORY_DOCSTRING = (
    "[SHELL] Deterministic lookup into older execution history that has been"
    " compressed out of the visible context. Searches step summaries, exact"
    " actions and validator results, reasoning, notes, on-screen XML/OCR text"
    " and history-chunk ledgers by keywords and/or a step-number range;"
    " a step_range also returns that range's full per-step action ledger"
    " (the entry point for 'ledger via recall_history' guidance/marker lines"
    " and for recall-only '[Era ...]' period paragraphs)."
    " Results are bounded and every result carries its step number.\n"
    "Call it when: the current screen closely resembles a much older state;"
    " you need an exact old value (text, id, amount, path); two consecutive"
    " steps made no progress; a history summary conflicts with what you"
    " observe; or the user asks to return to an earlier state. A"
    " '(Step-level ledger via recall_history for steps a–b)' line under an"
    " '[Era ...]' period paragraph is an explicit entry point — query that"
    " step range for the full per-step ledger. Do not call it speculatively"
    " on ordinary steps: the visible history is normally sufficient."
)


class RecallHistoryTool(ArtemisTool):
    """Unified cold-history recall over the DataEngine (design §10)."""

    def __init__(self):
        super().__init__(
            name="recall_history",
            description=RECALL_HISTORY_DOCSTRING,
            args_schema=RecallHistoryArgs,
            category="memory",
        )

    def is_available(self, ctx: Any = None) -> bool:
        if ctx is None or getattr(ctx, "data_engine", None) is None:
            return False
        return bool(getattr(_recall_config(warn=False), "enabled", True))

    # pylint: disable=too-many-arguments
    async def execute(
        self,
        driver: Any = None,  # pylint: disable=unused-argument
        ctx: Any = None,
        query: str = "",
        step_range: list[int] | None = None,
        include_details: bool = False,
        include_images: bool = False,
        max_results: int = 5,
        **kwargs: Any,
    ) -> Any:
        engine = getattr(ctx, "data_engine", None) if ctx else None
        if engine is None:
            return "recall_history unavailable: no active DataEngine session."
        if not query and not step_range:
            return (
                "recall_history needs a query and/or a step_range — e.g."
                ' recall_history(query="login timeout") or'
                ' recall_history(query="", step_range=[1, 40]).'
            )
        try:
            return search_history(
                engine,
                query=query or "",
                step_range=step_range,
                include_details=bool(include_details),
                include_images=bool(include_images),
                max_results=int(max_results or 5),
                recall_config=_recall_config(),
            )
        except Exception as e:
            logger.error(f"recall_history failed: {e}")
            return f"recall_history failed: {e}"


def _recall_config(warn: bool = True) -> Any:
    try:
        from artemis.config import load_agent_config

        return load_agent_config().memory.recall
    except Exception as e:
        if warn:
            logger.debug(f"recall config unavailable, using defaults: {e}")
        return None


recall_history = RecallHistoryTool()
ToolRegistry.register(recall_history)

_DECLARATION_TYPES = {
    "query": {"type": "string"},
    "step_range": {"type": "array", "items": {"type": "integer"}},
    "include_details": {"type": "boolean"},
    "include_images": {"type": "boolean"},
    "max_results": {"type": "integer"},
}

#: Flash-profile declaration of the same tool (FlashRunner binds JSON-schema
#: declarations rather than LangChain tools). Descriptions come from
#: ``RecallHistoryArgs`` so the two contracts never drift.
RECALL_HISTORY_TOOL = ToolDeclaration(
    name="recall_history",
    description=RECALL_HISTORY_DOCSTRING,
    parameters={
        "type": "object",
        "properties": {
            name: {**_DECLARATION_TYPES[name], "description": info.description or ""}
            for name, info in RecallHistoryArgs.model_fields.items()
        },
        "required": ["query"],
    },
)


def get_recall_history_tool(ctx: Any):
    """Exports recall_history as a traced LangChain BaseTool."""
    return trace_langchain_tool(recall_history.to_langchain_tool(ctx), ctx)


def _recall_available(ctx: Any) -> bool:
    return recall_history.is_available(ctx)


recall_history_wrapper = ToolWrapper(
    tool_fn_getter=get_recall_history_tool,
    on_success_fn=lambda *a, **k: "Recalled history",
    on_failure_fn=lambda err: f"recall_history failed: {err}",
    is_available_fn=_recall_available,
)
