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
