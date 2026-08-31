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

"""The one place where the historical verb split is resolved.

Canonical tool names are the agent-facing ones (``click``, ``long_press``,
``input_text``, ``manage_app``, ...) because those are what the prompts teach and the
prompts are the contract. The Operator/Validator-internal vocabulary
(``tap``, ``long_press_on``, ``focus_and_input_text``, ``launch_app``/``stop_app``,
``back``) is translated here and nowhere else.

``to_canonical_call`` converts a Validator action item (the dicts Operator writes into
``state.structured_decisions``) into a canonical ``(name, kwargs)`` pair for the
actuator/session layer, using the ``normalized_coordinates`` the Operator already
computed so the canonical surface never needs a pixel escape hatch.
"""

from typing import Any

__all__ = ["OPERATOR_ACTION_TO_CANONICAL", "to_canonical_call"]

#: Operator-internal action verb -> canonical tool name.
OPERATOR_ACTION_TO_CANONICAL: dict[str, str] = {
    "tap": "click",
    "long_press_on": "long_press",
    "swipe": "swipe",
    "focus_and_input_text": "input_text",
    "focus_and_clear_text": "focus_and_clear_text",
    "erase_one_char": "erase_one_char",
    "press_key": "press_key",
    "back": "press_key",
    "launch_app": "manage_app",
    "stop_app": "manage_app",
    "open_link": "open_link",
    "wait_for_delay": "wait_for_delay",
}


def _norm_pair(
    action_item: dict[str, Any], dims: tuple[int, int] | None = None
) -> tuple[int, int] | None:
    """Reads the Operator-computed normalized [nx, ny] off an action item.

    Falls back to converting the pixel ``coordinates`` when the item predates the
    ``normalized_coordinates`` field and screen ``dims`` are provided.
    """
    coords = action_item.get("normalized_coordinates")
    if isinstance(coords, (list, tuple)) and len(coords) >= 2:
        return int(coords[0]), int(coords[1])

    pixel = action_item.get("coordinates")
    if dims and isinstance(pixel, (list, tuple)) and len(pixel) >= 2:
        width, height = dims
        nx = int(max(0, min(1000, round(int(pixel[0]) * 1000 / max(1, width)))))
        ny = int(max(0, min(1000, round(int(pixel[1]) * 1000 / max(1, height)))))
        return nx, ny
    return None


def to_canonical_call(
    action_item: dict[str, Any], dims: tuple[int, int] | None = None
) -> tuple[str, dict[str, Any]]:
    """Translates a Validator action item into a canonical (tool, kwargs) call.

    Args:
        action_item: One entry of the Operator's structured decisions.
        dims: Optional (width, height) for pixel->normalized fallback on legacy items
            lacking ``normalized_coordinates``.

    Raises:
        ValueError: for an unknown action verb or an item missing the coordinates the
            canonical call requires.
    """
    verb = str(action_item.get("action", ""))
    canonical = OPERATOR_ACTION_TO_CANONICAL.get(verb)
    if canonical is None:
        raise ValueError(f"Unsupported action: {verb}")

    if verb == "tap":
        pair = _norm_pair(action_item, dims)
        if pair is None:
            raise ValueError("Invalid coords")
        return "click", {
            "target": [pair[0], pair[1]],
            "times": action_item.get("times") or action_item.get("click_times") or 1,
            "delay_ms": action_item.get("delay_ms") or action_item.get("delay") or 100,
        }

    if verb == "long_press_on":
        pair = _norm_pair(action_item, dims)
        if pair is None:
            raise ValueError("Invalid coords")
        return "long_press", {
            "target": [pair[0], pair[1]],
            "duration_ms": action_item.get("duration", 1000),
        }

    if verb == "swipe":
        coords = action_item.get("normalized_coordinates")
        if not (isinstance(coords, (list, tuple)) and len(coords) == 4):
            pixel = action_item.get("coordinates")
            if dims and isinstance(pixel, (list, tuple)) and len(pixel) == 4:
                width, height = dims
                coords = [
                    int(max(0, min(1000, round(int(pixel[0]) * 1000 / max(1, width))))),
                    int(max(0, min(1000, round(int(pixel[1]) * 1000 / max(1, height))))),
                    int(max(0, min(1000, round(int(pixel[2]) * 1000 / max(1, width))))),
                    int(max(0, min(1000, round(int(pixel[3]) * 1000 / max(1, height))))),
                ]
            else:
                raise ValueError("Invalid coords")
        return "swipe", {
            "start": [int(coords[0]), int(coords[1])],
            "end": [int(coords[2]), int(coords[3])],
            "duration_ms": action_item.get("duration", 400),
        }

    if verb == "focus_and_input_text":
        pair = _norm_pair(action_item, dims)
        if pair is None:
            raise ValueError("Invalid coords")
        return "input_text", {
            "text": action_item.get("text", ""),
            "target": [pair[0], pair[1]],
            "clear_exist": bool(action_item.get("clear_before_input", False)),
        }

    if verb == "focus_and_clear_text":
        pair = _norm_pair(action_item, dims)
        if pair is None:
            raise ValueError("Invalid coords")
        return "focus_and_clear_text", {"target": [pair[0], pair[1]]}

    if verb == "erase_one_char":
        return "erase_one_char", {}

    if verb == "press_key":
        keycode = str(action_item.get("keycode", ""))
        # Operator emits Android-style KEYCODE_* names; the canonical tool takes the
        # bare key word for the common keys. Anything else keeps its original spelling
        # so the driver can forward it verbatim (arbitrary KEYCODE_* / numeric codes).
        bare = keycode.removeprefix("KEYCODE_").lower() if keycode else ""
        known = {"home", "back", "enter", "delete", "tab", "search", "menu", "app_switch"}
        return "press_key", {"key": bare if bare in known else keycode}

    if verb == "back":
        return "press_key", {"key": "back"}

    if verb in ("launch_app", "stop_app"):
        return "manage_app", {
            "action": "launch" if verb == "launch_app" else "stop",
            "app_name": action_item.get("app_name", ""),
        }

    if verb == "open_link":
        return "open_link", {"url": action_item.get("url", "")}

    if verb == "wait_for_delay":
        return "wait_for_delay", {"time_in_ms": action_item.get("time_in_ms", 0)}

    raise ValueError(f"Unsupported action: {verb}")
