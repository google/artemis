"""A pinned lightweight model must still use the operator's configured provider."""

from types import SimpleNamespace

from artemis.llm.router import ModelEndpoint, ModelProvider
from artemis.services import llm as llm_service
from artemis.services.llm import get_node_model


def _capture(monkeypatch):
    seen: list[ModelEndpoint] = []
    monkeypatch.setattr(
        llm_service.ModelFactory,
        "create_model",
        staticmethod(lambda endpoint: seen.append(endpoint) or SimpleNamespace(endpoint=endpoint)),
    )
    return seen


def _endpoint(provider: ModelProvider) -> ModelEndpoint:
    return ModelEndpoint(provider=provider, model_name="configured-default", temperature=0.7)


def test_pinned_model_keeps_the_configured_openai_provider(monkeypatch):
    """Issue #138: the Flash summarizer pinned a model and got a Gemini client.

    An OpenAI-compatible deployment then failed on an empty GOOGLE_API_KEY
    before the first step ran, even though the provider was configured
    correctly.
    """
    seen = _capture(monkeypatch)
    monkeypatch.setattr(
        llm_service, "_resolve_endpoint", lambda *a, **k: _endpoint(ModelProvider.OPENAI)
    )

    get_node_model(
        SimpleNamespace(), "summarizer", model_name="my-lite", is_utils=True, temperature=0.0
    )

    assert len(seen) == 1
    assert seen[0].provider is ModelProvider.OPENAI
    assert seen[0].model_name == "my-lite"
    assert seen[0].temperature == 0.0


def test_a_google_deployment_is_unaffected(monkeypatch):
    seen = _capture(monkeypatch)
    monkeypatch.setattr(
        llm_service, "_resolve_endpoint", lambda *a, **k: _endpoint(ModelProvider.GOOGLE)
    )

    get_node_model(SimpleNamespace(), "summarizer", model_name="gemini-lite", is_utils=True)

    assert seen[0].provider is ModelProvider.GOOGLE
    assert seen[0].model_name == "gemini-lite"
    # Temperature is left alone when the caller does not pin one.
    assert seen[0].temperature == 0.7


def test_without_a_context_there_is_no_configured_provider_to_honour(monkeypatch):
    """No context means no config to read, so Google stays the default there."""
    seen = _capture(monkeypatch)

    get_node_model(None, "summarizer", model_name="gemini-lite", temperature=0.0)

    assert seen[0].provider is ModelProvider.GOOGLE
    assert seen[0].model_name == "gemini-lite"


def test_the_capsule_lens_builds_its_model_on_the_configured_provider(monkeypatch):
    """The same bypass at its real call site, which is what the issue reports.

    StepCapsuleLens pins its own compression model, so before the fix it built
    a GOOGLE endpoint no matter what the operator configured. This assertion,
    not the import, is the one that fails on an unpatched tree.
    """
    from artemis.memory.chunking import StepCapsuleLens

    seen = _capture(monkeypatch)
    monkeypatch.setattr(
        llm_service, "_resolve_endpoint", lambda *a, **k: _endpoint(ModelProvider.OPENAI)
    )

    lens = StepCapsuleLens(model_name="my-compressor", ctx=SimpleNamespace())
    lens._get_llm()

    assert seen[0].provider is ModelProvider.OPENAI, (
        "a pinned compression model must not force the Gemini client"
    )
    assert seen[0].model_name == "my-compressor"


def test_the_capsule_lens_fallback_model_follows_the_same_provider(monkeypatch):
    from artemis.memory.chunking import StepCapsuleLens

    seen = _capture(monkeypatch)
    monkeypatch.setattr(
        llm_service, "_resolve_endpoint", lambda *a, **k: _endpoint(ModelProvider.OPENAI)
    )

    lens = StepCapsuleLens(
        model_name="my-compressor", fallback_model_name="my-backup", ctx=SimpleNamespace()
    )
    lens._get_fallback_llm()

    assert seen[0].provider is ModelProvider.OPENAI
    assert seen[0].model_name == "my-backup"
