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

"""search_history: deterministic keyword / step-range lookup over stored history.

One search surface over a :class:`~artemis.data_engine.history_reader.HistoryReader`'s
raw records — step summaries, history chunks, exact actions and execution
results, reasoning, the tool calls a step made (name, arguments, result
text), notes, the OCR/UI-tree text of the step's pre and post screenshots
(which also carries package/activity strings) plus any screenshot a tool
result embedded, and the foreground-app stamp. Keyword + step-range filtering
in Python; no vector database, no model call.

Hard boundaries (config ``agent.memory.recall``):

- at most ``max_results`` results (default 5);
- one response is capped at ``max_text_tokens`` estimated tokens (char/4);
- screen text is scanned for the most recent ``screen_scan_steps`` steps;
- every result carries a step number / step id;
- large raw sources return only excerpts around the match plus a reference.

Two surfaces per step: the *scoring* haystack keeps the raw record (action
JSON, execution result JSON, UI-tree XML) so resource ids, bounds and package
names stay searchable; the *excerpt* shown as ``Match:`` is built from the
readable twin (the ledger action phrase, result status/error words, element
text / content descriptions / OCR text) so the model never reads JSON.

A ``step_range`` additionally returns the full-width ``build_action_ledger``
rows of that range — the re-entry point for compressed-history marker lines.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from artemis.core.tool_failure import ToolFailure
from artemis.utils.logger import get_logger
from artemis.utils.task_tree import format_actions_clean

logger = get_logger(__name__)

#: Character clamp for one matched excerpt.
EXCERPT_CHARS = 240

#: Per-tool-result clamp on the *search* surface (scoring/excerpts only; the
#: replay rendering has its own, looser clamp).
RESULT_SEARCH_CHARS = 8000

DEFAULT_MAX_RESULTS = 5
DEFAULT_MAX_TEXT_TOKENS = 2000
DEFAULT_SCREEN_SCAN_STEPS = 150


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


def result_search_text(result: Any) -> str:
    """Searchable text of a tool result. Content-block lists contribute their
    text blocks — image blocks have already been rewritten into
    ``[screenshot: …]`` descriptions by the DataEngine; any raw image block
    that slipped through contributes nothing (its OCR/XML description joins
    the haystack via :func:`_screen_text`), so base64 never enters the search."""
    if result is None:
        return ""
    # A content-block list stored as its JSON string (trace serialization)
    # is unwrapped first so its text blocks render, not the JSON.
    if isinstance(result, str) and result.lstrip().startswith("[{"):
        try:
            parsed = json.loads(result)
        except ValueError:
            parsed = None
        if isinstance(parsed, list) and any(isinstance(b, dict) and "type" in b for b in parsed):
            return result_search_text(parsed)
    if isinstance(result, list):
        texts = [
            str(block.get("text") or "")
            for block in result
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if texts or any(
            isinstance(block, dict) and block.get("type") in ("image_url", "image")
            for block in result
        ):
            return "\n".join(texts)[:RESULT_SEARCH_CHARS]
    if isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False, default=str)[:RESULT_SEARCH_CHARS]
    return str(result)[:RESULT_SEARCH_CHARS]


def _events_text(step: dict) -> str:
    parts: list[str] = []
    for event in step.get("interleaved_events") or []:
        etype = event.get("type")
        if etype == "tool_call":
            parts.append(str(event.get("name") or ""))
            parts.append(json.dumps(event.get("args") or {}, ensure_ascii=False, default=str))
            result_text = result_search_text(event.get("result"))
            if result_text:
                parts.append(result_text)
        elif etype and "thought" in etype:
            parts.append(str(event.get("content") or ""))
    return "\n".join(parts)


def _rendered_action(action_taken: Any) -> str:
    """The shared ledger phrase of a step's action (``format_actions_clean``)."""
    if not action_taken:
        return ""
    try:
        return format_actions_clean(action_taken)
    except Exception:
        return ""


def _action_text(action_taken: Any) -> str:
    """Combine the rendered action with raw fields for *scoring*.

    Rendered text identifies self-described targets. Raw JSON also makes
    resource IDs, bounds, and other fields searchable. Never shown as-is:
    the excerpt comes from :func:`_step_display_text`.
    """
    if not action_taken:
        return ""
    rendered = _rendered_action(action_taken)
    raw = json.dumps(action_taken, ensure_ascii=False, default=str)
    return f"{rendered}\n{raw}" if rendered else raw


def _step_haystack(step: dict) -> str:
    """The scoring surface of a step: every recorded field, raw JSON included."""
    extra = step.get("extra_metadata") or {}
    fields = [
        str(step.get("summary") or ""),
        _action_text(step.get("action_taken")),
        json.dumps(step.get("last_execution_result"), ensure_ascii=False, default=str)
        if step.get("last_execution_result")
        else "",
        str(step.get("operator_raw_thinking") or ""),
        _events_text(step),
        str(extra.get("injected_instruction") or ""),
        # The foreground app package stamped by record_step joins the search
        # surface, so package-name queries never depend on the screen-text
        # scan (capped to recent steps) alone.
        str(extra.get("foreground_app") or ""),
    ]
    return "\n".join(f for f in fields if f)


def _result_display_text(result: Any) -> str:
    """The execution result as words: its status and result/error strings
    (an execution incident renders as its one-line phrase), never JSON."""
    if not result:
        return ""
    if not isinstance(result, dict):
        return str(result)
    parts: list[str] = []
    status = result.get("status")
    if status:
        parts.append(f"status {status}")
    try:
        from artemis.utils.task_tree import format_result_clean

        incident_or_error = format_result_clean(result)
    except Exception:
        incident_or_error = None
    if incident_or_error:
        parts.append(str(incident_or_error))
    else:
        for key in ("result", "error", "error_msg", "message"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    return " | ".join(parts)


def _events_display_text(step: dict) -> str:
    """Tool calls and reasoning of a step as the replay shows them (name,
    arguments, result text), without the raw event JSON."""
    parts: list[str] = []
    for event in step.get("interleaved_events") or []:
        etype = event.get("type")
        if etype == "tool_call":
            name = str(event.get("name") or "")
            args = event.get("args") or {}
            result_text = result_search_text(event.get("result"))
            try:
                call = f"{name}({json.dumps(args, ensure_ascii=False, default=str)})"
            except (TypeError, ValueError):
                call = name
            parts.append(f"{call} -> {result_text}" if result_text else call)
        elif etype and "thought" in etype:
            parts.append(str(event.get("content") or ""))
    return "\n".join(p for p in parts if p)


def _step_display_text(step: dict) -> str:
    """The *excerpt* surface of a step: the same fields as the haystack, as the
    agent would read them. The summary is left out — it already heads the
    result as the ``Screen:`` line."""
    extra = step.get("extra_metadata") or {}
    fields = [
        _rendered_action(step.get("action_taken")),
        _result_display_text(step.get("last_execution_result")),
        str(step.get("operator_raw_thinking") or ""),
        _events_display_text(step),
        str(extra.get("injected_instruction") or ""),
        str(extra.get("foreground_app") or ""),
    ]
    # Excerpts flatten whitespace, so the field boundary must survive as text.
    return " | ".join(f.strip() for f in fields if f and f.strip())


def _image_text(reader: Any, image_name: str | None) -> str:
    """Raw XML/OCR text stored for one screenshot — the textual description
    of that image (package/activity strings live inside the XML blob)."""
    if not image_name:
        return ""
    try:
        record = reader.storage.get_image(image_name)
    except (sqlite3.Error, ValueError):
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


def referenced_image_names(step: dict) -> list[str]:
    """Screenshots that tool results of this step embedded (rewritten by the
    DataEngine into ``[screenshot: …]`` text blocks carrying ``image_name``)."""
    names: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            name = value.get("image_name")
            if value.get("type") == "text" and name and name not in names:
                names.append(str(name))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for event in step.get("interleaved_events") or []:
        if event.get("type") == "tool_call":
            walk(event.get("result"))
    return names


#: UI-tree node attributes whose values are human-readable screen content.
_UI_NODE_TEXT_KEYS = ("text", "content-desc", "content_desc", "resource-id", "resource_id")


def _collect_screen_strings(value: Any, out: list[str], depth: int = 0) -> None:
    """Walks a stored UI tree / OCR blob and appends its readable values
    (element text, content description, resource id, OCR text) in order."""
    if depth > 50:
        return
    if isinstance(value, str):
        text = " ".join(value.split())
        if text and text not in out:
            out.append(text)
    elif isinstance(value, dict):
        for key in _UI_NODE_TEXT_KEYS:
            text = value.get(key)
            if isinstance(text, str):
                text = " ".join(text.split())
                if text and text not in out:
                    out.append(text)
        for child_key in ("children", "nodes", "elements"):
            children = value.get(child_key)
            if isinstance(children, (list, tuple)):
                for child in children:
                    _collect_screen_strings(child, out, depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _collect_screen_strings(child, out, depth + 1)


def _screen_names(step: dict) -> list[str]:
    names: list[str] = []
    for name in (
        step.get("pre_image_name"),
        step.get("post_image_name"),
        *referenced_image_names(step),
    ):
        if name and name not in names:
            names.append(name)
    return names


def _screen_text(reader: Any, step: dict) -> str:
    """Description text of the screenshots behind a step (its own pre- and
    post-action screenshots plus any screenshot a tool result embedded) —
    the raw XML/OCR blobs, for *scoring* only."""
    return "\n".join(t for t in (_image_text(reader, n) for n in _screen_names(step)) if t)


def _screen_display_text(reader: Any, step: dict) -> str:
    """The readable content of the same screenshots for the *excerpt*: element
    text / content descriptions / resource ids and OCR text joined by
    ``" | "``, never the UI-tree JSON."""
    strings: list[str] = []
    for name in _screen_names(step):
        try:
            record = reader.storage.get_image(name)
        except (sqlite3.Error, ValueError, AttributeError):
            continue
        if record is None:
            continue
        for blob in (record.ui_tree, record.ocr_result):
            if blob:
                _collect_screen_strings(blob, strings)
    return " | ".join(strings)


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


def _parse_range(step_range: list[int] | None) -> tuple[int | None, int | None]:
    if not step_range:
        return None, None
    try:
        start, end = int(step_range[0]), int(step_range[-1])
    except (TypeError, ValueError, IndexError):
        return None, None
    if start > end:
        start, end = end, start
    return start, end


# pylint: disable=too-many-branches,too-many-locals,too-many-statements
def search_history_text(
    reader: Any,
    *,
    query: str = "",
    step_range: list[int] | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
    recall_config: Any = None,
) -> str:
    """Deterministic history search over ``reader``; always returns text."""
    cfg_max_results = int(getattr(recall_config, "max_results", None) or DEFAULT_MAX_RESULTS)
    cfg_max_tokens = int(getattr(recall_config, "max_text_tokens", None) or DEFAULT_MAX_TEXT_TOKENS)
    scan_steps = int(getattr(recall_config, "screen_scan_steps", None) or DEFAULT_SCREEN_SCAN_STEPS)
    max_results = max(1, min(int(max_results or cfg_max_results), cfg_max_results))
    char_budget = cfg_max_tokens * 4

    try:
        steps: list[dict] = reader.get_agent_friendly_steps() or []
    except Exception as e:
        return ToolFailure(f"search_history failed to load history: {e}")

    range_start, range_end = _parse_range(step_range)

    def in_range(number: Any) -> bool:
        if range_start is None:
            return True
        return isinstance(number, int) and range_start <= number <= range_end

    scoped_steps = [s for s in steps if in_range(s.get("step_number"))]
    terms = _terms(query)
    session_start = getattr(reader, "session_start_time", None)

    if not steps:
        return "search_history: no recorded steps yet."
    if step_range and not scoped_steps:
        return f"search_history: no recorded steps in range {range_start}–{range_end}."

    sections: list[str] = []

    # --- Range ledger: the explicit re-entry point for compressed-history
    # marker lines — full-width build_action_ledger rows.
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
        screen_eligible = {id(s) for s in scoped_steps[-scan_steps:]} if scan_steps > 0 else set()
        for step in scoped_steps:
            haystack = _step_haystack(step)
            screen = _screen_text(reader, step) if id(step) in screen_eligible else ""
            score = _score(haystack, terms)
            screen_score = _score(screen, terms) if screen else 0
            total = score + screen_score
            if total <= 0:
                continue
            lines = [f"[{_step_label(step, session_start)} | id {step.get('step_id')}]"]
            summary = step.get("summary")
            if summary:
                lines.append(f"  Screen: {_clamp(str(summary), EXCERPT_CHARS)}")
            # The excerpt comes from the readable twin of whichever surface
            # scored higher; the raw JSON only ever scores. When the hit sits
            # in the summary alone, the Screen line already shows it.
            display = (
                _step_display_text(step)
                if score >= screen_score
                else _screen_display_text(reader, step)
            )
            display_hit = _score(display, terms) > 0
            summary_hit = bool(summary) and _score(str(summary), terms) > 0
            if display and (display_hit or not summary_hit):
                lines.append(f"  Match: {_excerpt(display, terms, EXCERPT_CHARS)}")
            number = step.get("step_number") or 0
            results.append((total, number, "\n".join(lines)))

        # --- History chunks (bands ①②③ / rendered text).
        try:
            chunk_rows = reader.get_history_chunks() or []
        except (sqlite3.Error, ValueError):
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
            rendered = (
                f"[History chunk | Steps {start_n}–{end_n} | status"
                f" {getattr(row, 'status', '?')}]\n  Match: {_excerpt(text, terms, EXCERPT_CHARS)}"
            )
            results.append((score, int(end_n or 0), rendered))

        # --- Notes (filesystem; attributed to their writing steps).
        try:
            from artemis.utils.notes import list_notes_keys, read_note_content

            base_dir = getattr(reader, "base_dir", None)
            note_keys = list_notes_keys(base_dir) if base_dir else []
        except OSError:
            note_keys = []
        for key in note_keys:
            try:
                content = read_note_content(base_dir, key)
            except (OSError, UnicodeError):
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
            results.append(
                (
                    score,
                    int(anchor),
                    f"[Note '{key}' | {origin}]\n  Match: {_excerpt(content, terms, EXCERPT_CHARS)}",
                )
            )

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
            + "\n… (response truncated at the search token budget; narrow the"
            " query or step_range)"
        )
    return text
