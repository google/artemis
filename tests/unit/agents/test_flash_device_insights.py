from __future__ import annotations

from types import SimpleNamespace

from artemis.agents.flash.runner import FlashRunner


def _runner(rows: list[dict]) -> FlashRunner:
    runner = object.__new__(FlashRunner)
    storage = SimpleNamespace(device_insights=lambda serial: rows)
    runner.ctx = SimpleNamespace(
        device=SimpleNamespace(device_id="103.123.5.27:5555"),
        data_engine=SimpleNamespace(storage=storage),
    )
    return runner


def test_a_route_is_offered_when_its_screen_is_on_now() -> None:
    runner = _runner(
        [{"screen_key": "控制中心", "insight": "swipe 960 1050 to 960 700", "success_count": 3}]
    )

    notices = runner._device_insight_notices("Text: '控制中心' Bounds: [36,59][103,101]")

    assert len(notices) == 1
    assert "swipe 960 1050 to 960 700" in notices[0]
    assert "worked 3x" in notices[0]


def test_a_route_for_another_screen_stays_out() -> None:
    runner = _runner([{"screen_key": "控制中心", "insight": "swipe up", "success_count": 1}])

    assert runner._device_insight_notices("Text: '设置' Bounds: [0,0][10,10]") == []


def test_no_observation_means_no_notices() -> None:
    runner = _runner([{"screen_key": "控制中心", "insight": "swipe up", "success_count": 1}])

    assert runner._device_insight_notices(None) == []
