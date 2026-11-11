from __future__ import annotations

from unittest.mock import MagicMock

from artemis.agents.flash.runner import FlashRunner
from artemis.mcp.screen_diff import NO_SCREEN_CHANGES


def _runner() -> FlashRunner:
    return object.__new__(FlashRunner)


def _tools() -> list:
    return [MagicMock(name=name) for name in ("click", "click_sequence", "swipe", "press_key")]


def test_one_unchanged_screen_keeps_click_available() -> None:
    runner = _runner()
    tools = _tools()
    for tool in tools:
        tool.name = tool._mock_name

    runner._note_screen_change(NO_SCREEN_CHANGES)

    assert len(runner._tools_without_click(tools)) == len(tools)


def test_two_unchanged_screens_withdraw_click() -> None:
    runner = _runner()
    tools = _tools()
    for tool in tools:
        tool.name = tool._mock_name

    runner._note_screen_change(NO_SCREEN_CHANGES)
    runner._note_screen_change(NO_SCREEN_CHANGES)

    remaining = [t.name for t in runner._tools_without_click(tools)]
    assert remaining == ["swipe", "press_key"]


def test_an_empty_diff_counts_as_an_unchanged_screen() -> None:
    runner = _runner()

    runner._note_screen_change("")
    runner._note_screen_change("   ")

    assert runner._no_change_streak == 2


def test_a_changed_screen_brings_click_back() -> None:
    runner = _runner()
    tools = _tools()
    for tool in tools:
        tool.name = tool._mock_name

    runner._note_screen_change(NO_SCREEN_CHANGES)
    runner._note_screen_change(NO_SCREEN_CHANGES)
    runner._note_screen_change("+ [3] Text: '控制中心'")

    assert len(runner._tools_without_click(tools)) == len(tools)


def test_a_blocked_repeat_withdraws_click() -> None:
    runner = _runner()
    args = {"x": 300, "y": 850}

    for _ in range(3):
        runner._repetition_verdict("click", args, {"click"})

    assert runner._click_withdrawn is True
    tools = _tools()
    for tool in tools:
        tool.name = tool._mock_name
    assert "click" not in [t.name for t in runner._tools_without_click(tools)]


def test_changing_the_action_brings_click_back() -> None:
    runner = _runner()
    args = {"x": 300, "y": 850}
    for _ in range(3):
        runner._repetition_verdict("click", args, {"click"})

    runner._repetition_verdict("swipe", {"start": [960, 1050]}, {"click", "swipe"})

    assert getattr(runner, "_click_withdrawn", False) is False


def test_a_swipe_to_new_coordinates_is_a_new_action() -> None:
    runner = _runner()
    for _ in range(3):
        runner._repetition_verdict(
            "swipe",
            {"direction": "left", "start": [850, 357], "end": [750, 357]},
            {"swipe"},
        )

    runner._repetition_verdict(
        "swipe",
        {"direction": "left", "start": [750, 357], "end": [700, 357]},
        {"swipe"},
    )

    assert runner._repeat_count == 1


def test_a_click_at_a_new_x_on_one_row_is_a_new_action() -> None:
    runner = _runner()
    for x in (480, 432, 470):
        runner._repetition_verdict("click", {"coordinates": [x, 335]}, {"click"})

    assert runner._repeat_count == 1


def test_a_click_a_few_pixels_away_is_still_the_same_place() -> None:
    runner = _runner()
    runner._repetition_verdict("click", {"coordinates": [480, 335]}, {"click"})
    runner._repetition_verdict("click", {"coordinates": [483, 337]}, {"click"})

    assert runner._repeat_count == 2
