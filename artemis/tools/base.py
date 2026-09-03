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

"""Universal Tool Protocol for ARTEMIS.

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

from artemis.core.tool_declaration import ToolDeclaration
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

    def _flat_schema(self) -> tuple[dict[str, dict[str, Any]], list[str]]:
        if not self.args_schema:
            return {}, []
        return _flatten_json_schema_properties(self.args_schema.model_json_schema())

    def to_tool_declaration(self) -> ToolDeclaration:
        """Build the Flash declaration from the args_schema used by LangChain."""
        properties, required = self._flat_schema()
        return ToolDeclaration(
            name=self.name,
            description=self.description,
            parameters={"type": "object", "properties": properties, "required": required},
        )

    def to_genai_declaration(self) -> genai_types.FunctionDeclaration:
        """Exports this tool to a Google GenAI FunctionDeclaration schema."""
        flat_properties, required = self._flat_schema()
        properties = {}
        for prop_name, prop in flat_properties.items():
            genai_type = _GENAI_TYPES.get(prop["type"], genai_types.Type.STRING)
            items_schema = None
            if genai_type == genai_types.Type.ARRAY:
                item_type = (prop.get("items") or {}).get("type", "string")
                items_schema = genai_types.Schema(
                    type=_GENAI_TYPES.get(item_type, genai_types.Type.STRING)
                )
            properties[prop_name] = genai_types.Schema(
                type=genai_type,
                description=prop.get("description", ""),
                items=items_schema,
                enum=[str(v) for v in prop["enum"]] if prop.get("enum") else None,
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


_GENAI_TYPES = {
    "string": genai_types.Type.STRING,
    "integer": genai_types.Type.INTEGER,
    "number": genai_types.Type.NUMBER,
    "boolean": genai_types.Type.BOOLEAN,
    "array": genai_types.Type.ARRAY,
    "object": genai_types.Type.OBJECT,
}


def _flatten_property(prop_data: dict[str, Any]) -> dict[str, Any]:
    """One Pydantic JSON-schema property as a flat single-type entry.

    ``anyOf`` unions (``X | None``) collapse to their first non-null member;
    ``items`` (array element type) and ``enum`` (``Literal`` choices) are
    carried over; unknown shapes fall back to ``string``.
    """
    p_type = prop_data.get("type")
    items = prop_data.get("items")
    enum = prop_data.get("enum")
    if not p_type and "anyOf" in prop_data:
        for sub_schema in prop_data["anyOf"]:
            sub_type = sub_schema.get("type")
            if sub_type and sub_type != "null":
                p_type = sub_type
                items = items or sub_schema.get("items")
                enum = enum or sub_schema.get("enum")
                break
    if not p_type:
        p_type = "string"
    entry: dict[str, Any] = {"type": p_type}
    if p_type == "array":
        entry["items"] = {"type": (items or {}).get("type") or "string"}
    if enum:
        entry["enum"] = list(enum)
    return entry


def _flatten_json_schema_properties(
    schema: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Flattens a Pydantic model JSON schema into plain JSON-schema properties
    plus the required-field list. Descriptions are copied verbatim; non-null
    defaults are kept under ``default``."""
    properties: dict[str, dict[str, Any]] = {}
    for prop_name, prop_data in (schema.get("properties") or {}).items():
        entry = _flatten_property(prop_data)
        entry["description"] = prop_data.get("description", "")
        if prop_data.get("default") is not None:
            entry["default"] = prop_data["default"]
        properties[prop_name] = entry
    return properties, list(schema.get("required") or [])
