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

from artemis.mcp.screen_diff import (
    NO_SCREEN_CHANGES,
    UI_DIFF_MARKER,
    ScreenTracker,
    render_diff_block,
)


def _el(index, text, cx=540, cy=200, cls="android.widget.TextView", rid=None, ocr=False):
    return {
        "index": index,
        "center": [cx, cy],
        "text": text,
        "bounds": [cx - 100, cy - 50, cx + 100, cy + 50],
        "class": cls,
        "resource_id": rid if rid is not None else f"id/{text.lower().replace(' ', '_')}",
        "is_ocr": ocr,
    }


def _line(index, text, bounds="[0,0][100,100]"):
    return f"[{index}] Text: '{text}' | Bounds: {bounds}"


def _tracker():
    return ScreenTracker()


def test_first_frame_reports_full_screen():
    tracker = _tracker()
    elements = [_el(1, "Wi-Fi")]
    result = tracker.observe_frame(elements, [_line(1, "Wi-Fi")], 1080, 2400)
    assert result.is_full is True


def test_identical_frame_reports_no_changes():
    tracker = _tracker()
    elements = [_el(1, "Wi-Fi"), _el(2, "Bluetooth")]
    lines = [_line(1, "Wi-Fi"), _line(2, "Bluetooth")]
    tracker.observe_frame(elements, lines, 1080, 2400)
    result = tracker.observe_frame(elements, lines, 1080, 2400)
    assert result.is_full is False
    assert result.lines == []


def test_changed_value_reports_changed_line():
    tracker = _tracker()
    before = [_el(1, "Wi-Fi"), _el(2, "Bluetooth")]
    tracker.observe_frame(before, [_line(1, "Wi-Fi"), _line(2, "Bluetooth")], 1080, 2400)
    after = [_el(1, "Wi-Fi"), _el(2, "Bluetooth")]
    result = tracker.observe_frame(
        after, [_line(1, "Wi-Fi"), _line(2, "Bluetooth", "[0,10][100,110]")], 1080, 2400
    )
    assert result.is_full is False
    assert result.lines == ["~ [2] Text: 'Bluetooth' | Bounds: [0,10][100,110]"]


def test_inserted_element_shifts_indices_without_changed_lines():
    tracker = _tracker()
    tracker.observe_frame([_el(1, "Wi-Fi")], [_line(1, "Wi-Fi")], 1080, 2400)
    after = [_el(1, "Back"), _el(2, "Wi-Fi")]
    result = tracker.observe_frame(after, [_line(1, "Back"), _line(2, "Wi-Fi")], 1080, 2400)
    assert result.is_full is False
    assert result.lines == ["+ [1] Text: 'Back' | Bounds: [0,0][100,100]"]


def test_removed_element_reports_removed_line():
    tracker = _tracker()
    before = [_el(1, "Wi-Fi"), _el(2, "Bluetooth")]
    tracker.observe_frame(before, [_line(1, "Wi-Fi"), _line(2, "Bluetooth")], 1080, 2400)
    result = tracker.observe_frame([_el(1, "Wi-Fi")], [_line(1, "Wi-Fi")], 1080, 2400)
    assert result.is_full is False
    assert result.lines == ["- [2] Text: 'Bluetooth' | Bounds: [0,0][100,100]"]


def test_renamed_control_falls_back_to_full():
    tracker = _tracker()
    tracker.observe_frame([_el(1, "Play")], [_line(1, "Play")], 1080, 2400)
    result = tracker.observe_frame([_el(1, "Pause")], [_line(1, "Pause")], 1080, 2400)
    assert result.is_full is True


def test_moved_elements_keep_refs_and_report_changed_lines():
    tracker = _tracker()
    before = [_el(1, "Wi-Fi", cy=200), _el(2, "Bluetooth", cy=600)]
    tracker.observe_frame(before, [_line(1, "Wi-Fi"), _line(2, "Bluetooth")], 1080, 2400)
    after = [_el(1, "Wi-Fi", cy=600), _el(2, "Bluetooth", cy=1000)]
    result = tracker.observe_frame(
        after,
        [_line(1, "Wi-Fi", "[0,5][100,105]"), _line(2, "Bluetooth", "[0,9][100,109]")],
        1080,
        2400,
    )
    assert result.is_full is False
    assert [line[0] for line in result.lines] == ["~", "~"]


def test_duplicate_controls_keep_distinct_stable_refs():
    tracker = _tracker()
    before = [
        _el(1, "OK", cy=200, cls="android.widget.Button", rid="id/ok"),
        _el(2, "OK", cy=600, cls="android.widget.Button", rid="id/ok"),
    ]
    tracker.observe_frame(before, [_line(1, "OK"), _line(2, "OK")], 1080, 2400)
    after = [
        _el(1, "OK", cy=200, cls="android.widget.Button", rid="id/ok"),
        _el(2, "Cancel", cy=400, cls="android.widget.Button", rid="id/cancel"),
        _el(3, "OK", cy=600, cls="android.widget.Button", rid="id/ok"),
    ]
    result = tracker.observe_frame(
        after, [_line(1, "OK"), _line(2, "Cancel"), _line(3, "OK")], 1080, 2400
    )
    assert result.is_full is False
    assert result.lines == ["+ [2] Text: 'Cancel' | Bounds: [0,0][100,100]"]


def test_ocr_lines_match_by_exact_text():
    tracker = _tracker()
    before = [_el(1, "Sign in", ocr=True)]
    tracker.observe_frame(before, ["[1] OCR Text: 'Sign in' | Bounds: [0,0][100,100]"], 1080, 2400)
    after = [_el(1, "Sign in", ocr=True), _el(2, "Welcome back", ocr=True)]
    result = tracker.observe_frame(
        after,
        [
            "[1] OCR Text: 'Sign in' | Bounds: [0,0][100,100]",
            "[2] OCR Text: 'Welcome back' | Bounds: [0,1][100,101]",
        ],
        1080,
        2400,
    )
    assert result.is_full is False
    assert result.lines == ["+ [2] OCR Text: 'Welcome back' | Bounds: [0,1][100,101]"]


def test_diff_longer_than_screen_falls_back_to_full():
    tracker = _tracker()
    tracker.observe_frame([_el(1, "Wi-Fi")], [_line(1, "Wi-Fi")], 1080, 2400)
    after = [_el(1, "Back"), _el(2, "Search")]
    result = tracker.observe_frame(after, [_line(1, "Back"), _line(2, "Search")], 1080, 2400)
    assert result.is_full is True


def test_size_change_falls_back_to_full():
    tracker = _tracker()
    elements = [_el(1, "Wi-Fi")]
    tracker.observe_frame(elements, [_line(1, "Wi-Fi")], 1080, 2400)
    result = tracker.observe_frame(elements, [_line(1, "Wi-Fi")], 1080, 1920)
    assert result.is_full is True


def test_render_diff_block_without_diff_returns_none():
    assert render_diff_block(None) is None


def test_render_diff_block_with_empty_diff_names_no_changes():
    block = render_diff_block("")
    assert block is not None
    assert block.startswith(UI_DIFF_MARKER)
    assert NO_SCREEN_CHANGES in block


def test_render_diff_block_with_diff_lists_changes_first():
    block = render_diff_block("~ [2] Text: 'Bluetooth' | Bounds: [0,10][100,110]")
    assert block is not None
    assert block.startswith(UI_DIFF_MARKER)
    assert "~ [2]" in block
