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

"""Example: Automated Android System Settings Inspection.

This example demonstrates how to use the ARTEMIS SDK to launch the Android
Settings app, inspect device battery and storage configurations, and retrieve
verified diagnostics as a structured response.
"""

import asyncio
from pydantic import BaseModel, Field

from artemis.interfaces.sdk.client import ArtemisClient
from artemis.sdk.builders.task_request_builder import TaskRequestBuilder


class BatteryDiagnostics(BaseModel):
    """Structured report of Android battery diagnostics."""

    battery_percentage: str | None = Field(
        default=None, description="Current battery percentage level visible on screen"
    )
    battery_saver_status: str | None = Field(
        default=None, description="Battery Saver state (e.g. On, Off)"
    )
    diagnostics_summary: str = Field(
        description="Summary of the battery health and system status"
    )


async def main():
    # Initialize the Artemis SDK client
    client = ArtemisClient()

    # Build the task request targeting Android Settings
    task_request = (
        TaskRequestBuilder(goal="Open Android Settings, navigate to Battery, and extract the current battery level and status.")
        .with_name("settings_battery_inspection")
        .with_max_steps(15)
        .with_locked_app_package("com.android.settings")
        .with_output_format(BatteryDiagnostics)
        .build()
    )

    print(f"🚀 Running Artemis Task: {task_request.task_name}...")
    task = await client.run_task(task_request)

    print(f"✅ Task Status: {task.status}")
    if task.result and task.result.content:
        report = task.result.get_as_model(BatteryDiagnostics)
        print(f"📊 Battery Level: {report.battery_percentage}")
        print(f"⚡ Battery Saver: {report.battery_saver_status}")
        print(f"📝 Summary: {report.diagnostics_summary}")


if __name__ == "__main__":
    asyncio.run(main())
