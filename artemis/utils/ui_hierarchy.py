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

"""UI hierarchy traversal and element geometry primitives for ARTEMIS.

Provides structured spatial coordinates, bounding box metrics, and recursive
node querying across Android accessibility hierarchies and UIAutomator tree dumps.
"""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class Point(BaseModel):
    """Discrete pixel coordinate on screen."""

    model_config = ConfigDict(frozen=True)

    x: int
    y: int


class ElementBounds(BaseModel):
    """Axis-aligned rectangular boundary defining an on-screen element."""

    model_config = ConfigDict(extra="ignore")

    x: int = Field(description="Horizontal offset of the top-left corner in pixels.")
    y: int = Field(description="Vertical offset of the top-left corner in pixels.")
    width: int = Field(description="Horizontal extent of the bounding box.")
    height: int = Field(description="Vertical extent of the bounding box.")

    def get_center(self) -> Point:
        """Compute the centroid of the element boundary."""
        return Point(x=self.x + (self.width // 2), y=self.y + (self.height // 2))

    def get_relative_point(self, x_percent: float, y_percent: float) -> Point:
        """Compute an internal pixel coordinate by fractional offsets [0.0, 1.0]."""
        clamped_x = max(0.0, min(1.0, float(x_percent)))
        clamped_y = max(0.0, min(1.0, float(y_percent)))
        return Point(
            x=int(self.x + (self.width * clamped_x)),
            y=int(self.y + (self.height * clamped_y)),
        )


def get_bounds_for_element(element: dict[str, Any]) -> ElementBounds | None:
    """Extract and validate the bounding box from a node descriptor."""
    raw_bounds = element.get("bounds")
    if not isinstance(raw_bounds, dict):
        return None

    try:
        return ElementBounds(
            x=int(raw_bounds["x"]),
            y=int(raw_bounds["y"]),
            width=int(raw_bounds["width"]),
            height=int(raw_bounds["height"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.error(f"Failed to validate element bounds ({raw_bounds}): {exc}")
        return None


def text_input_is_empty(text: str | None, hint_text: str | None) -> bool:
    """Determine whether an editable field contains user-entered text."""
    if not text:
        return True
    return text == hint_text


def is_element_focused(element: dict[str, Any]) -> bool:
    """Check if the given node currently holds system focus."""
    focused_val = element.get("focused")
    if isinstance(focused_val, bool):
        return focused_val
    return str(focused_val).strip().lower() == "true"


def get_element_text(element: dict[str, Any], hint_text: bool = False) -> str | None:
    """Retrieve textual label or placeholder from an element node."""
    if hint_text:
        return element.get("hintText")
    return element.get("text")


def find_element_by_resource_id(
    ui_hierarchy: list[dict[str, Any]],
    resource_id: str,
    index: int | None = None,
    is_rich_hierarchy: bool = False,
) -> dict[str, Any] | None:
    """Locate an element in the hierarchy tree matching the specified resource ID.

    Supports both flat attribute mappings and rich accessibility tree hierarchies.
    """
    if not ui_hierarchy or not resource_id:
        return None

    if is_rich_hierarchy:
        return _search_rich_tree(ui_hierarchy, resource_id)

    target_index = max(0, index or 0)
    current_match = 0

    def _walk(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
        nonlocal current_match
        for node in nodes:
            if not isinstance(node, dict):
                continue

            if node.get("resourceId") == resource_id:
                if current_match == target_index:
                    return node
                current_match += 1

            children = node.get("children")
            if isinstance(children, list) and children:
                found = _walk(children)
                if found is not None:
                    return found
        return None

    return _walk(ui_hierarchy)


def _search_rich_tree(
    nodes: list[dict[str, Any]], resource_id: str
) -> dict[str, Any] | None:
    """Recursively traverse a rich XML attribute tree for resource ID match."""
    for node in nodes:
        if not isinstance(node, dict):
            continue

        attributes = node.get("attributes")
        if isinstance(attributes, dict) and attributes.get("resource-id") == resource_id:
            return attributes

        children = node.get("children")
        if isinstance(children, list) and children:
            sub = _search_rich_tree(children, resource_id)
            if sub is not None:
                return sub

    return None
