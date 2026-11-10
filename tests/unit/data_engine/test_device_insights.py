from __future__ import annotations

from pathlib import Path

from artemis.data_engine.storage import StorageManager

SERIAL = "103.123.5.27:5555"


def _storage(tmp_path: Path) -> StorageManager:
    return StorageManager(tmp_path / "trace.db", tmp_path / "traces")


def test_a_stored_route_comes_back_for_the_same_device(tmp_path: Path) -> None:
    storage = _storage(tmp_path)

    storage.remember_device_insight(SERIAL, "控制中心", "swipe 960 1050 to 960 700")

    rows = storage.device_insights(SERIAL)
    assert [row["insight"] for row in rows] == ["swipe 960 1050 to 960 700"]
    assert rows[0]["screen_key"] == "控制中心"


def test_the_same_route_twice_raises_its_count_instead_of_duplicating(
    tmp_path: Path,
) -> None:
    storage = _storage(tmp_path)

    storage.remember_device_insight(SERIAL, "控制中心", "swipe 960 1050 to 960 700")
    count = storage.remember_device_insight(SERIAL, "控制中心", "swipe 960 1050 to 960 700")

    assert count == 2
    assert len(storage.device_insights(SERIAL)) == 1


def test_routes_belong_to_one_device_only(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    storage.remember_device_insight(SERIAL, "控制中心", "swipe up")

    assert storage.device_insights("192.168.1.9:5555") == []


def test_the_most_reused_route_is_listed_first(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    storage.remember_device_insight(SERIAL, "桌面", "press_key home")
    for _ in range(2):
        storage.remember_device_insight(SERIAL, "控制中心", "swipe up")

    keys = [row["screen_key"] for row in storage.device_insights(SERIAL)]
    assert keys == ["控制中心", "桌面"]
