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

"""Dynamic LLM Schema definitions."""

from pydantic import BaseModel, Field


class ModelParameters(BaseModel):
    """Fine-grained generation hyperparameters."""

    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=1)
    max_output_tokens: int = Field(default=4096, ge=1)
    seed: int | None = Field(default=None)


class ThinkingBlock(BaseModel):
    """Extracted internal chain-of-thought from thinking models."""

    content: str = Field(..., description="Raw thought reasoning string")
    is_native: bool = Field(
        default=False, description="Whether thought was returned via native API field"
    )
