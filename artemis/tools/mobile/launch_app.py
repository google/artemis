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

from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from pydantic import BaseModel, Field

from artemis.agents.hopper.hopper import HopperOutput, hopper
from artemis.constants import VALIDATOR_MESSAGES_KEY
from artemis.context import ArtemisContext
from artemis.controllers.platform_specific_commands_controller import (
    list_packages_async,
)
from artemis.data_engine.trace import trace_langchain_tool
from artemis.drivers.base import BaseDeviceDriver
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolCategory, ToolRegistry
from artemis.tools.tool_wrapper import ToolWrapper
from artemis.tools.types import CyFunctionDetector
from artemis.utils.app_launch_utils import launch_app_with_retries
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class LaunchAppArgs(BaseModel):
    """Arguments schema for launching an application."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    app_name: str = Field(
        ...,
        description="The natural language name of the application to launch.",
    )


LAUNCH_APP_DOCSTRING = (
    "[ACTION] Finds and launches an application on the device using its natural language name."
)


async def find_package(ctx: ArtemisContext, app_name: str, use_fallback: bool = True) -> str | None:
    """Finds the package name for a given application name.

    Returns None if package not found or on error.
    """
    package_cache = getattr(ctx, "package_cache", None)
    if package_cache is None or not isinstance(package_cache, dict):
        try:
            ctx.package_cache = {}
            package_cache = ctx.package_cache
        except Exception:  # pylint: disable=broad-exception-caught
            package_cache = {}

    if isinstance(package_cache, dict) and app_name in package_cache:
        logger.info(f"Cache hit for app '{app_name}': {package_cache[app_name]}")
        return package_cache[app_name]

    try:
        all_packages = await list_packages_async(ctx=ctx)
        package_set = {p.strip() for p in all_packages.split("\n") if p.strip()}

        # Fast path: If app_name is already directly an installed package name
        if app_name in package_set:
            if isinstance(package_cache, dict):
                package_cache[app_name] = app_name
            return app_name

        hopper_output: HopperOutput = await hopper(
            ctx=ctx,
            request=(f"I'm looking for the package name of the following app: '{app_name}'"),
            data=all_packages,
            use_fallback=use_fallback,
        )
        if not hopper_output.found or not hopper_output.output:
            if isinstance(package_cache, dict):
                package_cache[app_name] = None
            return None

        package_name = hopper_output.output.strip()
        if package_name not in package_set:
            logger.warning(
                f"Hopper returned package '{package_name}' for '{app_name}', "
                "but it is NOT physically installed on the device!"
            )
            if isinstance(package_cache, dict):
                package_cache[app_name] = None
            return None

        if isinstance(package_cache, dict):
            package_cache[app_name] = package_name
        return package_name
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error(f"Failed to find package for '{app_name}': {e}")
        return None


class LaunchAppTool(ArtemisTool):
    """Universal tool for finding and launching an application on the device."""

    def __init__(self, category: ToolCategory = "action"):
        super().__init__(
            name="launch_app",
            description=LAUNCH_APP_DOCSTRING,
            args_schema=LaunchAppArgs,
            category=category,
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,
        ctx: ArtemisContext | None = None,
        app_name: str | None = None,
        tool_call_id: str | None = None,
        state: State | None = None,
        **kwargs: Any,
    ) -> Any:
        app = (
            app_name
            if app_name is not None
            else (kwargs.get("app_name") or kwargs.get("AppName") or "")
        )
        tcid = tool_call_id if tool_call_id is not None else kwargs.get("tool_call_id")
        st = state if state is not None else kwargs.get("state")

        success = False
        error_msg = None

        try:
            if not app:
                raise ValueError("app_name parameter is required.")

            if ctx is not None:
                package_name = await find_package(ctx=ctx, app_name=app)
                if not package_name:
                    success = False
                    outcome = f"Failed to launch app '{app}': Package not found."
                    error_msg = "Package not found."
                else:
                    success, error_msg = await launch_app_with_retries(
                        ctx=ctx, app_package=package_name
                    )
                    outcome = (
                        f"App '{app}' launched successfully."
                        if success
                        else f"Failed to launch app '{app}': {error_msg}"
                    )
            elif driver is not None and hasattr(driver, "launch_app"):
                success = await driver.launch_app(app)
                outcome = (
                    f"App '{app}' launched successfully."
                    if success
                    else f"Failed to launch app '{app}'."
                )
                error_msg = None if success else "Launch failed."
            else:
                success = False
                outcome = "Error during launch app: No driver or context provided."
                error_msg = "No driver or context provided."
        except Exception as e:  # pylint: disable=broad-exception-caught
            success = False
            outcome = f"Error during launch app: {e}"
            error_msg = str(e)

        if st and callable(getattr(st, "asanitize_update", None)):
            additional_kwargs = {} if success else ({"error": error_msg} if error_msg else {})
            tool_message = ToolMessage(
                tool_call_id=tcid or "",
                content=outcome,
                additional_kwargs=additional_kwargs,
                status="success" if success else "error",
            )
            return Command(
                update=await st.asanitize_update(
                    ctx=ctx,
                    update={
                        VALIDATOR_MESSAGES_KEY: [tool_message],
                    },
                    agent="validator",
                )
            )

        return outcome


# Universal tool instance & aliases
launch_app = LaunchAppTool()
LaunchApp = LaunchAppTool
ToolRegistry.register(launch_app)


def get_launch_app_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports launch_app as a LangChain BaseTool."""
    return trace_langchain_tool(launch_app.to_langchain_tool(ctx), ctx)


launch_app_wrapper = ToolWrapper(
    tool_fn_getter=get_launch_app_tool,
    on_success_fn=lambda *args, **kwargs: "Success",
    on_failure_fn=lambda *args, **kwargs: "Failure",
)
