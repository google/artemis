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

"""Unit tests for ARTEMIS tool utility functions and element interaction helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
from artemis.controllers.types import CoordinatesSelectorRequest
from artemis.tools.types import Target
from artemis.tools.utils import (
    IdSelectorRequest,
    IdWithTextSelectorRequest,
    SelectorRequestWithCoordinates,
    SelectorRequestWithPercentages,
    TextSelectorRequest,
    _extract_resource_id_and_text_from_selector,
    find_element_by_text,
    focus_element_if_needed,
    has_valid_selectors,
    move_cursor_to_end_if_bounds,
    tap_bottom_right_of_element,
    validate_coordinates_bounds,
)
from artemis.utils.ui_hierarchy import ElementBounds


@pytest.fixture
def dummy_context() -> MagicMock:
    """Fixture providing an ARTEMIS runtime context configured for Android."""
    ctx = MagicMock(spec=ArtemisContext)
    ctx.device = MagicMock(spec=DeviceContext)
    ctx.device.mobile_platform = DevicePlatform.ANDROID
    ctx.device.device_id = "test-device-pixel8"
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    return ctx


@pytest.fixture
def mock_graph_state() -> MagicMock:
    """Fixture providing a mock LangGraph state container."""
    state = MagicMock()
    state.latest_ui_hierarchy = []
    return state


@pytest.fixture
def sample_hierarchy_node() -> dict:
    """Fixture providing a standard flat hierarchy node."""
    return {
        "resourceId": "com.google.android.apps:id/search_query",
        "text": "Search Google",
        "bounds": {"x": 120, "y": 250, "width": 400, "height": 80},
        "focused": "false",
    }


@pytest.fixture
def sample_nested_node() -> dict:
    """Fixture providing a rich nested hierarchy node."""
    return {
        "attributes": {
            "resource-id": "com.google.android.apps:id/search_query",
            "text": "Search Google",
            "bounds": {"x": 120, "y": 250, "width": 400, "height": 80},
            "focused": "false",
        },
        "children": [],
    }


# ==============================================================================
# Cursor Placement & Boundary Alignment Tests
# ==============================================================================


class TestCursorAlignment:
    """Validates precise cursor placement against interactive targets."""

    @pytest.mark.asyncio
    @patch("artemis.tools.utils.tap", new_callable=AsyncMock)
    @patch("artemis.tools.utils.find_element_by_resource_id")
    async def test_cursor_routes_to_end_via_resource_id(
        self,
        mock_find_elem: MagicMock,
        mock_tap_fn: AsyncMock,
        dummy_context: MagicMock,
        mock_graph_state: MagicMock,
        sample_hierarchy_node: dict,
    ):
        """Resource ID locator should calculate end-boundary tap (99% width/height offset)."""
        mock_graph_state.latest_ui_hierarchy = [sample_hierarchy_node]
        mock_find_elem.return_value = sample_hierarchy_node

        target = Target(resource_id="com.google.android.apps:id/search_query")
        elem = await move_cursor_to_end_if_bounds(
            ctx=dummy_context, state=mock_graph_state, target=target
        )

        mock_find_elem.assert_called_once_with(
            ui_hierarchy=[sample_hierarchy_node],
            resource_id="com.google.android.apps:id/search_query",
            index=0,
        )
        mock_tap_fn.assert_awaited_once()
        kwargs = mock_tap_fn.await_args.kwargs
        selector_req = kwargs["selector_request"]
        assert isinstance(selector_req, SelectorRequestWithCoordinates)
        assert selector_req.coordinates.x == 516
        assert selector_req.coordinates.y == 329
        assert elem == sample_hierarchy_node

    @pytest.mark.asyncio
    @patch("artemis.tools.utils.tap", new_callable=AsyncMock)
    @patch("artemis.tools.utils.find_element_by_resource_id")
    async def test_cursor_routes_using_direct_bounds(
        self,
        mock_find_elem: MagicMock,
        mock_tap_fn: AsyncMock,
        dummy_context: MagicMock,
        mock_graph_state: MagicMock,
    ):
        """Direct bounding box coordinates should bypass hierarchy inspection."""
        bounds = ElementBounds(x=80, y=100, width=200, height=60)
        target = Target(bounds=bounds)

        result = await move_cursor_to_end_if_bounds(
            ctx=dummy_context, state=mock_graph_state, target=target
        )

        mock_find_elem.assert_not_called()
        mock_tap_fn.assert_awaited_once()
        kwargs = mock_tap_fn.await_args.kwargs
        selector_req = kwargs["selector_request"]
        assert selector_req.coordinates.x == 278
        assert selector_req.coordinates.y == 159
        assert result is None

    @pytest.mark.asyncio
    @patch("artemis.tools.utils.tap", new_callable=AsyncMock)
    @patch("artemis.tools.utils.find_element_by_text")
    async def test_cursor_routes_by_matched_text(
        self,
        mock_find_text: MagicMock,
        mock_tap_fn: AsyncMock,
        dummy_context: MagicMock,
        mock_graph_state: MagicMock,
        sample_hierarchy_node: dict,
    ):
        """Visible text locator should successfully resolve bounds and trigger cursor placement."""
        mock_graph_state.latest_ui_hierarchy = [sample_hierarchy_node]
        mock_find_text.return_value = sample_hierarchy_node

        target = Target(text="Search Google", text_index=0)
        result = await move_cursor_to_end_if_bounds(
            ctx=dummy_context, state=mock_graph_state, target=target
        )

        mock_find_text.assert_called_once_with([sample_hierarchy_node], "Search Google", index=0)
        mock_tap_fn.assert_awaited_once()
        assert result == sample_hierarchy_node

    @pytest.mark.asyncio
    @patch("artemis.tools.utils.tap", new_callable=AsyncMock)
    @patch("artemis.tools.utils.find_element_by_text")
    async def test_cursor_skips_when_text_unmatched(
        self,
        mock_find_text: MagicMock,
        mock_tap_fn: AsyncMock,
        dummy_context: MagicMock,
        mock_graph_state: MagicMock,
    ):
        """Unresolved text target returns None without tapping."""
        mock_find_text.return_value = None
        target = Target(text="Unmatched Query")

        result = await move_cursor_to_end_if_bounds(
            ctx=dummy_context, state=mock_graph_state, target=target
        )
        mock_tap_fn.assert_not_awaited()
        assert result is None


# ==============================================================================
# Element Focus Resolution Tests
# ==============================================================================


class TestElementFocusing:
    """Validates conditional focusing behavior prior to keyboard inputs."""

    @pytest.mark.asyncio
    @patch("artemis.tools.utils.tap", new_callable=AsyncMock)
    @patch("artemis.tools.utils.UnifiedMobileController")
    async def test_skips_tap_when_already_focused(
        self,
        mock_controller_cls: MagicMock,
        mock_tap_fn: AsyncMock,
        dummy_context: MagicMock,
        sample_hierarchy_node: dict,
    ):
        """Elements reporting focused state true should avoid redundant tap interactions."""
        focused_elem = sample_hierarchy_node.copy()
        focused_elem["focused"] = "true"

        mock_ctrl = MagicMock()
        mock_ctrl.get_ui_elements = AsyncMock(return_value=[focused_elem])
        mock_controller_cls.return_value = mock_ctrl

        target = Target(resource_id="com.google.android.apps:id/search_query")
        focus_strategy = await focus_element_if_needed(ctx=dummy_context, target=target)

        mock_tap_fn.assert_not_awaited()
        assert focus_strategy == "resource_id"

    @pytest.mark.asyncio
    @patch("artemis.tools.utils.tap", new_callable=AsyncMock)
    @patch("artemis.tools.utils.UnifiedMobileController")
    async def test_focus_executes_tap_for_unfocused_element(
        self,
        mock_controller_cls: MagicMock,
        mock_tap_fn: AsyncMock,
        dummy_context: MagicMock,
        sample_hierarchy_node: dict,
    ):
        """Unfocused element triggers focus tap and confirms state change."""
        unfocused = sample_hierarchy_node.copy()
        unfocused["focused"] = "false"
        focused = sample_hierarchy_node.copy()
        focused["focused"] = "true"

        mock_ctrl = MagicMock()
        mock_ctrl.get_ui_elements = AsyncMock(side_effect=[[unfocused], [focused]])
        mock_controller_cls.return_value = mock_ctrl

        target = Target(resource_id="com.google.android.apps:id/search_query")
        strategy = await focus_element_if_needed(ctx=dummy_context, target=target)

        mock_tap_fn.assert_awaited_once_with(
            ctx=dummy_context,
            selector_request=IdSelectorRequest(id="com.google.android.apps:id/search_query"),
            index=0,
        )
        assert strategy == "resource_id"

    @pytest.mark.asyncio
    @patch("artemis.tools.utils.tap", new_callable=AsyncMock)
    @patch("artemis.tools.utils.UnifiedMobileController")
    async def test_focus_falls_back_to_text_coordinates(
        self,
        mock_controller_cls: MagicMock,
        mock_tap_fn: AsyncMock,
        dummy_context: MagicMock,
        sample_hierarchy_node: dict,
    ):
        """When ID is unavailable, focusing falls back to text center tap coordinates."""
        elem = sample_hierarchy_node.copy()
        elem["bounds"] = {"x": 20, "y": 40, "width": 200, "height": 60}

        mock_ctrl = MagicMock()
        mock_ctrl.get_ui_elements = AsyncMock(return_value=[elem])
        mock_controller_cls.return_value = mock_ctrl

        target = Target(text="Search Google")
        strategy = await focus_element_if_needed(ctx=dummy_context, target=target)

        mock_tap_fn.assert_awaited_once()
        req = mock_tap_fn.await_args.kwargs["selector_request"]
        assert isinstance(req, SelectorRequestWithCoordinates)
        assert req.coordinates.x == 120  # 20 + 200/2
        assert req.coordinates.y == 70  # 40 + 60/2
        assert strategy == "text"

    @pytest.mark.asyncio
    @patch("artemis.tools.utils.logger")
    @patch("artemis.tools.utils.UnifiedMobileController")
    async def test_focus_aborts_when_no_locators_resolve(
        self,
        mock_controller_cls: MagicMock,
        mock_logger: MagicMock,
        dummy_context: MagicMock,
    ):
        """When none of the declared locators match an active element, returns None and logs error."""
        mock_ctrl = MagicMock()
        mock_ctrl.get_ui_elements = AsyncMock(return_value=[])
        mock_controller_cls.return_value = mock_ctrl

        target = Target(resource_id="unknown_id", text="unknown_text")
        strategy = await focus_element_if_needed(ctx=dummy_context, target=target)

        mock_logger.error.assert_called_once()
        assert strategy is None


# ==============================================================================
# Helper Verification & Selector Validation Tests
# ==============================================================================


class TestSelectorValidation:
    """Verifies target completeness and boundary constraint checks."""

    def test_has_valid_selectors_matrix(self):
        """Verifies selector detection across valid and invalid combinations."""
        assert has_valid_selectors(Target(resource_id="elem_id"))
        assert has_valid_selectors(Target(text="Label"))
        assert has_valid_selectors(Target(bounds=ElementBounds(x=0, y=0, width=10, height=10)))
        assert not has_valid_selectors(Target())

    def test_validate_coordinates_bounds(self):
        """Checks bounds containment against physical device display limits."""
        valid_target = Target(bounds=ElementBounds(x=100, y=200, width=100, height=100))
        assert validate_coordinates_bounds(valid_target, 1080, 2400) is None

        out_width = Target(bounds=ElementBounds(x=1050, y=200, width=100, height=100))
        assert validate_coordinates_bounds(out_width, 1080, 2400) is not None

        out_height = Target(bounds=ElementBounds(x=100, y=2380, width=100, height=100))
        assert validate_coordinates_bounds(out_height, 1080, 2400) is not None

    def test_extract_selector_attributes(self):
        """Extracts resource ID and text from heterogeneous selector requests."""
        req_id = IdSelectorRequest(id="view_item")
        assert _extract_resource_id_and_text_from_selector(req_id) == ("view_item", None)

        req_text = TextSelectorRequest(text="Confirm")
        assert _extract_resource_id_and_text_from_selector(req_text) == (None, "Confirm")

        req_both = IdWithTextSelectorRequest(id="submit_btn", text="Submit")
        assert _extract_resource_id_and_text_from_selector(req_both) == ("submit_btn", "Submit")

        req_coords = SelectorRequestWithCoordinates(
            coordinates=CoordinatesSelectorRequest(x=50, y=50)
        )
        assert _extract_resource_id_and_text_from_selector(req_coords) == (None, None)
