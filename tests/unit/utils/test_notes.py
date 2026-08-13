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

from artemis.utils.notes import (
    append_note_content,
    list_notes_info,
    read_note_content,
    save_note_content,
    update_note_content,
)
import pytest


def test_list_notes_info(tmp_path):
    base_dir = tmp_path

    # Create some notes
    save_note_content(base_dir, "note1", "line1\nline2\nline3")
    save_note_content(base_dir, "note2", "line1\nline2")

    info = list_notes_info(base_dir)
    assert info == {"note1": 3, "note2": 2}


def test_read_note_content_range(tmp_path):
    base_dir = tmp_path
    save_note_content(base_dir, "note1", "line1\nline2\nline3\nline4")

    # Read full
    assert read_note_content(base_dir, "note1") == "line1\nline2\nline3\nline4"

    # Read range
    assert read_note_content(base_dir, "note1", start_line=2, end_line=3) == "line2\nline3"

    # Read from start to line
    assert read_note_content(base_dir, "note1", end_line=2) == "line1\nline2"

    # Read from line to end
    assert read_note_content(base_dir, "note1", start_line=3) == "line3\nline4"


def test_update_note_exact_match(tmp_path):
    base_dir = tmp_path
    original_content = 'def demo():\n    print("Hello, World!")\n    return True\n'
    save_note_content(base_dir, "task_plan", original_content)

    # Exact match
    warning = update_note_content(
        base_dir,
        "task_plan",
        '    print("Hello, World!")',
        '    print("Hello, Jetski!")',
    )
    assert warning is None

    updated_content = read_note_content(base_dir, "task_plan")
    assert 'print("Hello, Jetski!")' in updated_content
    assert 'print("Hello, World!")' not in updated_content


def test_update_note_relaxed_match(tmp_path):
    base_dir = tmp_path
    # Multi-line content with checkboxes and indentation
    original_content = (
        "- [ ] Step 1: Research the issue\n  - [ ] Step 2: Write code\n  - [ ] Step 3: Run tests\n"
    )
    save_note_content(base_dir, "task_plan", original_content)

    # Relaxed match: case difference, quote difference, checkbox bullet difference, trailing whitespace
    target = "  * [ ] step 2: write code  "
    replacement = "  - [/] Step 2: In progress"

    warning = update_note_content(base_dir, "task_plan", target, replacement)

    # Warning should be returned
    assert warning is not None
    assert "We did our best to apply changes despite some inaccuracies" in warning

    updated_content = read_note_content(base_dir, "task_plan")
    assert "Step 2: In progress" in updated_content
    assert "Step 2: Write code" not in updated_content
    # Check indentation is preserved from replacement
    assert "  - [/] Step 2: In progress\n" in updated_content


def test_update_note_fuzzy_match(tmp_path):
    base_dir = tmp_path
    original_content = 'print("Alpha message")\nprint("Gamma message")\nprint("Delta message")\n'
    save_note_content(base_dir, "task_plan", original_content)

    # Fuzzy match: typo (Gemma vs Gamma)
    target = 'print("Gemma message")'
    replacement = 'print("Gamma updated!")'

    warning = update_note_content(base_dir, "task_plan", target, replacement)
    assert warning is not None
    assert "We did our best to apply changes despite some inaccuracies" in warning

    updated_content = read_note_content(base_dir, "task_plan")
    assert 'print("Gamma updated!")' in updated_content
    assert 'print("Gamma message")' not in updated_content


def test_update_note_ambiguous_match(tmp_path):
    base_dir = tmp_path
    # Multiple lines that could match target
    original_content = 'print("Beta message")\nprint("Zeta message")\nprint("Eta message")\n'
    save_note_content(base_dir, "task_plan", original_content)

    # Target is highly ambiguous (Xeta is almost equally close to Beta and Zeta)
    target = 'print("Xeta message")'
    replacement = 'print("Ambiguous match!")'

    # Should raise ValueError due to ambiguity or inability to find unique match
    with pytest.raises(ValueError) as exc_info:
        update_note_content(base_dir, "task_plan", target, replacement)

    assert "Target string 'print(\"Xeta message\")' not found" in str(
        exc_info.value
    ) or "not unique" in str(exc_info.value)


def test_update_note_not_found(tmp_path):
    base_dir = tmp_path
    original_content = 'print("Alpha message")\n'
    save_note_content(base_dir, "task_plan", original_content)

    # Totally different target
    target = 'print("Something totally different")'
    replacement = 'print("Hello")'

    with pytest.raises(ValueError) as exc_info:
        update_note_content(base_dir, "task_plan", target, replacement)
    assert "not found" in str(exc_info.value)


def test_append_note_content(tmp_path):
    base_dir = tmp_path

    # Case 1: File does not exist
    append_note_content(base_dir, "new_note", "hello")
    assert read_note_content(base_dir, "new_note") == "hello\n"

    # Case 2: File exists and ends with a newline
    append_note_content(base_dir, "new_note", "world")
    assert read_note_content(base_dir, "new_note") == "hello\nworld\n"

    # Case 3: File exists and does not end with a newline
    file_path = base_dir / "notes" / "no_newline.md"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("no newline", encoding="utf-8")

    append_note_content(base_dir, "no_newline", "added text")
    assert read_note_content(base_dir, "no_newline") == "no newline\nadded text\n"
