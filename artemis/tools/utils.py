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
#
# Portions of this file are derived from mobile-use (https://github.com/minitap-ai/mobile-use)
# Copyright 2025-2026 Minitap, Inc. Licensed under the Apache License 2.0.

"""Spatial positioning, focus resolution, and interaction helpers for ARTEMIS tools.

Coordinates locator resolution across hierarchy trees, validates bounded screen areas,
and routes focus and cursor placement to target elements.
"""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict

from artemis.context import ArtemisContext
from artemis.controllers.types import CoordinatesSelectorRequest, PercentagesSelectorRequest
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.graph.state import State
from artemis.tools.types import Target
from artemis.utils.logger import get_logger
from artemis.utils.ui_hierarchy import (
    ElementBounds,
    find_element_by_resource_id,
    get_bounds_for_element,
    get_element_text,
    is_element_focused,
)

logger = get_logger(__name__)


class IdSelectorRequest(BaseModel):
    """Target selector driven by Android resource ID."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    id: str

    def to_dict(self) -> dict[str, str | int]:
        return {"id": self.id}


class TextSelectorRequest(BaseModel):
    """Target selector driven by exact or case-insensitive element label."""

    model_config = ConfigDict(extra="forbid")
    text: str

    def to_dict(self) -> dict[str, str | int]:
        return {"text": self.text}


class SelectorRequestWithCoordinates(BaseModel):
    """Target selector pointing to an absolute pixel coordinate."""

    model_config = ConfigDict(extra="forbid")
    coordinates: CoordinatesSelectorRequest

    def to_dict(self) -> dict[str, str | int]:
        return {"point": self.coordinates.to_str()}


class SelectorRequestWithPercentages(BaseModel):
    """Target selector pointing to normalized fractional [0.0, 1.0] coordinates."""

    model_config = ConfigDict(extra="forbid")
    percentages: PercentagesSelectorRequest

    def to_dict(self) -> dict[str, str | int]:
        return {"point": self.percentages.to_str()}


class IdWithTextSelectorRequest(BaseModel):
    """Composite selector pairing resource ID with expected text."""

    model_config = ConfigDict(extra="forbid")
    id: str
    text: str

    def to_dict(self) -> dict[str, str | int]:
        return {"id": self.id, "text": self.text}


SelectorRequest = (
    IdSelectorRequest
    | SelectorRequestWithCoordinates
    | SelectorRequestWithPercentages
    | TextSelectorRequest
    | IdWithTextSelectorRequest
)


def _extract_resource_id_and_text_from_selector(
    selector: SelectorRequest,
) -> tuple[str | None, str | None]:
    """Unpack identifier and textual query from any supported SelectorRequest."""
    if isinstance(selector, IdSelectorRequest):
        return selector.id, None
    if isinstance(selector, TextSelectorRequest):
        return None, selector.text
    if isinstance(selector, IdWithTextSelectorRequest):
        return selector.id, selector.text
    return None, None


async def tap(
    ctx: ArtemisContext,
    selector_request: SelectorRequest,
    index: int | None = None,
) -> Any:
    """Dispatch an on-screen tap to an element or coordinate selector."""
    controller = UnifiedMobileController(ctx)
    if isinstance(selector_request, SelectorRequestWithCoordinates):
        res = await controller.tap_at(
            x=selector_request.coordinates.x,
            y=selector_request.coordinates.y,
        )
        return res.error if res.error else None

    if isinstance(selector_request, SelectorRequestWithPercentages):
        coords = selector_request.percentages.to_coords(
            width=ctx.device.device_width,
            height=ctx.device.device_height,
        )
        return await controller.tap_at(coords.x, coords.y)

    res_id, txt = _extract_resource_id_and_text_from_selector(selector_request)
    return await controller.tap_element(
        resource_id=res_id,
        text=txt,
        index=index if index is not None else 0,
    )


def find_element_by_text(
    ui_hierarchy: list[dict[str, Any]], text: str, index: int | None = None
) -> dict[str, Any] | None:
    """Search for elements matching label text across nested hierarchy tree nodes."""
    target_text = (text or "").strip().lower()
    if not target_text:
        return None

    matched_nodes: list[dict[str, Any]] = []

    def _traverse(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            attrs = node.get("attributes", node)
            val = attrs.get("text")
            if isinstance(val, str) and val.strip().lower() == target_text:
                matched_nodes.append(attrs if "attributes" in node else node)

            children = node.get("children")
            if isinstance(children, list) and children:
                _traverse(children)

    _traverse(ui_hierarchy)
    if not matched_nodes:
        return None

    target_idx = max(0, index or 0)
    if target_idx < len(matched_nodes):
        return matched_nodes[target_idx]
    return None


async def tap_bottom_right_of_element(bounds: ElementBounds, ctx: ArtemisContext) -> None:
    """Position pointer near the trailing boundary of an input element."""
    corner_point = bounds.get_relative_point(x_percent=0.99, y_percent=0.99)
    await tap(
        ctx=ctx,
        selector_request=SelectorRequestWithCoordinates(
            coordinates=CoordinatesSelectorRequest(x=corner_point.x, y=corner_point.y)
        ),
    )


async def move_cursor_to_end_if_bounds(
    ctx: ArtemisContext,
    state: State,
    target: Target,
    elt: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Position insertion point at the end of an input element boundary."""
    current_tree = state.latest_ui_hierarchy or []

    if target.resource_id:
        element_node = elt or find_element_by_resource_id(
            ui_hierarchy=current_tree,
            resource_id=target.resource_id,
            index=target.resource_id_index,
        )
        if not element_node:
            return None
        box = get_bounds_for_element(element_node)
        if not box:
            return element_node
        await tap_bottom_right_of_element(bounds=box, ctx=ctx)
        return element_node

    if target.bounds:
        await tap_bottom_right_of_element(bounds=target.bounds, ctx=ctx)
        return elt

    if target.text:
        matched = find_element_by_text(current_tree, target.text, index=target.text_index)
        if matched:
            box = get_bounds_for_element(matched)
            if box:
                await tap_bottom_right_of_element(bounds=box, ctx=ctx)
                return matched
        return None

    return None


async def focus_element_if_needed(
    ctx: ArtemisContext, target: Target
) -> Literal["resource_id", "coordinates", "text", "already_focused"] | None:
    """Ensure element possesses active input focus, falling back through available selectors."""
    if not target.resource_id and not target.bounds and not target.text:
        return "already_focused"

    controller = UnifiedMobileController(ctx)
    hierarchy = await controller.get_ui_elements()

    element_by_id = None
    if target.resource_id:
        element_by_id = find_element_by_resource_id(
            ui_hierarchy=hierarchy,
            resource_id=target.resource_id,
            index=target.resource_id_index,
            is_rich_hierarchy=False,
        )

    if element_by_id and target.text:
        observed_label = get_element_text(element_by_id)
        if not observed_label or target.text.strip().lower() != observed_label.strip().lower():
            logger.warning(
                f"ID '{target.resource_id}' and text '{target.text}' do not align to the same node."
                " Falling back to secondary selectors."
            )
            element_by_id = None

    if element_by_id:
        if not is_element_focused(element_by_id):
            await tap(
                ctx=ctx,
                selector_request=IdSelectorRequest(id=target.resource_id),  # type: ignore[arg-type]
                index=target.resource_id_index,
            )
            hierarchy = await controller.get_ui_elements()
            element_by_id = find_element_by_resource_id(
                ui_hierarchy=hierarchy,
                resource_id=target.resource_id,  # type: ignore[arg-type]
                index=target.resource_id_index,
                is_rich_hierarchy=False,
            )
        if element_by_id and is_element_focused(element_by_id):
            return "resource_id"

    if target.bounds:
        centroid = target.bounds.get_center()
        await tap(
            ctx=ctx,
            selector_request=SelectorRequestWithCoordinates(
                coordinates=CoordinatesSelectorRequest(x=centroid.x, y=centroid.y)
            ),
        )
        return "coordinates"

    if target.text:
        matched_text_node = find_element_by_text(hierarchy, target.text, index=target.text_index)
        if matched_text_node:
            text_box = get_bounds_for_element(matched_text_node)
            if text_box:
                centroid = text_box.get_center()
                await tap(
                    ctx=ctx,
                    selector_request=SelectorRequestWithCoordinates(
                        coordinates=CoordinatesSelectorRequest(x=centroid.x, y=centroid.y)
                    ),
                )
                return "text"

    logger.error("Failed to focus element. No valid locator (resource_id, coordinates, or text) succeeded.")
    return None


def validate_coordinates_bounds(target: Target, screen_width: int, screen_height: int) -> str | None:
    """Verify target centroid resides within active viewport boundaries."""
    if not target.bounds:
        return None
    c = target.bounds.get_center()
    errors: list[str] = []
    if not (0 <= c.x < screen_width):
        errors.append(f"x={c.x} is outside screen width (0-{screen_width})")
    if not (0 <= c.y < screen_height):
        errors.append(f"y={c.y} is outside screen height (0-{screen_height})")
    return "; ".join(errors) if errors else None


def has_valid_selectors(target: Target) -> bool:
    """Verify target defines at least one valid locator criterion."""
    return bool(target.bounds is not None or target.resource_id or target.text)
