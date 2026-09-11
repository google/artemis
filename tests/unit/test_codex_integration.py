"""Offline coverage for the optional Codex integration; no login or phone needed."""

import asyncio
import base64
from io import BytesIO
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from PIL import Image
import pytest

from artemis.integrations.codex.client import CodexClient, CodexError
from artemis.integrations.codex.device import DeviceTools
from artemis.integrations.codex.runner import drive_turn, run_task, start_thread


@pytest.fixture
def device_tools(tmp_path):
    buffer = BytesIO()
    Image.new("RGB", (100, 200)).save(buffer, "PNG")
    screen = Mock()
    screen.get_screen_data.return_value = SimpleNamespace(
        base64=base64.b64encode(buffer.getvalue()).decode(), hierarchy_xml="<hierarchy/>"
    )
    screen.tap.return_value = True
    return DeviceTools(screen, tmp_path, max_actions=2)


def test_observation_and_consumed_coordinates(device_tools):
    tools = device_tools
    with pytest.raises(ValueError, match="stale"):
        tools.call("android_tap", {"observation": 1, "x": 1, "y": 2})
    observation = tools.call("android_observe", {})
    assert observation["contentItems"][1]["imageUrl"].startswith("data:image/png;base64,")
    assert (tools.directory / "observation-0001.png").is_file()
    tools.call("android_tap", {"observation": 1, "x": 1, "y": 2})
    tools.screen.tap.assert_called_once_with(1, 2)
    with pytest.raises(ValueError, match="stale"):
        tools.call("android_tap", {"observation": 1, "x": 1, "y": 2})


@pytest.mark.parametrize(
    "arguments",
    [
        {"observation": 1, "x": 100, "y": 10},
        {"observation": 1, "x": -1, "y": 10},
        {"observation": 1, "x": "1; reboot", "y": 10},
        {"observation": 1, "x": True, "y": 10},
        {"observation": 1, "x": 10, "y": 10, "serial": "another-phone"},
    ],
)
def test_rejects_invalid_coordinates_and_device_override(device_tools, arguments):
    device_tools.observe()
    with pytest.raises(ValueError):
        device_tools.call("android_tap", arguments)
    device_tools.screen.tap.assert_not_called()


def test_action_limit_and_unicode(device_tools):
    for index in range(1, 3):
        device_tools.observe()
        device_tools.call(
            "android_type", {"observation": index, "text": "\u4f60\u597d & $(echo test)"}
        )
    device_tools.screen.send_text.assert_called_with("\u4f60\u597d & $(echo test)")
    device_tools.observe()
    with pytest.raises(ValueError, match="limit"):
        device_tools.call("android_key", {"observation": 3, "key": "home"})


def test_observation_expiry(device_tools, monkeypatch):
    device_tools.observe()
    monkeypatch.setattr(
        "artemis.integrations.codex.device.time.monotonic", lambda: device_tools.observed_at + 61
    )
    with pytest.raises(ValueError, match="expired"):
        device_tools.call("android_key", {"observation": 1, "key": "home"})


@pytest.mark.asyncio
async def test_rpc_queues_early_events_and_id_collision(tmp_path):
    client = CodexClient(tmp_path)
    client.send = AsyncMock()
    client._read = AsyncMock(
        side_effect=[
            {"method": "item/tool/call", "id": 1, "params": {}},
            {"method": "turn/completed", "params": {}},
            {"id": 1, "result": {"ok": True}},
        ]
    )
    assert await client.request("test", {}) == {"ok": True}
    assert (await client.event())["method"] == "item/tool/call"
    assert (await client.event())["method"] == "turn/completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [None, {"type": "apiKey"}])
async def test_requires_subscription_login(tmp_path, account):
    client = CodexClient(tmp_path)
    client.request = AsyncMock(return_value={"account": account})
    with pytest.raises(CodexError, match="login"):
        await client.require_chatgpt()


@pytest.mark.asyncio
async def test_account_does_not_expose_identity(tmp_path):
    client = CodexClient(tmp_path)
    client.request = AsyncMock(
        return_value={
            "account": {"type": "chatgpt", "email": "private@example.org", "id": "private"}
        }
    )
    assert await client.account() == {"authenticated": True, "auth_type": "chatgpt"}


@pytest.mark.asyncio
async def test_thread_uses_default_model_and_disables_user_mcp(tmp_path):
    client = Mock(cwd=tmp_path)
    client.request = AsyncMock(
        side_effect=[{"config": {"mcp_servers": {"personal": {}}}}, {"thread": {"id": "t"}}]
    )
    assert await start_thread(client, None) == "t"
    params = client.request.call_args.args[1]
    assert "model" not in params
    assert params["ephemeral"] is True
    assert params["config"]["mcp_servers.personal.enabled"] is False
    assert params["config"]["features.shell_tool"] is False


def _client(events):
    client = Mock()
    client.request = AsyncMock(return_value={"turn": {"id": "turn"}})
    client.send = AsyncMock()
    client.reject_request = AsyncMock()
    client.event = AsyncMock(side_effect=events)
    return client


def _call(call_id="a", thread="thread"):
    return {
        "id": 5,
        "method": "item/tool/call",
        "params": {
            "threadId": thread,
            "turnId": "turn",
            "callId": call_id,
            "tool": "android_observe",
            "arguments": {},
        },
    }


def _completed(succeeded=True):
    return [
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread",
                "item": {
                    "type": "agentMessage",
                    "text": json.dumps({"succeeded": succeeded, "summary": "Observed"}),
                },
            },
        },
        {
            "method": "turn/completed",
            "params": {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}},
        },
    ]


@pytest.mark.asyncio
async def test_turn_dispatch_and_goal_outcome(device_tools):
    client = _client([_call(), *_completed(False)])
    outcome = await drive_turn(client, "thread", "test", device_tools, 5)
    assert not outcome.succeeded  # Completed model turn is not successful Android task.
    assert client.send.call_args.args[0]["result"]["success"]
    assert (device_tools.directory / "codex-actions.jsonl").is_file()


@pytest.mark.asyncio
async def test_success_requires_final_observation(device_tools):
    client = _client(_completed())
    with pytest.raises(CodexError, match="final device observation"):
        await drive_turn(client, "thread", "test", device_tools, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("events", [[_call(thread="wrong")], [_call(), _call()]])
async def test_foreign_and_duplicate_calls_interrupt(device_tools, events):
    client = _client(events)
    with pytest.raises(CodexError):
        await drive_turn(client, "thread", "test", device_tools, 5)
    assert client.request.call_args.args[0] == "turn/interrupt"


@pytest.mark.asyncio
async def test_timeout_interrupts_turn(device_tools):
    client = _client([])

    async def stalled():
        await asyncio.sleep(10)

    client.event = stalled
    with pytest.raises(TimeoutError):
        await drive_turn(client, "thread", "test", device_tools, 0.01)
    assert client.request.call_args.args == (
        "turn/interrupt",
        {"threadId": "thread", "turnId": "turn"},
    )


@pytest.mark.asyncio
async def test_failed_setup_releases_lock_and_settles_trace(tmp_path, monkeypatch):
    from artemis.integrations.codex import runner

    client = AsyncMock()
    client.__aenter__.return_value = client
    monkeypatch.setattr(runner, "CodexClient", Mock(return_value=client))
    monkeypatch.setattr(runner, "device_state", lambda serial: "device")
    monkeypatch.setattr(runner.trace_store, "TRACES_DIR", str(tmp_path))
    lock, screen = Mock(), Mock()
    screen.connect.side_effect = RuntimeError("disconnected")
    monkeypatch.setattr(runner, "DeviceExecutionLock", Mock(return_value=lock))
    monkeypatch.setattr(runner, "AccessibilityClient", Mock(return_value=screen))
    with pytest.raises(CodexError, match="disconnected"):
        await run_task("test", "phone", "codex", None, 5, 3)
    lock.release.assert_called_once()
    screen.disconnect.assert_called_once()
    status = json.loads(next(tmp_path.glob("*/status.json")).read_text())
    assert status["status"] == "failed" and status["end_time"] is not None


@pytest.mark.asyncio
async def test_real_stdio_handshake_and_child_environment(tmp_path, monkeypatch):
    """Exercise actual pipe framing and cleanup with a tiny offline server."""
    server = tmp_path / "fake_server.py"
    server.write_text(
        """
import json, os, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        assert message["params"]["capabilities"]["experimentalApi"]
        result = {}
    elif method == "account/read":
        assert "OPENAI_API_KEY" not in os.environ
        assert "CODEX_API_KEY" not in os.environ
        result = {"account": {"type": "chatgpt", "email": "do-not-return"}}
    else:
        continue
    print(json.dumps({"id": message["id"], "result": result}), flush=True)
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("CODEX_API_KEY", "not-a-real-key")
    create = asyncio.create_subprocess_exec

    async def launch(*args, **kwargs):
        assert args[1:] == ("app-server", "--listen", "stdio://")
        return await create(sys.executable, "-u", str(server), **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    async with CodexClient(tmp_path, sys.executable) as client:
        assert (await client.account())["authenticated"]
        process = client.process
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_device_subprocess_timeout_is_tool_failure(device_tools):
    device_tools.screen.get_screen_data.side_effect = subprocess.TimeoutExpired("adb", 1)
    client = _client([_call(), *_completed(False)])
    outcome = await drive_turn(client, "thread", "test", device_tools, 5)
    assert not outcome.succeeded
    assert not client.send.call_args.args[0]["result"]["success"]


@pytest.mark.asyncio
async def test_cancellation_interrupts_turn(device_tools):
    client = _client([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await drive_turn(client, "thread", "test", device_tools, 5)
    assert client.request.call_args.args[0] == "turn/interrupt"


@pytest.mark.asyncio
async def test_busy_phone_does_not_disconnect_another_session(tmp_path, monkeypatch):
    from artemis.integrations.codex import runner
    from artemis.runtime.device_lock import DeviceBusyError

    client = AsyncMock()
    client.__aenter__.return_value = client
    monkeypatch.setattr(runner, "CodexClient", Mock(return_value=client))
    monkeypatch.setattr(runner, "device_state", lambda serial: "device")
    monkeypatch.setattr(runner.trace_store, "TRACES_DIR", str(tmp_path))
    lock, screen = Mock(), Mock()
    lock.acquire.side_effect = DeviceBusyError("busy")
    monkeypatch.setattr(runner, "DeviceExecutionLock", Mock(return_value=lock))
    monkeypatch.setattr(runner, "AccessibilityClient", Mock(return_value=screen))
    with pytest.raises(CodexError, match="busy"):
        await run_task("test", "phone", "codex", None, 5, 3)
    screen.connect.assert_not_called()
    screen.disconnect.assert_not_called()
    lock.release.assert_not_called()
