#!/usr/bin/env python3
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

"""Background execution runner for MCP autonomous mobile automation tasks."""

import argparse
import asyncio
import json
import logging
import os
import sys
import traceback

from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from artemis.config.paths import get_app_dir

    app_dir = str(get_app_dir())
    global_env = os.path.join(app_dir, ".env")
    if os.path.exists(global_env):
        load_dotenv(global_env)
    else:
        load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
except Exception:
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from artemis.runtime import trace_store
from mcp_server.notifiers import notify
from mcp_server.utils import device_utils


async def _initialize_agent(
    agent,
    *,
    retry_count: int,
    retry_wait_seconds: int,
    timeout_seconds: float,
) -> None:
    """Initialize one SDK agent without allowing a detached runner to hang forever."""
    try:
        await asyncio.wait_for(
            agent.init(
                retry_count=retry_count,
                retry_wait_seconds=retry_wait_seconds,
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError as error:
        raise TimeoutError(
            "Artemis Agent initialization exceeded "
            f"{timeout_seconds:.1f}s; check ADB/UIAutomator health and the device queue."
        ) from error


def resolve_profile_file() -> str | None:
    """Resolves the LLM configuration profile across multiple locations."""
    candidates = []
    artemis_config_dir = os.getenv("ARTEMIS_CONFIG_DIR")
    if artemis_config_dir:
        candidates.extend(
            [
                os.path.join(artemis_config_dir, "llm-config.override.jsonc"),
                os.path.join(artemis_config_dir, "llm-config.json"),
            ]
        )
    try:
        # Deliberate guarded import mirroring the module-level bootstrap above:
        # profile resolution must degrade to repo-relative candidates when the
        # artemis config package cannot be loaded.
        from artemis.config.paths import get_app_dir

        app_dir = str(get_app_dir())
        candidates.extend(
            [
                os.path.join(app_dir, "llm-config.override.jsonc"),
                os.path.join(app_dir, "llm-config.json"),
            ]
        )
    except Exception:
        pass

    candidates.extend(
        [
            os.path.join(PROJECT_ROOT, "llm-config.override.jsonc"),
            os.path.join(PROJECT_ROOT, "llm-config.json"),
            os.path.join(PROJECT_ROOT, "config", "llm-config.json"),
        ]
    )

    for cand in candidates:
        if os.path.exists(cand):
            return cand
    return None


async def run_task(
    trace_id: str,
    task_desc: str,
    model: str,
    conversation_id: str,
    locked_app_package: str | None = None,
    app_path: str | None = None,
    expected_output_desc: str | None = None,
    device_serial: str | None = None,
):
    """Executes the mobile automation agent task and logs all actions/results."""
    trace_dir = trace_store.get_trace_dir(trace_id)
    os.makedirs(trace_dir, exist_ok=True)
    stdout_log_path = os.path.join(trace_dir, "stdout.log")
    stderr_log_path = os.path.join(trace_dir, "stderr.log")

    original_stdout = sys.stdout
    original_stderr = sys.stderr

    log_file_out = open(stdout_log_path, "w", buffering=1, encoding="utf-8")
    log_file_err = open(stderr_log_path, "w", buffering=1, encoding="utf-8")

    sys.stdout = log_file_out
    sys.stderr = log_file_err

    logging.basicConfig(level=logging.INFO, force=True)
    logging.getLogger("absl").setLevel(logging.WARNING)
    logging.getLogger("artemis").setLevel(logging.INFO)

    print(f"Starting task execution for Trace ID: {trace_id}")
    print(f"Goal: {task_desc}")
    print(f"Model: {model}")
    print(f"Conversation ID: {conversation_id}")
    print("--------------------------------------------------")

    agent = None
    adb_path = device_utils.resolve_adb_path()
    target_serial = device_serial

    try:
        # Deliberate lazy imports: the SDK pulls in the full agent stack
        # (LLM clients, graph, drivers), and importing it inside this try
        # converts an import-time failure into a properly recorded task
        # failure (trace status + wakeup notification) instead of a crash
        # before status.json is ever updated.
        from artemis.config import settings
        from artemis.sdk import Agent
        from artemis.sdk.builders import Builders
        from artemis.sdk.types import AgentProfile

        connected_devices = device_utils.get_connected_devices(adb_path)

        if device_serial:
            target_serial = device_serial
            if connected_devices and device_serial not in connected_devices:
                print(
                    f"⚠️ Warning: Specified device serial '{device_serial}' was not detected in active ADB devices: {connected_devices}. "
                    "Proceeding with target serial (will attempt direct ADB connection)..."
                )
            else:
                print(f"✅ Using specified target device: '{device_serial}'.")
        else:
            if connected_devices:
                target_serial = settings.ADB_DEVICE_SERIAL or os.environ.get("ADB_DEVICE_SERIAL")
                if not target_serial:
                    try:
                        # Optional path: pool-based selection falls back to the
                        # first connected device on any import or query failure.
                        from artemis.runtime import device_pool
                        target_serial = device_pool.select_device()
                    except Exception:
                        target_serial = connected_devices[0]
                print(
                    f"✅ Detected active connected device(s): {connected_devices}. "
                    f"Auto-selected device: '{target_serial}'."
                )
            else:
                print("❌ No active connected devices detected. Booting emulator...")
                if not device_utils.ensure_emulator(adb_path=adb_path):
                    raise RuntimeError("Failed to start or connect to the Android emulator.")
                target_serial = "emulator-5554"

        if target_serial:
            trace_store.update_trace_device_serial(trace_id, target_serial)

        print("Initializing Artemis Agent...")
        from artemis.config import initialize_llm_config, settings

        profile_file = resolve_profile_file()
        if profile_file:
            profile = AgentProfile(name="default", from_file=profile_file)
        else:
            profile = AgentProfile(name="default", llm_config=initialize_llm_config())

        config_builder = Builders.AgentConfig.with_default_profile(profile)
        if settings.ADB_HOST:
            config_builder.with_adb_server(host=settings.ADB_HOST, port=settings.ADB_PORT)

        if target_serial:
            from artemis.context import DevicePlatform
            config_builder.for_device(DevicePlatform.ANDROID, target_serial)

        config = config_builder.build()

        agent = Agent(config=config)
        await _initialize_agent(
            agent,
            retry_count=int(os.getenv("ARTEMIS_HEALTH_RETRIES", 5)),
            retry_wait_seconds=int(os.getenv("ARTEMIS_HEALTH_DELAY", 2)),
            timeout_seconds=float(os.getenv("ARTEMIS_AGENT_INIT_TIMEOUT_SECONDS", 30)),
        )

        actual_serial = getattr(getattr(agent, "_device_context", None), "device_id", None) or target_serial
        if actual_serial:
            target_serial = actual_serial
            trace_store.update_trace_device_serial(trace_id, actual_serial)
            print(f"📱 Bound to device serial: '{actual_serial}'")

        print("Running task on agent...")
        task_builder = (
            agent.new_task(goal=task_desc)
            .with_name(f"task_{trace_id}")
            .with_trace_recording(enabled=True, path=trace_store.TRACES_DIR)
        )
        if locked_app_package:
            task_builder.with_locked_app_package(package_name=locked_app_package)
        if app_path:
            task_builder.with_app_path(app_path=app_path)
        if expected_output_desc:
            task_builder.with_output_description(description=expected_output_desc)
        if model.lower() == "flash":
            task_builder.using_profile("flash")

        result = await agent.run_task(request=task_builder.build())
        print(f"Task completed. Result: {result}")

        device_info_line = f"Device Serial: `{target_serial}`\n" if target_serial else ""

        if isinstance(result, dict) and "status" in result and result.get("status") != "completed":
            error_explanation = result.get("explanation", "Task execution returned failed status.")
            print(f"Task finished with non-completed status: {error_explanation}", file=sys.stderr)
            trace_store.update_trace_status(
                trace_id, "failed", error=error_explanation, result=result, device_serial=target_serial
            )

            formatted_result = json.dumps(result, indent=2, ensure_ascii=False)
            failure_msg = (
                f"Artemis background task finished with non-completed status.\n\n"
                f"Trace ID: {trace_id}\n"
                f"{device_info_line}"
                f"Goal: {task_desc}\n"
                f"Explanation: {error_explanation}\n\n"
                f"Result:\n```json\n{formatted_result}\n```\n\n"
                f'Check the logs using mobile_inspect_trace(action="view_summary", trace_id="{trace_id}") '
                f"or inspect stderr log: [stderr.log](file://{stderr_log_path})."
            )
            notify(
                conversation_id=conversation_id,
                message=failure_msg,
                title="Artemis Task Incomplete",
                event_type="failed",
                payload={"trace_id": trace_id, "device_serial": target_serial, "goal": task_desc, "result": result},
            )
            return

        if not result:
            if model.lower() == "flash":
                result = "Task executed successfully."
            else:
                result = (
                    "Task executed successfully. You can check the notes under "
                    "notes_dir to view more details."
                )

        trace_store.update_trace_status(trace_id, "completed", result=result, device_serial=target_serial)

        if isinstance(result, dict):
            formatted_result = json.dumps(result, indent=2, ensure_ascii=False)
            result_str = f"Result:\n```json\n{formatted_result}\n```\n"
        else:
            formatted_result = str(result)
            result_str = f"Result: {formatted_result}\n"

        success_msg = (
            "Artemis background task completed successfully.\n\n"
            f"Trace ID: {trace_id}\n"
            f"{device_info_line}"
            f"Goal: {task_desc}\n"
            f"{result_str}"
        )
        notify(
            conversation_id=conversation_id,
            message=success_msg,
            title="Artemis Task Completed",
            event_type="completed",
            payload={"trace_id": trace_id, "device_serial": target_serial, "goal": task_desc, "result": result},
        )

    except asyncio.CancelledError:
        print("Task was cancelled (asyncio.CancelledError)", file=sys.stderr)
        trace_store.update_trace_status(trace_id, "cancelled", error="Task was cancelled", device_serial=target_serial)
        device_info_line = f"Device Serial: `{target_serial}`\n" if target_serial else ""
        cancel_msg = (
            f"Artemis background task was cancelled.\n\nTrace ID: {trace_id}\n{device_info_line}Goal: {task_desc}\n"
        )
        notify(
            conversation_id=conversation_id,
            message=cancel_msg,
            title="Artemis Task Cancelled",
            event_type="cancelled",
            payload={"trace_id": trace_id, "device_serial": target_serial, "goal": task_desc},
        )
        raise

    except Exception as e:
        error_type = type(e).__name__
        raw_error_msg = str(e)
        tb_str = traceback.format_exc()
        print(
            f"CRITICAL ERROR during task execution: {error_type}: {raw_error_msg}\n{tb_str}",
            file=sys.stderr,
        )

        exc_type, exc_obj, tb = sys.exc_info()
        while tb and tb.tb_next:
            tb = tb.tb_next
        frame_summary = f" (at {tb.tb_frame.f_code.co_name}:{tb.tb_lineno})" if tb else ""
        full_error_desc = f"{error_type}: {raw_error_msg}{frame_summary}"

        trace_store.update_trace_status(trace_id, "failed", error=full_error_desc, device_serial=target_serial)

        device_info_line = f"Device Serial: `{target_serial}`\n" if target_serial else ""
        failure_msg = (
            f"Artemis background task failed.\n\n"
            f"Trace ID: {trace_id}\n"
            f"{device_info_line}"
            f"Goal: {task_desc}\n"
            f"Error: {full_error_desc}\n\n"
            f'Check the logs using mobile_inspect_trace(action="view_summary", trace_id="{trace_id}") '
            f"or inspect stderr log: [stderr.log](file://{stderr_log_path})."
        )
        notify(
            conversation_id=conversation_id,
            message=failure_msg,
            title="Artemis Task Failed",
            event_type="failed",
            payload={"trace_id": trace_id, "device_serial": target_serial, "goal": task_desc, "error": full_error_desc},
        )

    finally:
        print("Cleaning up resources...")
        if agent:
            try:
                await agent.clean()
            except Exception as e:
                print(f"Error cleaning agent: {e}", file=sys.stderr)

        sys.stdout = original_stdout
        sys.stderr = original_stderr
        log_file_out.close()
        log_file_err.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Artemis Background Task Runner")
    parser.add_argument("--trace-id", required=True, help="Unique trace identifier")
    parser.add_argument("--task-desc", required=True, help="Description of the task to run")
    parser.add_argument("--model", required=True, help="Model to use ('Flash' or 'Pro')")
    parser.add_argument(
        "--conversation-id",
        default="",
        help="Conversation ID for wakeup notification routing (optional; empty disables routing)",
    )
    parser.add_argument("--locked-app-package", help="Package name of app to lock execution to")
    parser.add_argument("--app-path", help="Path to local APK to install before running task")
    parser.add_argument("--expected-output-desc", help="Expected output description")
    parser.add_argument("--device-serial", help="Target specific device serial")

    args = parser.parse_args()

    asyncio.run(
        run_task(
            trace_id=args.trace_id,
            task_desc=args.task_desc,
            model=args.model,
            conversation_id=args.conversation_id,
            locked_app_package=args.locked_app_package,
            app_path=args.app_path,
            expected_output_desc=args.expected_output_desc,
            device_serial=args.device_serial,
        )
    )
