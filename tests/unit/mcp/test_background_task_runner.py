import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_server.background.task_runner import _initialize_agent


@pytest.mark.asyncio
async def test_background_agent_initialization_has_hard_timeout():
    async def slow_init(**_):
        await asyncio.sleep(60)

    agent = MagicMock()
    agent.init = AsyncMock(side_effect=slow_init)

    with pytest.raises(TimeoutError, match="initialization exceeded 0.0s"):
        await _initialize_agent(
            agent,
            retry_count=1,
            retry_wait_seconds=1,
            timeout_seconds=0.01,
        )


@pytest.mark.asyncio
async def test_background_agent_initialization_forwards_health_settings():
    agent = AsyncMock()

    await _initialize_agent(
        agent,
        retry_count=3,
        retry_wait_seconds=4,
        timeout_seconds=1.0,
    )

    agent.init.assert_awaited_once_with(retry_count=3, retry_wait_seconds=4)
