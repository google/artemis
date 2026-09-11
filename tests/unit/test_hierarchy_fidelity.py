"""What the agent sees must be what is on screen, whichever backend dumped it.

Covers the host-side half of the UIAutomator parity work: negative bounds no
longer lose an element's coordinates, hints and errors reach the element list
the model reads, an empty helper answer degrades to UIAutomator2 in ``auto``
mode, and the parity comparison used by ``artemis helper parity`` flags the
regressions it exists for.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from artemis.clients.accessibility_client import AccessibilityClient, HelperEmptyHierarchy
from artemis.clients.screen_client_factory import FallbackScreenClient
from artemis.clients.ui_automator_client import UIAutomatorClient, _parse_hierarchy_xml_to_elements
from artemis.core.diagnostics.hierarchy_parity import compare_dumps
from artemis.utils import ui_filter
from artemis.utils.visualization import format_minimal_list_with_elements


def _node(**attrs: str) -> str:
    base = {
        "index": "0",
        "text": "",
        "resource-id": "",
        "class": "android.widget.TextView",
        "package": "app",
        "content-desc": "",
        "clickable": "false",
        "enabled": "true",
        "bounds": "[0,0][100,100]",
    }
    base.update(attrs)
    return "<node " + " ".join(f'{k}="{v}"' for k, v in base.items()) + " />"


def _xml(*nodes: str) -> str:
    return '<?xml version="1.0"?><hierarchy rotation="0">' + "".join(nodes) + "</hierarchy>"


# --------------------------------------------------------------------------- #
# Negative bounds
# --------------------------------------------------------------------------- #


def test_negative_bounds_keep_their_coordinates_and_get_clipped():
    elements = _parse_hierarchy_xml_to_elements(_xml(_node(text="Row", bounds="[-20,-40][300,60]")))
    row = elements[1]
    assert row["parsed_bounds"] == {"left": -20, "top": -40, "right": 300, "bottom": 60}

    assert ui_filter._parse_bounds("[-20,-40][300,60]") == {
        "left": -20,
        "top": -40,
        "right": 300,
        "bottom": 60,
    }
    filtered = ui_filter.filter_ui_hierarchy([row], screen_width=1080, screen_height=2400)
    assert len(filtered) == 1
    assert filtered[0]["parsed_bounds"] == {"left": 0, "top": 0, "right": 300, "bottom": 60}


# --------------------------------------------------------------------------- #
# Hints and errors reach the model
# --------------------------------------------------------------------------- #


def test_minimal_list_shows_hint_for_empty_inputs_and_errors():
    elements = _parse_hierarchy_xml_to_elements(
        _xml(
            _node(text="", hint="Search settings", **{"class": "android.widget.EditText"}),
            _node(text="abc", error="Password too short", bounds="[0,200][100,300]"),
            _node(text="", bounds="[0,400][100,500]"),  # nothing to say: stays out
        )
    )
    text, items, labels = format_minimal_list_with_elements(elements[1:], 1000, 1000)
    lines = text.splitlines()
    assert lines[0] == "[1] Hint: 'Search settings' | Bounds: [0,0][100,100]"
    assert lines[1] == "[2] Text: 'abc' | Bounds: [0,200][100,300] | Error: 'Password too short'"
    assert len(lines) == 2 and labels == ["1", "2"]
    assert items[0]["is_hint"] is True and items[0]["text"] == "Search settings"
    assert items[1]["error"] == "Password too short"


# --------------------------------------------------------------------------- #
# Empty helper answer degrades instead of showing a blank screen
# --------------------------------------------------------------------------- #


def test_empty_helper_hierarchy_falls_back_to_uiautomator():
    helper = MagicMock(spec=AccessibilityClient)
    u2 = MagicMock(spec=UIAutomatorClient)
    helper.get_screen_data.side_effect = HelperEmptyHierarchy("no window")
    u2.get_screen_data.return_value = "from-u2"
    client = FallbackScreenClient(
        "dev", helper=helper, uiautomator_factory=lambda: u2, state_probe=lambda _s: "device"
    )
    assert client.get_screen_data() == "from-u2"
    assert client.active_backend == "uiautomator"
    assert "HelperEmptyHierarchy" in client.backend_history[-1]["reason"]


# --------------------------------------------------------------------------- #
# Parity comparison
# --------------------------------------------------------------------------- #


def test_parity_passes_when_both_backends_describe_the_same_screen():
    u2 = _xml(
        _node(text="Wi-Fi", bounds="[0,100][1080,200]"),
        _node(**{"content-desc": "Back", "bounds": "[0,0][120,120]"}),
    )
    helper = _xml(
        _node(text="Wi-Fi", bounds="[2,101][1078,199]"),
        _node(**{"content-desc": "Back", "bounds": "[0,0][120,120]"}),
    )
    report = compare_dumps(helper, u2, 1080, 2400)
    assert report["ok"], report["problems"]
    assert report["match"]["recall"] == 1.0 and report["match"]["precision"] == 1.0


@pytest.mark.parametrize(
    "helper_extra, expected",
    [
        (_node(text="Hidden drawer item", bounds="[-1080,300][0,400]"), "negative bounds"),
        (_node(text="Next page", bounds="[1080,300][2160,400]"), "outside the screen"),
        (
            "".join(
                _node(text=f"Ghost {i}", bounds=f"[0,{i * 50}][100,{i * 50 + 40}]")
                for i in range(8)
            ),
            "not on screen",
        ),
    ],
)
def test_parity_flags_elements_uiautomator_would_not_show(helper_extra, expected):
    u2 = _xml(_node(text="Wi-Fi", bounds="[0,100][1080,200]"))
    helper = _xml(_node(text="Wi-Fi", bounds="[0,100][1080,200]"), helper_extra)
    report = compare_dumps(helper, u2, 1080, 2400)
    assert not report["ok"]
    assert any(expected in problem for problem in report["problems"]), report["problems"]


def test_parity_flags_missing_elements():
    u2 = _xml(
        _node(text="Wi-Fi", bounds="[0,100][1080,200]"),
        _node(text="Bluetooth", bounds="[0,200][1080,300]"),
    )
    helper = _xml(_node(text="Wi-Fi", bounds="[0,100][1080,200]"))
    report = compare_dumps(helper, u2, 1080, 2400)
    assert not report["ok"]
    assert report["match"]["missing_in_helper"] == [
        "'Bluetooth' at {'left': 0, 'top': 200, 'right': 1080, 'bottom': 300}"
    ]
