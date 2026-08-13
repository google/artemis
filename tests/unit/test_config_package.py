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
from unittest.mock import MagicMock
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
    assert "operator" in agent_cfg.explorer_versions
    assert agent_cfg.explorer.default_version == "flash"
    assert agent_cfg.explorer.flash_mode == "flash"
    assert agent_cfg.explorer.pro_mode == "flash"
    assert agent_cfg.explorer.caching is True
    assert "explorer" in agent_cfg.denylisted_tools
    assert agent_cfg.video_analyzer.enable_ledger is True
    assert agent_cfg.planner_validation.enabled is False
    assert agent_cfg.planner_validation.similarity_threshold == 0.85
    assert agent_cfg.committee.enabled is False
    assert agent_cfg.committee.debate_rounds == 2
    assert agent_cfg.checker.enabled is False
    assert agent_cfg.checker.max_iterations == 20
    assert agent_cfg.checker.max_chat_rounds == 4
    assert agent_cfg.outputter.enabled is True
    assert agent_cfg.outputter.force_synthesis is False
    assert agent_cfg.flash.max_turns == 30
    assert agent_cfg.flash.explorer_mode == "flash"
    assert agent_cfg.pro.explorer.mode == "flash"
    assert agent_cfg.pro.checker.enabled is False
    assert agent_cfg.pro.committee.enabled is False
    assert agent_cfg.pro.planner_validation.enabled is False
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


def test_runtime_state_and_ipc():
    """Test IPC port and LS address state helpers."""
    # Test IPC port
    write_ipc_port(49152)
    assert read_ipc_port() == 49152
    clear_ipc_port()

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
    """Test AgentConfigBuilder methods for planner validation and validate_milestones threshold."""
    from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder
    from artemis.graph.graph import validate_milestones

    # Default builder inherits from artemis.jsonc (enabled=False, similarity_threshold=0.85)
    builder = AgentConfigBuilder()
    cfg = builder.build()
    assert cfg.disable_planner_validation is True
    assert cfg.planner_validation_threshold == 0.85

    # Fluent enabling
    cfg_enabled = (
        AgentConfigBuilder()
        .with_planner_validation(enabled=True, similarity_threshold=0.90)
        .build()
    )
    assert cfg_enabled.disable_planner_validation is False
    assert cfg_enabled.planner_validation_threshold == 0.90

    # Fluent disabling
    cfg_disabled = AgentConfigBuilder().with_disable_planner_validation(True).build()
    assert cfg_disabled.disable_planner_validation is True

    # Test validate_milestones with custom thresholds
    before = "- [ ] Tap Login button\n- [ ] Enter password"
    after_minor = "- [ ] Tap the Login button\n- [ ] Enter password"
    # Minor edit has ~0.94 similarity
    assert validate_milestones(before, after_minor, similarity_threshold=0.85) is False
    assert validate_milestones(before, after_minor, similarity_threshold=0.98) is True


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
    # Verify ask_committee is mounted when enabled
    op_tools_enabled = [t.name for t in op_node_enabled.bound.afunc.tools]
    assert "ask_committee" in op_tools_enabled


def test_checker_builder_and_context_propagation():
    """Test AgentConfigBuilder methods and context propagation for checker."""
    from unittest.mock import MagicMock
    from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
    from artemis.sdk.agent import Agent
    from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder

    # Default builder inherits from artemis.jsonc (enabled=False, max_iterations=20, max_chat_rounds=4)
    builder = AgentConfigBuilder()
    cfg = builder.build()
    assert cfg.disable_checker is True
    assert cfg.checker_max_iterations == 20
    assert cfg.checker_max_chat_rounds == 4

    # Fluent enabling
    cfg_enabled = (
        AgentConfigBuilder()
        .with_checker(enabled=True, max_iterations=25, max_chat_rounds=5)
        .build()
    )
    assert cfg_enabled.disable_checker is False
    assert cfg_enabled.checker_max_iterations == 25
    assert cfg_enabled.checker_max_chat_rounds == 5

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
    mock_task.request.goal = "Test goal"
    agent._prepare_tracing(mock_task, ctx)

    assert ctx.execution_setup is not None
    assert ctx.execution_setup.disable_checker is False
    assert ctx.execution_setup.checker_max_iterations == 25
    assert ctx.execution_setup.checker_max_chat_rounds == 5


def test_explorer_builder_and_resolution(monkeypatch):
    """Test AgentConfigBuilder explorer methods and multi-tier resolution logic."""
    from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
    from artemis.sdk.agent import Agent
    from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder
    from artemis.tools.explorer_tool import resolve_explorer_version

    # Default builder inherits from artemis.jsonc (default="flash", flash_mode="flash", pro_mode="flash", caching=True)
    builder = AgentConfigBuilder()
    cfg = builder.build()
    assert cfg.explorer.default_version == "flash"
    assert cfg.explorer.flash_mode == "flash"
    assert cfg.explorer.pro_mode == "flash"
    assert cfg.explorer.caching is True

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
    mock_task.request.goal = "Test explorer propagation"
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
