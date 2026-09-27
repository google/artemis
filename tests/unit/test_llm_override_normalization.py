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

"""The single normaliser every per-task LLM override goes through.

``normalize_llm_override`` replaced four copies of the same strip/validate
shape (API schema, SDK builder, MCP tool + background runner, queue service).
These tests pin the semantics once, then pin that the entry points really share
it, so a fourth copy cannot creep back in with its own wording.
"""

import pytest
from pydantic import ValidationError

from apps.admin_console.schemas.task_schema import RunRequest
from artemis.config.llm_override import SUPPORTED_LLM_PROVIDERS, normalize_llm_override
from artemis.sdk.builders.task_request_builder import TaskRequestBuilder


def test_model_is_stripped_but_never_re_casefolded():
    """Model ids are provider-defined and case-sensitive, so only trim them."""
    assert normalize_llm_override("  gpt-5.1  ") == ("gpt-5.1", None)
    assert normalize_llm_override("GPT-5.1") == ("GPT-5.1", None)


def test_provider_is_stripped_and_lower_cased():
    assert normalize_llm_override("gpt-5.1", "  OpenAI  ") == ("gpt-5.1", "openai")
    # Every supported provider survives its own canonical spelling.
    for provider in SUPPORTED_LLM_PROVIDERS:
        assert normalize_llm_override("m", provider.upper()) == ("m", provider)


def test_blank_values_mean_not_requested():
    assert normalize_llm_override(None, None) == (None, None)
    assert normalize_llm_override("", "") == (None, None)
    assert normalize_llm_override("   ", "   ") == (None, None)
    # A model on its own is a valid override (the provider then stays put).
    assert normalize_llm_override("  gpt-5.1  ", "  ") == ("gpt-5.1", None)


def test_provider_without_a_model_is_rejected():
    with pytest.raises(ValueError, match="llm_provider requires llm_model"):
        normalize_llm_override(None, "openai")
    # A blank model is the same mistake: it pins nothing.
    with pytest.raises(ValueError, match="llm_provider requires llm_model"):
        normalize_llm_override("   ", "openai")


def test_unknown_provider_is_rejected_and_lists_the_supported_ones():
    with pytest.raises(ValueError) as excinfo:
        normalize_llm_override("gpt-5.1", "acme-cloud")

    message = str(excinfo.value)
    assert "unknown llm_provider 'acme-cloud'" in message
    for provider in SUPPORTED_LLM_PROVIDERS:
        assert provider in message


def test_error_prefix_lands_in_both_messages():
    with pytest.raises(ValueError, match="^builder: unknown llm_provider"):
        normalize_llm_override("gpt-5.1", "nope", error_prefix="builder: ")
    with pytest.raises(ValueError, match="^builder: llm_provider requires llm_model"):
        normalize_llm_override(None, "openai", error_prefix="builder: ")


def test_non_string_input_is_stringified():
    """JSON callers can hand us a number; it is used as its own identifier.

    The MCP tool used to coerce and the schema used to reject, so coercion is
    the unifying choice: an unusable value now degrades to a string id instead
    of failing (or crashing) somewhere further downstream.
    """
    assert normalize_llm_override(5, "openai") == ("5", "openai")  # type: ignore[arg-type]
    # A non-string provider is stringified and then still validated.
    with pytest.raises(ValueError, match="unknown llm_provider False"):
        normalize_llm_override("gpt-5.1", False)  # type: ignore[arg-type]


# --- The entry points share the one implementation ---------------------------


def test_api_schema_normalises_through_the_shared_helper():
    request = RunRequest(goal="Audit checkout", llm_model="  gpt-5.1  ", llm_provider=" OpenAI ")

    assert (request.llm_model, request.llm_provider) == ("gpt-5.1", "openai")
    assert RunRequest(goal="Audit checkout", llm_model="   ").llm_model is None


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"llm_provider": "acme-cloud", "llm_model": "gpt-5.1"}, "unknown llm_provider"),
        ({"llm_provider": "openai"}, "llm_provider requires llm_model"),
        ({"llm_model": "  ", "llm_provider": "openai"}, "llm_provider requires llm_model"),
    ],
)
def test_api_schema_rejects_the_same_inputs_as_the_helper(kwargs, expected):
    with pytest.raises(ValidationError, match=expected):
        RunRequest(goal="Audit checkout", **kwargs)


def test_builder_reports_the_shared_message_for_the_same_inputs():
    """The SDK says the same thing, prefixed with the method that failed."""
    with pytest.raises(ValueError, match="^with_llm_override: unknown llm_provider"):
        TaskRequestBuilder(goal="Audit checkout").with_llm_override(
            model="gpt-5.1", provider="acme-cloud"
        )
    with pytest.raises(ValueError, match="^with_llm_override: llm_provider requires llm_model"):
        TaskRequestBuilder(goal="Audit checkout").with_llm_override(provider="openai")


def test_builder_stores_what_the_helper_returns():
    request = (
        TaskRequestBuilder(goal="Audit checkout")
        .with_llm_override(model="  gpt-5.1  ", provider=" OpenAI ")
        .build()
    )

    assert (request.llm_model, request.llm_provider) == ("gpt-5.1", "openai")
