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

"""Regression tests for empty-credential normalization and diagnostics.

Context: a `.env` containing `GOOGLE_API_KEY=` / `GEMINI_API_KEY=` (key names
present, values empty) produced `Planner requires GOOGLE_API_KEY in .env`, which
reads as "the variable is missing" and sent debugging down the wrong path.
"""

import pytest
from pydantic import SecretStr

from artemis.config.llm import LLM, _credential_hint
from artemis.config.settings import Settings


def test_empty_env_value_normalizes_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty `KEY=` in the environment must become None, not SecretStr('')."""
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.GOOGLE_API_KEY is None
    assert s.GEMINI_API_KEY is None
    assert s.get_api_key("google") is None


def test_gemini_key_aliases_to_google_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """GEMINI_API_KEY alone must satisfy the Google provider."""
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-value")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.GOOGLE_API_KEY is not None
    assert s.GOOGLE_API_KEY.get_secret_value() == "test-key-value"


def test_credential_hint_distinguishes_empty_from_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hint must say which dotenv was loaded and empty != unset, without leaking values."""
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GCP_API_KEY", "super-secret-value")
    hint = _credential_hint("GOOGLE_API_KEY", "GEMINI_API_KEY", "GCP_API_KEY")
    assert "GOOGLE_API_KEY=<present but empty/placeholder>" in hint
    assert "GEMINI_API_KEY=<unset>" in hint
    assert "GCP_API_KEY=<set>" in hint
    assert "super-secret-value" not in hint
    assert ".env" in hint
    assert "restart" in hint.lower()


def test_validate_provider_google_error_carries_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The planner-facing error must name the alias and the loaded dotenv path."""
    import artemis.config.llm as llm_mod

    monkeypatch.setattr(llm_mod.settings, "GOOGLE_API_KEY", None)
    monkeypatch.setenv("GOOGLE_API_KEY", "")
    model = LLM(provider="google", model="gemini-2.5-flash")
    with pytest.raises(Exception) as exc:
        model.validate_provider("Planner")
    msg = str(exc.value)
    assert "Planner requires GOOGLE_API_KEY" in msg
    assert "GEMINI_API_KEY" in msg
    assert "dotenv loaded from" in msg


def test_placeholder_secret_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Placeholder values must not count as credentials."""
    monkeypatch.setenv("GOOGLE_API_KEY", "your_gemini_api_key_here")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.GOOGLE_API_KEY is None
    assert isinstance(SecretStr("x"), SecretStr)
