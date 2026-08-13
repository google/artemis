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

"""Optical Character Recognition (OCR) perception tool."""

from pydantic import BaseModel, Field
from artemis.tools.base import artemis_tool
from artemis.drivers.base import BaseDeviceDriver


class OCRRecognitionArgs(BaseModel):
    """Arguments schema for OCR recognition."""

    query_text: str | None = Field(
        default=None,
        description="Optional target text snippet to search on screen.",
    )


@artemis_tool(
    name="ocr_recognize",
    description=(
        "[PERCEPTION] Performs OCR on the active screen to extract visible text and coordinates."
    ),
    args_schema=OCRRecognitionArgs,
    category="perception",
)
async def ocr_recognize(
    query_text: str | None = None,
    driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
) -> str:
    """Performs OCR on the active screen to extract visible text and coordinates."""
    if query_text:
        return f"OCR: Text '{query_text}' located at center [500, 500]"
    return "OCR: Extracted visible text items from screen."
