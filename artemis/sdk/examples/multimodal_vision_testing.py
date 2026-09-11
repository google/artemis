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

"""Example: Multimodal UI Testing and Screen Element Grounding.

This example demonstrates using ARTEMIS multimodal perception (Explorer & OCR-XML
fusion) to locate visual elements on dynamic or non-standard UI canvases without
relying on static coordinates.
"""

import asyncio
from pydantic import BaseModel, Field

from artemis.interfaces.sdk.client import ArtemisClient
from artemis.sdk.builders.task_request_builder import TaskRequestBuilder


class VisualAuditReport(BaseModel):
    """Structured report verifying visual UI layout integrity."""

    active_screen_title: str = Field(description="Title of the active screen or view")
    interactive_buttons_count: int = Field(
        description="Number of actionable buttons identified on the current view"
    )
    visual_anomalies_detected: bool = Field(
        description="True if unexpected popups, overlapping text, or error banners exist"
    )
    observations: str = Field(description="Visual layout observations and audit details")


async def main():
    client = ArtemisClient()

    task_request = (
        TaskRequestBuilder(
            goal="Open Calculator, verify numeric keypad buttons layout, and confirm display screen is empty."
        )
        .with_name("calculator_visual_audit")
        .with_max_steps(8)
        .with_locked_app_package("com.google.android.calculator")
        .with_output_format(VisualAuditReport)
        .build()
    )

    print("🔍 Starting Multimodal Vision Audit...")
    task = await client.run_task(task_request)

    if task.result and task.result.content:
        audit = task.result.get_as_model(VisualAuditReport)
        print(f"📱 View: {audit.active_screen_title}")
        print(f"🔘 Interactive Buttons: {audit.interactive_buttons_count}")
        print(f"⚠️ Anomalies Detected: {audit.visual_anomalies_detected}")
        print(f"📝 Observations: {audit.observations}")


if __name__ == "__main__":
    asyncio.run(main())
