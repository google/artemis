#!/usr/bin/env python3
"""Run a deterministic, real-device acceptance test for the video analyzer.

The script intentionally bypasses the autonomous planner. It uses the same
ARTEMIS device driver, recorder, controller, configured video model, and
persistent blackboard as production, while keeping the UI ground truth fully
deterministic and independently observable through UIAutomator.

Usage:
    python scripts/real_device_video_acceptance.py --device-id SERIAL
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

from adbutils import AdbClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from artemis.agents.video_analyzer.video_analyzer import VideoAnalyzer
from artemis.config import initialize_llm_config, settings
from artemis.context import ArtemisContext, DeviceContext, DevicePlatform, ExecutionSetup
from artemis.controllers.controller_factory import get_controller
from artemis.runtime import DeviceExecutionLock
from artemis.sdk.builders import Builders
from artemis.sdk.types import AgentProfile
from artemis.clients.ui_automator_client import UIAutomatorClient


CALCULATOR_PACKAGE = "com.google.android.calculator"
RESOURCE_PREFIX = f"{CALCULATOR_PACKAGE}:id/"

# Raw-device coordinates verified on a 1080x2424 Pixel. They are only used if
# dynamic resource-id lookup fails, and are scaled to the actual screen size.
REFERENCE_SIZE = (1080, 2424)
FALLBACK_COORDINATES = {
    "clear": (140, 1190),
    "digit_1": (140, 1950),
    "digit_2": (406, 1950),
    "digit_3": (674, 1950),
    "digit_4": (140, 1696),
    "op_add": (940, 1950),
    "digit_7": (140, 1442),
    "digit_8": (406, 1442),
    "op_mul": (940, 1442),
    "eq": (940, 2202),
    "history_toggle_button": (95, 262),
    "drag_handle_view_for_history": (540, 678),
}


@dataclass
class GroundTruthEvent:
    name: str
    relative_time_seconds: float
    observed_formula: str
    observed_result: str
    history_state: str


def _element_value(element: dict[str, Any] | None) -> str:
    if not element:
        return ""
    return str(element.get("text") or element.get("content-desc") or "").strip()


def _compact(value: str) -> str:
    return (
        re.sub(r"[\s`*_\[\](){}:;,.，。：；]", "", value.lower())
        .replace("×", "x")
        .replace("＊", "x")
        .replace("=", "=")
        .replace("＝", "=")
    )


def _contains_any(value: str, candidates: tuple[str, ...]) -> bool:
    return any(candidate in value for candidate in candidates)


def _grade_answer(kind: str, answer: str) -> tuple[bool, list[str]]:
    compact = _compact(answer)
    failures: list[str] = []

    if kind in {"timeline", "calculations"}:
        if "12+34" not in compact:
            failures.append("missing 12+34")
        if "46" not in compact:
            failures.append("missing result 46")
        if not _contains_any(compact, ("7x8", "7·8")):
            failures.append("missing 7×8")
        if "56" not in compact:
            failures.append("missing result 56")

    if kind in {"timeline", "history"}:
        if not _contains_any(compact, ("历史", "history")):
            failures.append("missing history panel")
        if not _contains_any(compact, ("打开", "开启", "open", "shown", "visible")):
            failures.append("missing history-open transition")
        if not _contains_any(compact, ("关闭", "close", "closed", "hidden", "返回")):
            failures.append("missing history-close transition")

    if answer.startswith(("PARTIAL VIDEO ANALYSIS", "All sub-agent chunks failed", "Error:")):
        failures.append("analysis was partial or failed")
    return not failures, failures


async def _find_by_resource(controller, resource_name: str) -> dict[str, Any] | None:
    data = await controller.driver.get_screen_data(skip_settling=True)
    expected = RESOURCE_PREFIX + resource_name
    for element in data.ui_elements or []:
        resource_id = str(element.get("resource_id") or element.get("resource-id") or "")
        if resource_id == expected or resource_id.endswith("/" + resource_name):
            return element
    return None


async def _tap(controller, resource_name: str, *, settle: float = 0.35) -> str:
    result = await controller.tap_element(resource_id=RESOURCE_PREFIX + resource_name)
    method = "resource-id"
    if result.error:
        if resource_name not in FALLBACK_COORDINATES:
            raise RuntimeError(f"Dynamic locator failed for {resource_name}: {result.error}")
        width, height = controller.driver.screen_size
        ref_x, ref_y = FALLBACK_COORDINATES[resource_name]
        x = round(ref_x * width / REFERENCE_SIZE[0])
        y = round(ref_y * height / REFERENCE_SIZE[1])
        fallback = await controller.tap_at(x, y)
        if fallback.error:
            raise RuntimeError(
                f"Dynamic and coordinate locators failed for {resource_name}: "
                f"{result.error}; {fallback.error}"
            )
        method = f"coordinate-fallback({x},{y})"
    await asyncio.sleep(settle)
    return method


async def _observe(controller) -> tuple[str, str, str, list[str]]:
    data = await controller.driver.get_screen_data(skip_settling=True)
    formula = ""
    result = ""
    history_state = ""
    visible_values: list[str] = []
    for element in data.ui_elements or []:
        resource_id = str(element.get("resource_id") or element.get("resource-id") or "")
        value = _element_value(element)
        if value:
            visible_values.append(value)
        if resource_id.endswith("/formula"):
            formula = value
        elif resource_id.endswith("/result_preview") or resource_id.endswith("/result_final"):
            result = value
        elif "history_toggle_button" in resource_id:
            history_state = value
    return formula, result, history_state, visible_values


def _assert_calculation(
    formula: str,
    result: str,
    expression: str,
    expected: str,
    *,
    require_expression: bool = True,
) -> None:
    observed = _compact(f"{formula} {result}")
    normalized_expression = _compact(expression)
    expression_missing = require_expression and normalized_expression not in observed
    if expression_missing or expected not in observed:
        raise AssertionError(
            f"Expected {expression}={expected}; observed formula={formula!r}, result={result!r}"
        )


async def _run_ui_sequence(
    controller, recording_started: float
) -> tuple[list[GroundTruthEvent], dict[str, str]]:
    device = controller.driver.device
    await asyncio.to_thread(
        device.shell,
        f"monkey -p {CALCULATOR_PACKAGE} -c android.intent.category.LAUNCHER 1",
    )
    await asyncio.sleep(1.0)

    locator_methods: dict[str, str] = {}
    events: list[GroundTruthEvent] = []

    # Calculator preserves its history drawer state across launches. Normalize
    # the starting layout so the deterministic sequence never inherits a
    # shifted keypad from a previous run.
    _, _, initial_history_state, _ = await _observe(controller)
    if _contains_any(_compact(initial_history_state), ("已开启", "open", "on")):
        locator_methods["normalize_history_closed"] = await _tap(
            controller, "history_toggle_button", settle=0.7
        )
        _, _, normalized_history_state, _ = await _observe(controller)
        if not _contains_any(_compact(normalized_history_state), ("已关闭", "close", "off")):
            raise AssertionError(
                f"Could not normalize history drawer; state={normalized_history_state!r}"
            )

    locator_methods["clear_1"] = await _tap(controller, "clear")
    for resource_name in ("digit_1", "digit_2", "op_add", "digit_3", "digit_4"):
        locator_methods[resource_name + "_first"] = await _tap(controller, resource_name)
    formula_before_equals, preview, _, _ = await _observe(controller)
    _assert_calculation(formula_before_equals, preview, "12+34", "46")
    locator_methods["eq_first"] = await _tap(controller, "eq")
    formula, result, history_state, _ = await _observe(controller)
    _assert_calculation(formula, result, "12+34", "46", require_expression=False)
    events.append(
        GroundTruthEvent(
            "12+34=46",
            round(time.monotonic() - recording_started, 2),
            formula_before_equals,
            result,
            history_state,
        )
    )

    locator_methods["clear_2"] = await _tap(controller, "clear")
    for resource_name in ("digit_7", "op_mul", "digit_8"):
        locator_methods[resource_name + "_second"] = await _tap(controller, resource_name)
    formula_before_equals, preview, _, _ = await _observe(controller)
    _assert_calculation(formula_before_equals, preview, "7x8", "56")
    locator_methods["eq_second"] = await _tap(controller, "eq")
    formula, result, history_state, _ = await _observe(controller)
    _assert_calculation(formula, result, "7x8", "56", require_expression=False)
    events.append(
        GroundTruthEvent(
            "7×8=56",
            round(time.monotonic() - recording_started, 2),
            formula_before_equals,
            result,
            history_state,
        )
    )

    locator_methods["history_open"] = await _tap(controller, "history_toggle_button", settle=0.7)
    formula, result, history_state, visible = await _observe(controller)
    if not _contains_any(_compact(history_state), ("已开启", "open", "on")):
        raise AssertionError(f"History panel did not open; state={history_state!r}")
    # The compact history drawer only exposes the newest item. Expand it so
    # both independently verified calculations become simultaneously visible
    # evidence for the video model.
    locator_methods["history_expand"] = await _tap(
        controller, "drag_handle_view_for_history", settle=0.7
    )
    formula, result, history_state, visible = await _observe(controller)
    history_text = _compact(" ".join(visible))
    for expected in ("12+34", "46", "7x8", "56"):
        if expected not in history_text:
            raise AssertionError(f"History is missing {expected}; visible={visible!r}")
    events.append(
        GroundTruthEvent(
            "history_open",
            round(time.monotonic() - recording_started, 2),
            formula,
            result,
            history_state,
        )
    )

    locator_methods["history_close"] = await _tap(controller, "history_toggle_button", settle=0.7)
    formula, result, history_state, _ = await _observe(controller)
    if not _contains_any(_compact(history_state), ("已关闭", "close", "off")):
        raise AssertionError(f"History panel did not close; state={history_state!r}")
    _assert_calculation(formula, result, "7x8", "56", require_expression=False)
    events.append(
        GroundTruthEvent(
            "history_close",
            round(time.monotonic() - recording_started, 2),
            formula,
            result,
            history_state,
        )
    )
    await asyncio.sleep(1.0)
    return events, locator_methods


async def _analyze(analyzer: VideoAnalyzer, end_time: float, repeats: int) -> list[dict[str, Any]]:
    queries = {
        "timeline": (
            "List four facts from the recording in chronological order with visible evidence and relative timestamps: "
            "First complete 12+34=46; then complete 7×8=56; subsequently open the history panel; finally close the history panel and return to the main screen showing 56. "
            "Do not guess based on prompts, only report what is actually visible in the video."
        ),
        "calculations": (
            "Only verify the two calculations actually entered in the recording and their final visible results. Must explicitly write the complete expressions 12+34=46 and 7×8=56, "
            "and report them in order of occurrence; if any is not visible, clearly state that it is missing."
        ),
        "history": (
            "Only verify state changes of the history panel: whether it was opened after completing both calculations, whether it displayed both calculation records, "
            "and whether it was subsequently closed returning to the calculator main screen. Please explicitly use 'open' and 'close' to describe the two transitions."
        ),
    }
    results: list[dict[str, Any]] = []
    for kind, query in queries.items():
        first_started = time.monotonic()
        first = await analyzer.exec_spawn_sub_agent(0.0, end_time, query)
        first_elapsed = round(time.monotonic() - first_started, 2)
        passed, failures = _grade_answer(kind, first)
        item: dict[str, Any] = {
            "kind": kind,
            "query": query,
            "first_result": first,
            "first_elapsed_seconds": first_elapsed,
            "accuracy_passed": passed,
            "accuracy_failures": failures,
            "cache_replays": [],
        }
        for _ in range(repeats):
            replay_started = time.monotonic()
            replay = await analyzer.exec_spawn_sub_agent(0.0, end_time, query)
            replay_elapsed = round(time.monotonic() - replay_started, 3)
            replay_passed, replay_failures = _grade_answer(kind, replay)
            item["cache_replays"].append(
                {
                    "result": replay,
                    "elapsed_seconds": replay_elapsed,
                    "cache_hit": replay.startswith("CACHED VIDEO ANALYSIS:"),
                    "accuracy_passed": replay_passed,
                    "accuracy_failures": replay_failures,
                }
            )
        results.append(item)
    return results


async def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    llm_config = initialize_llm_config()
    profile = AgentProfile(name="acceptance", llm_config=llm_config)
    agent_config = Builders.AgentConfig.with_default_profile(profile).build()
    adb_client = AdbClient(host=settings.ADB_HOST or "127.0.0.1", port=settings.ADB_PORT or 5037)
    device = adb_client.device(serial=args.device_id)
    width, height = await asyncio.to_thread(device.window_size)
    ui_client = UIAutomatorClient(device_id=args.device_id)
    ctx = ArtemisContext(
        device=DeviceContext(
            host_platform="WINDOWS" if os.name == "nt" else "LINUX",
            mobile_platform=DevicePlatform.ANDROID,
            device_id=args.device_id,
            device_width=width,
            device_height=height,
        ),
        llm_config=llm_config,
        agent_config=agent_config,
        adb_client=adb_client,
        ui_adb_client=ui_client,
        execution_setup=ExecutionSetup(
            traces_path=output_dir.parent,
            trace_name=output_dir.name,
            video_recording_tools_enabled=True,
        ),
    )

    lock = DeviceExecutionLock(args.device_id, description="real-device video acceptance")
    await asyncio.to_thread(lock.acquire, timeout=args.device_lock_timeout)
    controller = get_controller(ctx)
    recording_stopped = False
    started_wall = datetime.now().astimezone().isoformat()
    report: dict[str, Any] = {
        "device_id": args.device_id,
        "started_at": started_wall,
        "output_dir": str(output_dir),
        "status": "failed",
    }
    try:
        await asyncio.to_thread(ui_client.connect)
        recording = await controller.start_video_recording(output_dir=output_dir)
        if not recording.success:
            raise RuntimeError(recording.message)
        recording_clock = time.monotonic()
        events, locator_methods = await _run_ui_sequence(controller, recording_clock)
        end_time = round(max(0.5, time.monotonic() - recording_clock - 0.55), 1)

        analyzer = VideoAnalyzer(ctx)
        analyses = await _analyze(analyzer, end_time, args.cache_replays)
        stop_result = await controller.stop_video_recording()
        recording_stopped = True

        accuracy_checks = [item["accuracy_passed"] for item in analyses]
        cache_checks = [
            replay["cache_hit"] and replay["accuracy_passed"]
            for item in analyses
            for replay in item["cache_replays"]
        ]
        report.update(
            {
                "ground_truth": [asdict(event) for event in events],
                "locator_methods": locator_methods,
                "analysis_interval": [0.0, end_time],
                "analyses": analyses,
                "blackboard_metrics": analyzer.blackboard.metrics(),
                "recording": {
                    "success": stop_result.success,
                    "message": stop_result.message,
                    "video_path": str(stop_result.video_path) if stop_result.video_path else None,
                    "video_id": str(stop_result.video_id) if stop_result.video_id else None,
                    "generation": stop_result.generation,
                },
                "accuracy_pass_rate": (
                    sum(accuracy_checks) / len(accuracy_checks) if accuracy_checks else 0.0
                ),
                "cache_replay_pass_rate": (
                    sum(cache_checks) / len(cache_checks) if cache_checks else 0.0
                ),
            }
        )
        passed = bool(
            stop_result.success
            and accuracy_checks
            and all(accuracy_checks)
            and cache_checks
            and all(cache_checks)
        )
        report["status"] = "passed" if passed else "failed"
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if not recording_stopped:
            try:
                stop_result = await controller.stop_video_recording()
                report["emergency_recording_stop"] = {
                    "success": stop_result.success,
                    "message": stop_result.message,
                    "video_path": str(stop_result.video_path) if stop_result.video_path else None,
                }
            except Exception as stop_error:
                report["emergency_recording_stop_error"] = str(stop_error)
        try:
            await asyncio.to_thread(ui_client.disconnect)
        finally:
            await asyncio.to_thread(lock.release)
        report["finished_at"] = datetime.now().astimezone().isoformat()
        report_path = output_dir / "acceptance-report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--cache-replays", type=int, default=2)
    parser.add_argument("--device-lock-timeout", type=float, default=30.0)
    parser.add_argument(
        "--output-dir",
        default=str(
            PROJECT_ROOT
            / "traces"
            / f"real_device_video_acceptance_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        ),
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args))
    except Exception as error:
        print(f"Acceptance test failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
