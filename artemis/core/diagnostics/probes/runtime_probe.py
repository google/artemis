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

"""Python Runtime and System Configuration Diagnostic Probes."""

import platform
import sys

from artemis.config import parse_llm_config
from artemis.core.diagnostics.probes.base import BaseProbe
from artemis.core.diagnostics.schema import (
    ProbeAction,
    ProbeCategory,
    ProbeResult,
    ProbeStatus,
)


class PythonRuntimeProbe(BaseProbe):
    """Probe inspecting Python runtime environment, version, and architecture."""

    @property
    def probe_id(self) -> str:
        return "python_runtime"

    @property
    def category(self) -> ProbeCategory:
        return ProbeCategory.RUNTIME

    @property
    def is_blocker(self) -> bool:
        return True

    async def probe(self) -> ProbeResult:
        py_ver_tuple = sys.version_info
        py_ver_str = f"{py_ver_tuple.major}.{py_ver_tuple.minor}.{py_ver_tuple.micro}"
        is_supported = py_ver_tuple >= (3, 12)
        arch = platform.machine()
        executable = sys.executable

        metadata = {
            "version": py_ver_str,
            "major": py_ver_tuple.major,
            "minor": py_ver_tuple.minor,
            "micro": py_ver_tuple.micro,
            "executable": executable,
            "architecture": arch,
            "platform": sys.platform,
            "is_supported": is_supported,
        }

        if is_supported:
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Python Runtime Environment",
                status=ProbeStatus.PASS,
                is_blocker=self.is_blocker,
                summary=f"Python {py_ver_str} Ready",
                description=f"Python {py_ver_str} ({arch}, 64-bit) detected at {executable}. Satisfies requirement (>= 3.12).",
                metadata=metadata,
                actions=[
                    ProbeAction(
                        action_type="hint",
                        label="Python Runtime OK",
                        payload=f"Python {py_ver_str} ({arch})",
                    )
                ],
            )
        else:
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Python Runtime Environment",
                status=ProbeStatus.FAIL,
                is_blocker=self.is_blocker,
                summary=f"Python {py_ver_str} Unsupported",
                description=f"Python {py_ver_str} detected. Artemis requires Python >= 3.12.",
                metadata=metadata,
                actions=[
                    ProbeAction(
                        action_type="link",
                        label="Download Python 3.12+",
                        payload="https://www.python.org/downloads/",
                    ),
                    ProbeAction(
                        action_type="command",
                        label="Install with uv",
                        payload="uv python install 3.12",
                    ),
                ],
            )


class SystemConfigProbe(BaseProbe):
    """Probe verifying config/artemis.jsonc and settings integrity."""

    @property
    def probe_id(self) -> str:
        return "system_config"

    @property
    def category(self) -> ProbeCategory:
        return ProbeCategory.RUNTIME

    @property
    def is_blocker(self) -> bool:
        return True

    async def probe(self) -> ProbeResult:
        try:
            cfg = parse_llm_config()
            planner_model = getattr(getattr(cfg, "planner", None), "model", "Gemini 2.5 Flash")
            metadata = {
                "valid": True,
                "config_file": "config/artemis.jsonc",
                "planner_model": planner_model,
            }
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Artemis System Configuration",
                status=ProbeStatus.PASS,
                is_blocker=self.is_blocker,
                summary="Config Valid",
                description=f"System configuration loaded and validated (Planner model: {planner_model}).",
                metadata=metadata,
                actions=[
                    ProbeAction(
                        action_type="hint",
                        label="Config Healthy",
                        payload="config/artemis.jsonc is loaded and valid.",
                    )
                ],
            )
        except Exception as e:
            metadata = {
                "valid": False,
                "config_file": "config/artemis.jsonc",
                "error": str(e),
            }
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Artemis System Configuration",
                status=ProbeStatus.FAIL,
                is_blocker=self.is_blocker,
                summary="Config Error",
                description=f"Failed to parse config/artemis.jsonc: {e}",
                metadata=metadata,
                actions=[
                    ProbeAction(
                        action_type="command",
                        label="Run Artemis Init",
                        payload="artemis init",
                    )
                ],
            )
