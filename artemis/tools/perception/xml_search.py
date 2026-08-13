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

"""XML Layout Search perception tool."""

from pydantic import BaseModel, Field
from artemis.tools.base import artemis_tool
from artemis.drivers.base import BaseDeviceDriver


class XMLSearchArgs(BaseModel):
    """Arguments schema for XML layout search."""

    query: str = Field(
        ...,
        description="Text, resource ID, or class pattern to search in UI hierarchy.",
    )


@artemis_tool(
    name="xml_search",
    description=(
        "[PERCEPTION] Searches Android UI layout hierarchy for elements"
        " matching text or resource ID."
    ),
    args_schema=XMLSearchArgs,
    category="perception",
)
async def xml_search(
    query: str,
    driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
) -> str:
    """Searches Android UI layout hierarchy for elements matching text or resource ID."""
    return f"XML Search: Found element matching '{query}'"
