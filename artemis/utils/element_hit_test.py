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

"""Record-time action semantics enrichment via element hit testing.

An action addressed by bare coordinates carries no element semantics
("tap(632,1180)" says nothing about what was tapped). Before such an action is
persisted to the DataEngine, these helpers resolve the smallest indexed element
covering the tap point on the pre-action frame and attach best-effort
``target_text`` / ``target_class`` / ``target_resource_id`` fields, plus a
``target_label_source`` marker distinguishing model-named targets from
after-the-fact inference:

- ``"index"``: the model addressed the element by its perception index — the
  semantics come straight from the indexed element (highest confidence).
- ``"hit_test"``: inferred by hit testing the coordinates against XML-derived
  indexed elements on the pre-action frame.
- ``"ocr"``: inferred by hit testing against OCR-derived indexed elements
  (used only when no XML-derived element covers the point).
- ``"none"``: no element covers the point (or no perception data was
  available) — the action stays a bare-coordinate action.

Enrichment is strictly best-effort: missing/malformed element data degrades to
``"none"`` and never raises.
"""

from typing import Any

from artemis.utils.visualization import parse_bounds

_LABEL_SOURCE_KEY = "target_label_source"


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


def find_element_at_point(
    elements: list[dict[str, Any]] | None, x: int, y: int
) -> tuple[dict[str, Any] | None, str]:
    """Finds the smallest indexed element whose bounds contain pixel point (x, y).

    XML-derived elements are preferred over OCR-derived ones (OCR boxes carry
    text but weaker structure); within each group the smallest covering area
    wins. Returns ``(element, source)`` where source is ``"hit_test"``,
    ``"ocr"``, or ``"none"`` (with element None).
    """
    best_xml: tuple[int, dict[str, Any]] | None = None
    best_ocr: tuple[int, dict[str, Any]] | None = None

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
        if element.get("is_ocr"):
            if best_ocr is None or area < best_ocr[0]:
                best_ocr = (area, element)
        else:
            if best_xml is None or area < best_xml[0]:
                best_xml = (area, element)

    if best_xml is not None:
        return best_xml[1], "hit_test"
    if best_ocr is not None:
        return best_ocr[1], "ocr"
    return None, "none"


def hit_test_semantics(
    elements: list[dict[str, Any]] | None, x: int, y: int
) -> dict[str, Any]:
    """Best-effort semantic fields for a bare-coordinate action at pixel (x, y).

    Always returns a dict containing ``target_label_source``; the
    ``target_text`` / ``target_class`` / ``target_resource_id`` fields are only
    present when the covering element carries them.
    """
    try:
        element, source = find_element_at_point(elements, int(x), int(y))
    except Exception:
        return {_LABEL_SOURCE_KEY: "none"}
    semantics: dict[str, Any] = {_LABEL_SOURCE_KEY: source}
    if element is None:
        return semantics
    if element.get("text"):
        semantics["target_text"] = element["text"]
    if element.get("class"):
        semantics["target_class"] = element["class"]
    if element.get("resource_id"):
        semantics["target_resource_id"] = element["resource_id"]
    return semantics
