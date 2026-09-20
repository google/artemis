"""Device binding on ``AgentConfigBuilder.build``.

``Agent._init_internal`` falls back to ``get_first_device()`` whenever either
``device_id`` or ``device_platform`` is missing, so a serial that arrives via
``ADB_DEVICE_SERIAL`` / ``ARTEMIS_DEVICE_ID`` must leave the builder with a
platform as well, or it is silently discarded.
"""

from artemis.context import DevicePlatform
from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder


def test_env_serial_binds_id_and_platform(monkeypatch):
    monkeypatch.delenv("ARTEMIS_DEVICE_ID", raising=False)
    monkeypatch.setenv("ADB_DEVICE_SERIAL", "10.0.0.8:5555")

    cfg = AgentConfigBuilder().build()

    assert cfg.device_id == "10.0.0.8:5555"
    assert cfg.device_platform == DevicePlatform.ANDROID


def test_artemis_device_id_takes_precedence_over_adb_serial(monkeypatch):
    monkeypatch.setenv("ARTEMIS_DEVICE_ID", "10.0.0.1:5555")
    monkeypatch.setenv("ADB_DEVICE_SERIAL", "10.0.0.8:5555")

    cfg = AgentConfigBuilder().build()

    assert cfg.device_id == "10.0.0.1:5555"
    assert cfg.device_platform == DevicePlatform.ANDROID


def test_no_serial_leaves_first_device_fallback(monkeypatch):
    monkeypatch.delenv("ARTEMIS_DEVICE_ID", raising=False)
    monkeypatch.delenv("ADB_DEVICE_SERIAL", raising=False)

    cfg = AgentConfigBuilder().build()

    assert cfg.device_id is None
    assert cfg.device_platform is None


def test_explicit_for_device_wins_over_env(monkeypatch):
    monkeypatch.setenv("ADB_DEVICE_SERIAL", "10.0.0.8:5555")

    cfg = AgentConfigBuilder().for_device("emulator-5554").build()

    assert cfg.device_id == "emulator-5554"
    assert cfg.device_platform == DevicePlatform.ANDROID
