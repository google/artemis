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
from typing import Any


class ModelService:
    """Service handling agent architecture profile detection and metadata formatting."""

    @staticmethod
    def get_active_model_info(profile: str | None = None) -> dict[str, str]:
        """Return active architecture and underlying LLM model configuration."""
        # 1. Determine underlying LLM model and provider from config
        model_id = "gemini-3.7-flash"
        provider = "google"
        try:
            from artemis.config import parse_llm_config

            llm_cfg = parse_llm_config()
            if llm_cfg and llm_cfg.operator:
                provider = str(llm_cfg.operator.provider)
                model_id = str(llm_cfg.operator.model)
            elif llm_cfg and llm_cfg.default:
                provider = str(llm_cfg.default.provider)
                model_id = str(llm_cfg.default.model)
        except Exception:
            pass

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
            except Exception:
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
                    except Exception:
                        pass

        return None


model_service = ModelService()
