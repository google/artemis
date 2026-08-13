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

"""Global Plugin and Component Registry for Agents, Tools, and Drivers."""

from typing import Any, TypeVar

T = TypeVar("T")


class ComponentRegistry:
    """Generic typed component registry supporting runtime dynamic plugin loading."""

    def __init__(self, name: str):
        self.name = name
        self._items: dict[str, Any] = {}

    def register(self, key: str, item: Any) -> Any:
        self._items[key.lower()] = item
        return item

    def get(self, key: str) -> Any | None:
        return self._items.get(key.lower())

    def list_all(self) -> dict[str, Any]:
        return dict(self._items)

    def contains(self, key: str) -> bool:
        return key.lower() in self._items


# Global Registries
AgentRegistry = ComponentRegistry("Agents")
ToolRegistry = ComponentRegistry("Tools")
DriverRegistry = ComponentRegistry("Drivers")
