# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Convert Xcode's textual accessibility frames and hit points to screen pixels."""

import re
from typing import Any

_NUMBER = r"(-?\d+(?:\.\d+)?)"
_FRAME = re.compile(
    r"\{\{\s*"
    + _NUMBER
    + r",\s*"
    + _NUMBER
    + r"\},\s*\{\s*"
    + _NUMBER
    + r",\s*"
    + _NUMBER
    + r"\}\}"
)
_HIT = re.compile(r"hitPoint:\s*\{\s*" + _NUMBER + r",\s*" + _NUMBER + r"\}")
_BUNDLE = re.compile(r"Application bundle identifier:\s*(\S+)")


def application_bundle(hierarchy: str) -> str | None:
    bundles = set(_BUNDLE.findall(hierarchy))
    return next(iter(bundles)) if len(bundles) == 1 else None


def pixel_element(
    *, text: str, resource_id: str, class_name: str, left: int, top: int, right: int, bottom: int
) -> dict[str, Any]:
    """Element dict in the shared Android-shape format used by both iOS parsers."""
    return {
        "text": text,
        "resource_id": resource_id,
        "class": class_name,
        "bounds": f"[{left},{top}][{right},{bottom}]",
        "parsed_bounds": {"left": left, "top": top, "right": right, "bottom": bottom},
    }


def parse_hierarchy(
    hierarchy: str, width: int, height: int
) -> tuple[list[dict[str, Any]], tuple[float, float]]:
    """Determine scale from the screen window; never assume a Retina factor.

    UIKit hierarchy geometry is in the native interaction coordinate space.
    Artemis observations/actions use full screenshot pixels. Missing or rotated
    geometry is an error rather than silently tapping with an unverified scale.
    """
    lines = hierarchy.splitlines()
    windows = []
    for line in lines:
        match = _FRAME.search(line)
        if match and re.match(r"\s*(?:UI)?Window\b", line):
            x, y, w, h = map(float, match.groups())
            if x == 0 and y == 0 and w > 0 and h > 0:
                windows.append((w, h))
    matching = [(w, h) for w, h in windows if abs(width / w - height / h) < 0.05]
    if windows and not matching:
        raise RuntimeError(
            "Xcode hierarchy has no screen window matching the screenshot. Recapture before interacting."
        )
    # Xcode 27 exports the full screenshot at logical screen dimensions. An
    # inaccessible custom canvas can have no AX Window; visual targeting still
    # works in this documented native coordinate space.
    w, h = max(matching, key=lambda size: size[0] * size[1]) if matching else (width, height)
    scale = width / w, height / h
    elements = []
    for line in lines:
        match = _FRAME.search(line)
        if not match:
            continue
        x, y, w, h = map(float, match.groups())
        if w <= 0 or h <= 0:
            continue
        left, top = round(x * scale[0]), round(y * scale[1])
        right, bottom = round((x + w) * scale[0]), round((y + h) * scale[1])
        if right <= 0 or bottom <= 0 or left >= width or top >= height:
            continue
        label = re.search(r"label:\s*'((?:\\.|[^'])*)'", line)
        quoted = re.search(r'"([^"\n]*)"', line[: match.start()])
        identifier = re.search(r"identifier:\s*'((?:\\.|[^'])*)'", line)
        placeholder = re.search(r"placeholderValue:\s*'((?:\\.|[^'])*)'", line)
        value = re.search(r"value:\s*'((?:\\.|[^'])*)'", line)
        if value:
            value_text = value.group(1)
        else:
            # Xcode elides long values and drops quotes, e.g. `value: Text...`,
            # so read until the next native metadata delimiter verbatim.
            unquoted = re.search(
                r"\bvalue:\s*(.*?)(?=,\s*(?:Keyboard Focused\b|Selected\b|Disabled\b"
                r"|hitPoint:|activationBundleId:|identifier:|label:|placeholderValue:)|$)",
                line,
            )
            value_text = unquoted.group(1).strip() if unquoted else None
        hit = _HIT.search(line)
        activation = re.search(r"activationBundleId:\s*(\S+)", line)
        text = ""
        if label:
            text = label.group(1)
        elif quoted:
            text = quoted.group(1)
        elif value_text:
            text = value_text
        elif placeholder and placeholder.group(1):
            text = placeholder.group(1)
        element = pixel_element(
            text=text,
            resource_id=identifier.group(1) if identifier else "",
            class_name=line.strip().split(",", 1)[0].split(" ", 1)[0],
            left=left,
            top=top,
            right=right,
            bottom=bottom,
        )
        if hit:
            hx, hy = map(float, hit.groups())
            element["hit_point"] = [round(hx * scale[0]), round(hy * scale[1])]
        if activation:
            element["activation_bundle_id"] = activation.group(1)
        if placeholder:
            element["placeholder"] = placeholder.group(1)
        if value_text is not None:
            element["value"] = value_text
        elements.append(element)
    return elements, scale
