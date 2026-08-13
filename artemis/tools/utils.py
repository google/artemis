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

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from artemis.context import ArtemisContext
from artemis.controllers.types import (
    CoordinatesSelectorRequest,
    PercentagesSelectorRequest,
)
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.graph.state import State
from artemis.tools.types import Target
from artemis.utils.logger import get_logger
from artemis.utils.ui_hierarchy import (
    ElementBounds,
    Point,
    find_element_by_resource_id,
    get_bounds_for_element,
    get_element_text,
    is_element_focused,
)

logger = get_logger(__name__)


def find_element_by_text(
    ui_hierarchy: list[dict], text: str, index: int | None = None
) -> dict | None:
    """Find a UI element by its text content (adapted to both flat and rich hierarchy)

    This function performs a recursive, case-insensitive search.

    Args:
        ui_hierarchy: List of UI element dictionaries.
        text: The text content to search for.
        index: Optional index to select nth matching element.

    Returns:
        The complete UI element dictionary if found, None otherwise.
    """
    matches: list[dict] = []

    def search_recursive(elements: list[dict]) -> None:
        for element in elements:
            if isinstance(element, dict):
                src = element.get("attributes", element)
                element_text = src.get("text", "")

                # Guard against non-string values
                if not isinstance(element_text, str):
                    element_text = ""

                text_lower = text.lower()
                if element_text and text_lower == element_text.lower():
                    matches.append(element)

                if children := element.get("children", []):
                    search_recursive(children)

    search_recursive(ui_hierarchy)

    if not matches:
        return None

    if index is None:
        idx = 0
    elif index < 0:
        return None
    else:
        idx = index
    if idx < len(matches):
        return matches[idx]
    return None


async def tap_bottom_right_of_element(bounds: ElementBounds, ctx: ArtemisContext):
    """Tap the bottom-right corner of the specified element bounds."""
    bottom_right: Point = bounds.get_relative_point(x_percent=0.99, y_percent=0.99)
    await tap(
        ctx=ctx,
        selector_request=SelectorRequestWithCoordinates(
            coordinates=CoordinatesSelectorRequest(
                x=bottom_right.x,
                y=bottom_right.y,
            ),
        ),
    )


# pylint: disable=too-many-return-statements
async def move_cursor_to_end_if_bounds(
    ctx: ArtemisContext,
    state: State,
    target: Target,
    elt: dict | None = None,
) -> dict | None:
    """Best-effort move of the text cursor near the end of the input by tapping the

    bottom-right area of the focused element (if bounds are available).
    """
    if target.resource_id:
        if not elt:
            elt = find_element_by_resource_id(
                ui_hierarchy=state.latest_ui_hierarchy or [],
                resource_id=target.resource_id,
                index=target.resource_id_index,
            )
        if not elt:
            return None

        bounds = get_bounds_for_element(elt)
        if not bounds:
            return elt

        logger.debug("Tapping near the end of the input to move the cursor")
        await tap_bottom_right_of_element(bounds=bounds, ctx=ctx)
        logger.debug(f"Tapped end of input {target.resource_id}")
        return elt

    if target.bounds:
        await tap_bottom_right_of_element(target.bounds, ctx=ctx)
        logger.debug("Tapped end of input by coordinates")
        return elt

    if target.text:
        text_elt = find_element_by_text(
            state.latest_ui_hierarchy or [],
            target.text,
            index=target.text_index,
        )
        if text_elt:
            bounds = get_bounds_for_element(text_elt)
            if bounds:
                await tap_bottom_right_of_element(bounds=bounds, ctx=ctx)
                logger.debug(f"Tapped end of input that had text'{target.text}'")
                return text_elt
        return None

    return None


async def focus_element_if_needed(
    ctx: ArtemisContext, target: Target
) -> Literal["resource_id", "coordinates", "text", "already_focused"] | None:
    """Ensures the element is focused, with a sanity check to prevent trusting misleading IDs.

    If no target locator is provided (empty target), assumes the field is
    already focused
    and returns "already_focused" to allow typing without re-focusing.
    """
    # If no locator is provided, assume already focused (e.g., keyboard is visible)
    if not target.resource_id and not target.bounds and not target.text:
        logger.debug("No target locator provided, assuming element is already focused")
        return "already_focused"

    controller = UnifiedMobileController(ctx)
    rich_hierarchy = await controller.get_ui_elements()
    elt_from_id = None
    if target.resource_id:
        elt_from_id = find_element_by_resource_id(
            ui_hierarchy=rich_hierarchy,
            resource_id=target.resource_id,
            index=target.resource_id_index,
            is_rich_hierarchy=False,
        )

    if elt_from_id and target.text:
        text_from_id_elt = get_element_text(elt_from_id)
        if not text_from_id_elt or target.text.lower() != text_from_id_elt.lower():
            logger.warning(
                f"ID '{target.resource_id}' and text '{target.text}' seem to be"
                " on different elements. Ignoring the resource_id and falling"
                " back to other locators."
            )
            elt_from_id = None

    if elt_from_id:
        if not is_element_focused(elt_from_id):
            await tap(
                ctx=ctx,
                selector_request=IdSelectorRequest(id=target.resource_id),  # type: ignore
                index=target.resource_id_index,
            )
            logger.debug(f"Focused (tap) on resource_id={target.resource_id}")
            rich_hierarchy = await controller.get_ui_elements()
            elt_from_id = find_element_by_resource_id(
                ui_hierarchy=rich_hierarchy,
                resource_id=target.resource_id,  # type: ignore
                index=target.resource_id_index,
                is_rich_hierarchy=False,
            )
        if elt_from_id and is_element_focused(elt_from_id):
            logger.debug(f"Text input is focused: {target.resource_id}")
            return "resource_id"
        logger.warning(f"Failed to focus using resource_id='{target.resource_id}'. Fallback...")

    if target.bounds:
        relative_point = target.bounds.get_center()
        await tap(
            ctx=ctx,
            selector_request=SelectorRequestWithCoordinates(
                coordinates=CoordinatesSelectorRequest(x=relative_point.x, y=relative_point.y)
            ),
        )
        logger.debug(f"Tapped on coordinates ({relative_point.x}, {relative_point.y}) to focus.")
        return "coordinates"

    if target.text:
        text_elt = find_element_by_text(rich_hierarchy, target.text, index=target.text_index)
        if text_elt:
            bounds = get_bounds_for_element(text_elt)
            if bounds:
                relative_point = bounds.get_center()
                await tap(
                    ctx=ctx,
                    selector_request=SelectorRequestWithCoordinates(
                        coordinates=CoordinatesSelectorRequest(
                            x=relative_point.x, y=relative_point.y
                        )
                    ),
                )
                logger.debug(f"Tapped on text element '{target.text}' to focus.")
                return "text"

    logger.error(
        "Failed to focus element. No valid locator (resource_id, coordinates, or text) succeeded."
    )
    return None


def validate_coordinates_bounds(
    target: Target, screen_width: int, screen_height: int
) -> str | None:
    """Validate that coordinates are within screen bounds.

    Returns error message if invalid, None if valid.
    """
    if not target.bounds:
        return None

    center = target.bounds.get_center()
    errors = []

    if center.x < 0 or center.x >= screen_width:
        errors.append(f"x={center.x} is outside screen width (0-{screen_width})")
    if center.y < 0 or center.y >= screen_height:
        errors.append(f"y={center.y} is outside screen height (0-{screen_height})")

    return "; ".join(errors) if errors else None


def has_valid_selectors(target: Target) -> bool:
    """Check if target has at least one valid selector."""
    has_coordinates = target.bounds is not None
    has_resource_id = target.resource_id is not None and target.resource_id != ""
    has_text = target.text is not None and target.text != ""
    return has_coordinates or has_resource_id or has_text


class IdSelectorRequest(BaseModel):
    """Selector request using resource id."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    id: str

    def to_dict(self) -> dict[str, str | int]:
        """Convert selector to dictionary."""
        return {"id": self.id}


class TextSelectorRequest(BaseModel):
    """Selector request using element text."""

    model_config = ConfigDict(extra="forbid")
    text: str

    def to_dict(self) -> dict[str, str | int]:
        """Convert selector to dictionary."""
        return {"text": self.text}


class SelectorRequestWithCoordinates(BaseModel):
    """Selector request using absolute coordinates."""

    model_config = ConfigDict(extra="forbid")
    coordinates: CoordinatesSelectorRequest

    def to_dict(self) -> dict[str, str | int]:
        """Convert selector to dictionary."""
        return {"point": self.coordinates.to_str()}


class SelectorRequestWithPercentages(BaseModel):
    """Selector request using percentage coordinates."""

    model_config = ConfigDict(extra="forbid")
    percentages: PercentagesSelectorRequest

    def to_dict(self) -> dict[str, str | int]:
        """Convert selector to dictionary."""
        return {"point": self.percentages.to_str()}


class IdWithTextSelectorRequest(BaseModel):
    """Selector request using both resource id and text."""

    model_config = ConfigDict(extra="forbid")
    id: str
    text: str

    def to_dict(self) -> dict[str, str | int]:
        """Convert selector to dictionary."""
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
    """Extract resource_id and text from a selector."""
    resource_id = None
    text = None

    if isinstance(selector, IdSelectorRequest):
        resource_id = selector.id
    elif isinstance(selector, TextSelectorRequest):
        text = selector.text
    elif isinstance(selector, IdWithTextSelectorRequest):
        resource_id = selector.id
        text = selector.text

    return resource_id, text


async def tap(
    ctx: ArtemisContext,
    selector_request: SelectorRequest,
    index: int | None = None,
):
    """Tap on a selector.

    Index is optional and is used when you have multiple views matching the same
    selector. ui_hierarchy is optional and used for ADB taps to find elements.
    """
    controller = UnifiedMobileController(ctx)
    if isinstance(selector_request, SelectorRequestWithCoordinates):
        result = await controller.tap_at(
            x=selector_request.coordinates.x, y=selector_request.coordinates.y
        )
        return result.error if result.error else None
    if isinstance(selector_request, SelectorRequestWithPercentages):
        coords = selector_request.percentages.to_coords(
            width=ctx.device.device_width,
            height=ctx.device.device_height,
        )
        return await controller.tap_at(coords.x, coords.y)

    # For other selectors, we need the UI hierarchy
    resource_id, text = _extract_resource_id_and_text_from_selector(selector_request)

    return await controller.tap_element(
        resource_id=resource_id,
        text=text,
        index=index if index is not None else 0,
    )
