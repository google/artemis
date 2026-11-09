"""Provider inference for call sites that only carry a bare model id.

Regression cover for the paths listed in issue #95: the step summarizer,
memory chunking and the Flash operator fallback used to call
``get_google_llm`` directly, pinning ``provider=google`` regardless of the
provider configured for the run.
"""

from unittest.mock import patch

from artemis.config.llm import LLM, LLMWithFallback, lightweight_judge_default
from artemis.llm.router import ModelProvider
from artemis.services.llm import get_llm_for_model_id


def _endpoint_for(model_name: str):
    with patch("artemis.services.llm.ModelFactory.create_model") as create:
        get_llm_for_model_id(model_name)
    return create.call_args.args[0]


def test_namespaced_model_id_routes_to_openrouter():
    assert _endpoint_for("deepseek/deepseek-v4.1-flash").provider is ModelProvider.OPENROUTER


def test_namespaced_model_id_with_routing_suffix_routes_to_openrouter():
    assert _endpoint_for("vendor/model:nitro").provider is ModelProvider.OPENROUTER


def test_bare_model_id_keeps_the_google_default():
    assert _endpoint_for("gemini-3.8-flash").provider is ModelProvider.GOOGLE


def test_model_name_is_passed_through_unchanged():
    assert (
        _endpoint_for("deepseek/deepseek-v4.1-flash").model_name == "deepseek/deepseek-v4.1-flash"
    )


def test_judge_default_without_template_keeps_historical_default():
    judge = lightweight_judge_default()
    assert judge.provider == "google"
    assert judge.temperature == 0.0


def test_judge_default_keeps_the_lite_model_when_the_run_is_on_google():
    """Google setups must keep the cheap judge they have today."""
    template = LLMWithFallback(
        provider="google",
        model="gemini-3.8-flash",
        temperature=0.7,
        fallback=LLM(provider="google", model="gemini-3.7-flash", temperature=0.7),
    )
    judge = lightweight_judge_default(template)
    assert "lite" in judge.model
    assert judge.temperature == 0.0


def test_judge_default_inherits_the_runs_resolved_config():
    """On a non-Google provider the flash-lite id is unreachable, so the
    judges must follow the run's own config instead of failing."""
    template = LLMWithFallback(
        provider="openrouter",
        model="deepseek/deepseek-v4.1-flash",
        temperature=0.7,
        fallback=LLM(provider="openrouter", model="vendor/fallback", temperature=0.7),
    )
    judge = lightweight_judge_default(template)
    assert judge.provider == "openrouter"
    assert judge.model == "deepseek/deepseek-v4.1-flash"
    assert judge.fallback is not None
    assert judge.fallback.provider == "openrouter"
    # Temperature is forced to 0 for verdict calls, not inherited.
    assert judge.temperature == 0.0
    assert judge.fallback.temperature == 0.0
