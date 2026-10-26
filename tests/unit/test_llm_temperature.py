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

"""Which models accept a custom ``temperature``, and that the rest omit it."""

from artemis.llm.router import ModelEndpoint, ModelFactory, ModelProvider
from artemis.llm.router import supports_custom_temperature
import pytest


@pytest.mark.parametrize(
    "model, supported",
    [
        # The generations that reject the parameter outright.
        ("gpt-5.5", False),
        ("gpt-5.4-2026-03-05", False),
        ("claude-sonnet-5-5", False),
        ("claude-opus-5-5", False),
        ("claude-haiku-5-5", False),
        # Everything before them keeps its current behaviour.
        ("gpt-4.1", True),
        ("gpt-4.1-mini", True),
        ("gpt-4o", True),
        ("claude-sonnet-4-5-20250929", True),
        ("claude-opus-4-5-20251101", True),
        # A name with no parsable version is assumed current and left alone.
        ("gemini-3.8-flash", True),
        ("llama3.2-vision", True),
        ("", True),
        (None, True),
    ],
)
def test_supports_custom_temperature(model, supported):
    assert supports_custom_temperature(model) is supported


@pytest.mark.parametrize(
    "provider, model",
    [
        (ModelProvider.OPENAI, "gpt-5.5"),
        (ModelProvider.ANTHROPIC, "claude-sonnet-5-5"),
    ],
)
def test_temperature_is_left_out_for_models_that_reject_it(provider, model):
    """The request must carry no ``temperature`` at all, not a temperature of zero.

    Both APIs answer 400 rather than clamping: OpenAI with "does not support 0.0 with
    this model", Anthropic with "`temperature` is deprecated for this model".
    """
    built = ModelFactory.create_model(
        ModelEndpoint(provider=provider, model_name=model, api_key="test-key")
    )

    assert getattr(built, "temperature", None) is None


@pytest.mark.parametrize(
    "provider, model",
    [
        (ModelProvider.OPENAI, "gpt-4.1"),
        (ModelProvider.ANTHROPIC, "claude-sonnet-4-5-20250929"),
    ],
)
def test_temperature_still_reaches_models_that_accept_it(provider, model):
    built = ModelFactory.create_model(
        ModelEndpoint(provider=provider, model_name=model, api_key="test-key", temperature=0.3)
    )

    assert getattr(built, "temperature", None) == pytest.approx(0.3)
