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

import difflib
import hashlib
import json
import os
from pathlib import Path
import re

from artemis.data_engine.engine import _CURRENT_DATA_ENGINE
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

# Centralized Description and Documentation Constants (Single Source of Truth)
READ_NOTE_DOCSTRING = (
    "[NOTE] Reads a previously saved note from persistent memory by its"
    " key.\nUse this as persistent cross-turn memory to retrieve key data, plan"
    " cross-turn subgoals, and coordinate step progress.\nYou can optionally"
    " specify a line range to read by providing start_line and end_line"
    " (1-indexed, inclusive).\nTIP: For large notes, prefer reading a small"
    " range of lines at a time (e.g., up to 200 lines). For chronological"
    " files, consider reading from back to front to see the latest entries"
    " first.\n\nUse the key 'task_plan' to read the active task plan"
    " (checklist)."
)
READ_NOTE_ARG_KEY_DESC = "The key of the note to read."

LIST_NOTES_DOCSTRING = (
    "[NOTE] Lists all note keys currently stored in persistent memory, along"
    " with the number of lines in each note.\nUse this to coordinate step"
    " progress and recall persistent cross-turn memories."
)

SAVE_NOTE_DOCSTRING = (
    "[NOTE] Saves a text note to persistent memory with the given key. Use this"
    " as persistent cross-turn memory to save key data, plan cross-turn"
    " subgoals, and coordinate step progress. If the key already exists, it"
    " will be overwritten.\n\nUse the key 'task_plan' to overwrite the original"
    " task plan (checklist)."
)
SAVE_NOTE_ARG_KEY_DESC = "The unique key under which to save or overwrite the note."
SAVE_NOTE_ARG_CONTENT_DESC = "The text content to write to the note."

APPEND_NOTE_DOCSTRING = (
    "[NOTE] Appends text content to an existing note in persistent memory with"
    " the given key. Use this as persistent cross-turn memory to record"
    " chronological logs, save key data, or coordinate step progress. If the"
    " note does not exist, it will be created.\n\nUse the key 'task_plan' to"
    " append to the task plan (checklist)."
)
APPEND_NOTE_ARG_KEY_DESC = "The key of the note to append to."
APPEND_NOTE_ARG_CONTENT_DESC = "The text content to append to the note."

UPDATE_NOTE_DOCSTRING = (
    "[NOTE] Updates a note by replacing a specific target string with"
    " replacement content. Use this as persistent cross-turn memory to modify"
    " specific planning nodes or step details without overwriting the whole"
    " file.\n\nUse the key 'task_plan' to update specific sections of the task"
    " plan (checklist)."
)
UPDATE_NOTE_ARG_KEY_DESC = "The key of the note to update."
UPDATE_NOTE_ARG_TARGET_DESC = (
    "The exact target string currently in the note that you wish to replace."
)
UPDATE_NOTE_ARG_REPLACEMENT_DESC = (
    "The new replacement content to insert instead of the target content."
)


# Core Business Logic Filesystem Functions


def get_notes_dir(base_dir: str | Path) -> Path:
    """Gets the absolute path to the notes directory inside the session's base directory."""
    return Path(base_dir) / "notes"


def get_note_file_path(base_dir: str | Path, key: str) -> Path:
    """Gets the path of a note file by its key, mapping 'task_plan' to 'task_plan.md'.

    Case-insensitively strips any trailing '.md' from the key to prevent
    double-extension bugs.
    """
    notes_dir = get_notes_dir(base_dir)
    # Strip any trailing .md (e.g. 'findings.md' -> 'findings')
    clean_key = key[:-3] if key.lower().endswith(".md") else key
    if clean_key == "task_plan":
        return notes_dir / "task_plan.md"
    return notes_dir / f"{clean_key}.md"


def list_notes_keys(base_dir: str | Path) -> list[str]:
    """Lists all note keys currently stored in persistent memory."""
    notes_dir = get_notes_dir(base_dir)
    if not notes_dir.exists():
        return []
    return [f.stem for f in notes_dir.glob("*.md")]


def list_notes_info(base_dir: str | Path) -> dict[str, int]:
    """Lists all note keys currently stored in persistent memory, along with their line counts."""
    notes_dir = get_notes_dir(base_dir)
    if not notes_dir.exists():
        return {}
    info = {}
    for f in notes_dir.glob("*.md"):
        try:
            content = f.read_text(encoding="utf-8")
            info[f.stem] = len(content.splitlines())
        except Exception:
            info[f.stem] = -1
    return info


def read_note_content(
    base_dir: str | Path,
    key: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Reads the raw content of a note, optionally restricted to a line range (1-indexed, inclusive)."""
    file_path = get_note_file_path(base_dir, key)
    if not file_path.exists():
        raise FileNotFoundError(f"Note '{key}' not found.")
    content = file_path.read_text(encoding="utf-8")

    if start_line is not None or end_line is not None:
        lines = content.splitlines()
        start = (start_line - 1) if start_line else 0
        end = end_line if end_line else len(lines)
        return "\n".join(lines[start:end])

    return content


def save_note_content(base_dir: str | Path, key: str, content: str) -> None:
    """Saves or overwrites the raw content of a note."""
    notes_dir = get_notes_dir(base_dir)
    notes_dir.mkdir(parents=True, exist_ok=True)
    file_path = get_note_file_path(base_dir, key)

    content_before = ""
    if key == "task_plan" and file_path.exists():
        try:
            content_before = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning(
                "Failed to read previous task_plan content from %s; subgoal"
                " rename tracking will be skipped for this save: %s",
                file_path,
                exc,
            )

    file_path.write_text(content, encoding="utf-8")

    if key == "task_plan":
        try:
            record_subgoal_hash_chain(base_dir, content_before, content)
        except Exception as exc:
            # Hash-chain bookkeeping must never break a note save, but a
            # missed record can later confuse plan ratchet validation.
            logger.warning("Failed to record subgoal hash chain for task_plan: %s", exc)

    try:
        if _CURRENT_DATA_ENGINE:
            _CURRENT_DATA_ENGINE._publish("note_saved", {"key": key, "content": content})
    except Exception as exc:
        # Telemetry side channel: publishing must never break a note save.
        logger.debug("Failed to publish note_saved event for key '%s': %s", key, exc)


def append_note_content(base_dir: str | Path, key: str, content: str) -> None:
    """Appends raw content to a note, inserting a newline if the file is not empty and lacks a trailing newline."""
    notes_dir = get_notes_dir(base_dir)
    notes_dir.mkdir(parents=True, exist_ok=True)
    file_path = get_note_file_path(base_dir, key)

    prefix = ""
    if file_path.exists() and file_path.stat().st_size > 0:
        # Open in binary mode for safe and portable backward seek
        with open(file_path, "rb") as f:
            try:
                f.seek(-1, os.SEEK_END)
                last_char_bytes = f.read(1)
                if last_char_bytes != b"\n":
                    prefix = "\n"
            except OSError:
                # Cannot inspect the last byte; worst case is a missing
                # separator newline before the appended content.
                pass

    with open(file_path, "a", encoding="utf-8") as f:
        f.write(prefix + content + "\n")


def clean_line(line: str) -> str:
    """Cleans a line by lowering case, stripping quotes, removing markdown bullet/checkbox markers, and all whitespace."""
    s = line.lower()
    s = s.replace('"', "").replace("'", "")
    # Strip markdown bullet patterns: optional start spaces, bullet character (-*+), optional spaces, optional checkbox [ xX/]
    s = re.sub(r"^\s*[-*+]\s*(\[[ xX/]\])?\s*", "", s)
    return "".join(s.split())


def find_relaxed_match(content: str, target: str) -> tuple[int, int] | None:
    """Finds a unique relaxed match (ignoring whitespaces, casing, quotes, and list bullets).

    Returns (start_char_idx, end_char_idx) if a unique match is found, otherwise
    None.
    """
    note_lines = content.splitlines(keepends=True)
    target_lines = target.splitlines()

    clean_note = [clean_line(line) for line in note_lines]
    clean_target = [clean_line(line) for line in target_lines if clean_line(line)]

    n = len(clean_note)
    m = len(clean_target)
    if m == 0 or n < m:
        return None

    matches = []
    for i in range(n - m + 1):
        window = clean_note[i : i + m]
        if window == clean_target:
            matches.append((i, i + m))

    if len(matches) == 1:
        start_line, end_line = matches[0]
        start_idx = sum(len(line) for line in note_lines[:start_line])
        end_idx = sum(len(line) for line in note_lines[:end_line])
        return start_idx, end_idx

    return None


def find_fuzzy_match(content: str, target: str, threshold: float = 0.90) -> tuple[int, int] | None:
    """Uses sliding window difflib similarity to find a unique close match above the threshold.

    Returns (start_char_idx, end_char_idx) if a unique close match is found,
    otherwise None.
    """
    note_lines = content.splitlines(keepends=True)
    target_lines = target.splitlines()

    n = len(note_lines)
    m = len(target_lines)
    if m == 0 or n < m:
        return None

    best_match = None
    best_ratio = 0.0
    ambiguous = False

    target_clean = "\n".join(clean_line(line) for line in target_lines)

    for i in range(n - m + 1):
        window_clean = "\n".join(clean_line(line) for line in note_lines[i : i + m])
        matcher = difflib.SequenceMatcher(None, window_clean, target_clean)
        ratio = matcher.ratio()

        if ratio >= threshold:
            if ratio > best_ratio + 0.05:
                best_ratio = ratio
                best_match = (i, i + m)
                ambiguous = False
            elif abs(ratio - best_ratio) <= 0.05:
                ambiguous = True

    if best_match and not ambiguous:
        start_line, end_line = best_match
        start_idx = sum(len(line) for line in note_lines[:start_line])
        end_idx = sum(len(line) for line in note_lines[:end_line])
        return start_idx, end_idx

    return None


def apply_replacement(content: str, start_idx: int, end_idx: int, replacement: str) -> str:
    """Applies a replacement, preserving the trailing newline of the original slice if present."""
    original_slice = content[start_idx:end_idx]
    actual_replacement = replacement
    if original_slice.endswith("\n") and not actual_replacement.endswith("\n"):
        actual_replacement += "\n"
    return content[:start_idx] + actual_replacement + content[end_idx:]


def update_note_content(
    base_dir: str | Path, key: str, target: str, replacement: str
) -> str | None:
    """Replaces target content in a note using hierarchical exact, relaxed, or fuzzy match.

    Returns a warning message if relaxed/fuzzy match was used, otherwise None.
    Raises ValueError if target is not found or if the match is ambiguous/not
    unique.
    """
    file_path = get_note_file_path(base_dir, key)
    if not file_path.exists():
        raise FileNotFoundError(f"Note '{key}' not found.")

    content = file_path.read_text(encoding="utf-8")
    new_content_to_write = None
    warning = None

    # Phase 1: Exact Match
    if target in content:
        count = content.count(target)
        if count == 1:
            new_content_to_write = content.replace(target, replacement)
            warning = None
        elif count > 1:
            raise ValueError(
                f"Target string '{target}' is not unique in note '{key}' (found"
                f" {count} occurrences). To prevent accidental changes, please"
                " provide a longer and more complete target string."
            )

    # Phase 2: Relaxed Match (Whitespace, casing, quotes, checkboxes ignored)
    if new_content_to_write is None:
        relaxed_range = find_relaxed_match(content, target)
        if relaxed_range:
            start_idx, end_idx = relaxed_range
            new_content_to_write = apply_replacement(content, start_idx, end_idx, replacement)
            warning = (
                "We did our best to apply changes despite some inaccuracies."
                " Double check if the edit applied is what you intended."
            )

    # Phase 3: Fuzzy Match (difflib similarity)
    if new_content_to_write is None:
        fuzzy_range = find_fuzzy_match(content, target, threshold=0.90)
        if fuzzy_range:
            start_idx, end_idx = fuzzy_range
            new_content_to_write = apply_replacement(content, start_idx, end_idx, replacement)
            warning = (
                "We did our best to apply changes despite some inaccuracies."
                " Double check if the edit applied is what you intended."
            )

    # Phase 4: Write and Return, or Not Found Error
    if new_content_to_write is not None:
        file_path.write_text(new_content_to_write, encoding="utf-8")
        if key == "task_plan":
            try:
                record_subgoal_hash_chain(base_dir, content, new_content_to_write)
            except Exception as exc:
                # Hash-chain bookkeeping must never break a note edit, but a
                # missed record can later confuse plan ratchet validation.
                logger.warning("Failed to record subgoal hash chain for task_plan: %s", exc)
        return warning

    raise ValueError(
        f"Target string '{target}' not found in note '{key}'. Please re-read"
        " the note and ensure spelling, spacing, and indentation are close."
    )


# Shared Formatting Functions for Tool Outputs


def format_read_note_success(
    key: str,
    content: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Formats the success message for reading a note."""
    range_str = ""
    if start_line is not None or end_line is not None:
        range_str = f" (lines {start_line or 1} to {end_line or 'end'})"
    return f"Successfully read note '{key}'{range_str}. '{key}' note content:\n{content}"


def format_read_note_failure(key: str, error: str) -> str:
    """Formats the failure message for reading a note."""
    if "not found" in error.lower():
        return f"Note '{key}' not found in scratchpad."
    return f"Failed to read note '{key}': {error}"


def format_list_notes_success(notes_info: dict[str, int]) -> str:
    """Formats the success message for listing notes with line counts."""
    if not notes_info:
        return "No notes saved yet."
    lines = [f"- {key} ({count} lines)" for key, count in notes_info.items()]
    return "Here are all the notes:\n" + "\n".join(lines)


def format_list_notes_failure(error: str) -> str:
    """Formats the failure message for listing notes."""
    return f"Failed to list notes: {error}"


def record_subgoal_hash_chain(
    base_dir: str | Path, content_before: str, content_after: str
) -> None:
    """Detects if the active subgoal was renamed/changed, and records it in the subgoal_hash_chain.json."""
    if not content_before or not content_after:
        return

    # Helper to get the active subgoal hash from task_plan string
    def get_active_hash(task_plan: str) -> str | None:
        try:
            lines = task_plan.splitlines()
            for line in reversed(lines):
                if line.strip().startswith("- [/]"):
                    text = line.strip()[5:].strip()
                    return hashlib.md5(text.encode("utf-8")).hexdigest()

            # Fallback to first pending [ ] if no [/]
            for line in lines:
                if line.strip().startswith("- [ ]"):
                    text = line.strip()[5:].strip()
                    return hashlib.md5(text.encode("utf-8")).hexdigest()
        except Exception as exc:
            # Pure string/hash operations; a failure here is unexpected and
            # silently disables subgoal rename tracking.
            logger.warning("Failed to compute active subgoal hash: %s", exc)
        return None

    old_active = get_active_hash(content_before)
    new_active = get_active_hash(content_after)

    if old_active and new_active and old_active != new_active:
        notes_dir = Path(base_dir) / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        chain_path = notes_dir / "subgoal_hash_chain.json"

        chain = {}
        if chain_path.exists():
            try:
                chain = json.loads(chain_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning(
                    "Failed to load subgoal hash chain %s (starting a fresh"
                    " chain; previous rename history is lost): %s",
                    chain_path,
                    exc,
                )

        # Record mapping: old -> new
        chain[old_active] = new_active

        try:
            chain_path.write_text(
                json.dumps(chain, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(
                "Failed to persist subgoal hash chain to %s (rename %s -> %s"
                " not recorded): %s",
                chain_path,
                old_active,
                new_active,
                exc,
            )
