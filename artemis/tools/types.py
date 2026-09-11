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

"""Multi-strategy target element descriptors for ARTEMIS actions.

Encapsulates semantic identifiers, textual queries, and spatial bounding boxes
into a prioritized target resolution contract.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from artemis.utils.ui_hierarchy import ElementBounds


class Target(BaseModel):
    """Composite locator descriptor resolving UI nodes across dynamic and visual channels."""

    model_config = ConfigDict(extra="ignore")

    resource_id: str | None = Field(default=None, description="Android resource identifier token.")
    resource_id_index: int | None = Field(
        default=None,
        description="Zero-based ordinal index when multiple nodes match resource_id.",
    )
    text: str | None = Field(
        default=None,
        description="Exact or case-insensitive element label or content descriptor.",
    )
    text_index: int | None = Field(
        default=None,
        description="Zero-based ordinal index when multiple nodes match text query.",
    )
    bounds: ElementBounds | None = Field(
        default=None,
        description="Spatial boundary metrics (x, y, width, height) of the element.",
    )

    @model_validator(mode="after")
    def _normalize_indices(self) -> Target:
        """Coerce missing indices to zero if primary selector string is populated."""
        if self.resource_id and self.resource_id.strip() and self.resource_id_index is None:
            self.resource_id_index = 0
        if self.text and self.text.strip() and self.text_index is None:
            self.text_index = 0
        return self
