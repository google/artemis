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

"""Readiness collection and verdict shared by every diagnosis entry point.

``artemis doctor`` and the ``mobile_diagnose`` MCP tool must agree on which
probes run, in which order they are fixed, and what "ready" means. This module
holds exactly that common core; each entry point layers its own extra
conditions, fix policy and rendering on top.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Literal

from artemis.core.diagnostics.engine import readiness_engine
from artemis.core.diagnostics.probes.host_probe import IntegrationHostProbe
from artemis.core.diagnostics.schema import ProbeResult, ProbeStatus, SystemReadinessReport

Verdict = Literal["ready", "degraded", "blocked"]

#: Fix order. Runtime problems mask everything after them, credentials mask
#: device problems (a task cannot start without a model), and the toolchain is
#: optional.
CHECK_ORDER: dict[str, int] = {
    "python_runtime": 0,
    "system_config": 1,
    "integration_host": 2,
    "gemini_api_key": 3,
    "android_adb": 4,
    "toolchain": 5,
    "vision_ocr_key": 6,
}


def sort_by_fix_order(results: Sequence[ProbeResult]) -> list[ProbeResult]:
    """Order probe results so the first problem listed is the one to fix first."""
    return sorted(results, key=lambda r: CHECK_ORDER.get(r.id, len(CHECK_ORDER)))


async def collect_readiness() -> tuple[SystemReadinessReport, ProbeResult]:
    """Run the engine probes and the integration-host probe concurrently."""
    report, host = await asyncio.gather(
        readiness_engine.run_all(force_refresh=True),
        IntegrationHostProbe().probe(),
    )
    return report, host


def base_verdict(results: Sequence[ProbeResult]) -> Verdict:
    """Verdict from probe results alone.

    Blocked when there is no blocker at all (nothing was checked) or any
    blocker is not PASS; degraded when only optional checks fail; ready
    otherwise.
    """
    blockers = [r for r in results if r.is_blocker]
    if not blockers or any(r.status is not ProbeStatus.PASS for r in blockers):
        return "blocked"
    if any(r.status is not ProbeStatus.PASS for r in results):
        return "degraded"
    return "ready"


def adb_keys_corrupted(results: Sequence[ProbeResult]) -> bool:
    """Whether the ADB probe reported corrupted RSA keys (the one self-heal both entry points share)."""
    adb = next((r for r in results if r.id == "android_adb"), None)
    if adb is None:
        return False
    return bool((adb.metadata.get("adb_keys") or {}).get("is_corrupted"))
