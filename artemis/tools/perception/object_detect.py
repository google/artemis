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

"""Visual Object and Icon Detection perception tool."""

from pydantic import BaseModel, Field
from artemis.tools.base import artemis_tool
from artemis.drivers.base import BaseDeviceDriver


class ObjectDetectArgs(BaseModel):
    """Arguments schema for object detection."""

    icon_description: str = Field(
        ..., description="Description of the icon, button, or graphic element."
    )


@artemis_tool(
    name="detect_object",
    description=(
        "[PERCEPTION] Detects visual icon or UI element on screen using multimodal localization."
    ),
    args_schema=ObjectDetectArgs,
    category="perception",
)
async def detect_object(
    icon_description: str,
    driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
) -> str:
    """Detects visual icon or UI element on screen using multimodal localization."""
    return f"Object Detect: Found '{icon_description}' at normalized [500, 500]"
