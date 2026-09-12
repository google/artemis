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

"""Unit tests for the unified artemis.config package."""

from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch
import pytest
from pydantic import SecretStr

from artemis.config import (
    CONFIG_DIR,
    ROOT_DIR,
    AgentGlobalConfig,
    LLMConfig,
    OutputConfig,
    Settings,
    cleanup_temp_dir,
    clear_ipc_port,
    clear_ls_address,
    deep_merge_llm_config,
    get_app_dir,
    get_config_path,
    get_data_engine_db_path,
    get_default_llm_config,
    get_default_traces_path,
    get_temp_dir,
    get_traces_dir,
    load_agent_config,
    read_ipc_port,
    read_ls_address,
    record_events,
    write_ipc_port,
    write_ls_address,
)


def test_paths_and_directories():
    """Verify central path resolution."""
    assert ROOT_DIR.exists()
    assert (ROOT_DIR / "artemis").exists()
    assert CONFIG_DIR.exists()
    assert (CONFIG_DIR / "artemis.jsonc").exists()

    app_dir = get_app_dir()
    assert isinstance(app_dir, Path)

    traces_path = get_default_traces_path()
    assert isinstance(traces_path, Path)
    assert get_traces_dir() == traces_path

    db_path = get_data_engine_db_path()
    assert db_path.name == "data_engine.db"

    temp_dir = get_temp_dir("test_subfolder")
    assert temp_dir.exists()
    assert temp_dir.name == "test_subfolder"


def test_config_file_resolver():
    """Test get_config_path for unified artemis.jsonc."""
    artemis_path = get_config_path("artemis.jsonc")
    assert artemis_path.exists()

    with pytest.raises(FileNotFoundError):
        get_config_path("non_existent_config_12345.json")


def test_settings_and_api_key_fallbacks(monkeypatch):
    """Test Settings model, API key fallbacks, and get/set methods."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GCP_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    custom_settings = Settings(
        _env_file=None,
        GOOGLE_API_KEY=None,
        GEMINI_API_KEY=SecretStr("gemini_key_123"),
        OPENAI_API_KEY=SecretStr("openai_key_456"),
        OCR_API_KEY=SecretStr("ocr_key_789"),
    )
    # Automatic fallback should populate GOOGLE_API_KEY from GEMINI_API_KEY
    assert custom_settings.GOOGLE_API_KEY is not None
    assert custom_settings.GOOGLE_API_KEY.get_secret_value() == "gemini_key_123"
    assert custom_settings.get_api_key("ocr").get_secret_value() == "ocr_key_789"

    assert custom_settings.get_api_key("google").get_secret_value() == "gemini_key_123"
    assert custom_settings.get_api_key("gemini").get_secret_value() == "gemini_key_123"
    assert custom_settings.get_api_key("openai").get_secret_value() == "openai_key_456"
    assert custom_settings.get_api_key("nonexistent") is None

    # Test dynamic set_api_key
    custom_settings.set_api_key("xai", "xai_test_key", persist_to_env=False)
    assert custom_settings.get_api_key("xai").get_secret_value() == "xai_test_key"


def test_placeholder_api_key_filtering(monkeypatch):
    """Test that default template placeholders are filtered out and treated as unconfigured."""
    from artemis.config.settings import is_placeholder_key

    assert is_placeholder_key("your_gemini_api_key_here") is True
    assert is_placeholder_key("your_google_cloud_vision_api_key_here") is True
    assert is_placeholder_key("your_openai_api_key_here") is True
    assert is_placeholder_key("your_custom_key_here") is True
    assert is_placeholder_key("<your-api-key>") is True
    assert is_placeholder_key("[api_key]") is True
    assert is_placeholder_key("API_KEY") is True
    assert is_placeholder_key("") is True
    assert is_placeholder_key(None) is True
    assert is_placeholder_key("AIzaSyValidGeminiKey12345") is False

    for env_k in (
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GCP_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPEN_ROUTER_API_KEY",
        "XAI_API_KEY",
        "OCR_API_KEY",
        "VISION_API_KEY",
        "API_KEY",
    ):
        monkeypatch.delenv(env_k, raising=False)

    placeholder_settings = Settings(
        _env_file=None,
        GEMINI_API_KEY=SecretStr("your_gemini_api_key_here"),
        OPENAI_API_KEY=SecretStr("your_openai_api_key_here"),
        OCR_API_KEY=SecretStr("your_google_cloud_vision_api_key_here"),
    )
    assert placeholder_settings.get_api_key("google") is None
    assert placeholder_settings.get_api_key("gemini") is None
    assert placeholder_settings.get_api_key("openai") is None
    assert placeholder_settings.get_api_key("ocr") is None


def test_llm_config_parsing_and_merging():
    """Test LLMConfig parsing, agent querying, and deep merging."""
    llm_cfg = get_default_llm_config()
    assert isinstance(llm_cfg, LLMConfig)
    assert llm_cfg.planner.provider in ("google", "openai", "openrouter", "xai", "vertexai")
    assert llm_cfg.get_agent("planner") is not None
    assert llm_cfg.get_utils("hopper") is not None

    # Deep merge overrides
    overrides = {
        "planner": {
            "model": "custom-model-v1",
            "temperature": 0.7,
        }
    }
    merged = deep_merge_llm_config(llm_cfg, overrides)
    assert merged.planner.model == "custom-model-v1"
    assert merged.planner.temperature == 0.7


def test_agent_config_loading():
    """Test AgentGlobalConfig parsing from agent_config.json / artemis.jsonc."""
    agent_cfg = load_agent_config()
    assert isinstance(agent_cfg, AgentGlobalConfig)
    # The per-agent override ships empty so the profile knobs decide; caching
    # ships unset so each tier applies its own default (off pro, on ultra).
    assert agent_cfg.explorer_versions == {}
    assert agent_cfg.explorer.default_version == "flash"
    assert agent_cfg.explorer.flash_mode == "flash"
    assert agent_cfg.explorer.pro_mode == "flash"
    assert agent_cfg.explorer.caching is None
    assert "explorer" in agent_cfg.denylisted_tools
    assert agent_cfg.video_analyzer.enable_ledger is True
    assert agent_cfg.planner_validation.enabled is True
    assert agent_cfg.committee.enabled is False
    assert agent_cfg.committee.debate_rounds == 2
    assert agent_cfg.checker.enabled is True
    assert agent_cfg.checker.max_iterations == 20
    assert agent_cfg.checker.midway_checks is False
    assert agent_cfg.checker.final_check is True
    assert agent_cfg.checker.assert_failure_policy == "continue"
    assert agent_cfg.checker.device_probes is True
    assert agent_cfg.outputter.enabled is True
    assert agent_cfg.outputter.force_synthesis is False
    assert agent_cfg.flash.max_turns == 0
    assert agent_cfg.flash.explorer_mode == "flash"
    assert agent_cfg.pro.explorer.mode == "flash"
    assert agent_cfg.pro.checker.enabled is True
    assert agent_cfg.pro.committee.enabled is False
    assert agent_cfg.pro.planner_validation.enabled is True
    assert agent_cfg.pro.video_analyzer.enable_ledger is True


def test_output_config_and_recording():
    """Test OutputConfig behavior and event recording."""
    cfg = OutputConfig(output_description="Output a valid JSON summary")
    assert cfg.needs_structured_format() is True

    cfg_disabled = OutputConfig(output_description="Output JSON", enable_outputter=False)
    assert cfg_disabled.needs_structured_format() is False

    cfg_forced = OutputConfig(force_synthesis=True)
    assert cfg_forced.needs_structured_format() is True

    with tempfile.TemporaryDirectory() as tmpdir:
        output_file = Path(tmpdir) / "events.json"
        events_data = [{"event": "test_event", "status": "ok"}]
        record_events(output_file, events_data)
        assert output_file.exists()
        assert "test_event" in output_file.read_text(encoding="utf-8")


def test_runtime_state_and_ipc(tmp_path, monkeypatch):
    """Test IPC port and LS address state helpers."""
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", MagicMock(side_effect=Exception("offline")))
    ipc_state_file = tmp_path / ".artemis_ipc_port"
    monkeypatch.setattr("artemis.config.runtime.get_ipc_port_file", lambda: ipc_state_file)
    monkeypatch.setattr("artemis.config.runtime.ROOT_DIR", tmp_path)
    monkeypatch.setattr("artemis.config.runtime.get_app_dir", lambda: tmp_path)
    # Test IPC port
    write_ipc_port(49152)
    assert read_ipc_port() == 49152
    clear_ipc_port()
    assert read_ipc_port() is None

    # Test LS address
    write_ls_address("localhost:12345")
    assert read_ls_address() == "localhost:12345"
    clear_ls_address()

    # Test temp dir cleanup
    test_temp_sub = get_temp_dir("test_cleanup_dir")
    test_temp_file = test_temp_sub / "temp_marker.txt"
    test_temp_file.write_text("temporary data")
    assert test_temp_file.exists()

    deleted = cleanup_temp_dir("test_cleanup_dir")
    assert deleted >= 1
    assert not test_temp_file.exists()


def test_planner_validation_builder_and_milestones():
    """Test AgentConfigBuilder methods for planner validation and milestone drift detection."""
    from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder
    from artemis.utils.plan_grammar import milestones_changed, parse_plan

    # Default builder inherits from artemis.jsonc (enabled=True)
    builder = AgentConfigBuilder()
    cfg = builder.build()
    assert cfg.disable_planner_validation is False
    # The dead similarity-threshold knob is gone: validation has no tunable
    # trigger, every milestone text change is reviewed.
    assert not hasattr(cfg, "planner_validation_threshold")

    # Fluent enabling
    cfg_enabled = AgentConfigBuilder().with_planner_validation(enabled=True).build()
    assert cfg_enabled.disable_planner_validation is False

    # Fluent disabling
    cfg_disabled = AgentConfigBuilder().with_disable_planner_validation(True).build()
    assert cfg_disabled.disable_planner_validation is True

    # Milestone drift detection is threshold-free: any text change counts,
    # status-only flips never do (see artemis.utils.plan_grammar)
    before = parse_plan("- [ ] Tap Login button\n- [ ] Enter password")
    after_minor = parse_plan("- [ ] Tap the Login button\n- [ ] Enter password")
    after_status = parse_plan("- [x] Tap Login button\n- [/] Enter password")
    assert milestones_changed(before, after_minor) is True
    assert milestones_changed(before, after_status) is False


@pytest.mark.asyncio
async def test_committee_builder_and_graph_mounting():
    """Test AgentConfigBuilder committee methods and graph mounting."""
    from artemis.context import ArtemisContext, DeviceContext, DevicePlatform, ExecutionSetup
    from artemis.graph.graph import get_graph
    from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder

    # Default: disabled
    cfg_default = AgentConfigBuilder().build()
    assert cfg_default.enable_committee is False
    assert cfg_default.committee_debate_rounds == 2

    # Enabled via builder
    cfg_enabled = AgentConfigBuilder().with_committee(enabled=True, debate_rounds=3).build()
    assert cfg_enabled.enable_committee is True
    assert cfg_enabled.committee_debate_rounds == 3

    # Test mounting in graph
    device = DeviceContext(
        host_platform="LINUX",
        mobile_platform=DevicePlatform.ANDROID,
        device_id="dummy",
        device_width=1080,
        device_height=2400,
    )
    ctx_disabled = ArtemisContext(
        device=device,
        execution_setup=ExecutionSetup(enable_committee=False),
    )
    graph_disabled = await get_graph(ctx_disabled)
    op_node_disabled = graph_disabled.nodes.get("operator")
    assert op_node_disabled is not None
    # Verify ask_committee is not mounted when disabled
    op_tools_disabled = [t.name for t in op_node_disabled.bound.afunc.tools]
    assert "ask_committee" not in op_tools_disabled

    ctx_enabled = ArtemisContext(
        device=device,
        execution_setup=ExecutionSetup(enable_committee=True, committee_debate_rounds=3),
    )
    graph_enabled = await get_graph(ctx_enabled)
    op_node_enabled = graph_enabled.nodes.get("operator")
    assert op_node_enabled is not None
    # ask_committee is deliberately never mounted on the Operator (it sits outside
    # the pre-decision / turn-ending tool contract), even when the flag is on.
    op_tools_enabled = [t.name for t in op_node_enabled.bound.afunc.tools]
    assert "ask_committee" not in op_tools_enabled


def test_checker_builder_and_context_propagation():
    """Test AgentConfigBuilder methods and context propagation for checker."""
    from unittest.mock import MagicMock
    from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
    from artemis.sdk.agent import Agent
    from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder

    # Default builder inherits from artemis.jsonc (enabled=True, midway off, final on)
    builder = AgentConfigBuilder()
    cfg = builder.build()
    assert cfg.disable_checker is False
    assert cfg.checker_max_iterations == 20
    assert cfg.disable_midway_checks is True
    assert cfg.disable_final_check is False

    # Fluent enabling with the new two-gate knobs
    cfg_enabled = (
        AgentConfigBuilder()
        .with_checker(
            enabled=True,
            max_iterations=25,
            midway_checks=True,
            final_check=False,
            checkpoint_max_repairs=1,
            assert_failure_policy="halt",
            device_probes=False,
        )
        .build()
    )
    assert cfg_enabled.disable_checker is False
    assert cfg_enabled.checker_max_iterations == 25
    assert cfg_enabled.disable_midway_checks is False
    assert cfg_enabled.disable_final_check is True
    assert cfg_enabled.checkpoint_max_repairs == 1
    assert cfg_enabled.assert_failure_policy == "halt"
    assert cfg_enabled.disable_device_probes is True

    # Fluent disabling
    cfg_disabled = AgentConfigBuilder().with_disable_checker(True).build()
    assert cfg_disabled.disable_checker is True

    # Test propagation to ExecutionSetup via Agent._prepare_tracing
    agent = Agent(config=cfg_enabled)
    device = DeviceContext(
        host_platform="LINUX",
        mobile_platform=DevicePlatform.ANDROID,
        device_id="dummy",
        device_width=1080,
        device_height=2400,
    )
    ctx = ArtemisContext(device=device)
    mock_task = MagicMock()
    mock_task.get_name.return_value = "test_task"
    mock_task.request.record_trace = False
    mock_task.request.name = "test_task"
    mock_task.request.profile = None
    with patch("artemis.sdk.agent.DataEngine"):
        agent._prepare_tracing(mock_task, ctx)

    assert ctx.execution_setup is not None
    assert ctx.execution_setup.disable_checker is False
    assert ctx.execution_setup.checker_max_iterations == 25
    assert ctx.execution_setup.disable_final_check is True
    assert ctx.execution_setup.checkpoint_max_repairs == 1
    assert ctx.execution_setup.assert_failure_policy == "halt"
    assert ctx.execution_setup.disable_device_probes is True
    # Effective gate semantics: master alias off + individual gates
    assert ctx.execution_setup.midway_checks_enabled is True
    assert ctx.execution_setup.final_check_enabled is False


def test_factory_default_verification_layering():
    """Contract: out of the box, the verification stack is layered as
    final check ON / planner validation (ratchet) ON / midway checks OFF."""
    from artemis.config.agent import AgentGlobalConfig, CheckerConfig, PlannerValidationConfig
    from artemis.context import ExecutionSetup
    from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder

    # Config-model factory defaults
    assert CheckerConfig().enabled is True
    assert CheckerConfig().midway_checks is False
    assert CheckerConfig().final_check is True
    assert PlannerValidationConfig().enabled is True

    # Bare ExecutionSetup carries the same layering, including the effective
    # gate semantics (master on + midway off => final review runs, no midway
    # checkpoint ever spawns).
    setup = ExecutionSetup()
    assert setup.disable_checker is False
    assert setup.disable_midway_checks is True
    assert setup.disable_final_check is False
    assert setup.disable_planner_validation is False
    assert setup.final_check_enabled is True
    assert setup.midway_checks_enabled is False
    assert setup.checks_enabled is True

    # Builder default (fed by config/artemis.jsonc) agrees
    cfg = AgentConfigBuilder().build()
    assert cfg.disable_checker is False
    assert cfg.disable_midway_checks is True
    assert cfg.disable_final_check is False
    assert cfg.disable_planner_validation is False

    # Shipped artemis.jsonc agrees with the model factory defaults
    agent_cfg = load_agent_config()
    assert agent_cfg.checker.enabled is True
    assert agent_cfg.checker.midway_checks is False
    assert agent_cfg.checker.final_check is True
    assert agent_cfg.planner_validation.enabled is True
    assert AgentGlobalConfig().checker.enabled is True


def test_explorer_builder_and_resolution(monkeypatch):
    """Test AgentConfigBuilder explorer methods and multi-tier resolution logic."""
    from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
    from artemis.sdk.agent import Agent
    from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder
    from artemis.tools.explorer_tool import resolve_explorer_version

    monkeypatch.delenv("ARTEMIS_EXPLORER_VERSION", raising=False)

    # Default builder inherits from artemis.jsonc (default="flash", flash_mode="flash",
    # pro_mode="flash", caching unset, no per-agent override).
    builder = AgentConfigBuilder()
    cfg = builder.build()
    assert cfg.explorer.default_version == "flash"
    assert cfg.explorer.flash_mode == "flash"
    assert cfg.explorer.pro_mode == "flash"
    assert cfg.explorer.caching is None
    assert cfg.explorer_versions == {}

    # With the shipped (empty) override the profile knobs actually win: the
    # Pro agents follow pro_mode and the Flash runner follows flash_mode.
    cfg_pro_ultra = AgentConfigBuilder().with_explorer(pro_mode="ultra").build()
    assert cfg_pro_ultra.get_explorer_version(agent_name="operator") == "ultra"
    assert cfg_pro_ultra.get_explorer_version(agent_name="validator") == "ultra"
    assert cfg_pro_ultra.get_explorer_version(agent_name="flash") == "flash"
    cfg_flash_pro = AgentConfigBuilder().with_explorer(flash_mode="pro").build()
    assert cfg_flash_pro.get_explorer_version(agent_name="flash") == "pro"
    assert cfg_flash_pro.get_explorer_version(agent_name="operator") == "flash"

    # Fluent configuration with with_explorer
    cfg_custom = (
        AgentConfigBuilder()
        .with_explorer(
            version="pro",
            flash_mode="flash",
            pro_mode="ultra",
            caching=False,
            versions={"operator": "ultra", "validator": "pro"},
        )
        .build()
    )
    assert cfg_custom.explorer.default_version == "pro"
    assert cfg_custom.explorer.flash_mode == "flash"
    assert cfg_custom.explorer.pro_mode == "ultra"
    assert cfg_custom.explorer.caching is False
    assert cfg_custom.explorer_versions["operator"] == "ultra"
    assert cfg_custom.explorer_versions["validator"] == "pro"

    # Fluent configuration with with_explorer_version shorthand
    cfg_shorthand = AgentConfigBuilder().with_explorer_version("ultra").build()
    assert cfg_shorthand.explorer.default_version == "ultra"

    # Context setup for resolution tests
    device = DeviceContext(
        host_platform="LINUX",
        mobile_platform=DevicePlatform.ANDROID,
        device_id="dummy",
        device_width=1080,
        device_height=2400,
    )
    ctx = ArtemisContext(device=device, agent_config=cfg_custom)

    # 1. Explicit override takes highest priority
    assert resolve_explorer_version(ctx, explicit_version="flash") == "flash"
    assert resolve_explorer_version(ctx, explicit_version="pro") == "pro"
    assert resolve_explorer_version(ctx, explicit_version="ultra") == "ultra"

    # 2. Environment variable override
    monkeypatch.setenv("ARTEMIS_EXPLORER_VERSION", "ultra")
    assert resolve_explorer_version(ctx) == "ultra"
    monkeypatch.delenv("ARTEMIS_EXPLORER_VERSION", raising=False)

    # 3. Per-agent mapping in explorer_versions
    assert resolve_explorer_version(ctx, agent_or_profile_name="operator") == "ultra"
    assert resolve_explorer_version(ctx, agent_or_profile_name="validator") == "pro"

    # 4. Pro profile vs Flash profile resolution
    cfg_profiled = (
        AgentConfigBuilder()
        .with_explorer(flash_mode="flash", pro_mode="pro", default_version="flash", versions={})
        .build()
    )
    ctx_profiled = ArtemisContext(device=device, agent_config=cfg_profiled)
    assert resolve_explorer_version(ctx_profiled, agent_or_profile_name="flash") == "flash"
    assert resolve_explorer_version(ctx_profiled, agent_or_profile_name="flash_runner") == "flash"
    assert resolve_explorer_version(ctx_profiled, agent_or_profile_name="pro") == "pro"
    assert resolve_explorer_version(ctx_profiled, agent_or_profile_name="operator") == "pro"

    # 5. Propagation to ExecutionSetup via Agent._prepare_tracing
    agent = Agent(config=cfg_custom)
    mock_task = MagicMock()
    mock_task.get_name.return_value = "explorer_test_task"
    mock_task.request.record_trace = False
    mock_task.request.name = "explorer_test_task"
    mock_task.request.profile = None
    with patch("artemis.sdk.agent.DataEngine"):
        agent._prepare_tracing(mock_task, ctx)

    assert ctx.execution_setup is not None
    assert ctx.execution_setup.explorer_version == "pro"
    assert ctx.execution_setup.explorer_flash_mode == "flash"
    assert ctx.execution_setup.explorer_pro_mode == "ultra"
    assert ctx.execution_setup.explorer_caching is False


def test_outputter_builder_and_context_propagation():
    """Test AgentConfigBuilder outputter methods and propagation to ExecutionSetup."""
    from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
    from artemis.sdk.agent import Agent
    from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder

    # Default builder inherits from artemis.jsonc (enabled=True, force_synthesis=False)
    builder = AgentConfigBuilder()
    cfg = builder.build()
    assert cfg.disable_outputter is False
    assert cfg.outputter.enabled is True
    assert cfg.outputter.force_synthesis is False

    # Fluent configuration with with_outputter
    cfg_custom = AgentConfigBuilder().with_outputter(enabled=True, force_synthesis=True).build()
    assert cfg_custom.disable_outputter is False
    assert cfg_custom.outputter.enabled is True
    assert cfg_custom.outputter.force_synthesis is True

    # Fluent disabling with with_disable_outputter
    cfg_disabled = AgentConfigBuilder().with_disable_outputter(True).build()
    assert cfg_disabled.disable_outputter is True
    assert cfg_disabled.outputter.enabled is False

    # Test propagation to ExecutionSetup via Agent._prepare_tracing
    device = DeviceContext(
        host_platform="LINUX",
        mobile_platform=DevicePlatform.ANDROID,
        device_id="dummy",
        device_width=1080,
        device_height=2400,
    )
    ctx = ArtemisContext(device=device)
    agent = Agent(config=cfg_custom)
    mock_task = MagicMock()
    mock_task.get_name.return_value = "outputter_test_task"
    mock_task.request.record_trace = False
    mock_task.request.name = "outputter_test_task"
    mock_task.request.profile = None
    mock_task.request.goal = "Test outputter propagation"
    with patch("artemis.sdk.agent.DataEngine"):
        agent._prepare_tracing(mock_task, ctx)

    assert ctx.execution_setup is not None
    assert ctx.execution_setup.disable_outputter is False
    assert ctx.execution_setup.outputter.enabled is True
    assert ctx.execution_setup.outputter.force_synthesis is True


def test_categorized_flash_and_pro_profile_builders():
    """Test with_flash_config and with_pro_config fluent builders and bidirectional sync."""
    from artemis.config.agent import AgentGlobalConfig
    from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder

    # 1. Test with_flash_config
    cfg_flash = AgentConfigBuilder().with_flash_config(max_turns=15, explorer_mode="flash").build()
    assert cfg_flash.flash.max_turns == 15
    assert cfg_flash.flash.explorer_mode == "flash"
    assert cfg_flash.explorer.flash_mode == "flash"

    # 2. Test with_pro_config
    cfg_pro = (
        AgentConfigBuilder()
        .with_pro_config(
            explorer_mode="ultra",
            planner_validation=True,
            committee=True,
            checker=True,
            video_ledger=False,
        )
        .build()
    )
    assert cfg_pro.pro.explorer.mode == "ultra"
    assert cfg_pro.explorer.pro_mode == "ultra"
    assert cfg_pro.disable_planner_validation is False
    assert cfg_pro.enable_committee is True
    assert cfg_pro.disable_checker is False
    assert cfg_pro.enable_video_ledger is False

    # 3. Test AgentGlobalConfig bidirectional synchronization
    raw_dict = {
        "flash": {"max_turns": 25, "explorer_mode": "flash"},
        "pro": {
            "explorer": {"mode": "pro", "caching": False},
            "checker": {"enabled": True, "max_iterations": 10},
            "committee": {"enabled": True, "debate_rounds": 4},
        },
    }
    synced = AgentGlobalConfig.model_validate(raw_dict)
    assert synced.flash.max_turns == 25
    assert synced.explorer.flash_mode == "flash"
    assert synced.explorer.pro_mode == "pro"
    assert synced.explorer.caching is False
    assert synced.checker.enabled is True
    assert synced.checker.max_iterations == 10
    assert synced.committee.enabled is True
    assert synced.committee.debate_rounds == 4


def test_agent_config_environment_variable_overrides(monkeypatch):
    """Test environment variable overrides for AgentGlobalConfig switches."""
    from artemis.config.agent import load_agent_config

    monkeypatch.setenv("ARTEMIS_CHECKER_ENABLED", "true")
    monkeypatch.setenv("ARTEMIS_COMMITTEE_ENABLED", "1")
    monkeypatch.setenv("ARTEMIS_PLANNER_VALIDATION_ENABLED", "yes")
    monkeypatch.setenv("ARTEMIS_OUTPUTTER_ENABLED", "false")
    monkeypatch.setenv("ARTEMIS_VIDEO_LEDGER_ENABLED", "0")

    cfg = load_agent_config()
    assert cfg.checker.enabled is True
    assert cfg.pro.checker.enabled is True
    assert cfg.committee.enabled is True
    assert cfg.pro.committee.enabled is True
    assert cfg.planner_validation.enabled is True
    assert cfg.pro.planner_validation.enabled is True
    assert cfg.outputter.enabled is False
    assert cfg.video_analyzer.enable_ledger is False
    assert cfg.pro.video_analyzer.enable_ledger is False


def test_consolidated_workspace_and_admin_paths():
    """Verify consolidated workspace and admin paths exported from artemis.config."""
    from artemis.config import (
        DB_PATH,
        IMAGES_DIR,
        PAUSE_FILE,
        REPLAY_BASE_DIR,
        ROOT_DIR,
        TEST_DATA_DIR,
        TEST_OUTPUTS_DIR,
        TRACES_PATH,
        WORKSPACE_ROOT,
        get_images_dir,
        get_pause_file,
        get_replay_dir,
        get_test_data_dir,
        get_test_outputs_dir,
    )

    assert WORKSPACE_ROOT == ROOT_DIR
    assert PAUSE_FILE == get_pause_file()
    assert PAUSE_FILE.name == ".artemis_paused"
    assert REPLAY_BASE_DIR == get_replay_dir()
    assert TEST_DATA_DIR == get_test_data_dir()
    assert TEST_OUTPUTS_DIR == get_test_outputs_dir()
    assert IMAGES_DIR == get_images_dir()
    assert DB_PATH.name == "data_engine.db"
    assert TRACES_PATH.name == "traces"


def test_admin_console_config_facade_backward_compatibility():
    """Verify apps.admin_console.core.config continues to re-export consolidated paths."""
    from apps.admin_console.core.config import (
        DB_PATH,
        IMAGES_DIR,
        PAUSE_FILE,
        REPLAY_BASE_DIR,
        TEST_DATA_DIR,
        TEST_OUTPUTS_DIR,
        TRACES_PATH,
        WORKSPACE_ROOT,
        init_ls_address,
    )
    import artemis.config as ac

    assert WORKSPACE_ROOT == ac.WORKSPACE_ROOT
    assert PAUSE_FILE == ac.PAUSE_FILE
    assert REPLAY_BASE_DIR == ac.REPLAY_BASE_DIR
    assert TEST_DATA_DIR == ac.TEST_DATA_DIR
    assert TEST_OUTPUTS_DIR == ac.TEST_OUTPUTS_DIR
    assert IMAGES_DIR == ac.IMAGES_DIR
    assert DB_PATH == ac.DB_PATH
    assert TRACES_PATH == ac.TRACES_PATH
    assert callable(init_ls_address)


class TestVerificationLevelPresets:
    """The coarse Checker presets behind ``--verification-level``."""

    def test_every_preset_is_a_valid_checker_override(self):
        from artemis.config import (
            DEFAULT_VERIFICATION_LEVEL,
            VERIFICATION_LEVEL_PRESETS,
            CheckerConfig,
            checker_overrides_for_level,
        )

        assert DEFAULT_VERIFICATION_LEVEL in VERIFICATION_LEVEL_PRESETS
        for level in VERIFICATION_LEVEL_PRESETS:
            cfg = CheckerConfig(**checker_overrides_for_level(level))
            assert isinstance(cfg, CheckerConfig)

    def test_presets_form_a_monotonic_ladder(self):
        from artemis.config import CheckerConfig, checker_overrides_for_level

        off = CheckerConfig(**checker_overrides_for_level("off"))
        final = CheckerConfig(**checker_overrides_for_level("final"))
        checkpoints = CheckerConfig(**checker_overrides_for_level("checkpoints"))
        strict = CheckerConfig(**checker_overrides_for_level("strict"))

        assert off.enabled is False
        assert final.enabled and final.final_check and not final.midway_checks
        # ``final`` is the factory layering: identical to a default CheckerConfig.
        assert final == CheckerConfig()
        assert checkpoints.midway_checks and checkpoints.final_check
        assert checkpoints.assert_failure_policy == "continue"
        assert strict.midway_checks and strict.final_check
        assert strict.assert_failure_policy == "halt"
        assert strict.checkpoint_max_repairs > checkpoints.checkpoint_max_repairs
        assert strict.final_check_max_attempts > checkpoints.final_check_max_attempts
        assert strict.max_iterations > checkpoints.max_iterations

    def test_level_lookup_is_case_and_whitespace_insensitive(self):
        from artemis.config import checker_overrides_for_level

        assert checker_overrides_for_level(" STRICT ") == checker_overrides_for_level("strict")
        # A copy is returned so callers cannot mutate the shared preset table.
        overrides = checker_overrides_for_level("off")
        overrides["enabled"] = True
        assert checker_overrides_for_level("off")["enabled"] is False

    @pytest.mark.parametrize("bad", [None, "", "maximum", "ultra"])
    def test_unknown_level_raises(self, bad):
        from artemis.config import checker_overrides_for_level

        with pytest.raises(ValueError, match="Unknown verification level"):
            checker_overrides_for_level(bad)

    def test_level_preset_survives_explicit_master_switch(self):
        """``--verification-level strict --disable-checker`` keeps the master switch off."""
        from artemis.config import checker_overrides_for_level
        from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder

        builder = AgentConfigBuilder()
        builder.with_checker(**checker_overrides_for_level("strict"))
        assert builder._disable_checker is False
        assert builder._disable_midway_checks is False
        assert builder._assert_failure_policy == "halt"
        builder.with_checker(enabled=False)
        assert builder._disable_checker is True
        # The preset's finer-grained fields survive the master switch flip.
        assert builder._assert_failure_policy == "halt"

    @pytest.mark.parametrize("level", ["off", "final", "checkpoints", "strict"])
    def test_effective_config_classifies_back_onto_the_ladder(self, level):
        from artemis.config import (
            CheckerConfig,
            checker_overrides_for_level,
            verification_level_for_checker,
        )

        cfg = CheckerConfig(**checker_overrides_for_level(level))
        assert verification_level_for_checker(cfg) == level
        # A config with both gates switched off is "off" even if enabled=True.
        assert (
            verification_level_for_checker(
                CheckerConfig(enabled=True, midway_checks=False, final_check=False)
            )
            == "off"
        )


class TestRunTuningSummary:
    """``run_tuning_for_profile``: the per-run tuning persisted with a session."""

    def test_flash_has_no_tuning(self):
        from artemis.config import CheckerConfig, ExplorerConfig, run_tuning_for_profile

        kwargs = {"checker": CheckerConfig(), "explorer": ExplorerConfig()}
        assert run_tuning_for_profile("flash", **kwargs) is None
        assert run_tuning_for_profile(None, **kwargs) is None

    def test_pro_reports_both_sliders(self):
        from artemis.config import (
            CheckerConfig,
            ExplorerConfig,
            checker_overrides_for_level,
            run_tuning_for_profile,
        )

        summary = run_tuning_for_profile(
            " Pro ",
            checker=CheckerConfig(**checker_overrides_for_level("strict")),
            explorer=ExplorerConfig(pro_mode="ultra"),
        )
        assert summary == {"verification_level": "strict", "explorer_mode": "ultra"}

    def test_pro_defaults_match_launcher_defaults(self):
        from artemis.config import (
            CheckerConfig,
            ExplorerConfig,
            run_tuning_for_profile,
            verification_level_for_checker,
        )

        checker, explorer = CheckerConfig(), ExplorerConfig()
        summary = run_tuning_for_profile("pro", checker=checker, explorer=explorer)
        assert summary["verification_level"] == verification_level_for_checker(checker)
        assert summary["explorer_mode"] == explorer.resolve(profile="pro")
