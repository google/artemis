"""SDK entry point for the Pro-profile tuning knobs.

``AgentConfigBuilder.with_verification_level`` mirrors ``--verification-level``
and ``with_pro_config(verification_level=..., explorer_mode=...)`` carries both
knobs in one call; both resolve through the single preset table in
``artemis.config``.
"""

import pytest

from artemis.config import VERIFICATION_LEVEL_PRESETS, checker_overrides_for_level
from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder


def test_with_verification_level_off_disables_the_checker():
    cfg = AgentConfigBuilder().with_verification_level("off").build(validate_profiles=False)
    assert cfg.disable_checker is True


def test_with_verification_level_final_keeps_factory_layering():
    cfg = AgentConfigBuilder().with_verification_level("final").build(validate_profiles=False)
    assert cfg.disable_checker is False
    assert cfg.disable_midway_checks is True
    assert cfg.disable_final_check is False


def test_with_verification_level_checkpoints_enables_midway_checks():
    cfg = AgentConfigBuilder().with_verification_level("checkpoints").build(validate_profiles=False)
    assert cfg.disable_checker is False
    assert cfg.disable_midway_checks is False
    assert cfg.disable_final_check is False
    # Unspecified fields keep the artemis.jsonc values.
    assert cfg.assert_failure_policy == "continue"


def test_with_verification_level_strict_applies_the_full_preset():
    cfg = AgentConfigBuilder().with_verification_level("strict").build(validate_profiles=False)
    preset = VERIFICATION_LEVEL_PRESETS["strict"]
    assert cfg.disable_checker is False
    assert cfg.disable_midway_checks is False
    assert cfg.disable_final_check is False
    assert cfg.assert_failure_policy == preset["assert_failure_policy"]
    assert cfg.checkpoint_max_repairs == preset["checkpoint_max_repairs"]
    assert cfg.final_check_max_attempts == preset["final_check_max_attempts"]
    assert cfg.checker_max_iterations == preset["max_iterations"]


def test_with_verification_level_is_case_and_whitespace_insensitive():
    cfg = AgentConfigBuilder().with_verification_level("  STRICT ").build(validate_profiles=False)
    assert cfg.assert_failure_policy == "halt"


def test_with_verification_level_rejects_unknown_preset():
    builder = AgentConfigBuilder()
    with pytest.raises(ValueError, match="Unknown verification level"):
        builder.with_verification_level("paranoid")
    # The builder is untouched by a rejected call.
    assert builder.build(validate_profiles=False).disable_checker is False


def test_with_verification_level_matches_the_shared_preset_table():
    for level in VERIFICATION_LEVEL_PRESETS:
        cfg = AgentConfigBuilder().with_verification_level(level).build(validate_profiles=False)
        overrides = checker_overrides_for_level(level)
        assert cfg.disable_checker is (not overrides["enabled"])
        if "midway_checks" in overrides:
            assert cfg.disable_midway_checks is (not overrides["midway_checks"])


def test_with_verification_level_is_chainable_and_explicit_checker_wins():
    cfg = (
        AgentConfigBuilder()
        .with_verification_level("strict")
        .with_checker(enabled=False)
        .build(validate_profiles=False)
    )
    assert cfg.disable_checker is True
    # The preset's other fields survive the later master switch.
    assert cfg.assert_failure_policy == "halt"


def test_with_pro_config_carries_both_knobs():
    cfg = (
        AgentConfigBuilder()
        .with_pro_config(verification_level="checkpoints", explorer_mode="ultra")
        .build(validate_profiles=False)
    )
    assert cfg.disable_checker is False
    assert cfg.disable_midway_checks is False
    assert cfg.explorer.pro_mode == "ultra"
    # Flash-profile perception is untouched by the Pro knob.
    assert cfg.explorer.flash_mode == "flash"


def test_with_pro_config_checker_switch_wins_over_verification_level():
    cfg = (
        AgentConfigBuilder()
        .with_pro_config(verification_level="strict", checker=False)
        .build(validate_profiles=False)
    )
    assert cfg.disable_checker is True


def test_with_explorer_pro_mode_is_the_explorer_mode_knob(monkeypatch):
    monkeypatch.delenv("ARTEMIS_EXPLORER_VERSION", raising=False)
    cfg = AgentConfigBuilder().with_explorer(pro_mode="pro").build(validate_profiles=False)
    assert cfg.explorer.pro_mode == "pro"
    assert cfg.get_explorer_version(agent_name=None, profile="pro") == "pro"
    # The shipped per-agent override is empty, so the Pro agents follow the knob.
    assert cfg.get_explorer_version(agent_name="operator") == "pro"
    assert cfg.get_explorer_version(agent_name="validator") == "pro"


def test_with_flash_config_explorer_mode_is_the_flash_profile_knob(monkeypatch):
    monkeypatch.delenv("ARTEMIS_EXPLORER_VERSION", raising=False)
    cfg = AgentConfigBuilder().with_flash_config(explorer_mode="pro").build(validate_profiles=False)
    assert cfg.flash.explorer_mode == "pro"
    assert cfg.explorer.flash_mode == "pro"
    assert cfg.get_explorer_version(agent_name="flash") == "pro"
    # Pro-profile perception is untouched by the Flash knob.
    assert cfg.get_explorer_version(agent_name="operator") == "flash"


def test_with_explorer_versions_is_an_advanced_per_agent_override(monkeypatch):
    monkeypatch.delenv("ARTEMIS_EXPLORER_VERSION", raising=False)
    cfg = (
        AgentConfigBuilder()
        .with_explorer(pro_mode="flash", versions={"validator": "ultra"})
        .build(validate_profiles=False)
    )
    assert cfg.get_explorer_version(agent_name="validator") == "ultra"
    assert cfg.get_explorer_version(agent_name="operator") == "flash"


def test_ios_workspace_path_defaults_to_none_and_survives_build():
    cfg = AgentConfigBuilder().build(validate_profiles=False)
    assert cfg.ios_workspace_path is None


def test_with_ios_workspace_accepts_str_and_path_and_clears(tmp_path):
    project = tmp_path / "My App.xcodeproj"
    project.mkdir()
    builder = AgentConfigBuilder()
    cfg = builder.with_ios_workspace(str(project)).build(validate_profiles=False)
    assert cfg.ios_workspace_path == project
    cfg = builder.with_ios_workspace(None).build(validate_profiles=False)
    assert cfg.ios_workspace_path is None


def test_for_ios_simulator_workspace_kwarg_and_omission(tmp_path):
    project = tmp_path / "App.xcodeproj"
    project.mkdir()
    cfg = (
        AgentConfigBuilder()
        .for_ios_simulator(workspace_path=project)
        .build(validate_profiles=False)
    )
    assert cfg.ios_workspace_path == project
    # Omitting the kwarg does not clear a workspace set earlier on the builder.
    cfg = (
        AgentConfigBuilder()
        .with_ios_workspace(project)
        .for_ios_simulator()
        .build(validate_profiles=False)
    )
    assert cfg.ios_workspace_path == project


def test_for_ios_device_and_alias_preserve_device_and_workspace(tmp_path):
    """The canonical name and the simulator-era alias carry identical config."""
    from artemis.context import DevicePlatform

    project = tmp_path / "App.xcworkspace"
    project.mkdir()
    udid = "AAAA-1111-0000"
    canonical = (
        AgentConfigBuilder()
        .for_ios_device(udid, workspace_path=project)
        .build(validate_profiles=False)
    )
    alias = (
        AgentConfigBuilder()
        .for_ios_simulator(udid, workspace_path=project)
        .build(validate_profiles=False)
    )
    for cfg in (canonical, alias):
        assert cfg.device_platform == DevicePlatform.IOS
        assert cfg.device_id == udid
        assert cfg.ios_workspace_path == project
    # The default iOS target stays the lazy 'booted' selector.
    default = AgentConfigBuilder().for_ios_device().build(validate_profiles=False)
    assert default.device_platform == DevicePlatform.IOS
    assert default.device_id == "booted"
