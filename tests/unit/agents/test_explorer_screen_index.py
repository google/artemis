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

"""Unit tests for the Explorer's in-memory screen index."""

import pytest

from artemis.agents.explorer.screen_index import (
    ScreenElement,
    ScreenIndex,
    normalize_text,
    text_similarity,
)

W, H = 1080, 2400


def _node(text=None, bounds="[0,0][100,100]", desc=None, **attrs):
    node = {"bounds": bounds, "class": "android.widget.TextView"}
    if text is not None:
        node["text"] = text
    if desc is not None:
        node["content-desc"] = desc
    node.update(attrs)
    return node


def _hierarchy():
    return [
        _node("Gmail", "[300,1600][500,1800]", clickable="true", **{"resource-id": "app:gmail"}),
        _node(None, "[0,0][1080,2400]"),  # unlabeled root wrapper is skipped
        _node(desc="Settings", bounds="[900,50][1000,150]", clickable="true"),
        {
            "bounds": "[100,2100][900,2300]",
            "class": "android.widget.FrameLayout [OCR]",
            "ocr_elements": [{"text": "Search", "bounds": "[150,2150][350,2250]"}],
        },
        _node("Search", "[120,2120][380,2280]"),  # overlaps the OCR "Search"
        _node("Offscreen", "[-500,-500][-400,-400]"),
        _node("Zero", "[10,10][10,10]"),
    ]


def test_normalize_and_similarity():
    assert normalize_text("  Send\n Message ") == "send message"
    assert text_similarity("gmail", "Gmail") == 1.0
    assert text_similarity("send", "Send message") >= 0.85
    assert text_similarity("ok", "Look here") < 0.85
    assert text_similarity("", "x") == 0.0


def test_from_hierarchy_skips_invisible_and_unlabeled():
    index = ScreenIndex.from_hierarchy(_hierarchy(), W, H)
    texts = sorted(e.text for e in index.elements)
    assert texts == ["Gmail", "Search", "Search", "Settings"]
    assert len(index.ocr_elements) == 1
    gmail = next(e for e in index.elements if e.text == "Gmail")
    assert gmail.interactive and gmail.resource_id == "app:gmail" and gmail.source == "xml"
    assert gmail.center == (400, 1700)


def test_from_hierarchy_tolerates_garbage():
    index = ScreenIndex.from_hierarchy([None, "x", {"bounds": "bad"}, {}], W, H)
    assert len(index) == 0
    assert ScreenIndex.empty(W, H).search_text("x") == []


def test_search_text_orders_by_score_and_respects_threshold():
    index = ScreenIndex.from_hierarchy(_hierarchy(), W, H)
    matches = index.search_text("Gmai", threshold=0.6)
    assert matches and matches[0].element.text == "Gmail"
    assert all(m.score > 0.6 for m in matches)
    assert index.search_text("zzzz", threshold=0.6) == []
    assert len(index.search_text("Search", threshold=0.4, limit=1)) == 1


def test_exact_matches_collapse_overlapping_duplicates_preferring_xml():
    index = ScreenIndex.from_hierarchy(_hierarchy(), W, H)
    hits = index.exact_matches(" search ")
    assert len(hits) == 1
    assert hits[0].source == "xml"
    assert index.exact_matches("settings")[0].text == "Settings"
    assert index.exact_matches("") == []
    assert index.exact_matches("nope") == []


def test_elements_at_reports_innermost_then_ocr():
    elements = [
        ScreenElement("outer", (0, 0, 1000, 1000), "xml"),
        ScreenElement("middle", (100, 100, 900, 900), "xml", interactive=False),
        ScreenElement("button", (400, 400, 600, 600), "xml", interactive=True),
        ScreenElement("label", (450, 450, 550, 550), "ocr"),
    ]
    index = ScreenIndex(elements, W, H)
    found = index.elements_at(500, 500)
    # innermost interactive node stops the UI-tree walk; OCR appended after
    assert [e.text for e in found] == ["button", "label"]
    found = index.elements_at(150, 150)
    assert [e.text for e in found] == ["middle", "outer"]
    assert index.elements_at(1050, 1050) == []


def test_describe_and_overlap():
    a = ScreenElement("A", (0, 0, 100, 100), "xml", class_name="Btn", resource_id="id/a")
    b = ScreenElement("B", (50, 50, 150, 150), "ocr")
    assert "class=Btn" in a.describe() and "id=id/a" in a.describe()
    assert "source=ocr" in b.describe()
    assert a.overlap_ratio(b) == pytest.approx(0.25)
    assert a.overlap_ratio(ScreenElement("C", (200, 200, 300, 300), "xml")) == 0.0
