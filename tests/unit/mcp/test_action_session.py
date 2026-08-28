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

"""ActionSession lifecycle tests, including the anyio same-task exit hazard."""

import asyncio

import pytest

from artemis.mcp.action_server import build_action_server
from artemis.mcp.action_session import ActionSession, get_action_session
from artemis.mcp.actuators import MockActuator

pytestmark = pytest.mark.asyncio


async def test_start_and_close():
    session = ActionSession(build_action_server(MockActuator()))
    await session.start()
    assert session.started
    await session.aclose()
    assert not session.started


async def test_close_from_different_task():
    """The regression guard for the anyio cancel-scope same-task rule.

    The in-memory transport's task group is entered inside the owner task; closing
    from any *other* task must not raise ``RuntimeError: Attempted to exit cancel
    scope in a different task``.
    """
    session = ActionSession(build_action_server(MockActuator()))
    await session.start()

    async def _closer():
        await session.aclose()

    await asyncio.create_task(_closer())
    assert not session.started


async def test_caller_timeout_does_not_brick_the_session():
    """Regression: cancelling a caller must never corrupt the shared transport.

    Observed live on-device: ``asyncio.wait_for(session.ui_hierarchy(), 1.0)`` around
    a slow hierarchy fetch cancelled ``call_tool`` mid-flight, corrupting the
    in-memory streams -- every later call failed with an empty-message
    ``BrokenResourceError`` and the server never received another request. The owner
    request queue makes an abandoned caller harmless; this test pins that down.
    """
    actuator = MockActuator(width=1000, height=2000)

    async def _slow_get_ui_elements():
        await asyncio.sleep(2.0)
        return []

    actuator.get_ui_elements = _slow_get_ui_elements
    session = await ActionSession(build_action_server(actuator)).start()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(session.ui_hierarchy(), timeout=0.1)

        # The session must still be fully functional afterwards.
        result = await session.call("click", {"target": [500, 500]})
        assert result.ok
        taps = [h for h in actuator.action_history if h["action"] == "tap"]
        assert len(taps) == 1
    finally:
        await session.aclose()


async def test_per_call_timeout_fires_server_side_and_session_survives():
    """A per-call ``timeout`` runs inside the server tool, never on the transport.

    mcp 1.29's ``read_timeout_seconds`` is NOT safe on the in-memory transport: a
    late response to a timed-out request crashes the client receive loop
    (``_handle_response`` sends into the request's already-closed response stream) --
    observed live on-device as a session bricked after a single clean
    ``McpError`` timeout. Server-side timeouts produce an ordinary error payload
    and no late response, so the session stays healthy by construction.
    """
    actuator = MockActuator(width=1000, height=2000)

    async def _slow_get_ui_elements():
        await asyncio.sleep(2.0)
        return []

    actuator.get_ui_elements = _slow_get_ui_elements
    session = await ActionSession(build_action_server(actuator)).start()
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            await session.ui_hierarchy(timeout=0.1)

        result = await session.call("click", {"target": [500, 500]})
        assert result.ok
    finally:
        await session.aclose()


async def test_transport_death_retires_session_for_rebuild():
    """A dead transport must flip ``started`` so get_action_session rebuilds.

    Without this, one transport failure leaves the rest of a run failing on a
    corpse (observed live: 24 minutes of empty-message ``ClosedResourceError``).
    """
    import anyio

    session = await ActionSession(build_action_server(MockActuator())).start()
    real_call_tool = session._session.call_tool

    async def _dying_call_tool(name, args, **kw):
        raise anyio.ClosedResourceError()

    session._session.call_tool = _dying_call_tool
    with pytest.raises(anyio.ClosedResourceError):
        await session.call("click", {"target": [1, 1]})

    # Owner loop retires; the session reports itself unusable. The owner task may
    # finish with an exception group from the transport teardown -- aclose() is the
    # API that swallows that, so here we just wait for `started` to flip.
    for _ in range(50):
        if not session.started:
            break
        await asyncio.sleep(0.1)
    assert not session.started

    class Ctx:
        action_session = session

    fresh = await get_action_session(Ctx(), actuator=MockActuator())
    assert fresh is not session
    assert fresh.started
    await fresh.aclose()
    del real_call_tool


async def test_calls_are_serialized_by_the_lock():
    """Two concurrent calls must not interleave on the single device."""
    actuator = MockActuator(width=1000, height=2000)
    session = await ActionSession(build_action_server(actuator)).start()
    try:
        await asyncio.gather(
            session.call("click", {"target": [100, 100]}),
            session.call("click", {"target": [900, 900]}),
        )
        taps = [h for h in actuator.action_history if h["action"] == "tap"]
        assert len(taps) == 2
    finally:
        await session.aclose()


async def test_call_before_start_raises():
    session = ActionSession(build_action_server(MockActuator()))
    with pytest.raises(RuntimeError, match="not started"):
        await session.call("click", {"target": [1, 1]})


async def test_double_start_is_idempotent():
    session = ActionSession(build_action_server(MockActuator()))
    await session.start()
    owner = session._owner
    await session.start()
    assert session._owner is owner
    await session.aclose()


async def test_get_action_session_caches_on_ctx():
    class Ctx:
        action_session = None

    ctx = Ctx()
    actuator = MockActuator()
    session = await get_action_session(ctx, actuator=actuator)
    assert ctx.action_session is session
    again = await get_action_session(ctx)
    assert again is session
    await session.aclose()
