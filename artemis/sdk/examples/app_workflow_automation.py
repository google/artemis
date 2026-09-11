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

"""Example: App Workflow Automation with Trace Recording.

This example demonstrates how to orchestrate multi-step mobile UI navigation,
capture step-by-step traces, and enforce deterministic completion criteria
using ARTEMIS.
"""

import asyncio
from artemis.interfaces.sdk.client import ArtemisClient
from artemis.sdk.builders.task_request_builder import TaskRequestBuilder


async def main():
    client = ArtemisClient()

    # Build a task with full trace recording
    task_request = (
        TaskRequestBuilder(
            goal="Open Clock app, verify current alarm settings, and ensure the active alarms list is visible."
        )
        .with_name("clock_alarm_audit")
        .with_max_steps(12)
        .with_locked_app_package("com.google.android.deskclock")
        .with_trace_recording(enabled=True, path="traces/clock_alarm_audit")
        .with_output_description("Summary of configured alarms and their status")
        .build()
    )

    print(f"🎬 Initiating Workflow Automation: {task_request.task_name}...")
    task = await client.run_task(task_request)

    print(f"🏁 Execution Finished: status={task.status}")
    if task.result:
        print(f"⏱️ Duration: {task.result.execution_time_seconds:.2f}s, Steps: {task.result.steps_taken}")
        print(f"📄 Output: {task.result.content}")


if __name__ == "__main__":
    asyncio.run(main())
