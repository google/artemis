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

"""☕ ARTEMIS: Autonomous Multimodal Android Agent & Testing Framework."""

from artemis._version import __version__
from artemis import _legacy_imports

# Old import paths of the modules moved to third_party/ (deprecated).
_legacy_imports.install()

import importlib
from typing import Any

_LAZY_EXPORTS = {
    "ArtemisClient": ("artemis.interfaces.sdk.client", "ArtemisClient"),
    "ConcurrencyMode": ("artemis.interfaces.sdk.client", "ConcurrencyMode"),
    "StreamEvent": ("artemis.interfaces.sdk.task", "StreamEvent"),
    "StreamEventType": ("artemis.interfaces.sdk.task", "StreamEventType"),
    "Task": ("artemis.interfaces.sdk.task", "Task"),
    "Agent": ("artemis.sdk.agent", "Agent"),
    "Builders": ("artemis.sdk.builders", "Builders"),
    "BaseDeviceDriver": ("artemis.drivers.base", "BaseDeviceDriver"),
}

_CLIENT_EXPORTS = ("Capabilities", "Device", "TaskHandle", "TaskResult")


def __getattr__(name: str) -> Any:
    """Resolve the public names on first use.

    Importing the package used to pull the agent graph, the flash runner, and
    the SDK stack, which made every CLI command pay for them. Only the version
    and the legacy import shim are needed at import time.
    """
    if name in _LAZY_EXPORTS:
        module_name, attr = _LAZY_EXPORTS[name]
        value = getattr(importlib.import_module(module_name), attr)
        globals()[name] = value
        return value
    if name in _CLIENT_EXPORTS:
        module = importlib.import_module("artemis_client")
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), *__all__])


__all__ = [
    "__version__",
    "ArtemisClient",
    "Capabilities",
    "ConcurrencyMode",
    "Device",
    "StreamEvent",
    "StreamEventType",
    "Agent",
    "Builders",
    "Task",
    "TaskHandle",
    "TaskResult",
    "BaseDeviceDriver",
]
