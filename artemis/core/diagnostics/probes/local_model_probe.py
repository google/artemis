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

"""Local Model Endpoint Readiness Probe.

When artemis.jsonc routes nodes to a ``custom`` provider (an OpenAI-compatible
endpoint at ``api_base``), every configured model must actually be served
there. This probe verifies the endpoint is reachable and that each required
model appears in ``GET {api_base}/models``.

Guidance is model-aware: aliases in the Artemis local-model catalog get
``artemis model pull`` / ``artemis model serve`` commands; other models are
BYO — the probe suggests serving them with the endpoint's own tooling
(Ollama, vLLM, LM Studio, ...) instead of a command Artemis cannot run.
"""

from typing import Any
from urllib.parse import urlparse

import httpx

from artemis.config.local_models import resolve_model_ref
from artemis.core.diagnostics.probes.base import BaseProbe
from artemis.core.diagnostics.schema import (
    ProbeAction,
    ProbeCategory,
    ProbeResult,
    ProbeStatus,
)
from third_party.mobile_use.config.llm import LLM
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

_PROBE_HTTP_TIMEOUT_S = 5.0


def _artemis_managed(model: str) -> bool:
    """Whether the model is an Artemis catalog alias (``artemis model``
    lifecycle commands apply) or a BYO name the endpoint serves itself."""
    try:
        from artemis.config.local_models import LOCAL_MODELS

        return model in LOCAL_MODELS
    except Exception:
        return False


def _serve_port(api_base: str) -> str | None:
    """Port from an api_base URL, for ``--port`` in serve guidance."""
    try:
        from urllib.parse import urlparse

        port = urlparse(api_base).port
        return str(port) if port else None
    except Exception:
        return None


def _iter_configured_llms() -> list[LLM]:
    """Every LLM/LLMWithFallback configured in the active artemis.jsonc,
    including per-node fallbacks."""
    from artemis.config.llm import parse_llm_config

    llms: list[LLM] = []
    cfg = parse_llm_config()
    for name in type(cfg).model_fields:
        node = getattr(cfg, name, None)
        if isinstance(node, LLM):
            llms.append(node)
            fallback = getattr(node, "fallback", None)
            if isinstance(fallback, LLM):
                llms.append(fallback)
        elif node is not None and hasattr(node, "model_fields_set"):
            # Container (e.g. LLMConfigUtils) - walk its LLM fields too.
            for sub_name in type(node).model_fields:
                sub = getattr(node, sub_name, None)
                if isinstance(sub, LLM):
                    llms.append(sub)
                    fallback = getattr(sub, "fallback", None)
                    if isinstance(fallback, LLM):
                        llms.append(fallback)
    return llms


def _local_endpoints() -> dict[str, set[str]]:
    """Map api_base -> set of model aliases required on it (custom provider)."""
    endpoints: dict[str, set[str]] = {}
    for llm in _iter_configured_llms():
        if llm.provider == "custom" and llm.api_base:
            base = llm.api_base.rstrip("/")
            endpoints.setdefault(base, set()).add(llm.model)
    return endpoints


class LocalModelEndpointProbe(BaseProbe):
    """Verify local model endpoints serve every configured custom model."""

    @property
    def probe_id(self) -> str:
        return "local_model_endpoint"

    @property
    def category(self) -> ProbeCategory:
        return ProbeCategory.RUNTIME

    @property
    def is_blocker(self) -> bool:
        return True

    async def probe(self) -> ProbeResult:
        try:
            endpoints = _local_endpoints()
        except Exception as e:
            # A broken config is the SystemConfigProbe's job - don't double-report.
            logger.debug(f"local model probe skipped: cannot parse LLM config: {e}")
            endpoints = {}

        if not endpoints:
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Local Model Endpoint",
                status=ProbeStatus.PASS,
                is_blocker=self.is_blocker,
                summary="Not used",
                description="No node is bound to a local custom endpoint; cloud providers serve all models.",
                metadata={"endpoints": {}},
                actions=[
                    ProbeAction(
                        action_type="hint",
                        label="Cloud Providers",
                        payload="Configured providers are remote; no local model install is required.",
                    )
                ],
            )

        reports: list[dict[str, Any]] = []
        missing_actions: list[ProbeAction] = []
        worst_summary = ""
        managed_all: set[str] = set()
        async with httpx.AsyncClient(timeout=_PROBE_HTTP_TIMEOUT_S) as client:
            for base, required in sorted(endpoints.items()):
                managed_all.update(a for a in required if _artemis_managed(a))
                try:
                    resp = await client.get(f"{base}/models")
                    resp.raise_for_status()
                    served = {
                        m["id"]
                        for m in resp.json().get("data", [])
                        if isinstance(m, dict) and isinstance(m.get("id"), str)
                    }
                except Exception as e:
                    reports.append({"api_base": base, "reachable": False, "error": str(e)})
                    worst_summary = f"{base} unreachable"
                    managed = sorted(a for a in required if _artemis_managed(a))
                    if managed:
                        port = _serve_port(base)
                        port_arg = f" --port {port}" if port else ""
                        missing_actions.append(
                            ProbeAction(
                                action_type="command",
                                label="Start Model Server",
                                payload=f"artemis model serve {managed[0]}{port_arg}",
                            )
                        )
                    else:
                        missing_actions.append(
                            ProbeAction(
                                action_type="hint",
                                label="Start Model Server",
                                payload=(
                                    f"Start the OpenAI-compatible model server for {base}"
                                    " and re-run diagnostics."
                                ),
                            )
                        )
                    continue
                missing = sorted(required - served)
                reports.append(
                    {
                        "api_base": base,
                        "reachable": True,
                        "required": sorted(required),
                        "served": sorted(served),
                        "missing": missing,
                    }
                )
                for alias in missing:
                    worst_summary = f"model '{alias}' not installed"
                    if _artemis_managed(alias):
                        missing_actions.append(
                            ProbeAction(
                                action_type="command",
                                label=f"Pull {alias}",
                                payload=f"artemis model pull {alias}",
                            )
                        )
                    else:
                        missing_actions.append(
                            ProbeAction(
                                action_type="hint",
                                label=f"Serve {alias}",
                                payload=(
                                    f"'{alias}' is not served at {base}. Pull and serve"
                                    " it with that server's own tooling"
                                    " (e.g. `ollama pull`, a vLLM/LM Studio config)"
                                    " or choose a model the endpoint already serves."
                                ),
                            )
                        )

        unreachable = [r for r in reports if not r.get("reachable")]
        with_missing = [r for r in reports if r.get("missing")]

        if not unreachable and not with_missing:
            served_total = sorted({m for r in reports for m in r.get("served", [])})
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Local Model Endpoint",
                status=ProbeStatus.PASS,
                is_blocker=self.is_blocker,
                summary=f"Serving {len(served_total)} model(s)",
                description=(
                    f"{len(reports)} local endpoint(s) reachable and all configured "
                    f"model aliases are pulled and serving."
                ),
                metadata={"endpoints": reports, "managed": sorted(managed_all)},
                actions=[
                    ProbeAction(
                        action_type="hint",
                        label="Models Ready",
                        payload=f"Serving: {', '.join(served_total)}",
                    )
                ],
            )

        if unreachable:
            description = (
                "Local model endpoint(s) unreachable: "
                + ", ".join(r["api_base"] for r in unreachable)
                + ". The agent cannot run until the server is up."
            )
        else:
            missing_all = sorted({m for r in with_missing for m in r["missing"]})
            description = (
                "Configured model(s) not installed on the local endpoint: "
                + ", ".join(missing_all)
                + ". Pull them before running a task."
            )
        return ProbeResult(
            id=self.probe_id,
            category=self.category,
            title="Local Model Endpoint",
            status=ProbeStatus.FAIL,
            is_blocker=self.is_blocker,
            summary=worst_summary,
            description=description,
            metadata={"endpoints": reports, "managed": sorted(managed_all)},
            actions=missing_actions,
        )
