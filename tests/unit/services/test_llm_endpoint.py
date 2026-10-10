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

"""Tests for endpoint field pass-through in ``_resolve_endpoint``.

Custom/self-hosted providers declare ``api_base``/``api_key``/``max_tokens``/
``timeout`` in JSONC config. Previously the LLM schema dropped them as extra
fields and ``_resolve_endpoint`` never read them, so a custom provider always
silently fell back to the environment OpenAI base URL.
"""

from types import SimpleNamespace

from artemis.services.llm import _resolve_endpoint
from third_party.mobile_use.config.llm import LLM, LLMWithFallback


def _ctx(llm_cfg):
    return SimpleNamespace(
        llm_config=SimpleNamespace(
            get_agent=lambda _name: llm_cfg,
            get_utils=lambda _name: llm_cfg,
        )
    )


def test_llm_model_accepts_endpoint_fields():
    llm = LLM(
        provider="custom",
        model="gemma4-e4b",
        api_base="http://127.0.0.1:8080/v1",
        api_key="k",
        max_tokens=4096,
        timeout=120.0,
    )
    assert llm.api_base == "http://127.0.0.1:8080/v1"
    assert llm.api_key == "k"
    assert llm.max_tokens == 4096
    assert llm.timeout == 120.0


def test_resolve_endpoint_passes_custom_fields():
    llm = LLM(
        provider="custom",
        model="gemma4-e4b",
        api_base="http://127.0.0.1:8080/v1",
        api_key="k",
        max_tokens=4096,
        timeout=120.0,
    )
    ep = _resolve_endpoint(_ctx(llm), "planner")
    assert ep.api_base == "http://127.0.0.1:8080/v1"
    assert ep.api_key == "k"
    assert ep.max_tokens == 4096
    assert ep.timeout_seconds == 120.0


def test_resolve_endpoint_defaults_when_fields_absent():
    llm = LLM(provider="google", model="gemini-2.5-flash")
    ep = _resolve_endpoint(_ctx(llm), "planner")
    assert ep.api_base is None
    assert ep.api_key is None
    assert ep.max_tokens is None
    assert ep.timeout_seconds == 60.0


def test_resolve_endpoint_fallback_uses_fallback_endpoint_fields():
    llm = LLMWithFallback(
        provider="google",
        model="gemini-2.5-flash",
        timeout=45.0,
        fallback=LLM(
            provider="custom",
            model="gemma4-e4b",
            api_base="http://127.0.0.1:8080/v1",
            timeout=15.0,
        ),
    )
    primary = _resolve_endpoint(_ctx(llm), "planner")
    assert primary.timeout_seconds == 45.0
    fallback = _resolve_endpoint(_ctx(llm), "planner", use_fallback=True)
    assert fallback.api_base == "http://127.0.0.1:8080/v1"
    assert fallback.timeout_seconds == 15.0
