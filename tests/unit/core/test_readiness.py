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

"""Unit tests for the readiness core shared by ``artemis doctor`` and ``mobile_diagnose``."""

import asyncio
import time
from unittest.mock import AsyncMock

from artemis.core.diagnostics import readiness
from artemis.core.diagnostics.probes.host_probe import IntegrationHostProbe
from artemis.core.diagnostics.readiness import (
    CHECK_ORDER,
    adb_keys_corrupted,
    base_verdict,
    collect_readiness,
    sort_by_fix_order,
)
from artemis.core.diagnostics.schema import (
    ProbeCategory,
    ProbeResult,
    ProbeStatus,
    SystemReadinessReport,
)


def _probe(
    probe_id: str,
    status: ProbeStatus = ProbeStatus.PASS,
    *,
    blocker: bool = True,
    metadata: dict | None = None,
) -> ProbeResult:
    return ProbeResult(
        id=probe_id,
        title=probe_id,
        category=ProbeCategory.RUNTIME,
        status=status,
        summary=status.value,
        description=f"{probe_id} is {status.value}",
        is_blocker=blocker,
        metadata=metadata or {},
    )


# --------------------------------------------------------------------------- #
# base_verdict
# --------------------------------------------------------------------------- #


def test_base_verdict_without_any_blocker_is_blocked():
    assert base_verdict([]) == "blocked"
    assert base_verdict([_probe("toolchain", blocker=False)]) == "blocked"


def test_base_verdict_failing_blocker_is_blocked():
    results = [
        _probe("python_runtime"),
        _probe("android_adb", ProbeStatus.FAIL),
        _probe("toolchain", blocker=False),
    ]
    assert base_verdict(results) == "blocked"


def test_base_verdict_passing_blockers_with_warning_optional_is_degraded():
    results = [
        _probe("python_runtime"),
        _probe("android_adb"),
        _probe("toolchain", ProbeStatus.WARN, blocker=False),
    ]
    assert base_verdict(results) == "degraded"


def test_base_verdict_all_pass_is_ready():
    results = [_probe("python_runtime"), _probe("android_adb"), _probe("toolchain", blocker=False)]
    assert base_verdict(results) == "ready"


def test_base_verdict_warning_blocker_is_blocked_not_degraded():
    results = [_probe("python_runtime"), _probe("integration_host", ProbeStatus.WARN)]
    assert base_verdict(results) == "blocked"


# --------------------------------------------------------------------------- #
# adb_keys_corrupted
# --------------------------------------------------------------------------- #


def test_adb_keys_corrupted_reads_the_adb_probe_flag():
    corrupted = _probe("android_adb", metadata={"adb_keys": {"is_corrupted": True}})
    healthy = _probe("android_adb", metadata={"adb_keys": {"is_corrupted": False}})
    assert adb_keys_corrupted([_probe("python_runtime"), corrupted]) is True
    assert adb_keys_corrupted([_probe("python_runtime"), healthy]) is False


def test_adb_keys_corrupted_is_false_without_probe_or_metadata():
    assert adb_keys_corrupted([]) is False
    assert adb_keys_corrupted([_probe("python_runtime")]) is False
    assert adb_keys_corrupted([_probe("android_adb")]) is False
    assert adb_keys_corrupted([_probe("android_adb", metadata={"adb_keys": None})]) is False


# --------------------------------------------------------------------------- #
# ordering / collection
# --------------------------------------------------------------------------- #


def test_sort_by_fix_order_follows_check_order_and_appends_unknown_ids():
    results = [
        _probe("custom_probe"),
        _probe("toolchain", blocker=False),
        _probe("android_adb"),
        _probe("python_runtime"),
    ]
    ordered = [r.id for r in sort_by_fix_order(results)]
    assert ordered == ["python_runtime", "android_adb", "toolchain", "custom_probe"]
    assert list(CHECK_ORDER) == [
        "python_runtime",
        "system_config",
        "integration_host",
        "gemini_api_key",
        "android_adb",
        "toolchain",
        "vision_ocr_key",
    ]


def test_collect_readiness_gathers_engine_report_and_host_probe(monkeypatch):
    report = SystemReadinessReport(
        overall_ready=True,
        blocker_count=1,
        passed_blocker_count=1,
        probes=[_probe("python_runtime")],
        os_type="windows",
        timestamp=time.time(),
    )
    host = _probe("integration_host")
    run_all = AsyncMock(return_value=report)
    monkeypatch.setattr(readiness.readiness_engine, "run_all", run_all)
    monkeypatch.setattr(IntegrationHostProbe, "probe", AsyncMock(return_value=host))

    got_report, got_host = asyncio.run(collect_readiness())

    assert got_report is report
    assert got_host is host
    run_all.assert_awaited_once_with(force_refresh=True)
