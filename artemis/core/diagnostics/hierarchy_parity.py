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

"""Real-device parity check between the Accessibility Helper and UIAutomator2.

The unit tests prove the two backends produce the same element *shapes*; only
a real screen can prove they describe the same *content*. ``compare_backends``
dumps the current screen with both, then checks what the agent depends on:

* every labelled UIAutomator element (text or content-desc) has a helper
  element with the same label at the same place (bounds IoU >= 0.5), and vice
  versa, so neither backend invents or loses what is on screen;
* the helper emits no negative and no off-screen bounds (its bounds are
  clipped to the display, like UIAutomator's);
* dump latency of both, for the record.

``artemis helper parity`` is the CLI front end.
"""

from __future__ import annotations

from statistics import median
import time
from typing import Any

from artemis.clients.ui_automator_client import _parse_hierarchy_xml_to_elements

MIN_RECALL = 0.9
MIN_PRECISION = 0.8


def _label(element: dict[str, Any]) -> str:
    text = str(element.get("text") or "").strip()
    if text:
        return text
    return str(element.get("content-desc") or "").strip()


def _iou(a: dict[str, int], b: dict[str, int]) -> float:
    left = max(a["left"], b["left"])
    top = max(a["top"], b["top"])
    right = min(a["right"], b["right"])
    bottom = min(a["bottom"], b["bottom"])
    if right <= left or bottom <= top:
        return 0.0
    inter = (right - left) * (bottom - top)
    area_a = max(1, (a["right"] - a["left"]) * (a["bottom"] - a["top"]))
    area_b = max(1, (b["right"] - b["left"]) * (b["bottom"] - b["top"]))
    return inter / (area_a + area_b - inter)


def analyze_xml(xml: str, width: int, height: int) -> dict[str, Any]:
    """Counts and labelled elements of one dump, plus the bounds problems the helper must not have."""
    elements = _parse_hierarchy_xml_to_elements(xml)
    labelled: list[tuple[str, dict[str, int]]] = []
    negative = offscreen = 0
    package = None
    for element in elements:
        bounds = element.get("parsed_bounds")
        if package is None and element.get("package"):
            package = element["package"]
        if not isinstance(bounds, dict):
            continue
        if min(bounds.values()) < 0:
            negative += 1
        if (
            bounds["left"] >= width
            or bounds["top"] >= height
            or bounds["right"] > width
            or bounds["bottom"] > height
        ):
            offscreen += 1
        label = _label(element)
        if label:
            labelled.append((label, bounds))
    return {
        "nodes": len(elements),
        "labelled": len(labelled),
        "labels": labelled,
        "negative_bounds": negative,
        "offscreen_bounds": offscreen,
        "package": package,
    }


def match_labels(
    source: list[tuple[str, dict[str, int]]], target: list[tuple[str, dict[str, int]]]
) -> tuple[int, list[str]]:
    """How many labelled ``source`` elements have a same-label, same-place element in ``target``."""
    used = [False] * len(target)
    found = 0
    missing: list[str] = []
    for label, bounds in source:
        hit = False
        for i, (other_label, other_bounds) in enumerate(target):
            if used[i] or other_label != label:
                continue
            if _iou(bounds, other_bounds) >= 0.5:
                used[i] = True
                hit = True
                break
        if hit:
            found += 1
        else:
            missing.append(f"{label!r} at {bounds}")
    return found, missing


def compare_dumps(helper_xml: str, uiautomator_xml: str, width: int, height: int) -> dict[str, Any]:
    """Pure comparison of two dumps of the same screen."""
    helper = analyze_xml(helper_xml, width, height)
    u2 = analyze_xml(uiautomator_xml, width, height)
    u2_found, missing_in_helper = match_labels(u2["labels"], helper["labels"])
    helper_found, extra_in_helper = match_labels(helper["labels"], u2["labels"])
    recall = u2_found / u2["labelled"] if u2["labelled"] else 1.0
    precision = helper_found / helper["labelled"] if helper["labelled"] else 1.0
    problems: list[str] = []
    if helper["negative_bounds"]:
        problems.append(
            f"helper emitted {helper['negative_bounds']} element(s) with negative bounds"
        )
    if helper["offscreen_bounds"]:
        problems.append(
            f"helper emitted {helper['offscreen_bounds']} element(s) outside the screen"
        )
    if recall < MIN_RECALL:
        problems.append(
            f"only {recall:.0%} of UIAutomator's labelled elements appear in the helper dump "
            f"(need {MIN_RECALL:.0%})"
        )
    if precision < MIN_PRECISION:
        problems.append(
            f"only {precision:.0%} of the helper's labelled elements appear in the UIAutomator dump "
            f"(need {MIN_PRECISION:.0%}); the helper is showing things that are not on screen"
        )
    for side in (helper, u2):
        side.pop("labels", None)
    return {
        "ok": not problems,
        "problems": problems,
        "helper": helper,
        "uiautomator": u2,
        "match": {
            "uiautomator_labelled": u2["labelled"],
            "uiautomator_in_helper": u2_found,
            "recall": recall,
            "helper_labelled": helper["labelled"],
            "helper_in_uiautomator": helper_found,
            "precision": precision,
            "missing_in_helper": missing_in_helper,
            "extra_in_helper": extra_in_helper,
        },
    }


def compare_backends(serial: str, rounds: int = 3) -> dict[str, Any]:
    """Dump the current screen of ``serial`` with both backends and compare (device required)."""
    from artemis.clients.accessibility_client import AccessibilityClient
    from artemis.clients.ui_automator_client import UIAutomatorClient

    helper = AccessibilityClient(serial)
    u2 = UIAutomatorClient(serial)

    def timed(dump) -> tuple[str, float]:
        times: list[float] = []
        xml = ""
        for _ in range(rounds):
            started = time.perf_counter()
            xml = dump()
            times.append((time.perf_counter() - started) * 1000.0)
        return xml, median(times)

    # The two cannot run at once: UIAutomator2's UiAutomation connection unbinds
    # the helper. Dump with the helper first, then with UIAutomator2, then stop
    # its server so the helper is back for whoever comes next.
    helper_version = None
    try:
        helper.connect()
        helper_version = helper.session.version_name if helper.session else None
        info = helper.get_hierarchy_json()
        width, height = int(info.get("width") or 0), int(info.get("height") or 0)
        if width <= 0 or height <= 0:
            screen = helper.get_screen_data()
            width, height = screen.width, screen.height
        helper_xml, helper_ms = timed(helper.get_hierarchy)
    finally:
        helper.disconnect()

    try:
        u2.connect()
        u2_xml, u2_ms = timed(u2.get_hierarchy)
    finally:
        u2.disconnect(stop_server=True)

    report = compare_dumps(helper_xml, u2_xml, width, height)
    report["serial"] = serial
    report["screen"] = {"width": width, "height": height}
    report["helper"]["median_ms"] = helper_ms
    report["helper"]["version"] = helper_version
    report["uiautomator"]["median_ms"] = u2_ms
    report["xml"] = {"helper": helper_xml, "uiautomator": u2_xml}
    return report


__all__ = ["analyze_xml", "compare_backends", "compare_dumps", "match_labels"]
