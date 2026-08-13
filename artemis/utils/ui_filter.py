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

import re
from typing import Any

from artemis.utils.logger import get_logger

logger = get_logger(__name__)


def _parse_bounds(bounds: Any) -> dict[str, int] | None:
    """Normalize bounds from different formats to a standard dictionary.

    Supports: 1. String format: "[x1,y1][x2,y2]" 2. Dictionary format: {"x":
    int, "y": int, "width": int, "height": int} 3. Dictionary format: {"left":
    int, "top": int, "right": int, "bottom": int} 4. Cached format:
    "parsed_bounds" inside element dict
    """
    if not bounds:
        return None

    if isinstance(bounds, dict):
        if "parsed_bounds" in bounds and isinstance(bounds["parsed_bounds"], dict):
            return bounds["parsed_bounds"]
        if "left" in bounds and "top" in bounds:
            return {
                "left": int(bounds.get("left", 0)),
                "top": int(bounds.get("top", 0)),
                "right": int(bounds.get("right", 0)),
                "bottom": int(bounds.get("bottom", 0)),
            }
        if "x" in bounds and "y" in bounds:
            x = int(bounds.get("x", 0))
            y = int(bounds.get("y", 0))
            width = int(bounds.get("width", 0))
            height = int(bounds.get("height", 0))
            return {
                "left": x,
                "top": y,
                "right": x + width,
                "bottom": y + height,
            }
        if "bounds" in bounds and isinstance(bounds["bounds"], str):
            res = _parse_bounds(bounds["bounds"])
            if res:
                bounds["parsed_bounds"] = res
            return res

    if isinstance(bounds, str):
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if match:
            x1, y1, x2, y2 = map(int, match.groups())
            return {"left": x1, "top": y1, "right": x2, "bottom": y2}
        return None
    return None


def _intersects_screen(bounds: dict[str, int], screen_width: int, screen_height: int) -> bool:
    """Check if element intersects screen bounds."""
    left = bounds.get("left", 0)
    top = bounds.get("top", 0)
    right = bounds.get("right", 0)
    bottom = bounds.get("bottom", 0)
    return not (right <= 0 or bottom <= 0 or left >= screen_width or top >= screen_height)


def _meets_min_size(bounds: dict[str, int], min_size: int) -> bool:
    """Check if element meets minimum size requirements."""
    width = bounds.get("right", 0) - bounds.get("left", 0)
    height = bounds.get("bottom", 0) - bounds.get("top", 0)
    return width > min_size and height > min_size


def _clip_bounds(
    node: dict[str, Any],
    bounds_data: dict[str, int],
    ancestor_bounds: dict[str, int],
) -> dict[str, int]:
    """Clips bounds to ancestor bounds and updates the node inplace."""
    left = max(bounds_data["left"], ancestor_bounds.get("left", 0))
    top = max(bounds_data["top"], ancestor_bounds.get("top", 0))
    right = min(bounds_data["right"], ancestor_bounds.get("right", 0))
    bottom = min(bounds_data["bottom"], ancestor_bounds.get("bottom", 0))

    is_fully_clipped = False
    if left >= right or top >= bottom:
        is_fully_clipped = True
        # Clamp to a point on the edge of ancestor bounds
        left = right = max(
            ancestor_bounds.get("left", 0),
            min(bounds_data["left"], ancestor_bounds.get("right", 0)),
        )
        top = bottom = max(
            ancestor_bounds.get("top", 0),
            min(bounds_data["top"], ancestor_bounds.get("bottom", 0)),
        )

    new_bounds = {"left": left, "top": top, "right": right, "bottom": bottom}

    if is_fully_clipped:
        node["is_clipped"] = True

    # Re-serialize bounds into the node matching original format
    orig = node.get("bounds")
    if isinstance(orig, str):
        node["bounds"] = f"[{left},{top}][{right},{bottom}]"
    elif isinstance(orig, dict):
        if "left" in orig and "top" in orig:
            node["bounds"] = new_bounds.copy()
        elif "x" in orig and "y" in orig:
            node["bounds"] = {
                "x": left,
                "y": top,
                "width": right - left,
                "height": bottom - top,
            }
    return new_bounds


def _is_semantic_empty(node: dict[str, Any]) -> bool:
    """Check if a node has no semantic value or interactive capability."""
    # Check text
    text = node.get("text")
    if text and str(text).strip():
        return False

    # Check content description
    content_desc = (
        node.get("content-desc")
        or node.get("contentDescription")
        or node.get("content-description")
    )
    if content_desc and str(content_desc).strip():
        return False

    # Check clickable
    clickable = node.get("clickable")
    if clickable is True or str(clickable).lower() == "true":
        return False

    # Check focusable
    focusable = node.get("focusable")
    if focusable is True or str(focusable).lower() == "true":
        return False

    # Check if it's a valid image container
    class_name = str(node.get("className") or node.get("class") or "").lower()
    if "image" in class_name or "icon" in class_name or "photo" in class_name:
        return False

    return True


def _merge_complementary_children(children: list[dict], parent_node: dict) -> list[dict]:
    """Strategy 2: Merge complementary siblings under a clickable parent."""
    # Rule 1: Parent must be clickable or focusable
    clickable = parent_node.get("clickable")
    focusable = parent_node.get("focusable")
    if not (
        clickable is True
        or str(clickable).lower() == "true"
        or focusable is True
        or str(focusable).lower() == "true"
    ):
        return children

    # Classify children
    textual_children = []
    decoration_children = []

    for child in children:
        # Rule 3: No child is a form control
        class_name = str(child.get("className") or child.get("class") or "").lower()
        if any(kw in class_name for kw in ["checkbox", "switch", "radio", "edittext"]):
            return children

        text = child.get("text")
        content_desc = (
            child.get("content-desc")
            or child.get("contentDescription")
            or child.get("content-description")
        )

        if (text and str(text).strip()) or (content_desc and str(content_desc).strip()):
            textual_children.append(child)
        else:
            # Check if it's a decoration (not clickable, not focusable)
            c_clickable = child.get("clickable")
            c_focusable = child.get("focusable")
            if not (
                c_clickable is True
                or str(c_clickable).lower() == "true"
                or c_focusable is True
                or str(c_focusable).lower() == "true"
            ):
                decoration_children.append(child)
            else:
                # It's an interactive element without text, don't merge as decoration
                return children

    # Rule 2: Exactly ONE textual child and at least one decoration child
    if (
        len(textual_children) == 1
        and len(decoration_children) >= 1
        and len(children) == len(textual_children) + len(decoration_children)
    ):
        # Perform merge
        txt_child = textual_children[0]

        # Compute Union Bounds
        all_bounds = []
        for c in children:
            b = _parse_bounds(c.get("bounds"))
            if b:
                all_bounds.append(b)

        if not all_bounds:
            return children

        left = min(b["left"] for b in all_bounds)
        top = min(b["top"] for b in all_bounds)
        right = max(b["right"] for b in all_bounds)
        bottom = max(b["bottom"] for b in all_bounds)

        merged_node = txt_child.copy()

        # Update bounds matching original format if possible, or just dict
        orig = txt_child.get("bounds")
        if isinstance(orig, str):
            merged_node["bounds"] = f"[{left},{top}][{right},{bottom}]"
        else:
            merged_node["bounds"] = {
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
            }

        # Concatenate text and content-desc
        desc_parts = []
        for c in children:
            desc = (
                c.get("content-desc") or c.get("contentDescription") or c.get("content-description")
            )
            if desc and str(desc).strip():
                desc_parts.append(f"[{str(desc).strip()}]")
            elif "image" in str(c.get("className") or c.get("class") or "").lower():
                # User tip: use [Icon] for images without desc
                desc_parts.append("[Icon]")

        text_parts = []
        for c in children:
            txt = c.get("text")
            if txt and str(txt).strip():
                text_parts.append(str(txt).strip())

        if desc_parts:
            merged_node["content-desc"] = " ".join(desc_parts)
        if text_parts:
            merged_node["text"] = " ".join(text_parts)

        return [merged_node]

    return children


def _prune_parent_child_redundancy(
    parent: dict[str, Any], children: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Strategy 3: Remove redundant children that have the same text as the parent."""
    parent_text = str(parent.get("text") or "").strip()
    parent_desc = str(
        parent.get("content-desc")
        or parent.get("contentDescription")
        or parent.get("content-description")
        or ""
    ).strip()

    if not parent_text and not parent_desc:
        return children

    new_children = []
    for child in children:
        child_text = str(child.get("text") or "").strip()
        child_desc = str(
            child.get("content-desc")
            or child.get("contentDescription")
            or child.get("content-description")
            or ""
        ).strip()

        text_matches = False
        if parent_text and child_text and parent_text == child_text:
            text_matches = True
        elif parent_desc and child_text and parent_desc == child_text:
            text_matches = True
        elif parent_text and child_desc and parent_text == child_desc:
            text_matches = True
        elif parent_desc and child_desc and parent_desc == child_desc:
            text_matches = True

        if text_matches:
            class_name = str(child.get("className") or child.get("class") or "").lower()
            is_form_control = any(
                kw in class_name for kw in ["checkbox", "switch", "radio", "edittext"]
            )
            has_children = bool(child.get("children"))

            clickable = child.get("clickable")
            focusable = child.get("focusable")
            is_interactive = (
                clickable is True
                or str(clickable).lower() == "true"
                or focusable is True
                or str(focusable).lower() == "true"
            )

            if is_form_control or has_children or is_interactive:
                # Keep but erase text
                merged_node = child.copy()
                if "text" in merged_node:
                    merged_node["text"] = ""
                if "content-desc" in merged_node:
                    merged_node["content-desc"] = ""
                if "contentDescription" in merged_node:
                    merged_node["contentDescription"] = ""
                if "content-description" in merged_node:
                    merged_node["content-description"] = ""
                new_children.append(merged_node)
            else:
                # Remove child
                continue
        else:
            new_children.append(child)

    return new_children


def _filter_and_prune(
    node: dict[str, Any],
    screen_width: int,
    screen_height: int,
    min_element_size: int = 5,
    use_dynamic_min_size: bool = True,
    clip_bounds: bool = True,
    semantic_pruning: str = "hoist",
    ancestor_bounds: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Recursively filters, clips bounds, and applies semantic pruning strategies."""
    if ancestor_bounds is None:
        ancestor_bounds = {
            "left": 0,
            "top": 0,
            "right": screen_width,
            "bottom": screen_height,
        }

    bounds_data = _parse_bounds(node.get("bounds"))

    if bounds_data:
        # We no longer check _intersects_screen to return [] immediately,
        # to ensure zero info loss. Out-of-bounds elements will be clipped to a point.

        if clip_bounds:
            bounds_data = _clip_bounds(node, bounds_data, ancestor_bounds)

        # Dynamic min size logic: use relative percentage (0.5% of short side) if enabled
        actual_min_size = min_element_size
        if use_dynamic_min_size:
            short_side = min(screen_width, screen_height)
            dynamic_min_size = int(short_side * 0.005)
            actual_min_size = max(min_element_size, dynamic_min_size)

        # If it's fully clipped, we skip min size check to preserve it
        if not node.get("is_clipped", False) and not _meets_min_size(bounds_data, actual_min_size):
            return []

    # Recursively process children
    filtered_node = node.copy()
    filtered_children = []
    if "children" in node:
        for child in node.get("children", []):
            if isinstance(child, dict):
                filtered_children.extend(
                    _filter_and_prune(
                        child,
                        screen_width,
                        screen_height,
                        min_element_size,
                        use_dynamic_min_size,
                        clip_bounds,
                        semantic_pruning,
                        ancestor_bounds=bounds_data if bounds_data else ancestor_bounds,
                    )
                )

        # Apply Strategy 3: Parent-Child Redundancy Removal
        if filtered_children:
            filtered_children = _prune_parent_child_redundancy(filtered_node, filtered_children)

        # Apply Strategy 2: Sibling Merging
        if filtered_children:
            filtered_children = _merge_complementary_children(filtered_children, filtered_node)

        filtered_node["children"] = filtered_children

    # Apply semantic pruning strategies
    if _is_semantic_empty(filtered_node):
        if semantic_pruning == "safe":
            # Only eliminate if it's a leaf node
            if not filtered_node.get("children"):
                return []
        elif semantic_pruning == "hoist":
            # Skip this node and hoist its valid children up
            return filtered_node.get("children", [])

    return [filtered_node]


def _detect_fixed_system_bars(
    ui_hierarchy: list[dict[str, Any]], screen_width: int, screen_height: int
) -> list[dict[str, int]]:
    """Detect fixed, non-scrollable system or navigation bars anchored to the extreme top/bottom edges.

    Enforces strict triple guardrails to guarantee near-zero false positives:

      1. Edge & Width Guarantee: Must anchor to top (top==0) or bottom
      (bottom==screen_height) and span >= 95% of screen width.
      2. Non-Scrollable Root Branch: The candidate node and all its
      descendants/ancestors must NOT be scrollable.
      3. Identity & Height Bounds: High-confidence system package/IDs OR strict
      navigation bar height bounds.
    """
    fixed_bars = []

    def check_scrollable(node: dict[str, Any]) -> bool:
        if node.get("scrollable") is True or str(node.get("scrollable", "")).lower() == "true":
            return True
        for child in node.get("children", []):
            if isinstance(child, dict) and check_scrollable(child):
                return True
        return False

    def scan_for_bars(node: dict[str, Any], has_scrollable_ancestor: bool):
        is_curr_scrollable = has_scrollable_ancestor or (
            node.get("scrollable") is True or str(node.get("scrollable", "")).lower() == "true"
        )

        bounds = _parse_bounds(node.get("bounds"))
        if bounds and not is_curr_scrollable and not check_scrollable(node):
            width = bounds["right"] - bounds["left"]
            height = bounds["bottom"] - bounds["top"]
            package_name = str(node.get("package") or node.get("packageName") or "").lower()
            res_id = str(node.get("resource-id") or node.get("resourceId") or "").lower()
            class_name = str(node.get("className") or node.get("class") or "").lower()

            is_full_width = width >= int(screen_width * 0.95)
            is_high_conf_id = (
                any(
                    kw in res_id or kw in class_name
                    for kw in [
                        "bottom_navigation",
                        "navigation_bar",
                        "bottom_bar",
                        "tab_layout",
                        "action_bar",
                    ]
                )
                or package_name == "com.android.systemui"
            )

            # Check bottom fixed bar (e.g., bottom navigation bar / system navigation bar)
            if bounds["bottom"] >= screen_height and is_full_width and height > 0:
                if is_high_conf_id or (25 <= height <= int(screen_height * 0.15)):
                    fixed_bars.append(bounds.copy())
                    return  # Don't recurse into children of confirmed fixed bars

            # Check top fixed bar (e.g., status bar / top action bar)
            if bounds["top"] <= 0 and is_full_width and height > 0:
                if package_name == "com.android.systemui" or (
                    15 <= height <= int(screen_height * 0.12)
                ):
                    fixed_bars.append(bounds.copy())
                    return

        for child in node.get("children", []):
            if isinstance(child, dict):
                scan_for_bars(child, is_curr_scrollable)

    for root_node in ui_hierarchy:
        if isinstance(root_node, dict):
            scan_for_bars(root_node, False)

    return fixed_bars


def _clamp_by_fixed_bars(
    node: dict[str, Any], fixed_bars: list[dict[str, int]], min_size: int
) -> bool:
    """Non-destructively clamp the vertical bounds of a node if it overlaps with confirmed fixed system bars.

    Returns False if the remaining visible height is smaller than min_size
    (silent discard of thin slivers).
    """
    bounds = _parse_bounds(node.get("bounds"))
    if not bounds or not fixed_bars:
        return True

    left, top, right, bottom = (
        bounds["left"],
        bounds["top"],
        bounds["right"],
        bounds["bottom"],
    )

    for bar in fixed_bars:
        # Check horizontal overlap
        if right <= bar["left"] or left >= bar["right"]:
            continue

        # If it's the fixed bar itself or fully inside the bar, keep it if it belongs to the bar
        if (
            left == bar["left"]
            and top == bar["top"]
            and right == bar["right"]
            and bottom == bar["bottom"]
        ):
            return True

        # Clamp against a bottom fixed bar
        if bar["bottom"] >= bottom and top < bar["top"] < bottom:
            bottom = min(bottom, bar["top"])

        # Clamp against a top fixed bar
        if bar["top"] <= top and top < bar["bottom"] < bottom:
            top = max(top, bar["bottom"])

    # If remaining height is too small, silently discard this occluded sliver
    if bottom - top <= max(min_size, 20):
        return False

    new_bounds = {"left": left, "top": top, "right": right, "bottom": bottom}
    orig = node.get("bounds")
    if isinstance(orig, str):
        node["bounds"] = f"[{left},{top}][{right},{bottom}]"
    elif isinstance(orig, dict):
        if "left" in orig and "top" in orig:
            node["bounds"] = new_bounds.copy()
        elif "x" in orig and "y" in orig:
            node["bounds"] = {
                "x": left,
                "y": top,
                "width": right - left,
                "height": bottom - top,
            }
    return True


def filter_node(
    node: dict[str, Any],
    screen_width: int,
    screen_height: int,
    min_element_size: int = 5,
    use_dynamic_min_size: bool = True,
    clip_bounds: bool = True,
    semantic_pruning: str = "hoist",
) -> dict[str, Any] | None:
    """Recursively filter a UI node and its children based on screen bounds, size, and semantics.

    Returns None if the node does not meet the criteria.
    """
    res_list = _filter_and_prune(
        node,
        screen_width,
        screen_height,
        min_element_size,
        use_dynamic_min_size,
        clip_bounds,
        semantic_pruning,
    )
    if not res_list:
        return None

    if len(res_list) == 1:
        return res_list[0]

    # If the root node itself was hoisted resulting in multiple children, wrap or attach
    new_node = node.copy()
    new_node["children"] = res_list
    return new_node


def filter_ui_hierarchy(
    ui_hierarchy: list[dict[str, Any]],
    screen_width: int = 1080,
    screen_height: int = 2400,
    min_element_size: int = 5,
    use_dynamic_min_size: bool = True,
    clip_bounds: bool = True,
    semantic_pruning: str = "hoist",
) -> list[dict[str, Any]]:
    """Pure function to filter an entire list of UI elements (either flat or nested).

    Removes elements outside screen bounds, smaller than minimum size, or
    semantically empty. Applies non-destructive safe-bounds clamping against
    confirmed fixed system/navigation bars.
    """
    # 1. Detect high-confidence fixed system bars before flattening
    fixed_bars = _detect_fixed_system_bars(ui_hierarchy, screen_width, screen_height)

    filtered_elements = []
    for element in ui_hierarchy:
        if isinstance(element, dict):
            filtered_elements.extend(
                _filter_and_prune(
                    element,
                    screen_width=screen_width,
                    screen_height=screen_height,
                    min_element_size=min_element_size,
                    use_dynamic_min_size=use_dynamic_min_size,
                    clip_bounds=clip_bounds,
                    semantic_pruning=semantic_pruning,
                )
            )

    # 2. Apply safe-bounds clamping against fixed system bars on final candidate elements
    if fixed_bars:
        clamped_elements = []
        for el in filtered_elements:
            if _clamp_by_fixed_bars(el, fixed_bars, min_element_size):
                clamped_elements.append(el)
        return clamped_elements

    return filtered_elements
