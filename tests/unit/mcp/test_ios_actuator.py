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

"""iOS actuator input_text verification: exact NFKC match on the field value."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from artemis.mcp.actuators import ios as ios_actuator


def _field(value=None, placeholder=None, focused=True, cls="TextField"):
    element = {
        "class": cls,
        "bounds": "[0,0][100,40]",
        "resource_id": "",
    }
    if value is not None:
        element["value"] = value
    if placeholder is not None:
        element["placeholder"] = placeholder
    if focused:
        element["keyboard_focused"] = True
    return element


class _Controller:
    def __init__(self, elements):
        self._elements = elements
        self.driver = SimpleNamespace(screen_size=(402, 874))
        self.type_text = AsyncMock(return_value=True)

    async def get_ui_elements(self):
        return self._elements


@pytest.mark.asyncio
async def test_verify_accepts_exact_value_match(monkeypatch):
    monkeypatch.setattr(ios_actuator, "_VERIFY_POLL_SECONDS", 0.01)
    controller = _Controller([_field(value="hello", placeholder="Type here")])
    assert await ios_actuator._verify_field_value(controller, "hello") is True


@pytest.mark.asyncio
async def test_verify_never_accepts_placeholder(monkeypatch):
    monkeypatch.setattr(ios_actuator, "_VERIFY_POLL_SECONDS", 0.01)
    monkeypatch.setattr(ios_actuator, "_VERIFY_TIMEOUT_SECONDS", 0.05)
    # Placeholder equals the typed text but no ``value`` exists: never a match.
    controller = _Controller([_field(placeholder="hello")])
    assert await ios_actuator._verify_field_value(controller, "hello") is False


@pytest.mark.asyncio
async def test_verify_nfkc_normalizes_both_sides(monkeypatch):
    monkeypatch.setattr(ios_actuator, "_VERIFY_POLL_SECONDS", 0.01)
    # Decomposed é (e + combining accent) vs precomposed é must verify equal.
    controller = _Controller([_field(value="café")])
    assert await ios_actuator._verify_field_value(controller, "café") is True


@pytest.mark.asyncio
async def test_verify_times_out_on_mismatch(monkeypatch):
    monkeypatch.setattr(ios_actuator, "_VERIFY_POLL_SECONDS", 0.01)
    monkeypatch.setattr(ios_actuator, "_VERIFY_TIMEOUT_SECONDS", 0.05)
    controller = _Controller([_field(value="partial")])
    assert await ios_actuator._verify_field_value(controller, "complete") is False


@pytest.mark.asyncio
async def test_input_text_fails_when_value_never_matches(monkeypatch):
    monkeypatch.setattr(ios_actuator, "_VERIFY_POLL_SECONDS", 0.01)
    monkeypatch.setattr(ios_actuator, "_VERIFY_TIMEOUT_SECONDS", 0.05)
    controller = _Controller([_field(value="wrong")])
    actuator = ios_actuator.IosActuator(ctx=SimpleNamespace(device=None), controller=controller)
    result = await actuator.input_text("right")
    assert result.ok is False
    assert "could not be verified" in result.message
