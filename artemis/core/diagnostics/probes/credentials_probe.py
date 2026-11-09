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

"""LLM & Multimodal Vision Credentials Readiness Probe."""

from typing import Any
from artemis.config import settings
from artemis.core.diagnostics.probes.base import BaseProbe
from artemis.core.diagnostics.schema import (
    ProbeAction,
    ProbeCategory,
    ProbeResult,
    ProbeStatus,
)


class LLMCredentialsProbe(BaseProbe):
    """Probe verifying multi-provider multimodal LLM credentials."""

    @property
    def probe_id(self) -> str:
        return "llm_api_key"

    @property
    def category(self) -> ProbeCategory:
        return ProbeCategory.CREDENTIALS

    @property
    def is_blocker(self) -> bool:
        return True

    def _mask_key(self, key_str: str) -> str:
        """Helper to safely mask an API credential for display."""
        if len(key_str) > 10:
            return f"{key_str[:6]}...{key_str[-4:]}"
        return "***"

    async def probe(self) -> ProbeResult:
        import os

        from artemis.config.settings import is_placeholder_key

        openai_key = settings.get_api_key("openai")
        claude_key = settings.get_api_key("anthropic")
        openrouter_key = settings.get_api_key("openrouter")
        xai_key = settings.get_api_key("xai")

        configured_providers: list[dict[str, Any]] = []
        api_keys_map: dict[str, str] = {}
        if openai_key and not is_placeholder_key(openai_key):
            o_val = openai_key.get_secret_value()
            configured_providers.append(
                {
                    "provider": "openai",
                    "label": "ChatGPT",
                    "masked": self._mask_key(o_val),
                    "raw_key": o_val,
                    "key": o_val,
                }
            )
            api_keys_map["openai"] = o_val
        if claude_key and not is_placeholder_key(claude_key):
            c_val = claude_key.get_secret_value()
            configured_providers.append(
                {
                    "provider": "anthropic",
                    "label": "Claude",
                    "masked": self._mask_key(c_val),
                    "raw_key": c_val,
                    "key": c_val,
                }
            )
            api_keys_map["anthropic"] = c_val
        if openrouter_key and not is_placeholder_key(openrouter_key):
            or_val = openrouter_key.get_secret_value()
            configured_providers.append(
                {
                    "provider": "openrouter",
                    "label": "OpenRouter",
                    "masked": self._mask_key(or_val),
                    "raw_key": or_val,
                    "key": or_val,
                }
            )
            api_keys_map["openrouter"] = or_val
        if xai_key and not is_placeholder_key(xai_key):
            x_val = xai_key.get_secret_value()
            configured_providers.append(
                {
                    "provider": "xai",
                    "label": "xAI (Grok)",
                    "masked": self._mask_key(x_val),
                    "raw_key": x_val,
                    "key": x_val,
                }
            )
            api_keys_map["xai"] = x_val

        # Detect any custom model endpoints or environment variables defined in files
        for env_var, label, prov_id in [
            ("DEEPSEEK_API_KEY", "DeepSeek", "deepseek"),
            ("GROQ_API_KEY", "Groq", "groq"),
            ("OPENAI_BASE_URL", "Custom OpenAI Endpoint", "custom"),
            ("OLLAMA_BASE_URL", "Local Ollama", "ollama"),
            ("VLLM_BASE_URL", "vLLM Endpoint", "vllm"),
        ]:
            val = os.environ.get(env_var)
            if val and val.strip() and not is_placeholder_key(val.strip()):
                configured_providers.append(
                    {
                        "provider": prov_id,
                        "label": label,
                        "masked": self._mask_key(val.strip()),
                        "raw_key": val.strip(),
                        "key": val.strip(),
                    }
                )
                api_keys_map[prov_id] = val.strip()

        current_active_key = configured_providers[0]["key"] if configured_providers else ""

        metadata = {
            "configured_count": len(configured_providers),
            "providers": configured_providers,
            "current_key": current_active_key,
            "api_keys": api_keys_map,
        }

        # Case 1: An LLM provider is configured
        if configured_providers:
            first_p = configured_providers[0]
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Multimodal LLM API Key",
                status=ProbeStatus.PASS,
                is_blocker=self.is_blocker,
                summary=f"Active ({first_p['label']})",
                description=f"{first_p['label']} multimodal API credential is active ({first_p['masked']}).",
                metadata=metadata,
                actions=[
                    ProbeAction(
                        action_type="hint",
                        label="Provider Active",
                        payload=f"{first_p['label']} multimodal vision endpoint is active.",
                    )
                ],
            )

        # Case 2: No LLM key configured
        return ProbeResult(
            id=self.probe_id,
            category=self.category,
            title="Multimodal LLM API Key",
            status=ProbeStatus.FAIL,
            is_blocker=self.is_blocker,
            summary="Key Missing",
            description="No Multimodal LLM credential (e.g. OPENAI_API_KEY, ANTHROPIC_API_KEY) found in environment or .env file.",
            metadata=metadata,
            actions=[
                ProbeAction(
                    action_type="command",
                    label="Run Artemis Init",
                    payload="artemis init",
                ),
                ProbeAction(
                    action_type="link",
                    label="Get OpenAI Key",
                    payload="https://platform.openai.com/api-keys",
                ),
            ],
        )


class VisionOCRProbe(BaseProbe):
    """Probe verifying that local OCR is importable and usable."""

    @property
    def probe_id(self) -> str:
        return "vision_ocr_key"

    @property
    def category(self) -> ProbeCategory:
        return ProbeCategory.CREDENTIALS

    @property
    def is_blocker(self) -> bool:
        return False

    async def probe(self) -> ProbeResult:
        from artemis.utils.ocr_api import is_ocr_configured

        if is_ocr_configured():
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Screen Text OCR (Optional)",
                status=ProbeStatus.PASS,
                is_blocker=False,
                summary="Active (Local OCR)",
                description=(
                    "Local OCR (RapidOCR on onnxruntime) reads on-screen text with no"
                    " credential and no network call."
                ),
                metadata={"configured": True, "engine": "rapidocr", "needs_key": False},
                actions=[
                    ProbeAction(
                        action_type="hint",
                        label="OCR Enabled",
                        payload="OCR is active and will fuse text with UI XML hierarchy.",
                    )
                ],
            )

        return ProbeResult(
            id=self.probe_id,
            category=self.category,
            title="Screen Text OCR (Optional)",
            status=ProbeStatus.PASS,
            is_blocker=False,
            summary="Not Installed (Optional)",
            description=(
                "rapidocr is not installed. Perception uses the UI XML hierarchy without OCR."
                " Install it with `uv sync`."
            ),
            metadata={"configured": False, "engine": None},
            actions=[
                ProbeAction(
                    action_type="hint",
                    label="Standard XML Perception",
                    payload="Artemis uses pure UI layout parsing and Set-of-Marks visual grounding.",
                )
            ],
        )
