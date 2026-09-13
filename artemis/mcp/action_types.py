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

"""Typed results for the device action layer.

These models replace the ``"Error" in text`` substring sniffing: success is carried by
``ok``/``code``, while ``message`` remains the human/LLM-facing summary and keeps its
historical wording. Kept dependency-free (pydantic only) so servers, adapters, and
tests can all import it cheaply.
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

__all__ = ["ActionCode", "ActionResult", "ObserveResult"]


class ActionCode(str, Enum):
    """Machine-readable outcome classification for a device action."""

    OK = "OK"
    INVALID_ARGS = "INVALID_ARGS"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    DEVICE_ERROR = "DEVICE_ERROR"
    PACKAGE_NOT_FOUND = "PACKAGE_NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    UNSUPPORTED = "UNSUPPORTED"


class ActionResult(BaseModel):
    """Outcome of one device action, independent of any transport or agent."""

    ok: bool
    code: ActionCode
    action: str
    message: str
    """Agent-facing summary; wording matches the historical executor outcomes."""
    detail: str | None = None
    """Raw driver/controller error for diagnostics; never shown to the LLM."""
    normalized_coordinates: list[int] | None = None
    duration_ms: int | None = None

    @classmethod
    def success(cls, action: str, message: str, **kwargs: Any) -> "ActionResult":
        return cls(ok=True, code=ActionCode.OK, action=action, message=message, **kwargs)

    @classmethod
    def failure(
        cls,
        action: str,
        message: str,
        code: ActionCode = ActionCode.DEVICE_ERROR,
        **kwargs: Any,
    ) -> "ActionResult":
        return cls(ok=False, code=code, action=action, message=message, **kwargs)


class ObserveResult(BaseModel):
    """Post-action observation: persisted screenshot plus the parsed element list."""

    ok: bool
    code: ActionCode = ActionCode.OK
    message: str = ""
    screenshot_path: str | None = None
    image_name: str | None = None
    """Stem of ``screenshot_path``; stable key used by trace/replay consumers."""
    width: int | None = None
    height: int | None = None
    elements_text: str | None = None
    elements: list[dict[str, Any]] = Field(default_factory=list)
    hierarchy_ok: bool = True
    """False when the screenshot succeeded but the UI hierarchy parse failed; callers
    must then keep their previous element index rather than clobbering it."""
