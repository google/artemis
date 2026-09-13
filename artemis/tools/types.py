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

from pydantic import BaseModel, Field, model_validator

from artemis.utils.ui_hierarchy import ElementBounds


class _CyFunctionDetectorMeta(type):
    """Metaclass for detecting cython compiled functions."""

    def __instancecheck__(cls, instance):
        name = type(instance).__name__
        return (
            name
            in (
                "cyfunction",
                "cython_function_or_method",
                "builtin_function_or_method",
            )
            or "cyfunction" in name.lower()
        )


# pylint: disable=too-few-public-methods
class CyFunctionDetector(metaclass=_CyFunctionDetectorMeta):
    """Detector for cython compiled functions."""


class Target(BaseModel):
    """A comprehensive locator for a UI element, supporting a fallback mechanism."""

    model_config = {"ignored_types": (CyFunctionDetector,)}

    resource_id: str | None = Field(None, description="The resource-id of the element.")
    resource_id_index: int | None = Field(
        None,
        description=("The zero-based index if multiple elements share the same resource-id."),
    )
    text: str | None = Field(
        None,
        description=("The text content of the element (e.g., a label or placeholder)."),
    )
    text_index: int | None = Field(
        None,
        description=("The zero-based index if multiple elements share the same text."),
    )
    bounds: ElementBounds | None = Field(
        None, description="The x, y, width, and height of the element."
    )

    @model_validator(mode="after")
    def _default_indices(self):
        # Treat empty strings like “not provided”
        if (
            self.resource_id is not None and self.resource_id != ""
        ) and self.resource_id_index is None:
            self.resource_id_index = 0
        if (self.text is not None and self.text != "") and self.text_index is None:
            self.text_index = 0
        return self
