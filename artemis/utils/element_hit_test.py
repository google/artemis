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

"""Element hit testing: which indexed element covers a pixel point.

Used by the Explorer to deduplicate candidates against the screen index.
Recorded coordinate actions use the model's ``target_description``.
"""

from typing import Any

from artemis.utils.visualization import parse_bounds


def _element_bounds(element: dict[str, Any]) -> tuple[int, int, int, int] | None:
    """Returns (left, top, right, bottom) pixel bounds of an indexed element.

    Accepts both list bounds (``[l, t, r, b]``, the shape produced by
    ``format_minimal_list_with_elements``) and Android bounds strings
    (``"[l,t][r,b]"``). Returns None for missing or malformed bounds.
    """
    bounds = element.get("bounds")
    if isinstance(bounds, str):
        return parse_bounds(bounds)
    if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
        try:
            left, top, right, bottom = (int(v) for v in bounds)
        except (TypeError, ValueError):
            return None
        return left, top, right, bottom
    return None


_CONTAINER_HINTS = (
    "other",
    "view",
    "group",
    "layout",
    "container",
    "scroll",
    "table",
    "collection",
    "frame",
    "linear",
    "relative",
    "constraint",
    "window",
    "root",
    "pane",
    "panel",
    "grid",
    "stack",
    "box",
    "list",
    "toolbar",
    "navigation",
    "navbar",
)


def _tie_rank(element: dict[str, Any]) -> int:
    """Preference rank when two covering elements have exactly the same frame.

    Editable text targets win over layout containers: search field, text
    field, text view, then any element holding keyboard focus, then other
    semantic elements, and plain containers last. iOS emits
    container-over-box hierarchies where the field and its container share
    one frame; Android EditText shows the same shape.
    """
    class_name = str(element.get("class") or "").lower()
    resource_id = str(element.get("resource_id") or "").lower()
    haystack = f"{class_name} {resource_id}"
    if "search" in haystack:
        return 0
    if "textfield" in haystack or "edittext" in haystack or "textbox" in haystack:
        return 1
    if "textview" in haystack:
        return 2
    if (
        element.get("keyboard_focused")
        or element.get("has_keyboard_focus")
        or element.get("hasKeyboardFocus")
        or str(element.get("focused", "")).lower() == "true"
    ):
        return 3
    if any(hint in haystack for hint in _CONTAINER_HINTS):
        return 5
    return 4


def find_element_at_point(
    elements: list[dict[str, Any]] | None, x: int, y: int
) -> tuple[dict[str, Any] | None, str]:
    """Finds the smallest indexed element whose bounds contain pixel point (x, y).

    XML-derived elements are preferred over OCR-derived ones (OCR boxes carry
    text but weaker structure); within each group the smallest covering area
    wins, with equal-area ties broken by :func:`_tie_rank` so an editable
    field beats a container sharing its exact frame. Returns ``(element,
    source)`` where source is ``"hit_test"``, ``"ocr"``, or ``"none"`` (with
    element None). Malformed element data is skipped; the function never
    raises on it.
    """
    best_xml: tuple[tuple[int, int], dict[str, Any]] | None = None
    best_ocr: tuple[tuple[int, int], dict[str, Any]] | None = None

    for element in elements or []:
        if not isinstance(element, dict):
            continue
        rect = _element_bounds(element)
        if rect is None:
            continue
        left, top, right, bottom = rect
        if not (left <= x <= right and top <= y <= bottom):
            continue
        area = max(1, (right - left)) * max(1, (bottom - top))
        key = (area, _tie_rank(element))
        if element.get("is_ocr"):
            if best_ocr is None or key < best_ocr[0]:
                best_ocr = (key, element)
        else:
            if best_xml is None or key < best_xml[0]:
                best_xml = (key, element)

    if best_xml is not None:
        return best_xml[1], "hit_test"
    if best_ocr is not None:
        return best_ocr[1], "ocr"
    return None, "none"
