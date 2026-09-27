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

import json
import logging
import time
from typing import Any, get_args

logger = logging.getLogger(__name__)


class ModelService:
    """Service handling agent architecture profile detection and metadata formatting."""

    # parse_llm_config re-reads and re-validates the config file on every call;
    # /api/sessions calls this once per session row, so cache (provider, model_id)
    # for a short TTL. Config edits take effect within _LLM_INFO_TTL seconds.
    _LLM_INFO_TTL = 10.0
    _llm_info_cache: tuple[float, str, str] | None = None

    @classmethod
    def _get_llm_provider_and_model(cls) -> tuple[str, str]:
        cached = cls._llm_info_cache
        now = time.monotonic()
        if cached and now - cached[0] < cls._LLM_INFO_TTL:
            return cached[1], cached[2]
        provider = "google"
        model_id = "gemini-3.7-flash"
        try:
            from artemis.config import parse_llm_config

            llm_cfg = parse_llm_config()
            if llm_cfg and llm_cfg.operator:
                provider = str(llm_cfg.operator.provider)
                model_id = str(llm_cfg.operator.model)
            elif llm_cfg and llm_cfg.default:
                provider = str(llm_cfg.default.provider)
                model_id = str(llm_cfg.default.model)
        except Exception as exc:
            # Unreadable/invalid LLM config: fall back to display defaults
            # (cached for the TTL, so this logs at most once per window).
            logger.warning("Could not resolve LLM config for display: %s", exc)
        cls._llm_info_cache = (now, provider, model_id)
        return provider, model_id

    @classmethod
    def get_active_model_info(
        cls,
        profile: str | None = None,
        llm_model: str | None = None,
        llm_provider: str | None = None,
    ) -> dict[str, str]:
        """Return active architecture and underlying LLM model configuration.

        Args:
            profile: Resolved architecture ('flash' / 'pro') or None for Flash.
            llm_model: Per-task model override recorded for the session. It wins
                the ``id`` so the reported model is the one that actually ran.
            llm_provider: Provider of that override. Only replaces the
                configured provider when the task pinned it; without it each
                node keeps its own provider.
        """
        # 1. Determine underlying LLM model and provider from config (cached)
        provider, model_id = cls._get_llm_provider_and_model()

        # 1b. A recorded per-task override outranks the configured defaults.
        if llm_model:
            model_id = str(llm_model)
        if llm_provider:
            provider = str(llm_provider)

        # 2. Determine agent architecture name (Flash vs Pro)
        arch_name = "Flash"
        if profile:
            p_lower = str(profile).lower()
            if "pro" in p_lower:
                arch_name = "Pro"
            elif "flash" in p_lower:
                arch_name = "Flash"
            else:
                arch_name = str(profile).capitalize()

        return {
            "name": arch_name,
            "id": model_id,
            "provider": provider,
            "architecture": f"ARTEMIS {arch_name}",
        }

    @classmethod
    def get_llm_options(cls) -> dict[str, Any]:
        """Providers, the raw ``artemis.jsonc`` presets, and the configured default.

        Feeds the Console model picker: the provider allowlist is the same
        ``LLMProvider`` literal the override validates against, and the presets
        are read straight from the config so the choices stay in sync with
        ``artemis.jsonc`` instead of being duplicated in the UI. An unreadable
        config yields no presets rather than an error, matching how the rest of
        the display paths degrade.
        """
        from artemis.config.constants import ARTEMIS_CONFIG_FILENAME, LLMProvider
        from artemis.config.paths import get_config_path
        from artemis.utils.file import load_jsonc

        provider, model = cls._get_llm_provider_and_model()
        presets: list[dict[str, str]] = []
        try:
            with open(get_config_path(ARTEMIS_CONFIG_FILENAME), encoding="utf-8") as f:
                raw = load_jsonc(f)
        except Exception as exc:
            logger.warning("Could not read LLM presets for display: %s", exc)
            raw = None
        block = raw.get("presets") if isinstance(raw, dict) else None
        if isinstance(block, dict):
            for name, preset in block.items():
                if isinstance(preset, dict):
                    presets.append(
                        {
                            "name": str(name),
                            "provider": str(preset.get("provider") or ""),
                            "model": str(preset.get("model") or ""),
                        }
                    )
        return {
            "providers": list(get_args(LLMProvider)),
            "presets": presets,
            "default": {"provider": provider, "model": model},
        }

    @staticmethod
    def resolve_session_llm_override(row_dict: dict[str, Any]) -> tuple[str | None, str | None]:
        """Read the per-task LLM override a session recorded in its device_info.

        Reads the same schemaless JSON ``resolve_session_profile`` parses. Rows
        written before the override existed carry neither key, which is reported
        as "no override" so the caller falls back to the configured model.
        """
        d_info_raw = row_dict.get("device_info")
        if not d_info_raw:
            return (None, None)
        try:
            d_info = json.loads(d_info_raw) if isinstance(d_info_raw, str) else d_info_raw
        except (ValueError, TypeError):
            # Malformed device_info JSON: treat the run as un-overridden.
            return (None, None)
        if not isinstance(d_info, dict):
            return (None, None)
        model = d_info.get("llm_model")
        provider = d_info.get("llm_provider")
        return (
            str(model).strip() or None if isinstance(model, str) else None,
            str(provider).strip().lower() or None if isinstance(provider, str) else None,
        )

    @staticmethod
    def resolve_session_profile(
        row_dict: dict[str, Any],
        llm_trace_payloads: list[str] | None = None,
        running_profile: str | None = None,
        agent_names: list[str] | None = None,
    ) -> str | None:
        """Resolve the Artemis agent architecture profile ('flash' or 'pro') for a session."""
        sess_profile = None
        d_info_raw = row_dict.get("device_info")
        if d_info_raw:
            try:
                d_info = json.loads(d_info_raw) if isinstance(d_info_raw, str) else d_info_raw
                if isinstance(d_info, dict):
                    p = d_info.get("profile")
                    if p:
                        p_str = str(p).lower()
                        if "pro" in p_str:
                            return "pro"
                        elif "flash" in p_str:
                            return "flash"
            except (ValueError, TypeError):
                # Malformed device_info JSON: try the other profile sources.
                pass

        if (row_dict.get("status") == "running") and running_profile:
            p_str = str(running_profile).lower()
            if "pro" in p_str:
                return "pro"
            elif "flash" in p_str:
                return "flash"

        # Check Agent/Trace names - the most definitive indicator of architecture
        if agent_names:
            agent_names_lower = [str(a).lower() for a in agent_names]
            if any(
                name in ("planner", "validator", "summarizer", "operator", "checker", "diagnoser")
                for name in agent_names_lower
            ):
                return "pro"
            if any("flashrunner" in name for name in agent_names_lower):
                return "flash"

        # Check LLM traces for agent name
        if llm_trace_payloads:
            for tr_payload in llm_trace_payloads:
                if tr_payload:
                    try:
                        p_obj = (
                            json.loads(tr_payload) if isinstance(tr_payload, str) else tr_payload
                        )
                        if isinstance(p_obj, dict):
                            agent_name = str(p_obj.get("agent") or p_obj.get("name") or "").lower()
                            if "flashrunner" in agent_name:
                                return "flash"
                            if any(
                                x in agent_name
                                for x in ("planner", "validator", "operator", "checker")
                            ):
                                return "pro"
                    except (ValueError, TypeError):
                        # Malformed trace payload JSON: skip this trace.
                        pass

        return None


model_service = ModelService()
