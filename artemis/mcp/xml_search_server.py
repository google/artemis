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

import os
from pathlib import Path
import re
import sys

# Ensure repository root is in sys.path when executed directly or via MCP runner
_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# pylint: disable=wrong-import-position
from mcp.server.fastmcp import FastMCP

from artemis.data_engine.storage import StorageManager

mcp = FastMCP("Android_XML_Fuzzy_Search")


def get_subtree_info(element):
    """Calculate total nodes and max depth of a subtree."""
    total_nodes = sum(1 for _ in element.iter()) - 1
    if total_nodes <= 0:
        return 0, 0

    def get_depth(el):
        children = list(el)
        if not children:
            return 0
        return 1 + max(get_depth(c) for c in children)

    return total_nodes, get_depth(element)


def serialize_node(node, max_depth=1):
    """Serialize an XML node to string with depth limit and truncation info."""

    def _serialize(el, current_depth):
        # Reconstruct attributes string
        attrs = " ".join([f'{k}="{v}"' for k, v in el.attrib.items()])
        tag = el.tag
        res = f"<{tag} {attrs}" if attrs else f"<{tag}"

        text = el.text.strip() if el.text else ""
        children = list(el)

        if current_depth >= max_depth and children:
            res += ">"
            if text:
                res += text
            total_nodes, subtree_depth = get_subtree_info(el)
            res += (
                f"\n<!-- [Truncated] {total_nodes} nodes across {subtree_depth}"
                " levels hidden. -->\n"
            )
            res += f"</{tag}>"
        elif children or text:
            res += ">"
            if text:
                res += text
            for child in children:
                res += "\n" + _serialize(child, current_depth + 1)
            res += f"\n</{tag}>"
        else:
            res += " />"
        return res

    return _serialize(node, 0)


@mcp.tool()
def search_ui(image_hash: str, query: str, threshold: float = 0.6) -> dict:
    """Search for text in UI data (XML and OCR) retrieved from Data Engine by image hash.

    Args:
        image_hash: The SHA-256 hash of the image.
        query: The text to search for.
        threshold: The similarity threshold (0.0 to 1.0). Defaults to 0.6.

    Returns:
        A dictionary containing raw matches with bounds.
    """
    db_path = os.getenv("DATA_ENGINE_DB_PATH")
    if not db_path:
        raise ValueError("DATA_ENGINE_DB_PATH environment variable is not set.")
    base_dir = Path(db_path).parent

    try:
        storage = StorageManager(db_path, base_dir)
    except Exception as e:
        return {
            "error": f"Error initializing StorageManager: {e}",
            "matches": [],
        }

    matches = storage.search_ui_by_hash(image_hash, query, threshold)

    if not matches:
        return {"matches": []}

    top_matches = matches[:8]

    results = []
    for m in top_matches:
        # Extract bounds
        box = None
        if m["type"] == "xml":
            bounds_str = m["node"].get("bounds")
            if bounds_str:
                box = parse_bounds(bounds_str)
        elif m["type"] == "ocr":
            position = m["node"].get("position", [])
            if position:
                left = min(v.get("x", 0) for v in position)
                top = min(v.get("y", 0) for v in position)
                right = max(v.get("x", 0) for v in position)
                bottom = max(v.get("y", 0) for v in position)
                box = (left, top, right, bottom)

        results.append(
            {
                "matched_text": m["matched_text"],
                "type": m["type"],
                "bounds": list(box) if box else None,
            }
        )

    return {"matches": results}


def parse_bounds(bounds_str):
    """Parse bounds string like '[left,top][right,bottom]'."""
    if not bounds_str:
        return None
    match = re.match(r"\[(\-?\d+),(\-?\d+)\]\[(\-?\d+),(\-?\d+)\]", bounds_str)
    if match:
        return tuple(map(int, match.groups()))
    return None


@mcp.tool()
def search_by_coordinates(image_hash: str, x: int, y: int) -> str:
    """Search for elements in UI data (XML and OCR) retrieved from Data Engine by image hash that overlap with the given coordinates.

    Args:
        image_hash: The SHA-256 hash of the image.
        x: The X coordinate.
        y: The Y coordinate.

    Returns:
        A string containing the matching nodes and OCR results.
    """
    db_path = os.getenv("DATA_ENGINE_DB_PATH")
    if not db_path:
        raise ValueError("DATA_ENGINE_DB_PATH environment variable is not set.")
    base_dir = Path(db_path).parent

    try:
        storage = StorageManager(db_path, base_dir)
    except Exception as e:
        return f"Error initializing StorageManager: {e}"

    record = storage.get_image(image_hash)
    if not record:
        return "No record found for image hash."

    raw_xml_matches = []
    if record.ui_tree:
        for node in record.ui_tree:
            bounds_str = node.get("bounds")
            if bounds_str:
                bounds = parse_bounds(bounds_str)
                if bounds:
                    left, top, right, bottom = bounds
                    if left <= x <= right and top <= y <= bottom:
                        raw_xml_matches.append(node)

    # Sort XML matches by area (smallest first) to prioritize "deepest/bottom" layers
    def get_area(node):
        bounds_str = node.get("bounds")
        if bounds_str:
            bounds = parse_bounds(bounds_str)
            if bounds:
                left, top, right, bottom = bounds
                return (right - left) * (bottom - top)
        return float("inf")

    raw_xml_matches.sort(key=get_area)

    # Collect bottom layers up to the first interactable node (max 2 nodes)
    interaction_keys = {
        "clickable",
        "scrollable",
        "long-clickable",
        "checkable",
        "focusable",
        "editable",
        "selected",
    }
    xml_matches = []
    for node in raw_xml_matches:
        has_text = bool(node.get("text")) or bool(node.get("content-desc"))
        is_interactable = any(node.get(k) == "true" for k in interaction_keys)

        # Skip completely empty/useless middle wrappers
        if not has_text and not is_interactable:
            continue

        xml_matches.append(node)

        # Stop collecting if we hit an interactable node, or we reached the max of 2 nodes
        if is_interactable or len(xml_matches) >= 2:
            break

    ocr_matches = []
    if record.ocr_result:
        for ocr in record.ocr_result:
            position = ocr.get("position", [])
            if position:
                left = min(v.get("x", 0) for v in position)
                top = min(v.get("y", 0) for v in position)
                right = max(v.get("x", 0) for v in position)
                bottom = max(v.get("y", 0) for v in position)

                if left <= x <= right and top <= y <= bottom:
                    ocr_matches.append(ocr)

    if not xml_matches and not ocr_matches:
        return f"No elements found at coordinates ({x}, {y})."

    output = []
    output.append(f"Found matches overlapping with ({x}, {y}):")

    if xml_matches:
        for i, node in enumerate(xml_matches, 1):
            allowed_keys = {
                "class",
                "text",
                "resource-id",
                "content-desc",
                "clickable",
                "enabled",
                "checked",
                "focused",
                "scrollable",
            }
            # Drop empty strings and None, but keep explicit "false" or "0"
            node_copy = {k: v for k, v in node.items() if k in allowed_keys and v not in ("", None)}

            # Extract class to put it first, then format the rest
            cls_name = node_copy.pop("class", "UnknownClass")
            props = [f"{k}: {v}" for k, v in node_copy.items()]

            line = f"Node {i}: {cls_name}"
            if props:
                line += " | " + " | ".join(props)
            output.append(line)

    if ocr_matches:
        for i, ocr in enumerate(ocr_matches[:5], 1):  # Limit to top 5
            output.append(f"OCR {i}: Text: {ocr.get('text')}")

    return " ".join(output)


if __name__ == "__main__":
    mcp.run()
