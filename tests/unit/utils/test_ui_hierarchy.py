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

"""Unit tests validating Artemis UI hierarchy parsing and geometry computation."""

from unittest.mock import patch

from artemis.utils.ui_hierarchy import (
    ElementBounds,
    Point,
    find_element_by_resource_id,
    get_bounds_for_element,
    get_element_text,
    is_element_focused,
    text_input_is_empty,
)


def test_text_input_is_empty():
    assert text_input_is_empty(text=None, hint_text=None)
    assert text_input_is_empty(text="", hint_text=None)
    assert text_input_is_empty(text="", hint_text="")
    assert text_input_is_empty(text="search", hint_text="search")

    assert not text_input_is_empty(text="search", hint_text=None)
    assert not text_input_is_empty(text="search", hint_text="")


def test_find_element_by_resource_id():
    tree = [
        {
            "resourceId": "com.google.android.settings:id/search_bar",
            "text": "Search Settings",
            "children": [],
        },
        {
            "resourceId": "com.google.android.settings:id/content_frame",
            "children": [
                {
                    "resourceId": "com.google.android.settings:id/network_item",
                    "text": "Network & Internet",
                    "children": [],
                }
            ],
        },
    ]

    target = find_element_by_resource_id(tree, "com.google.android.settings:id/search_bar")
    assert target is not None
    assert target["resourceId"] == "com.google.android.settings:id/search_bar"
    assert target["text"] == "Search Settings"

    nested = find_element_by_resource_id(tree, "com.google.android.settings:id/network_item")
    assert nested is not None
    assert nested["resourceId"] == "com.google.android.settings:id/network_item"

    assert find_element_by_resource_id(tree, "com.google.android.settings:id/missing") is None
    assert find_element_by_resource_id([], "com.google.android.settings:id/search_bar") is None


def test_find_element_by_resource_id_with_index():
    duplicate_items = [
        {"resourceId": "com.artemis.ui:id/row_item", "text": "Item Alpha", "children": []},
        {"resourceId": "com.artemis.ui:id/row_item", "text": "Item Beta", "children": []},
        {
            "resourceId": "com.artemis.ui:id/section_group",
            "children": [
                {"resourceId": "com.artemis.ui:id/row_item", "text": "Item Gamma", "children": []}
            ],
        },
    ]

    first = find_element_by_resource_id(duplicate_items, "com.artemis.ui:id/row_item", index=0)
    assert first is not None
    assert first["text"] == "Item Alpha"

    second = find_element_by_resource_id(duplicate_items, "com.artemis.ui:id/row_item", index=1)
    assert second is not None
    assert second["text"] == "Item Beta"

    third = find_element_by_resource_id(duplicate_items, "com.artemis.ui:id/row_item", index=2)
    assert third is not None
    assert third["text"] == "Item Gamma"

    assert find_element_by_resource_id(duplicate_items, "com.artemis.ui:id/row_item", index=3) is None


def test_find_element_by_resource_id_rich_hierarchy():
    rich_nodes = [
        {
            "attributes": {"resource-id": "com.android.systemui:id/clock"},
            "children": [],
        },
        {
            "attributes": {"resource-id": "com.android.systemui:id/status_bar"},
            "children": [
                {
                    "attributes": {"resource-id": "com.android.systemui:id/battery_meter"},
                    "children": [],
                }
            ],
        },
    ]

    match = find_element_by_resource_id(
        rich_nodes, "com.android.systemui:id/clock", is_rich_hierarchy=True
    )
    assert match is not None
    assert match["resource-id"] == "com.android.systemui:id/clock"

    nested_match = find_element_by_resource_id(
        rich_nodes, "com.android.systemui:id/battery_meter", is_rich_hierarchy=True
    )
    assert nested_match is not None
    assert nested_match["resource-id"] == "com.android.systemui:id/battery_meter"

    assert find_element_by_resource_id(
        rich_nodes, "com.android.systemui:id/unknown", is_rich_hierarchy=True
    ) is None


def test_is_element_focused():
    assert is_element_focused({"focused": "true"})
    assert is_element_focused({"focused": True})
    assert not is_element_focused({"focused": "false"})
    assert not is_element_focused({"focused": False})
    assert not is_element_focused({"text": "sample"})
    assert not is_element_focused({"focused": None})


def test_get_element_text():
    node = {"text": "Battery", "hintText": "Search settings"}
    assert get_element_text(node) == "Battery"
    assert get_element_text(node, hint_text=False) == "Battery"
    assert get_element_text(node, hint_text=True) == "Search settings"

    node_no_text = {"hintText": "Placeholder"}
    assert get_element_text(node_no_text) is None
    assert get_element_text(node_no_text, hint_text=True) == "Placeholder"

    assert get_element_text({}) is None


def test_get_bounds_for_element():
    node_with_box = {"bounds": {"x": 24, "y": 48, "width": 400, "height": 80}}
    box = get_bounds_for_element(node_with_box)
    assert box is not None
    assert isinstance(box, ElementBounds)
    assert box.x == 24
    assert box.y == 48
    assert box.width == 400
    assert box.height == 80

    assert get_bounds_for_element({"text": "Click"}) is None

    with patch("artemis.utils.ui_hierarchy.logger.error"):
        bad_box = {"bounds": {"x": "corrupted", "y": 48, "width": 400, "height": 80}}
        assert get_bounds_for_element(bad_box) is None


def test_element_bounds_geometry():
    box = ElementBounds(x=100, y=200, width=500, height=100)

    center = box.get_center()
    assert isinstance(center, Point)
    assert center.x == 350
    assert center.y == 250

    mid = box.get_relative_point(0.5, 0.5)
    assert mid.x == 350
    assert mid.y == 250

    top_left = box.get_relative_point(0.0, 0.0)
    assert top_left.x == 100
    assert top_left.y == 200

    bottom_right = box.get_relative_point(1.0, 1.0)
    assert bottom_right.x == 600
    assert bottom_right.y == 300
