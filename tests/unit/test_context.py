import asyncio
import pytest
from artemis.context import ArtemisContext, DeviceContext, DevicePlatform


@pytest.mark.asyncio
async def test_artemis_context_waits_for_background_tasks():
    device = DeviceContext(
        host_platform="LINUX",
        mobile_platform=DevicePlatform.ANDROID,
        device_id="dummy",
        device_width=1080,
        device_height=2400,
    )

    context = ArtemisContext(device=device)

    task_run = False

    async def dummy_task():
        nonlocal task_run
        await asyncio.sleep(0.1)
        task_run = True

    async with context:
        task = asyncio.create_task(dummy_task())
        context.background_tasks.append(task)

    # After exiting the context, the task should have run to completion
    assert task_run is True
