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

import asyncio
import base64
import difflib
import json
import math
import os
from pathlib import Path
import psutil
import re
import sys
import time
import traceback
import uuid
from uuid import UUID

from adbutils import AdbClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from langchain_core.messages import HumanMessage, SystemMessage
from artemis.agents.validator.failure_analyzer import (
    FailureAnalyzer,
    ValidationErrorCategory,
)
from artemis.clients.ui_automator_client import UIAutomatorClient
from artemis.constants import (
    VALIDATOR_POLL_INTERVAL,
    VALIDATOR_POLL_TIMEOUT,
    VALIDATOR_UI_HIERARCHY_TIMEOUT,
)
from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.data_engine.trace import CURRENT_TRACE_ID, trace
from artemis.graph.state import State
from artemis.platform import platform
from artemis.services.llm import get_llm
from artemis.tools.mobile.launch_app import find_package
from artemis.utils import app_launch_utils, image_diff, visualization
from artemis.utils.decorators import wrap_with_callbacks
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

# Define actions that are permitted to succeed without a detected UI change
ALLOW_NO_UI_CHANGE = {
    "wait_for_delay",
    "focus_and_clear_text",
    "launch_app",
    "stop_app",
    "press_key",
    "back",
    "erase_one_char",
    "swipe",
}


class ValidatorNode:
    """Node responsible for executing actions on the device and verifying the results.

    Despite its name, this node handles:
    1. Parsing structured decisions from the Operator.
    2. Executing these actions via an MCP session (calling tools like tap,
    swipe, etc.).
    3. Verifying if the action resulted in a UI change.
    4. Triggering failure analysis and local repair if actions fail.

    In a more strict architecture, execution and validation might be split into
    separate nodes.
    """

    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx

    def _kill_mcp_server_instantly(self):
        """Instantly terminate MCP ADB server child processes."""

        try:
            current_process = psutil.Process()
            children = current_process.children(recursive=True)
            for child in children:
                try:
                    cmdline = child.cmdline()
                    if any("adb_server.py" in part for part in cmdline):
                        logger.warning(f"Instantly killing MCP Server child process: {child.pid}")
                        child.kill()
                except psutil.NoSuchProcess:
                    pass
        except Exception as e:
            logger.error(f"Failed to instantly kill MCP server child processes: {e}")

    async def _close_failed_mcp_session(
        self,
        *,
        session,
        session_entered: bool,
        client_ctx,
        client_entered: bool,
    ) -> bool:
        """Exit partially-entered MCP contexts in reverse order.

        The MCP stdio transport owns an AnyIO task group and cancellation
        scope. Dropping an entered transport without calling ``__aexit__``
        leaves that scope attached to the validator task and can cancel an
        unrelated action later in the same node.

        Returns ``True`` when every entered context closed cleanly.
        """
        self.ctx.mcp_session = None
        self.ctx.mcp_client_ctx = None
        cleanup_succeeded = True

        for manager, entered, label in (
            (session, session_entered, "MCP session"),
            (client_ctx, client_entered, "MCP stdio transport"),
        ):
            if not entered or manager is None:
                continue
            try:
                await manager.__aexit__(None, None, None)
            except asyncio.CancelledError:
                cleanup_succeeded = False
                logger.warning(f"{label} cleanup was cancelled.")
            except Exception as cleanup_error:
                cleanup_succeeded = False
                logger.warning(f"Failed to close {label}: {cleanup_error}")

        return cleanup_succeeded

    async def _get_mcp_session(self):
        if getattr(self.ctx, "mcp_session", None) is not None:
            return self.ctx.mcp_session

        root_dir = Path(__file__).parent.parent.parent
        venv_python = root_dir / ".venv" / "bin" / "python3"
        python_exe = str(venv_python) if venv_python.exists() else sys.executable

        server_env = os.environ.copy()
        # The parent Artemis process already owns the device-awake policy.
        # Starting it again in this short-lived child can block the MCP
        # handshake on cross-process ADB/lease cleanup.
        server_env["ARTEMIS_KEEP_DEVICE_AWAKE"] = "false"
        server_params = StdioServerParameters(
            command=python_exe,
            args=[str(root_dir / "mcp" / "adb_server.py")],
            env=server_env,
        )

        logger.info("Starting persistent MCP server session...")
        client_ctx = stdio_client(server_params)
        session = None
        client_entered = False
        session_entered = False
        self.ctx.mcp_client_ctx = client_ctx
        try:
            read, write = await client_ctx.__aenter__()
            client_entered = True
            session = ClientSession(read, write)
            self.ctx.mcp_session = session
            await session.__aenter__()
            session_entered = True
            # Handshake timeout protection
            await asyncio.wait_for(session.initialize(), timeout=15.0)
            return session
        except asyncio.CancelledError:
            await self._close_failed_mcp_session(
                session=session,
                session_entered=session_entered,
                client_ctx=client_ctx,
                client_entered=client_entered,
            )
            raise
        except Exception as e:
            err_stack = traceback.format_exc()
            logger.critical(
                "CRITICAL: Failed to initialize MCP server session:"
                f" {e}\n{err_stack}. Cleaning up child processes."
            )
            cleanup_succeeded = await self._close_failed_mcp_session(
                session=session,
                session_entered=session_entered,
                client_ctx=client_ctx,
                client_entered=client_entered,
            )
            if not cleanup_succeeded:
                self._kill_mcp_server_instantly()

            logger.warning(
                "Falling back to robust local in-process execution bypassing MCP protocol."
            )

            try:
                adb = AdbClient(host="localhost", port=5037)
                devices = adb.device_list()
                if devices:
                    device_id = devices[0].serial
                    ui_client = UIAutomatorClient(device_id=device_id)
                    try:
                        ui_data = ui_client.get_screen_data()
                        w, h = ui_data.width, ui_data.height
                    except Exception:
                        w, h = 1080, 2400
                    local_ctx = ArtemisContext(
                        trace_id="local-fallback",
                        device=DeviceContext(
                            host_platform=platform.os_type.name,
                            mobile_platform=DevicePlatform.ANDROID,
                            device_id=device_id,
                            device_width=w,
                            device_height=h,
                        ),
                        adb_client=adb,
                        ui_adb_client=ui_client,
                    )
                    self._local_controller = UnifiedMobileController(local_ctx)
            except Exception as le:
                logger.error(f"Failed to setup local fallback controller: {le}")

            return None

    def _parse_decisions(self, structured_decisions: str) -> tuple[list[dict] | None, str | None]:
        if not structured_decisions:
            return None, "No structured decisions found, nothing to execute."
        try:
            return json.loads(structured_decisions), None
        except json.JSONDecodeError as e:
            return None, f"Failed to parse structured decisions: {e}"

    async def _get_initial_screenshot(self, session, state: State) -> tuple[str, str | None]:
        screenshot_path = getattr(state, "latest_screenshot", None)
        if not screenshot_path:
            logger.error("No screenshot path found in state.latest_screenshot")
            raise ValueError("No screenshot path found in state.latest_screenshot")

        if not Path(screenshot_path).exists():
            logger.error(f"Screenshot file does not exist: {screenshot_path}")
            raise FileNotFoundError(f"Screenshot file does not exist: {screenshot_path}")

        try:
            with open(screenshot_path, "rb") as f:
                screenshot_b64 = base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            logger.error(f"Failed to read screenshot from {screenshot_path}: {e}")
            raise e

        screenshot_name = Path(screenshot_path).stem
        return screenshot_b64, screenshot_name

    async def _poll_for_ui_change(
        self, session, pre_screenshot_b64, action_item
    ) -> tuple[bool, str, str | None]:

        start_time = time.time()
        timeout = VALIDATOR_POLL_TIMEOUT
        ui_changed = False
        post_screenshot_b64 = None
        error_msg = ""

        while time.time() - start_time < timeout:
            if session is not None:
                result = await session.call_tool("take_screenshot", {})
                success_img, content_img = self._parse_mcp_result(result)
            elif getattr(self, "_local_controller", None):
                try:
                    content_img = await self._local_controller.take_screenshot()
                    success_img = True
                except Exception as e:
                    success_img, content_img = False, str(e)
            else:
                success_img, content_img = (
                    False,
                    "No session or local controller",
                )
            if success_img:
                post_screenshot_b64 = content_img
                img_before_bytes = base64.b64decode(pre_screenshot_b64)
                img_after_bytes = base64.b64decode(post_screenshot_b64)

                ui_changed = image_diff.check_ui_change(
                    img_before_bytes, img_after_bytes, action_item
                )
                if ui_changed:
                    logger.info("UI change detected during polling.")
                    break
            else:
                logger.error(f"Failed to take screenshot via MCP: {content_img}")

            await asyncio.sleep(VALIDATOR_POLL_INTERVAL)

        if not ui_changed:
            error_msg = "Screen did not change"

        return ui_changed, error_msg, post_screenshot_b64

    async def _execute_validation_loop(self, state: State):
        actions, error_msg = self._parse_decisions(state.structured_decisions)
        if error_msg:
            return await state.asanitize_update(
                ctx=self.ctx,
                update={},
                agent="validator",
            )

        execution = []
        failed_action = None
        failure_reason = None

        actions_to_execute = list(actions)

        session = await self._get_mcp_session()

        try:
            last_screenshot_b64, last_screenshot_name = await self._get_initial_screenshot(
                session, state
            )
            # Preserve the turn-initial screenshot (what the operator saw when making decisions)
            decision_screenshot_b64 = last_screenshot_b64
            decision_screenshot_name = last_screenshot_name
        except Exception:
            return await state.asanitize_update(
                ctx=self.ctx,
                update={},
                agent="validator",
            )

        step_id = None
        if state.current_step_id:
            step_id = UUID(state.current_step_id)

        analysis_result = {}

        while actions_to_execute:
            action_item = actions_to_execute.pop(0)
            action_name = action_item.get("action")

            logger.info(f"Executing action: {action_name}")

            action_trace_id = uuid.uuid4()
            parent_id = CURRENT_TRACE_ID.get()
            token = CURRENT_TRACE_ID.set(action_trace_id)
            start_time = time.time()

            if self.ctx.data_engine and step_id:
                self.ctx.data_engine.record_trace(
                    type="action",
                    name=action_name,
                    payload={"action": action_item, "status": "running"},
                    status="running",
                    parent_trace_id=parent_id,
                    step_id=step_id,
                    trace_id=action_trace_id,
                )

            try:
                pre_screenshot_b64 = last_screenshot_b64
                success = False
                error_msg = ""
                error_category = ValidationErrorCategory.GENERAL
                post_screenshot_b64 = None
                post_image_name = None

                action_item = dict(
                    action_item
                )  # Make a copy to avoid mutating original state actions
                attempts_log = []

                # 1. Pre-execution validation (Safety Net)
                original_coords = list(action_item.get("coordinates") or [])

                is_explorer_candidate = action_item.get("target_class") == "ExplorerCandidate"
                is_ocr_element = "[OCR]" in str(action_item.get("target_class") or "")
                has_index_metadata = bool(
                    (
                        action_item.get("target_text")
                        or action_item.get("target_bounds")
                        or action_item.get("target_resource_id")
                    )
                    and not is_explorer_candidate
                    and not is_ocr_element
                )

                if has_index_metadata:
                    # Index-based validation is fastest and safest when metadata is present
                    (
                        validation_passed,
                        validation_category,
                        validation_error,
                    ) = await self._validate_action_precondition(session, action_item, state=state)

                    if not validation_passed:
                        if validation_category == ValidationErrorCategory.XML_BYPASSED:
                            logger.info(
                                "Pre-execution XML-based validation bypassed"
                                f" ({validation_error}). Falling back to"
                                " Pixel-based validation..."
                            )
                            (
                                validation_passed,
                                validation_category,
                                validation_error,
                            ) = await self._validate_action_precondition_pixel(
                                session,
                                action_item,
                                pre_screenshot_b64,
                                original_coords,
                                state=state,
                            )
                        else:
                            logger.info(
                                "Pre-execution XML-based validation failed:"
                                f" {validation_error}. Attempting Pixel-based"
                                " validation fallback..."
                            )
                            (
                                pixel_passed,
                                pixel_category,
                                pixel_error,
                            ) = await self._validate_action_precondition_pixel(
                                session,
                                action_item,
                                pre_screenshot_b64,
                                original_coords,
                                state=state,
                            )
                            if pixel_passed and pixel_category == ValidationErrorCategory.NONE:
                                logger.success(
                                    "Pixel-based validation fallback PASSED!"
                                    " Overriding XML validation failure."
                                )
                                validation_passed = True
                                validation_category = ValidationErrorCategory.NONE
                                validation_error = ""
                else:
                    # Fall back to Pixel-based validation for pure coordinate/non-index interactions
                    (
                        validation_passed,
                        validation_category,
                        validation_error,
                    ) = await self._validate_action_precondition_pixel(
                        session,
                        action_item,
                        pre_screenshot_b64,
                        original_coords,
                        state=state,
                    )

                if not validation_passed:
                    success = False
                    error_msg = f"Pre-execution validation failed: {validation_error}"
                    error_category = validation_category
                    attempts_log.append(error_msg)

                    # Capture the live mismatch screenshot to provide context for FailureAnalyzer
                    try:
                        if session is not None:
                            result = await session.call_tool("take_screenshot", {})
                            success_img, content = self._parse_mcp_result(result)
                            if success_img and content:
                                post_screenshot_b64 = content
                            else:
                                logger.warning(f"Take screenshot via MCP returned non-success: {content}")
                        elif getattr(self, "_local_controller", None):
                            post_screenshot_b64 = await self._local_controller.take_screenshot()
                    except Exception as e:
                        logger.error(f"Failed to capture live screenshot for failure analysis: {e}")
                        post_screenshot_b64 = None
                else:
                    # 2. Local execution attempts
                    max_local_retries = 1 if action_name == "launch_app" else 2
                    for attempt in range(max_local_retries):
                        try:
                            exec_success, exec_error = await self._exec_action(session, action_item)
                        except Exception as e:
                            exec_success = False
                            exec_error = str(e)

                        # Skip polling: assume execution success implies action success
                        if exec_success:
                            success = True
                            error_msg = ""
                            attempts_log.append("Success")
                            break
                        else:
                            success = False
                            error_msg = exec_error
                            attempts_log.append(error_msg)

                        if attempt < max_local_retries - 1:
                            await asyncio.sleep(0.5)

                    if not success and not post_screenshot_b64:
                        logger.info("Action failed, capturing failure screenshot...")
                        try:
                            if session is not None:
                                result = await session.call_tool("take_screenshot", {})
                                success_img, content = self._parse_mcp_result(result)
                                if success_img and content:
                                    post_screenshot_b64 = content
                                else:
                                    logger.warning(f"Take failure screenshot via MCP returned non-success: {content}")
                            elif getattr(self, "_local_controller", None):
                                post_screenshot_b64 = await self._local_controller.take_screenshot()
                        except Exception as e:
                            logger.error(f"Failed to capture failure screenshot: {e}")
                            post_screenshot_b64 = None

                # Enrich and record execution
                if len(attempts_log) > 1 or not success:
                    action_item["attempts"] = list(attempts_log)
                execution.append(action_item)

                if post_screenshot_b64:
                    decoded_bytes = base64.b64decode(post_screenshot_b64)
                    last_screenshot_b64 = post_screenshot_b64
                    logger.info(f"Successfully decoded post_screenshot_b64 ({len(decoded_bytes)} bytes)")
                    if self.ctx.data_engine:
                        post_image_name = self.ctx.data_engine.get_or_create_image(decoded_bytes)
                        last_screenshot_name = post_image_name

                        screenshot_path = str(self.ctx.data_engine.get_image_path(post_image_name))
                        state.latest_screenshot = screenshot_path
                        logger.info(
                            f"Validator updated state.latest_screenshot to: {screenshot_path}"
                        )

                # Update action trace with final results
                duration = time.time() - start_time
                if self.ctx.data_engine and step_id:
                    relative_time = self.ctx.data_engine.get_relative_time(time.time())
                    self.ctx.data_engine.record_trace(
                        type="action",
                        name=action_name,
                        payload={
                            "action": action_item,
                            "success": success,
                            "error_msg": error_msg,
                            "post_screenshot": post_image_name,
                            "timestamp": time.time(),
                            "relative_time": relative_time,
                        },
                        status="success" if success else "failed",
                        duration=duration,
                        parent_trace_id=parent_id,
                        step_id=step_id,
                        trace_id=action_trace_id,
                    )
            finally:
                CURRENT_TRACE_ID.reset(token)

            if not success:
                failed_action = action_item
                failure_reason = error_msg

                logger.warning(f"Action failed: {failed_action}. Triggering failure analysis.")

                analyzer = FailureAnalyzer(self.ctx)
                analysis_result = await analyzer.analyze(
                    state,
                    failed_action,
                    failure_reason,
                    pre_screenshot=decision_screenshot_b64,
                    post_screenshot=post_screenshot_b64,
                    pre_screenshot_name=decision_screenshot_name,
                    post_screenshot_name=post_image_name,
                    executed_actions=execution[:-1],
                    unexecuted_actions=list(actions_to_execute),
                    error_category=error_category,
                )
                action_item["repair"] = analysis_result.get("analysis", "No analysis provided.")

                status = analysis_result.get("status")
                if status == "fixed":
                    logger.success("Failure repaired locally!")
                    actions_to_execute = []

                    # Reload the latest screenshot updated by FailureAnalyzer during repair
                    try:
                        screenshot_path = getattr(state, "latest_screenshot", None)
                        if screenshot_path:
                            if Path(screenshot_path).exists():
                                with open(screenshot_path, "rb") as f:
                                    last_screenshot_b64 = base64.b64encode(f.read()).decode("utf-8")
                                if self.ctx.data_engine:
                                    last_screenshot_name = self.ctx.data_engine.get_or_create_image(
                                        base64.b64decode(last_screenshot_b64)
                                    )
                                logger.info(
                                    "Validator reloaded post-repair screenshot"
                                    f" from: {screenshot_path}"
                                )
                    except Exception as e:
                        logger.error(f"Failed to reload post-repair screenshot: {e}")

                    continue
                else:
                    break

        # Append any skipped actions to the execution log
        for skipped in actions_to_execute:
            skipped_copy = dict(skipped)
            skipped_copy["attempts"] = ["Skipped"]
            execution.append(skipped_copy)

        report = {
            "execution": execution,
            "status": (
                "success"
                if success or (failed_action and analysis_result.get("status") == "fixed")
                else "failed"
            ),
            "repair_status": (analysis_result.get("status") if failed_action else None),
        }

        # Determine if a distinct post-action screenshot was captured
        distinct_post_image_name = (
            last_screenshot_name
            if (last_screenshot_name and last_screenshot_name != decision_screenshot_name)
            else None
        )

        if self.ctx.data_engine and step_id:
            self.ctx.data_engine.update_step_execution_result(
                step_id, report, post_image_name=distinct_post_image_name
            )

        return await state.asanitize_update(
            ctx=self.ctx,
            update={"last_execution_result": report},
            agent="validator",
        )

    @wrap_with_callbacks(
        before=lambda: logger.info("Starting Validator Agent..."),
        on_success=lambda _: logger.success("Validator Agent"),
        on_failure=lambda _: logger.error("Validator Agent"),
    )
    @trace(type="agent", name="validator")
    async def __call__(self, state: State):

        step_id = None
        if state.current_step_id:
            step_id = UUID(state.current_step_id)

        original_step_id = getattr(self.ctx.data_engine, "current_step_id", None)
        if self.ctx.data_engine and step_id:
            self.ctx.data_engine.current_step_id = step_id

        try:
            return await self._execute_validation_loop(state)
        except asyncio.CancelledError:
            logger.warning(
                "Validator task cancelled. Instantly killing MCP server processes."
            )
            self._kill_mcp_server_instantly()
            raise
        except Exception as e:
            err_stack = traceback.format_exc()
            logger.critical(f"CRITICAL ERROR in Validator execution loop: {e}\n{err_stack}")
            if self.ctx.data_engine and step_id:
                try:
                    self.ctx.data_engine.update_step_execution_result(
                        step_id,
                        {
                            "status": "error",
                            "error_msg": str(e),
                            "traceback": err_stack,
                        },
                    )
                except Exception:
                    pass
            self._kill_mcp_server_instantly()
            raise e
        finally:
            if self.ctx.data_engine:
                self.ctx.data_engine.current_step_id = original_step_id

    async def _exec_action(
        self, session: ClientSession | None, action_item: dict
    ) -> tuple[bool, str]:
        action_name = action_item.get("action")
        coordinates = action_item.get("coordinates")

        # Intercept non-wait_for_text actions for local fallback
        if session is None and getattr(self, "_local_controller", None) is not None:
            ctrl = self._local_controller
            logger.info(f"Executing in-process action fallback: {action_name}")
            try:
                if action_name == "tap":
                    if coordinates and len(coordinates) == 2:
                        times = action_item.get("times", 1)
                        delay_ms = action_item.get("delay_ms", 100)
                        res = await ctrl.tap_at(
                            coordinates[0],
                            coordinates[1],
                            times=times,
                            delay_ms=delay_ms,
                        )
                        return (True, "Success") if not res.error else (False, res.error)
                    return False, "Invalid coords"
                elif action_name == "long_press_on":
                    d = action_item.get("duration", 1000)
                    if coordinates and len(coordinates) == 2:
                        res = await ctrl.tap_at(
                            coordinates[0],
                            coordinates[1],
                            long_press=True,
                            long_press_duration=d,
                        )
                        return (True, "Success") if not res.error else (False, res.error)
                    return False, "Invalid coords"
                elif action_name == "swipe":
                    d = action_item.get("duration", 400)
                    if coordinates and len(coordinates) == 4:
                        err = await ctrl.swipe_coords(
                            coordinates[0],
                            coordinates[1],
                            coordinates[2],
                            coordinates[3],
                            duration=d,
                        )
                        return (True, "Success") if not err else (False, str(err))
                    return False, "Invalid coords"
                elif action_name == "focus_and_input_text":
                    t = action_item.get("text")
                    clear_before = action_item.get("clear_before_input", False)
                    if coordinates and len(coordinates) == 2:
                        await ctrl.tap_at(coordinates[0], coordinates[1])

                        await asyncio.sleep(0.5)
                        if clear_before:
                            await ctrl.erase_text()
                        else:
                            await ctrl.press_key("123")  # Move cursor to end for clean append
                        success = await ctrl.type_text(t, clear_existing=False)
                        return (True, "Success") if success else (False, "Failed typing")
                    return False, "Invalid coords"
                elif action_name == "focus_and_clear_text":
                    if coordinates and len(coordinates) == 2:
                        await ctrl.tap_at(coordinates[0], coordinates[1])

                        await asyncio.sleep(0.5)
                        success = await ctrl.erase_text()
                        return (True, "Success") if success else (False, "Failed erase")
                    return False, "Invalid coords"
                elif action_name == "erase_one_char":
                    success = await ctrl.erase_text(nb_chars=1)
                    return (True, "Success") if success else (False, "Failed erase")
                elif action_name == "press_key":
                    keycode = action_item.get("keycode")
                    success = await ctrl.press_key(keycode)
                    return (True, "Success") if success else (False, f"Failed press {keycode}")
                elif action_name == "back":
                    success = await ctrl.go_back()
                    return (True, "Success") if success else (False, "Failed back")
                elif action_name == "launch_app":
                    app_name = action_item.get("app_name")
                    package_name = await find_package(self.ctx, app_name, use_fallback=False)
                    if package_name:
                        success, err_msg = await app_launch_utils.launch_app_with_retries(
                            ctrl.ctx, package_name
                        )
                        return (True, "Success") if success else (False, err_msg)
                    return False, "Package not found"
                elif action_name == "stop_app":
                    app_name = action_item.get("app_name")
                    package_name = await find_package(self.ctx, app_name, use_fallback=False)
                    if package_name:
                        success = await ctrl.terminate_app(package_name)
                        return (True, "Success") if success else (False, "Failed terminate")
                    return False, "Package not found"
                elif action_name == "open_link":
                    url = action_item.get("url")
                    success = await ctrl.open_url(url)
                    return (True, "Success") if success else (False, "Failed open url")
                elif action_name == "wait_for_delay":
                    time_in_ms = action_item.get("time_in_ms", 0)

                    await asyncio.sleep(time_in_ms / 1000.0)
                    return True, ""
                else:
                    return (
                        False,
                        f"Unsupported action for local fallback: {action_name}",
                    )
            except Exception as e:
                return False, f"In-process fallback failed: {e}"

        # Normal MCP Execution Path
        try:
            if action_name == "tap":
                coordinates = action_item.get("coordinates")
                times = action_item.get("times") or action_item.get("click_times") or 1
                delay_ms = action_item.get("delay_ms") or action_item.get("delay") or 100
                result = await session.call_tool(
                    "tap",
                    {
                        "coordinates": coordinates,
                        "times": times,
                        "delay_ms": delay_ms,
                    },
                )
                return self._parse_mcp_result(result)

            elif action_name == "long_press_on":
                coordinates = action_item.get("coordinates")
                duration = action_item.get("duration", 1000)
                result = await session.call_tool(
                    "long_press_on",
                    {"coordinates": coordinates, "duration": duration},
                )
                return self._parse_mcp_result(result)

            elif action_name == "swipe":
                coordinates = action_item.get("coordinates")
                duration = action_item.get("duration", 400)
                result = await session.call_tool(
                    "swipe", {"coordinates": coordinates, "duration": duration}
                )
                return self._parse_mcp_result(result)

            elif action_name == "focus_and_input_text":
                coordinates = action_item.get("coordinates")
                text = action_item.get("text", "")
                clear_before_input = action_item.get("clear_before_input", False)
                result = await session.call_tool(
                    "focus_and_input_text",
                    {
                        "coordinates": coordinates,
                        "text": text,
                        "clear_before_input": clear_before_input,
                    },
                )
                return self._parse_mcp_result(result)

            elif action_name == "focus_and_clear_text":
                coordinates = action_item.get("coordinates")
                result = await session.call_tool(
                    "focus_and_clear_text", {"coordinates": coordinates}
                )
                return self._parse_mcp_result(result)

            elif action_name == "erase_one_char":
                result = await session.call_tool("erase_one_char", {})
                return self._parse_mcp_result(result)

            elif action_name == "press_key":
                keycode = action_item.get("keycode", "")
                result = await session.call_tool("press_key", {"keycode": keycode})
                return self._parse_mcp_result(result)

            elif action_name == "back":
                result = await session.call_tool("back", {})
                return self._parse_mcp_result(result)

            elif action_name == "launch_app":
                app_name = action_item.get("app_name")
                package_name = await find_package(self.ctx, app_name, use_fallback=False)
                if not package_name:
                    return False, f"Failed to find package for app: {app_name}"

                success, err_msg = await app_launch_utils.launch_app_with_retries(
                    self.ctx, package_name
                )
                return success, "Success" if success else err_msg

            elif action_name == "stop_app":
                app_name = action_item.get("app_name")
                package_name = await find_package(self.ctx, app_name, use_fallback=False)
                if not package_name:
                    return False, f"Failed to find package for app: {app_name}"
                result = await session.call_tool("stop_app", {"package_name": package_name})
                return self._parse_mcp_result(result)

            elif action_name == "open_link":
                url = action_item.get("url")
                result = await session.call_tool("open_link", {"url": url})
                return self._parse_mcp_result(result)

            elif action_name == "wait_for_delay":
                time_in_ms = action_item.get("time_in_ms", 0)
                logger.info(f"Validator performing wait_for_delay for {time_in_ms}ms...")

                await asyncio.sleep(time_in_ms / 1000.0)
                return True, ""

            else:
                return False, f"Unsupported action: {action_name}"
        except Exception as e:
            return False, f"MCP call failed: {e}"

    @trace(type="tool", name="safety_net_validation")
    async def _validate_action_precondition(
        self, session, action_item: dict, state: State | None = None
    ) -> tuple[bool, ValidationErrorCategory, str]:
        """Safety Net: Validates if the target element for interactive actions

        is still present on the live screen and is substantially consistent,
        with a short retry loop to accommodate screen transitions and loading
        delays.
        """
        max_pre_attempts = 3
        pre_retry_delay = 0.4

        passed = False
        category = ValidationErrorCategory.NONE
        reason = ""

        for pre_attempt in range(1, max_pre_attempts + 1):
            passed, category, reason = await self._validate_action_precondition_single(
                session, action_item, state
            )
            if passed:
                return True, category, reason

            if category == ValidationErrorCategory.XML_BYPASSED:
                # Do not retry on XML bypass/timeout; return immediately
                # so it falls back to Pixel-based VLM validation
                return False, category, reason

            if pre_attempt < max_pre_attempts:
                logger.info(
                    "Pre-execution validation failed on attempt"
                    f" {pre_attempt}/{max_pre_attempts}: {reason}. Retrying in"
                    f" {pre_retry_delay}s..."
                )
                await asyncio.sleep(pre_retry_delay)
            else:
                break

        return passed, category, reason

    async def _validate_action_precondition_single(
        self, session, action_item: dict, state: State | None = None
    ) -> tuple[bool, ValidationErrorCategory, str]:
        """Safety Net: Validates if the target element for interactive actions

        is still present on the live screen and is substantially consistent.

        Returns (True, ValidationErrorCategory.NONE, "") if validation passes or
        is skipped.
        Returns (False, category, error_msg) if validation fails.
        """
        action_name = action_item.get("action")

        # 1. White-list filtering: Only validate interactive, element-focused actions
        if action_name not in [
            "tap",
            "long_press_on",
            "focus_and_input_text",
            "focus_and_clear_text",
        ]:
            return True, ValidationErrorCategory.NONE, ""

        target_text = action_item.get("target_text")
        target_bounds = action_item.get("target_bounds")  # [l, t, r, b]
        target_resource_id = action_item.get("target_resource_id")

        width = 1080
        height = 2400
        operator_raw_data = getattr(state, "operator_raw_data", {}) or {}
        w_raw = operator_raw_data.get("width")
        h_raw = operator_raw_data.get("height")
        if isinstance(w_raw, int) and isinstance(h_raw, int):
            width = w_raw
            height = h_raw
        else:
            device = getattr(self.ctx, "device", None)
            if device:
                width = getattr(device, "device_width", 1080)
                height = getattr(device, "device_height", 2400)

        # Calculate diagonal-based scale factor for resolution-independent thresholds

        ref_diagonal = math.sqrt(1080**2 + 2400**2)  # ~2631.8
        current_diagonal = math.sqrt(width**2 + height**2)
        scale_factor = current_diagonal / ref_diagonal

        # 2. Skip validation if there's no target metadata to match against
        if not target_text and not target_bounds and not target_resource_id:
            return True, ValidationErrorCategory.NONE, ""

        logger.info(
            f"Pre-execution validation: checking target '{target_text}'"
            f" ({target_resource_id}) at {target_bounds}..."
        )

        # 3. Pull live XML tree with strict timeout to avoid hanging the execution flow

        try:
            elements = None
            if session is not None:
                try:
                    result = await asyncio.wait_for(
                        session.call_tool("get_ui_hierarchy", {}),
                        timeout=VALIDATOR_UI_HIERARCHY_TIMEOUT,
                    )
                    if hasattr(result, "content") and result.content:
                        text_content = (
                            result.content[0].text
                            if hasattr(result.content[0], "text")
                            else str(result.content[0])
                        )
                        elements = json.loads(text_content)
                except Exception as e:
                    logger.warning(
                        f"Failed to get live XML via MCP: {e}. Falling back to"
                        " Pixel-based validation."
                    )
                    return (
                        False,
                        ValidationErrorCategory.XML_BYPASSED,
                        f"XML hierarchy fetch timed out or errored: {e}",
                    )
            elif getattr(self, "_local_controller", None):
                try:
                    elements = await asyncio.wait_for(
                        self._local_controller.get_ui_elements(),
                        timeout=VALIDATOR_UI_HIERARCHY_TIMEOUT,
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to get live XML via local controller: {e}."
                        " Falling back to Pixel-based validation."
                    )
                    return (
                        False,
                        ValidationErrorCategory.XML_BYPASSED,
                        f"XML hierarchy fetch timed out or errored: {e}",
                    )

            if not elements or not isinstance(elements, list):
                logger.warning(
                    "Live hierarchy empty or invalid. Falling back to Pixel-based validation."
                )
                return (
                    False,
                    ValidationErrorCategory.XML_BYPASSED,
                    "Live hierarchy empty or invalid.",
                )

        except Exception as e:
            logger.error(
                f"Unexpected error during live XML fetch: {e}. Falling back to"
                " Pixel-based validation."
            )
            return (
                False,
                ValidationErrorCategory.XML_BYPASSED,
                f"Unexpected error during live XML fetch: {e}",
            )

        # 4. Matching Heuristics & Scoring

        # Normalization Helper
        def normalize_text(s: str | None) -> str:
            if not s:
                return ""
            s = s.lower()
            s = re.sub(r"\(\d+\+?\)", "", s)
            s = re.sub(r"\[\d+\+?\]", "", s)
            s = re.sub(r"[^\w\s\d]", "", s)
            return s.strip()

        # Recursive text aggregator for candidate container nodes
        def aggregate_text(elem: dict) -> str:
            text = (
                elem.get("text") or elem.get("content-desc") or elem.get("accessibilityText") or ""
            )
            if not isinstance(text, str):
                text = str(text)
            children = elem.get("children") or []
            if children:
                for child in children:
                    if isinstance(child, dict):
                        text += " " + aggregate_text(child)
            return text

        norm_target_text = normalize_text(target_text)

        candidates = []
        for elem in elements:
            if not isinstance(elem, dict):
                continue

            elem_bounds_str = elem.get("bounds")
            elem_bounds = visualization.parse_bounds(elem_bounds_str)
            if not elem_bounds:
                continue

            elem_raw_text = aggregate_text(elem)
            elem_norm_text = normalize_text(elem_raw_text)
            elem_res_id = elem.get("resource-id") or elem.get("resourceId") or ""

            elem_cx, elem_cy = visualization.get_center_coordinates(*elem_bounds)

            original_coords = action_item.get("coordinates") or [0, 0]
            dist = math.sqrt(
                (elem_cx - original_coords[0]) ** 2 + (elem_cy - original_coords[1]) ** 2
            )

            id_match = target_resource_id and elem_res_id and target_resource_id == elem_res_id

            text_score = 0.0
            if norm_target_text and elem_norm_text:
                if norm_target_text == elem_norm_text:
                    text_score = 1.0
                elif norm_target_text in elem_norm_text or elem_norm_text in norm_target_text:
                    text_score = 0.8
                else:
                    text_score = difflib.SequenceMatcher(
                        None, norm_target_text, elem_norm_text
                    ).ratio()

            # Calculate bounds match (IoU)
            bounds_score = 0.0
            size_mismatch = False
            if target_bounds and elem_bounds and len(target_bounds) == 4 and len(elem_bounds) == 4:
                l1, t1, r1, b1 = target_bounds
                l2, t2, r2, b2 = elem_bounds

                # Check size ratio mismatch to prevent matching small widget to giant container
                w1, h1 = r1 - l1, b1 - t1
                w2, h2 = r2 - l2, b2 - t2
                if w1 > 0 and h1 > 0 and w2 > 0 and h2 > 0:
                    size_ratio_w = max(w1, w2) / min(w1, w2)
                    size_ratio_h = max(h1, h2) / min(h1, h2)
                    if size_ratio_w > 2.5 or size_ratio_h > 2.5:
                        size_mismatch = True

                li = max(l1, l2)
                ti = max(t1, t2)
                ri = min(r1, r2)
                bi = min(b1, b2)
                if ri > li and bi > ti:
                    inter_area = (ri - li) * (bi - ti)
                    union_area = (r1 - l1) * (b1 - t1) + (r2 - l2) * (b2 - t2) - inter_area
                    if union_area > 0:
                        bounds_score = inter_area / union_area

            # Calculate Coordinate Containment
            contains_coord = False
            if (
                original_coords
                and len(original_coords) == 2
                and elem_bounds
                and len(elem_bounds) == 4
            ):
                cx, cy = original_coords
                l2, t2, r2, b2 = elem_bounds
                contains_coord = l2 <= cx <= r2 and t2 <= cy <= b2

            # Apply size mismatch penalty
            if size_mismatch and not id_match:
                text_score = 0.0
                bounds_score = 0.0
                contains_coord = False

            # Define signal weights
            W_id = 0.5
            W_text = 0.4
            W_bounds = 0.3
            W_coord = 0.3

            signals = []
            weights = []
            identity_signals = []
            identity_weights = []

            # 1. Resource ID Signal
            if target_resource_id:
                weights.append(W_id)
                identity_weights.append(W_id)
                if id_match:
                    signals.append(W_id * 1.0)
                    identity_signals.append(W_id * 1.0)
                elif not elem_res_id:
                    signals.append(W_id * 0.3)  # Soft penalty for missing ID
                    identity_signals.append(W_id * 0.3)
                else:
                    signals.append(W_id * -0.5)  # Hard penalty for mismatched ID
                    identity_signals.append(W_id * -0.5)

            # 2. Text Signal
            if target_text:
                weights.append(W_text)
                identity_weights.append(W_text)
                if norm_target_text and elem_norm_text:
                    signals.append(W_text * text_score)
                    identity_signals.append(W_text * text_score)
                elif not elem_norm_text:
                    signals.append(W_text * 0.0)  # Penalty for missing expected text
                    identity_signals.append(W_text * 0.0)

            # 3. Bounds Signal
            if target_bounds:
                weights.append(W_bounds)
                signals.append(W_bounds * bounds_score)

            # 4. Coordinate Containment Signal
            if original_coords:
                weights.append(W_coord)
                signals.append(W_coord * (1.0 if contains_coord else 0.0))

            if weights:
                score = sum(signals) / sum(weights)
            else:
                score = 1.0

            if identity_weights:
                identity_score = sum(identity_signals) / sum(identity_weights)
            else:
                identity_score = 1.0

            decay = max(0.5, 1.0 - (dist / 800.0))
            score *= decay

            candidates.append(
                {
                    "element": elem,
                    "center": [elem_cx, elem_cy],
                    "bounds": elem_bounds,
                    "text": elem_raw_text,
                    "resource_id": elem_res_id,
                    "score": score,
                    "identity_score": identity_score,
                    "distance": dist,
                    "text_score": text_score,
                    "id_match": id_match,
                }
            )

        if not candidates:
            logger.warning(
                "No candidates with valid bounds found. Falling back to Pixel-based validation."
            )
            return (
                False,
                ValidationErrorCategory.XML_BYPASSED,
                "No candidates with valid bounds found.",
            )

        candidates.sort(key=lambda x: x["score"], reverse=True)
        best = candidates[0]

        passed = False
        category = ValidationErrorCategory.NONE
        reason = ""

        threshold = 0.55 if best["distance"] <= 150 * scale_factor else 0.75

        if best["score"] >= threshold:
            passed = True
            if best["distance"] <= 200 * scale_factor:
                new_coords = best["center"]
                old_coords = action_item.get("coordinates")
                if old_coords != new_coords:
                    logger.success(
                        "Pre-execution validation SUCCESS"
                        f" (score={best['score']:.2f}). Target element"
                        f" '{target_text}' shifted by {best['distance']:.1f}px."
                        f" Self-healing: correcting coordinates {old_coords} ->"
                        f" {new_coords}."
                    )
                    action_item["coordinates"] = new_coords
            else:
                logger.info(
                    "Pre-execution validation SUCCESS"
                    f" (score={best['score']:.2f}). Target element matched but"
                    f" shifted by {best['distance']:.1f}px"
                    f" (>{200 * scale_factor:.1f}px). Bypassing coordinate"
                    " self-healing to remain conservative."
                )
        else:
            original_coords = action_item.get("coordinates")

            # 1. Shifted Case: we must be conservative to avoid confusing disappearance as shift.
            # Shift misclassification as disappeared is acceptable, but disappeared as shift
            # causes errors.
            is_valid_shift = False

            # Enforce strict shift criteria (preferring disappeared over shifted)
            max_allowed_distance = 100.0 * scale_factor
            if best.get("id_match", False):
                # Allow larger distance threshold only for strong resource ID matches
                max_allowed_distance = 300.0 * scale_factor

            if best["distance"] <= max_allowed_distance:
                if best["identity_score"] >= 0.85:
                    # High confidence identity match (e.g. exact text or exact ID)
                    is_valid_shift = True
                elif best["identity_score"] >= 0.5 and best.get("id_match", False):
                    # Moderate confidence with exact resource ID match
                    is_valid_shift = True

            if is_valid_shift:
                category = ValidationErrorCategory.TARGET_SHIFTED
                reason = (
                    f"Target element '{target_text}' ({target_resource_id})"
                    f" expected at {original_coords} (bounds: {target_bounds})"
                    f" has shifted.\n- New location: {best['center']} (bounds:"
                    f" {best['bounds']})"
                )
            else:
                # 2. Find the most specific element containing expected coordinates
                occupant = None
                if original_coords and len(original_coords) == 2:
                    orig_cx, orig_cy = original_coords
                    for elem in elements:
                        if not isinstance(elem, dict):
                            continue
                        elem_bounds_str = elem.get("bounds")
                        elem_bounds = visualization.parse_bounds(elem_bounds_str)
                        if elem_bounds:
                            left, top, right, bottom = elem_bounds
                            if left <= orig_cx <= right and top <= orig_cy <= bottom:
                                elem_area = (right - left) * (bottom - top)
                                if not occupant or elem_area < occupant[2]:
                                    occupant = (elem, elem_bounds, elem_area)

                def is_interactive(e: dict) -> bool:
                    clickable = e.get("clickable")
                    long_clickable = e.get("long-clickable") or e.get("longClickable")
                    focusable = e.get("focusable")
                    enabled = e.get("enabled")

                    def to_bool(v) -> bool:
                        if v is None:
                            return False
                        if isinstance(v, bool):
                            return v
                        return str(v).lower() == "true"

                    return (
                        to_bool(clickable)
                        or to_bool(long_clickable)
                        or to_bool(focusable)
                        or to_bool(enabled)
                    )

                is_occupied = False
                occupant_desc = "anonymous element"
                occupant_bounds = None

                if occupant:
                    elem, elem_bounds, _ = occupant
                    occupant_text = (
                        elem.get("text")
                        or elem.get("content-desc")
                        or elem.get("accessibilityText")
                        or ""
                    )
                    occupant_res_id = elem.get("resource-id") or elem.get("resourceId") or ""
                    occupant_bounds = elem_bounds

                    # Check if the occupant has content
                    has_content = bool(occupant_text or occupant_res_id)

                    # Check if this occupant or any container covering this point is interactive
                    coords_interactive = False
                    if original_coords and len(original_coords) == 2:
                        orig_cx, orig_cy = original_coords
                        for e in elements:
                            if not isinstance(e, dict):
                                continue
                            eb_str = e.get("bounds")
                            eb = visualization.parse_bounds(eb_str)
                            if eb:
                                e_left, e_top, e_right, e_bottom = eb
                                if e_left <= orig_cx <= e_right and e_top <= orig_cy <= e_bottom:
                                    # Exclude full-screen root background elements
                                    e_width = e_right - e_left
                                    e_height = e_bottom - e_top
                                    is_full_screen = (
                                        e_width >= width - 10 and e_height >= height - 10
                                    )

                                    if is_interactive(e) and not is_full_screen:
                                        coords_interactive = True
                                        break

                    # Exclude full-screen or giant parent containers from being considered blockers
                    is_occupant_full_screen = False
                    if occupant_bounds:
                        o_w = occupant_bounds[2] - occupant_bounds[0]
                        o_h = occupant_bounds[3] - occupant_bounds[1]
                        is_occupant_full_screen = o_w >= width - 10 and o_h >= height - 10

                        # If target bounds are known, check if occupant is a giant container
                        if target_bounds:
                            t_w = target_bounds[2] - target_bounds[0]
                            t_h = target_bounds[3] - target_bounds[1]
                            t_area = t_w * t_h
                            o_area = o_w * o_h
                            if t_area > 0 and o_area > 3.0 * t_area:
                                is_occupant_full_screen = True

                    if (has_content or coords_interactive) and not is_occupant_full_screen:
                        is_occupied = True
                        occupant_desc = (
                            f"'{occupant_text}' ({occupant_res_id})"
                            if has_content
                            else "interactive anonymous element"
                        )

                # If coordinates are occupied by a different active/labeled element
                if is_occupied:
                    category = ValidationErrorCategory.TARGET_OCCUPIED
                    reason = (
                        f"The expected position {original_coords} for"
                        f" '{target_text}' ({target_resource_id}) is"
                        " occupied/intercepted by a different element:"
                        f" {occupant_desc} at bounds {occupant_bounds}."
                    )
                else:
                    # 3. Completely Missing Case
                    category = ValidationErrorCategory.TARGET_DISAPPEARED
                    reason = (
                        f"Target element '{target_text}' ({target_resource_id})"
                        f" expected at {original_coords} (bounds:"
                        f" {target_bounds}) was not found on the screen."
                    )

        return passed, category, reason

    def _parse_mcp_result(self, result) -> tuple[bool, str]:
        """Helper to parse MCP tool result."""
        if hasattr(result, "content") and result.content:
            text_content = (
                result.content[0].text
                if hasattr(result.content[0], "text")
                else str(result.content[0])
            )
            if text_content.startswith("Error:") or text_content.startswith("Failed:"):
                return False, text_content
            elif text_content == "Success":
                return True, ""
            elif text_content == "Failed":
                return False, "Action failed on server"
            return True, text_content
        return True, ""

    @trace(type="tool", name="safety_net_pixel_validation")
    async def _validate_action_precondition_pixel(
        self,
        session,
        action_item: dict,
        pre_screenshot_b64: str,
        original_coords: list[int] | None = None,
        state: State | None = None,
    ) -> tuple[bool, ValidationErrorCategory, str]:
        """Pixel-level Safety Net: Validates if visual target button/component at
        coordinates is still present, visible, and interactive on the live screen
        compared to the original screenshot.
        """
        action_name = action_item.get("action")

        # 1. White-list filtering: Only validate interactive, element-focused actions
        if action_name not in [
            "tap",
            "long_press_on",
            "focus_and_input_text",
            "input_text",
            "click_coordinate",
            "click",
            "long_press",
        ]:
            return True, ValidationErrorCategory.PIXEL_BYPASSED, ""

        current_coords = action_item.get("coordinates")
        orig_coords = original_coords or current_coords

        if not orig_coords or len(orig_coords) != 2:
            logger.info("Pixel safety net skipped: no valid coordinates.")
            return True, ValidationErrorCategory.PIXEL_BYPASSED, ""

        if not current_coords or len(current_coords) != 2:
            current_coords = orig_coords

        logger.info(
            "Pre-execution pixel validation: checking target position"
            f" {orig_coords} in original image vs {current_coords} on live"
            " screen..."
        )

        # 2. Decode and crop the static reference (original) image ONCE outside the loop to save CPU

        try:
            orig_bytes = base64.b64decode(pre_screenshot_b64)
            orig_crop_bytes = visualization.crop_and_annotate_target(
                orig_bytes, orig_coords, crop_size=None, dot_radius=15
            )
        except Exception as e:
            logger.error(
                f"Failed to prepare original crop for pixel safety net: {e}. Bypassing check."
            )
            return True, ValidationErrorCategory.PIXEL_BYPASSED, ""

        # 3. Get LLM Configuration once outside the loop

        try:
            try:
                llm = get_llm(self.ctx, name="validator_pixel_safety_net")
            except Exception:
                llm = get_llm(self.ctx, name="validator")

            # Load prompt template once
            prompt_path = Path(__file__).parent.joinpath("pixel_safety_net.md")
            prompt = prompt_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(
                f"Failed to initialize LLM/prompt for pixel safety net: {e}. Bypassing check."
            )
            return True, ValidationErrorCategory.PIXEL_BYPASSED, ""

        # 4. Start high-precision execution validation retry loop
        max_attempts = 3
        retry_delay_seconds = 0.3
        last_error = None

        for attempt in range(1, max_attempts + 1):
            logger.info(f"Pre-execution pixel validation: Attempt {attempt}/{max_attempts}...")
            try:
                # A. Take fresh live screenshot (captures state changes / dynamic loading settling)
                live_screenshot_b64 = None
                if session is not None:
                    result = await session.call_tool("take_screenshot", {})
                    success_img, live_screenshot_b64 = self._parse_mcp_result(result)
                elif getattr(self, "_local_controller", None):
                    live_screenshot_b64 = await self._local_controller.take_screenshot()
                    success_img = True
                else:
                    success_img, live_screenshot_b64 = False, None

                if not success_img or not live_screenshot_b64:
                    raise Exception("Failed to acquire live screenshot from controller/MCP tool.")

                # B. Decode and crop the live screen target
                live_bytes = base64.b64decode(live_screenshot_b64)
                live_crop_bytes = visualization.crop_and_annotate_target(
                    live_bytes, current_coords, crop_size=None, dot_radius=15
                )

                orig_b64 = base64.b64encode(orig_crop_bytes).decode("utf-8")
                live_b64 = base64.b64encode(live_crop_bytes).decode("utf-8")

                # C. Formulate multi-modal verification message
                user_content = [
                    {"type": "text", "text": "[Image 1 (Reference)]"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{orig_b64}"},
                    },
                    {"type": "text", "text": "[Image 2 (Current State)]"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{live_b64}"},
                    },
                ]
                if state:
                    thoughts = []
                    native_thought = getattr(state, "operator_native_thinking", None)
                    if native_thought and native_thought.strip():
                        thoughts.append(native_thought.strip())
                    raw_thought = getattr(state, "operator_raw_thinking", None)
                    if raw_thought and raw_thought.strip():
                        thoughts.append(raw_thought.strip())

                    operator_thought = "\n\n".join(thoughts)
                    if operator_thought.strip():
                        context_text = (
                            "[Planned Action & Original Thinking]\nAction:"
                            f" {action_name}\nOriginal"
                            f" Thinking:\n{operator_thought.strip()}"
                        )
                        user_content.append({"type": "text", "text": f"\n{context_text.strip()}"})

                messages = [
                    SystemMessage(content=prompt),
                    HumanMessage(content=user_content),
                ]

                # D. Invoke Universal VLM
                response = await llm.ainvoke(messages)
                output = response.content if isinstance(response.content, str) else ""
                if isinstance(response.content, list):
                    output = "".join(
                        b.get("text", "")
                        for b in response.content
                        if isinstance(b, dict) and "text" in b
                    )

                if output.startswith("```"):
                    code_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", output)
                    if code_match:
                        output = code_match.group(1).strip()

                res_json = json.loads(output)
                reasoning = res_json.get("reasoning", "")
                is_present = res_json.get("is_present", True)
                confidence = res_json.get("confidence", 1.0)

                logger.info(
                    f"Pixel validation attempt {attempt}/{max_attempts} result:"
                    f" is_present={is_present}, confidence={confidence:.2f}, reasoning={reasoning}"
                )

                # E. Branching based on outcome
                if is_present:
                    logger.info(f"Pixel validation SUCCESS on attempt {attempt}/{max_attempts}.")
                    return True, ValidationErrorCategory.NONE, ""

                # Target not present: fail or bypass based on confidence without retry!
                if confidence >= 0.7:
                    reason_msg = (
                        f"Pixel-level validation failed: {reasoning}"
                        if reasoning
                        else (
                            "Pixel-level validation failed: The target UI"
                            " element at the exact coordinates has disappeared,"
                            " changed, or is blocked."
                        )
                    )
                    return (
                        False,
                        ValidationErrorCategory.PIXEL_TARGET_DISAPPEARED,
                        reason_msg,
                    )
                else:
                    logger.warning(
                        "Pixel validation failed but confidence"
                        f" ({confidence:.2f}) is below 0.7. Bypassing check."
                    )
                    return True, ValidationErrorCategory.PIXEL_BYPASSED, ""

            except Exception as attempt_err:
                logger.warning(
                    f"Error during pixel validation attempt {attempt}/{max_attempts}: {attempt_err}"
                )
                last_error = attempt_err
                if attempt < max_attempts:
                    await asyncio.sleep(retry_delay_seconds)
                    continue
                else:
                    logger.error(
                        "Pixel validation repeatedly failed with errors. Last"
                        f" error: {last_error}. Bypassing check to avoid"
                        " blocking flow."
                    )
                    return True, ValidationErrorCategory.PIXEL_BYPASSED, ""

        return True, ValidationErrorCategory.PIXEL_BYPASSED, ""
