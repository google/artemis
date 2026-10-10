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

"""Local model endpoint probe: guidance must be model-aware.

Artemis catalog aliases get ``artemis model pull`` / ``artemis model serve``
commands; BYO models on third-party OpenAI-compatible servers get neutral
hints — the probe must never emit a command Artemis cannot run on that
endpoint (or reference a foreign binary).
"""

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.core.diagnostics.probes.local_model_probe import (
    LocalModelEndpointProbe,
    _artemis_managed,
    _serve_port,
)


class _FakeLLM:
    def __init__(self, model, api_base="http://127.0.0.1:8097/v1", provider="custom"):
        self.provider = provider
        self.model = model
        self.api_base = api_base


@pytest.fixture
def catalog():
    """Inject a stand-in catalog module; restore the import miss afterwards."""
    fake = types.ModuleType("artemis.config.local_models")
    fake.LOCAL_MODELS = {"gemma4-e4b": object(), "qwen-vl": object()}
    sys.modules["artemis.config.local_models"] = fake
    yield fake
    del sys.modules["artemis.config.local_models"]


def _llms(*models, base="http://127.0.0.1:8097/v1"):
    return [_FakeLLM(m, api_base=base) for m in models]


def _run(llms):
    with patch(
        "artemis.core.diagnostics.probes.local_model_probe._iter_configured_llms",
        side_effect=lambda: llms,
    ):
        return __import__("asyncio").run(LocalModelEndpointProbe().probe())


def _commands(result):
    return [a.payload for a in result.actions if a.action_type == "command"]


def _hints(result):
    return [a.payload for a in result.actions if a.action_type == "hint"]


def test_catalog_detection_and_port_parsing(catalog):
    assert _artemis_managed("gemma4-e4b") is True
    assert _artemis_managed("llama3.2-vision") is False
    assert _serve_port("http://127.0.0.1:8097/v1") == "8097"
    assert _serve_port("https://api.example.com/v1") is None


def test_unreachable_catalog_model_suggests_artemis_serve(catalog):
    result = _run(_llms("gemma4-e4b", base="http://127.0.0.1:9/v1"))
    assert _commands(result) == ["artemis model serve gemma4-e4b --port 9"]
    assert result.status.name == "FAIL"


def test_unreachable_byo_model_suggests_neutral_hint(catalog):
    result = _run(_llms("llama3.2-vision", base="http://127.0.0.1:9/v1"))
    assert _commands(result) == []
    assert any("OpenAI-compatible" in h for h in _hints(result))
    assert "ondevice-agent-platform" not in " ".join(_hints(result))


def _serve_models(*ids):
    """Patch httpx so ``GET /models`` returns the given ids."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"data": [{"id": i} for i in ids]})
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return patch(
        "artemis.core.diagnostics.probes.local_model_probe.httpx.AsyncClient",
        return_value=cm,
    )


def test_missing_catalog_model_suggests_artemis_pull(catalog):
    """A reachable endpoint lacking a catalog alias gets `artemis model pull`."""
    with _serve_models("something-else"):
        result = _run(_llms("qwen-vl"))
    assert _commands(result) == ["artemis model pull qwen-vl"]


def test_missing_byo_model_suggests_own_tooling(catalog):
    with _serve_models("gemma4-e4b"):
        result = _run(_llms("llama3.2-vision"))
    assert _commands(result) == []
    assert any("own tooling" in h for h in _hints(result))


def test_cloud_only_config_is_not_used_pass(catalog):
    class _Cloud:
        provider = "google"
        model = "gemini-3.8-flash"
        api_base = None

    result = _run([_Cloud()])
    assert result.status.name == "PASS"
    assert result.summary == "Not used"
