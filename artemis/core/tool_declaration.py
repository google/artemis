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

"""Standardized tool declaration adhering to JSON Schema format."""

from typing import Any


class ToolDeclaration(dict):
    """Standardized tool declaration adhering to JSON Schema format with property accessors."""

    def __init__(self, name: str, description: str, parameters: dict[str, Any]):
        super().__init__(
            type="function",
            function={
                "name": name,
                "description": description,
                "parameters": parameters,
            },
            name=name,
            description=description,
            parameters=parameters,
        )

    @property
    def name(self) -> str:
        return self.get("name") or self.get("function", {}).get("name", "")

    @property
    def description(self) -> str:
        return self.get("description") or self.get("function", {}).get("description", "")

    @property
    def parameters(self) -> dict[str, Any]:
        return self.get("parameters") or self.get("function", {}).get("parameters", {})
