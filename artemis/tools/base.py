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

"""Universal Tool Protocol and Registry for ARTEMIS.

Enables tools to be authored once using standard Pydantic schemas and async handlers,
then automatically exported to LangChain Tools (Pro Graph), Google GenAI FunctionDeclarations
(FlashRunner), and MCP Tools (FastMCP) with zero code duplication.
"""

import asyncio
from collections.abc import Callable
import concurrent.futures
import inspect
from typing import Annotated, Any, Literal

from google.genai import types as genai_types
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.prebuilt import InjectedState
from pydantic import BaseModel

from artemis.drivers.base import BaseDeviceDriver
from artemis.drivers.factory import get_driver
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

ToolCategory = Literal["action", "perception", "system", "memory", "custom"]


class ArtemisTool:
    """Unified tool wrapper encapsulating schema, execution logic, and multi-protocol export."""

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def __init__(
        self,
        name: str,
        description: str,
        args_schema: type[BaseModel],
        handler: Callable[..., Any] | None = None,
        category: ToolCategory = "action",
    ):
        self.name = name
        self.description = description
        self.args_schema = args_schema
        self.handler = handler
        self.category = category

    def is_available(self, ctx: Any = None) -> bool:
        """Determines if this tool is currently available and should be exposed to agents."""
        return True

    async def execute(self, driver: BaseDeviceDriver, ctx: Any, **kwargs: Any) -> Any:
        """Executes the tool handler with injected driver and context."""
        # Inspect handler signature to only pass supported arguments
        if self.handler is not None:
            sig = inspect.signature(self.handler)
            call_kwargs = {}
            for param_name in sig.parameters:
                if param_name == "driver":
                    call_kwargs["driver"] = driver
                elif param_name in ("ctx", "context"):
                    call_kwargs[param_name] = ctx
                elif param_name in kwargs:
                    call_kwargs[param_name] = kwargs[param_name]

            if inspect.iscoroutinefunction(self.handler):
                return await self.handler(**call_kwargs)
            return self.handler(**call_kwargs)
        raise NotImplementedError("Subclasses must implement execute() or provide a handler.")

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Allows ArtemisTool instances to be invoked directly as callables."""
        driver = kwargs.pop("driver", None)
        ctx = kwargs.pop("ctx", None) or kwargs.pop("context", None)
        return await self.execute(driver=driver, ctx=ctx, *args, **kwargs)

    def to_langchain_tool(self, ctx: Any, name: str | None = None) -> BaseTool:
        """Exports this tool to a LangChain StructuredTool."""

        def _get_context_driver() -> BaseDeviceDriver | None:
            if (
                ctx is not None
                and hasattr(ctx, "device")
                and ctx.device
                and getattr(ctx.device, "device_id", None)
            ):
                try:
                    return get_driver(ctx)
                except Exception as e:  # pylint: disable=broad-exception-caught
                    logger.debug(f"Failed to get driver: {e}")
                    return None
            return getattr(ctx, "_active_driver", None) if ctx is not None else None

        def _func(state: Annotated[Any, InjectedState] = None, **kwargs):
            driver = _get_context_driver()
            if state is not None:
                kwargs["state"] = state
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(
                        asyncio.run, self.execute(driver=driver, ctx=ctx, **kwargs)
                    ).result()
            return asyncio.run(self.execute(driver=driver, ctx=ctx, **kwargs))

        async def _coroutine(state: Annotated[Any, InjectedState] = None, **kwargs):
            driver = _get_context_driver()
            if state is not None:
                kwargs["state"] = state
            return await self.execute(driver=driver, ctx=ctx, **kwargs)

        return StructuredTool.from_function(
            func=_func,
            coroutine=_coroutine,
            name=name or self.name,
            description=self.description,
            args_schema=self.args_schema,
        )

    def to_genai_declaration(self) -> genai_types.FunctionDeclaration:
        """Exports this tool to a Google GenAI FunctionDeclaration schema."""
        properties = {}
        required = []

        if self.args_schema:
            schema = self.args_schema.model_json_schema()
            props = schema.get("properties", {})
            required = schema.get("required", [])

            for prop_name, prop_data in props.items():
                p_type_str = prop_data.get("type")
                items_data = prop_data.get("items")
                if not p_type_str and "anyOf" in prop_data:
                    for sub_schema in prop_data["anyOf"]:
                        sub_type = sub_schema.get("type")
                        if sub_type and sub_type != "null":
                            p_type_str = sub_type
                            if "items" in sub_schema and not items_data:
                                items_data = sub_schema["items"]
                            break
                if not p_type_str:
                    p_type_str = "string"

                p_desc = prop_data.get("description", "")

                genai_type = genai_types.Type.STRING
                if p_type_str == "integer":
                    genai_type = genai_types.Type.INTEGER
                elif p_type_str == "number":
                    genai_type = genai_types.Type.NUMBER
                elif p_type_str == "boolean":
                    genai_type = genai_types.Type.BOOLEAN
                elif p_type_str == "array":
                    genai_type = genai_types.Type.ARRAY
                elif p_type_str == "object":
                    genai_type = genai_types.Type.OBJECT

                # Handle array items if present
                items_schema = None
                if genai_type == genai_types.Type.ARRAY:
                    item_type_str = (items_data or {}).get("type", "string")
                    item_genai_type = (
                        genai_types.Type.INTEGER
                        if item_type_str == "integer"
                        else genai_types.Type.STRING
                    )
                    items_schema = genai_types.Schema(type=item_genai_type)

                properties[prop_name] = genai_types.Schema(
                    type=genai_type,
                    description=p_desc,
                    items=items_schema,
                )

        return genai_types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties=properties,
                required=required,
            ),
        )


class ToolRegistry:
    """Global registry managing universal tools."""

    _tools: dict[str, ArtemisTool] = {}

    @classmethod
    def register(cls, tool: ArtemisTool) -> ArtemisTool:
        """Registers a tool in the global registry."""
        cls._tools[tool.name] = tool
        return tool

    @classmethod
    def get(cls, name: str) -> ArtemisTool | None:
        """Retrieves a tool by name from the registry."""
        normalized = name.split(":")[-1] if ":" in name else name
        return cls._tools.get(normalized)

    @classmethod
    def list_tools(
        cls, category: ToolCategory | None = None, available_only: bool = True
    ) -> list[ArtemisTool]:
        """Lists all registered tools, optionally filtered by category and availability."""
        tools = [t for t in cls._tools.values() if not available_only or t.is_available()]
        if category:
            return [t for t in tools if t.category == category]
        return list(tools)

    @classmethod
    def get_langchain_tools(
        cls, ctx: Any, names: list[str] | None = None, available_only: bool = True
    ) -> list[BaseTool]:
        """Returns LangChain tool wrappers for the requested or all registered tools."""
        tools = (
            [cls._tools[n] for n in names if n in cls._tools]
            if names
            else list(cls._tools.values())
        )
        if available_only:
            tools = [t for t in tools if t.is_available(ctx)]
        return [t.to_langchain_tool(ctx) for t in tools]

    @classmethod
    def get_genai_declarations(
        cls, names: list[str] | None = None, available_only: bool = True
    ) -> list[genai_types.FunctionDeclaration]:
        """Returns Google GenAI function declarations for registered tools."""
        tools = (
            [cls._tools[n] for n in names if n in cls._tools]
            if names
            else list(cls._tools.values())
        )
        if available_only:
            tools = [t for t in tools if t.is_available()]
        return [t.to_genai_declaration() for t in tools]

    @classmethod
    async def execute(
        cls, name: str, args: dict[str, Any], driver: BaseDeviceDriver, ctx: Any
    ) -> Any:
        """Executes a registered tool by name with provided arguments, driver, and context."""
        tool = cls.get(name)
        if not tool:
            raise ValueError(f"Tool '{name}' not found in ToolRegistry.")
        return await tool.execute(driver=driver, ctx=ctx, **args)


def artemis_tool(
    name: str,
    description: str,
    args_schema: type[BaseModel],
    category: ToolCategory = "action",
):
    """Decorator to define and register a universal ARTEMIS tool."""

    def decorator(fn: Callable[..., Any]) -> ArtemisTool:
        tool_obj = ArtemisTool(
            name=name,
            description=description,
            args_schema=args_schema,
            handler=fn,
            category=category,
        )
        ToolRegistry.register(tool_obj)
        return tool_obj

    return decorator
