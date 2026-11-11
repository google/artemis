from __future__ import annotations

from artemis.utils.visualization import format_minimal_list_with_elements


def _seekbar() -> dict[str, object]:
    return {
        "class": "android.widget.SeekBar",
        "text": "",
        "content-desc": "",
        "clickable": "false",
        "focusable": "true",
        "bounds": "[554,338][1138,386]",
        "resource-id": "com.sgmw.systemsettings:id/sb_brightness",
    }


def test_a_seekbar_without_a_label_still_gets_an_index() -> None:
    """The brightness slider is clickable=false, so a model that only sees clickable nodes has to guess its pixels."""

    text, elements, _ = format_minimal_list_with_elements([_seekbar()], 1920, 1080)

    assert "SeekBar" in text
    assert "[288,312][592,357]" in text
    assert any(element["class"] == "android.widget.SeekBar" for element in elements)


def test_a_plain_unlabeled_layout_stays_out() -> None:
    node = {
        "class": "android.widget.FrameLayout",
        "text": "",
        "content-desc": "",
        "clickable": "false",
        "bounds": "[0,0][100,100]",
    }

    text, _, _ = format_minimal_list_with_elements([node], 1920, 1080)

    assert "FrameLayout" not in text


def test_a_measured_slider_reports_its_value_instead_of_bounds() -> None:
    text, elements, _ = format_minimal_list_with_elements(
        [_seekbar()], 1920, 1080, {"[554,338][1138,386]": 44}
    )

    assert "约 44%" in text
    assert "约 44%" in text
    assert elements[0]["value_percent"] == 44


def test_two_frames_of_one_slider_report_what_each_frame_shows() -> None:
    first, _, _ = format_minimal_list_with_elements([_seekbar()], 1920, 1080, {"[554,338][1138,386]": 44})
    second, _, _ = format_minimal_list_with_elements([_seekbar()], 1920, 1080, {"[554,338][1138,386]": 71})

    assert "约 44%" in first
    assert "约 71%" in second
