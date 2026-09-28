"""Regression coverage for Pro report configuration and UI report persistence."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from artemis.config import OutputConfig
from artemis.sdk.agent import Agent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configured_enabled,configured_force,enabled,force,expected_enabled,expected_force",
    [
        (True, True, True, None, True, True),
        (True, True, True, False, True, False),
        (True, True, False, None, False, True),
        (False, False, None, True, False, True),
    ],
)
async def test_cli_preserves_unspecified_outputter_settings(
    monkeypatch,
    configured_enabled,
    configured_force,
    enabled,
    force,
    expected_enabled,
    expected_force,
):
    import artemis.interfaces.cli.commands.run as cli

    builder = MagicMock()
    agent = MagicMock()
    agent.init = AsyncMock()
    agent.run_task = AsyncMock()
    agent.clean = AsyncMock()
    monkeypatch.setattr(cli, "initialize_llm_config", MagicMock())
    monkeypatch.setattr(cli, "AgentProfile", MagicMock())
    builders = MagicMock()
    builders.AgentConfig.with_default_profile.return_value = builder
    monkeypatch.setattr(cli, "Builders", builders)
    monkeypatch.setattr(cli, "Agent", MagicMock(return_value=agent))
    monkeypatch.setattr(cli, "publish_startup_progress", MagicMock())
    monkeypatch.setattr(
        cli,
        "load_agent_config",
        lambda: SimpleNamespace(
            outputter=SimpleNamespace(enabled=configured_enabled, force_synthesis=configured_force)
        ),
    )
    monkeypatch.setenv("ARTEMIS_TASK_INGRESS", "test")

    await cli.execute_task(
        "Test goal",
        device_serial="mock-device",
        profile="pro",
        enable_outputter=enabled,
        force_output_synthesis=force,
    )

    builder.with_outputter.assert_called_once_with(
        enabled=expected_enabled,
        force_synthesis=expected_force,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["# 测试报告\n通过", {"结论": "通过"}])
async def test_pro_synthesis_saves_ui_report_without_output_description(
    monkeypatch, tmp_path, result
):
    import artemis.sdk.agent as sdk

    synthesize = AsyncMock(return_value=result)
    monkeypatch.setattr(sdk, "outputter", synthesize)
    ctx = SimpleNamespace(
        execution_setup=SimpleNamespace(
            outputter=SimpleNamespace(enabled=True, force_synthesis=True),
            disable_outputter=False,
        ),
        data_engine=SimpleNamespace(base_dir=tmp_path),
    )
    request = SimpleNamespace(llm_output_path=None, output_format=None)
    output = await Agent._extract_output(
        object.__new__(Agent),
        "test",
        ctx,
        request,
        OutputConfig(),
        SimpleNamespace(),
    )

    assert output == result
    synthesize.assert_awaited_once()
    report = (tmp_path / "notes" / "output.md").read_text(encoding="utf-8")
    assert "通过" in report
    if isinstance(result, str):
        assert report == result
    else:
        import json

        assert json.loads(report.removeprefix("```json\n").removesuffix("\n```")) == result


@pytest.mark.asyncio
async def test_explicit_disable_does_not_generate_report(monkeypatch, tmp_path):
    import artemis.sdk.agent as sdk

    synthesize = AsyncMock()
    monkeypatch.setattr(sdk, "outputter", synthesize)
    ctx = SimpleNamespace(
        execution_setup=SimpleNamespace(
            outputter=SimpleNamespace(enabled=True, force_synthesis=True),
            disable_outputter=False,
        ),
        data_engine=SimpleNamespace(base_dir=tmp_path),
    )
    output = await Agent._extract_output(
        object.__new__(Agent),
        "test",
        ctx,
        SimpleNamespace(llm_output_path=None, output_format=None),
        OutputConfig(enable_outputter=False),
        SimpleNamespace(),
    )

    assert output is None
    synthesize.assert_not_awaited()
    assert not (tmp_path / "notes" / "output.md").exists()
