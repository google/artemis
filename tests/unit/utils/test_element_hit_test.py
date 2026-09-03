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

"""Tests for record-time action semantics enrichment (element hit testing)."""

from artemis.utils.element_hit_test import find_element_at_point, hit_test_semantics


def _xml_el(text, bounds, resource_id=None, cls="android.widget.TextView"):
    return {
        "text": text,
        "bounds": bounds,
        "class": cls,
        "resource_id": resource_id,
        "is_ocr": False,
    }


def test_smallest_covering_element_wins():
    elements = [
        _xml_el("Whole screen", [0, 0, 1080, 2400]),
        _xml_el("Card", [100, 100, 500, 500]),
        _xml_el("Button", [200, 200, 300, 260], resource_id="btn_ok"),
    ]
    el, source = find_element_at_point(elements, 250, 230)
    assert source == "hit_test"
    assert el["text"] == "Button"

    semantics = hit_test_semantics(elements, 250, 230)
    assert semantics == {
        "target_label_source": "hit_test",
        "target_text": "Button",
        "target_class": "android.widget.TextView",
        "target_resource_id": "btn_ok",
    }


def test_xml_element_preferred_over_smaller_ocr_element():
    elements = [
        _xml_el("Login", [100, 100, 500, 300]),
        {
            "text": "Login OCR",
            "bounds": [200, 150, 260, 180],
            "class": None,
            "resource_id": None,
            "is_ocr": True,
        },
    ]
    el, source = find_element_at_point(elements, 220, 160)
    assert source == "hit_test"
    assert el["text"] == "Login"


def test_ocr_fallback_when_no_xml_element_covers_point():
    elements = [
        _xml_el("Elsewhere", [800, 800, 900, 900]),
        {
            "text": "OCR Label",
            "bounds": [100, 100, 300, 200],
            "class": None,
            "resource_id": None,
            "is_ocr": True,
        },
    ]
    semantics = hit_test_semantics(elements, 150, 150)
    assert semantics["target_label_source"] == "ocr"
    assert semantics["target_text"] == "OCR Label"
    assert "target_class" not in semantics
    assert "target_resource_id" not in semantics


def test_string_bounds_are_parsed():
    elements = [_xml_el("Item", "[100,100][300,500]")]
    el, source = find_element_at_point(elements, 200, 300)
    assert source == "hit_test"
    assert el["text"] == "Item"


def test_graceful_degradation_to_none():
    # No elements at all
    assert hit_test_semantics(None, 100, 100) == {"target_label_source": "none"}
    assert hit_test_semantics([], 100, 100) == {"target_label_source": "none"}

    # Point outside every element
    elements = [_xml_el("Item", [0, 0, 50, 50])]
    assert hit_test_semantics(elements, 500, 500) == {"target_label_source": "none"}

    # Malformed element data must not raise
    malformed = [
        {"text": "no bounds"},
        {"text": "bad bounds", "bounds": "garbage"},
        {"text": "short bounds", "bounds": [1, 2]},
        {"text": "non-numeric", "bounds": ["a", "b", "c", "d"]},
        "not-a-dict",
        None,
    ]
    assert hit_test_semantics(malformed, 10, 10) == {"target_label_source": "none"}


def test_element_without_optional_fields_only_reports_source():
    elements = [{"text": "", "bounds": [0, 0, 100, 100], "class": None, "resource_id": None}]
    semantics = hit_test_semantics(elements, 50, 50)
    assert semantics == {"target_label_source": "hit_test"}
