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

import copy
import json
import re
from typing import Any


def parse_swipe_parameters(
    args: Any,
    default_duration: int | None = 800,
) -> tuple[str | None, str | list[int] | None, int | None]:
    """Parses swipe/drag/scroll parameters from various possible input formats into a standardized tuple:

    (kind, target, duration) where kind is 'direction' or 'coords' or None.
    If 'coords', target is [start_x, start_y, end_x, end_y] in 0-1000 scale.
    If 'direction', target is 'up' | 'down' | 'left' | 'right'.
    """
    if args is None:
        return "direction", "up", default_duration

    # Extract raw dictionary if args is a dict
    if isinstance(args, dict):
        direction = args.get("direction")
        start = args.get("start")
        end = args.get("end")
        coordinates = args.get("coordinates") or args.get("normalized_coordinates")
        action = args.get("action")
        gesture = args.get("gesture")
        duration = (
            args.get("duration")
            or args.get("duration_ms")
            or args.get("time_in_ms")
            or default_duration
        )
    else:
        direction = None
        start = None
        end = None
        coordinates = None
        action = args
        gesture = None
        duration = default_duration

    try:
        duration_int = int(duration) if duration is not None else default_duration
    except (ValueError, TypeError):
        duration_int = default_duration

    def _parse_pt(p: Any) -> list[int] | None:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            try:
                return [int(float(p[0])), int(float(p[1]))]
            except Exception:
                pass
        if isinstance(p, str):
            nums = re.findall(r"-?\d+(?:\.\d+)?", p)
            if len(nums) >= 2:
                return [int(float(nums[0])), int(float(nums[1]))]
        return None

    # 1. Check start and end
    if start is not None and end is not None:
        pt_s = _parse_pt(start)
        pt_e = _parse_pt(end)
        if pt_s and pt_e:
            return "coords", [pt_s[0], pt_s[1], pt_e[0], pt_e[1]], duration_int

    # 2. Check candidate values in priority order
    candidates = [coordinates, action, gesture, direction]
    for c in candidates:
        if c is None:
            continue
        # If list of 4 numbers
        if isinstance(c, (list, tuple)):
            if len(c) == 4:
                try:
                    return "coords", [int(float(x)) for x in c], duration_int
                except Exception:
                    pass
            elif (
                len(c) == 2 and isinstance(c[0], (list, tuple)) and isinstance(c[1], (list, tuple))
            ):
                pt1 = _parse_pt(c[0])
                pt2 = _parse_pt(c[1])
                if pt1 and pt2:
                    return "coords", [pt1[0], pt1[1], pt2[0], pt2[1]], duration_int

        # If string
        if isinstance(c, str):
            s = c.strip()
            # Try JSON parse first
            try:
                parsed = json.loads(s)
                if isinstance(parsed, (list, tuple)):
                    if len(parsed) == 4:
                        return "coords", [int(float(x)) for x in parsed], duration_int
                    elif len(parsed) == 2:
                        pt1 = _parse_pt(parsed[0])
                        pt2 = _parse_pt(parsed[1])
                        if pt1 and pt2:
                            return "coords", [pt1[0], pt1[1], pt2[0], pt2[1]], duration_int
            except Exception:
                pass

            # Regex extract numbers
            nums = re.findall(r"-?\d+(?:\.\d+)?", s)
            if len(nums) == 4:
                return "coords", [int(float(x)) for x in nums], duration_int

            # Check direction string (only if no digits)
            s_lower = s.lower()
            if not any(char.isdigit() for char in s):
                for d in ("up", "down", "left", "right"):
                    if d in s_lower:
                        return "direction", d, duration_int

    return None, None, duration_int


def _extract_bounds(val: Any) -> list[int] | None:
    """Safely parses bounds representation into [left, top, right, bottom]."""
    if val is None:
        return None
    if isinstance(val, (list, tuple)) and len(val) == 4:
        try:
            return [int(float(x)) for x in val]
        except (ValueError, TypeError):
            return None
    if isinstance(val, str):
        match = re.match(r"\[(\-?\d+),(\-?\d+)\]\[(\-?\d+),(\-?\d+)\]", val.strip())
        if match:
            return [int(x) for x in match.groups()]
        nums = re.findall(r"-?\d+", val)
        if len(nums) == 4:
            return [int(x) for x in nums]
    return None


def resolve_scrollable_container_bounds(
    target: Any = None,
    indexed_elements: list[dict[str, Any]] | None = None,
    ui_hierarchy: list[dict[str, Any]] | None = None,
    screen_width: int = 1080,
    screen_height: int = 2400,
) -> list[int]:
    """Resolves the best-effort bounding box [left, top, right, bottom] for scrolling.

    Heuristic Priority:
    1. Explicit target element bounds (if target index/element/bounds passed).
    2. Primary active scrollable container (RecyclerView, ScrollView, ListView, WebView, ViewPager).
    3. Fallback to full screen [0, 0, screen_width, screen_height].
    """
    # 1. Target element reference
    if target is not None:
        direct_b = _extract_bounds(target)
        if direct_b and len(direct_b) == 4:
            return direct_b

        target_idx = None
        if isinstance(target, (int, float)):
            target_idx = int(target)
        elif isinstance(target, str) and target.strip().isdigit():
            target_idx = int(target.strip())

        if target_idx is not None and indexed_elements:
            if 1 <= target_idx <= len(indexed_elements):
                elem = indexed_elements[target_idx - 1]
                b = _extract_bounds(elem.get("bounds"))
                if b and len(b) == 4:
                    return b

    # 2. Look for primary scrollable container in ui_hierarchy or indexed_elements
    candidates: list[list[int]] = []

    def scan_hierarchy(nodes: list[dict[str, Any]]):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            is_scroll = (
                node.get("scrollable") is True or str(node.get("scrollable", "")).lower() == "true"
            )
            cls_name = str(node.get("class") or node.get("className") or "").lower()
            if not is_scroll:
                for kw in ("scrollview", "recyclerview", "listview", "webview", "viewpager"):
                    if kw in cls_name:
                        is_scroll = True
                        break
            if is_scroll:
                b = _extract_bounds(node.get("bounds"))
                if b and len(b) == 4:
                    w = b[2] - b[0]
                    h = b[3] - b[1]
                    if w >= int(screen_width * 0.25) and h >= int(screen_height * 0.15):
                        candidates.append(b)
            children = node.get("children")
            if isinstance(children, list) and children:
                scan_hierarchy(children)

    if ui_hierarchy:
        scan_hierarchy(ui_hierarchy)
    elif indexed_elements:
        for elem in indexed_elements:
            b = _extract_bounds(elem.get("bounds"))
            cls_name = str(elem.get("class") or elem.get("className") or "").lower()
            if any(
                kw in cls_name
                for kw in ("scrollview", "recyclerview", "listview", "webview", "viewpager")
            ):
                if b and len(b) == 4:
                    candidates.append(b)

    if candidates:

        def area(b: list[int]) -> int:
            return max(0, b[2] - b[0]) * max(0, b[3] - b[1])

        candidates.sort(key=area, reverse=True)
        return candidates[0]

    return [0, 0, screen_width, screen_height]


def compute_smart_swipe_coordinates(
    direction: str,
    target: Any = None,
    indexed_elements: list[dict[str, Any]] | None = None,
    ui_hierarchy: list[dict[str, Any]] | None = None,
    width: int = 1080,
    height: int = 2400,
    duration: int | None = None,
) -> tuple[int, int, int, int, int]:
    """Computes best-effort container-aware intelligent swipe coordinates and duration.

    Returns: (start_x, start_y, end_x, end_y, duration_ms)
    """
    container = resolve_scrollable_container_bounds(
        target=target,
        indexed_elements=indexed_elements,
        ui_hierarchy=ui_hierarchy,
        screen_width=width,
        screen_height=height,
    )

    c_left = max(0, min(width - 1, container[0]))
    c_top = max(0, min(height - 1, container[1]))
    c_right = max(c_left + 50, min(width, container[2]))
    c_bottom = max(c_top + 50, min(height, container[3]))

    c_width = c_right - c_left
    c_height = c_bottom - c_top

    dir_str = str(direction or "up").strip().lower()

    if dir_str == "up":
        # Drag upwards -> reveal content below
        sx = ex = int(c_left + c_width * 0.60)
        sy = int(c_top + c_height * 0.70)
        ey = int(c_top + c_height * 0.30)
    elif dir_str == "down":
        # Drag downwards -> reveal content above
        sx = ex = int(c_left + c_width * 0.60)
        sy = int(c_top + c_height * 0.30)
        ey = int(c_top + c_height * 0.70)
    elif dir_str == "left":
        # Drag leftwards -> page right
        sy = ey = int(c_top + c_height * 0.50)
        sx = int(c_left + c_width * 0.75)
        ex = int(c_left + c_width * 0.25)
    elif dir_str == "right":
        # Drag rightwards -> page left
        sy = ey = int(c_top + c_height * 0.50)
        sx = int(c_left + c_width * 0.25)
        ex = int(c_left + c_width * 0.75)
    else:
        # Default up
        sx = ex = int(c_left + c_width * 0.60)
        sy = int(c_top + c_height * 0.70)
        ey = int(c_top + c_height * 0.30)

    # Clamp coordinates to screen bounds
    sx = max(0, min(width - 1, sx))
    ex = max(0, min(width - 1, ex))
    sy = max(0, min(height - 1, sy))
    ey = max(0, min(height - 1, ey))

    if duration is None:
        dist = ((ex - sx) ** 2 + (ey - sy) ** 2) ** 0.5
        if dist <= 300:
            duration_ms = 350
        elif dist <= 600:
            duration_ms = 550
        elif dist <= 1000:
            duration_ms = 800
        else:
            duration_ms = 900
    else:
        duration_ms = int(duration)

    return sx, sy, ex, ey, duration_ms


def normalize_point(x: int, y: int, width: int, height: int) -> list[int]:
    """Convert physical pixels to normalized 0-1000 coordinates."""
    nx = int(round(x * 1000.0 / width))
    ny = int(round(y * 1000.0 / height))
    # Clamp to [0, 1000] to be safe
    return [max(0, min(1000, nx)), max(0, min(1000, ny))]


def denormalize_point(nx: int, ny: int, width: int, height: int) -> list[int]:
    """Convert normalized 0-1000 coordinates to physical pixels."""
    x = int(round(nx * width / 1000.0))
    y = int(round(ny * height / 1000.0))
    # Clamp to device bounds
    return [max(0, min(width - 1, x)), max(0, min(height - 1, y))]


def normalize_action_dict(action: dict[str, Any], width: int, height: int) -> dict[str, Any]:
    """Recursively search and normalize coordinate fields inside an action dictionary."""
    if not isinstance(action, dict):
        return action

    action_copy = copy.deepcopy(action)

    # Normalize 'coordinates' if present and valid
    coords = action_copy.get("coordinates")
    if coords and isinstance(coords, list):
        if len(coords) == 2:
            action_copy["coordinates"] = normalize_point(coords[0], coords[1], width, height)
        elif len(coords) == 4:
            p1 = normalize_point(coords[0], coords[1], width, height)
            p2 = normalize_point(coords[2], coords[3], width, height)
            action_copy["coordinates"] = p1 + p2

    # Normalize 'start_coordinates' and 'end_coordinates' for generic formats
    for key in ["start_coordinates", "end_coordinates", "target"]:
        val = action_copy.get(key)
        if val and isinstance(val, list) and len(val) == 2:
            action_copy[key] = normalize_point(val[0], val[1], width, height)

    return action_copy


def normalize_any_structure(obj: Any, width: int, height: int) -> Any:
    """Recursively searches lists and dicts to normalize any action coordinate schemas."""
    if isinstance(obj, list):
        return [normalize_any_structure(item, width, height) for item in obj]
    elif isinstance(obj, dict):
        # If it looks like a single action item
        if "action" in obj or "name" in obj:
            return normalize_action_dict(obj, width, height)
        # Otherwise recurse down keys
        return {k: normalize_any_structure(v, width, height) for k, v in obj.items()}
    return obj


def normalize_step_actions(step_dict: dict[str, Any]) -> dict[str, Any]:
    """Normalizes step action representation between action_taken and generic_tools,

    and ensures explicit normalized_coordinates (0-1000 scale) are injected for
    UI rendering.
    """
    if not isinstance(step_dict, dict):
        return step_dict

    extra = step_dict.get("extra_metadata") or {}
    width = extra.get("width") or 1080
    height = extra.get("height") or 2400

    def _enrich_action_item(item: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(item, dict):
            return item
        coords = item.get("coordinates")
        act_name = str(item.get("action") or item.get("name") or "").lower()

        if (
            "swipe" in act_name or "scroll" in act_name or "drag" in act_name
        ) and "normalized_coordinates" not in item:
            kind, target, _ = parse_swipe_parameters(item)
            if kind == "coords" and isinstance(target, list) and len(target) == 4:
                item["normalized_coordinates"] = target
                item["normalized_start_coordinates"] = target[:2]
                item["normalized_end_coordinates"] = target[2:]
                if not coords:
                    item["coordinates"] = target
            elif kind == "direction" and isinstance(target, str):
                if target == "up":
                    norm = [600, 700, 600, 300]
                elif target == "down":
                    norm = [600, 300, 600, 700]
                elif target == "left":
                    norm = [750, 500, 250, 500]
                elif target == "right":
                    norm = [250, 500, 750, 500]
                else:
                    norm = None
                if norm:
                    item["normalized_coordinates"] = norm
                    item["normalized_start_coordinates"] = norm[:2]
                    item["normalized_end_coordinates"] = norm[2:]

        coords = item.get("coordinates")
        if coords and isinstance(coords, list) and "normalized_coordinates" not in item:
            # If coordinates look physical (e.g., target_bounds present or > 1000), normalize using width/height
            if item.get("target_bounds") or any(
                isinstance(c, (int, float)) and c > 1000 for c in coords
            ):
                if len(coords) == 2 and all(isinstance(c, (int, float)) for c in coords):
                    item["normalized_coordinates"] = [
                        int(round(coords[0] * 1000.0 / width)),
                        int(round(coords[1] * 1000.0 / height)),
                    ]
                elif len(coords) == 4 and all(isinstance(c, (int, float)) for c in coords):
                    nx1, ny1 = (
                        int(round(coords[0] * 1000.0 / width)),
                        int(round(coords[1] * 1000.0 / height)),
                    )
                    nx2, ny2 = (
                        int(round(coords[2] * 1000.0 / width)),
                        int(round(coords[3] * 1000.0 / height)),
                    )
                    item["normalized_coordinates"] = [nx1, ny1, nx2, ny2]
                    item["normalized_start_coordinates"] = [nx1, ny1]
                    item["normalized_end_coordinates"] = [nx2, ny2]
            else:
                item["normalized_coordinates"] = coords

        seq = item.get("sequence") or item.get("targets")
        if seq:
            if isinstance(seq, str):
                try:
                    seq = json.loads(seq)
                except Exception:
                    try:
                        import ast

                        seq = ast.literal_eval(seq)
                    except Exception:
                        pass
                if isinstance(seq, (list, tuple)):
                    item["sequence"] = list(seq)
            if isinstance(item.get("sequence"), list) and "normalized_sequence" not in item:
                norm_seq = []
                for pt in item["sequence"]:
                    if (
                        isinstance(pt, (list, tuple))
                        and len(pt) == 2
                        and all(isinstance(c, (int, float)) for c in pt)
                    ):
                        if any(c > 1000 for c in pt) or item.get("target_bounds"):
                            norm_seq.append(
                                [
                                    int(round(pt[0] * 1000.0 / width)),
                                    int(round(pt[1] * 1000.0 / height)),
                                ]
                            )
                        else:
                            norm_seq.append([int(round(pt[0])), int(round(pt[1]))])
                    elif isinstance(pt, int):
                        norm_seq.append(pt)
                if norm_seq:
                    item["normalized_sequence"] = norm_seq

        return item

    action_taken = step_dict.get("action_taken")
    if isinstance(action_taken, str):
        try:
            action_taken = json.loads(action_taken)
            step_dict["action_taken"] = action_taken
        except Exception:
            pass

    if isinstance(action_taken, dict):
        step_dict["action_taken"] = _enrich_action_item(action_taken)
    elif isinstance(action_taken, list):
        step_dict["action_taken"] = [
            _enrich_action_item(x) if isinstance(x, dict) else x for x in action_taken
        ]
        action_taken = step_dict["action_taken"]

    generic_tools = step_dict.get("generic_tools")
    if not isinstance(generic_tools, list) or not generic_tools:
        return step_dict

    action_tool_names = {
        "click",
        "click_sequence",
        "long_press",
        "input_text",
        "swipe",
        "press_key",
        "object_detection",
        "manage_app",
        "wait_for_delay",
        "wait_for_text",
        "report_failure_analysis",
        "ask_explorer",
        "run_adb_command",
        "run_short_adb_command",
    }

    has_atomic_action_tool = False
    for tool in generic_tools:
        if not isinstance(tool, dict) or tool.get("type") == "llm_call":
            continue
        raw_name = str(tool.get("name", "")).lower().replace("_exec_", "").lstrip("_")
        if raw_name in action_tool_names:
            has_atomic_action_tool = True
            if isinstance(action_taken, (dict, list)):
                payload = tool.get("payload") or {}
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                args = payload.get("args") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                if not isinstance(args, dict):
                    args = {}

                first_act = (
                    action_taken[0]
                    if isinstance(action_taken, list)
                    and action_taken
                    and isinstance(action_taken[0], dict)
                    else (action_taken if isinstance(action_taken, dict) else {})
                )

                is_seq_tool = (
                    raw_name in ("click_sequence", "tap_sequence")
                    or "sequence" in args
                    or "targets" in args
                )

                # Enrich missing coordinates/target/input from action_taken only if not a multi-point sequence action
                if not is_seq_tool:
                    if "target" not in args and "coordinates" not in args:
                        if first_act.get("coordinates"):
                            args["coordinates"] = first_act["coordinates"]
                        if first_act.get("args", {}).get("target"):
                            args["target"] = first_act["args"]["target"]
                    if "normalized_coordinates" not in args and first_act.get(
                        "normalized_coordinates"
                    ):
                        args["normalized_coordinates"] = first_act["normalized_coordinates"]
                    if "text" not in args and first_act.get("text"):
                        args["text"] = first_act["text"]

                args = _enrich_action_item(args)
                payload["args"] = args
                tool["payload"] = payload

    return step_dict
