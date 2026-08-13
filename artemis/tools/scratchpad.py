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
from langchain_core.tools.base import BaseTool
from langgraph.types import Command
from pydantic import BaseModel, Field

from artemis.constants import VALIDATOR_MESSAGES_KEY
from artemis.context import ArtemisContext
from artemis.data_engine.trace import trace_langchain_tool
from artemis.drivers.base import BaseDeviceDriver
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.tools.tool_wrapper import ToolWrapper
from artemis.tools.types import CyFunctionDetector
from artemis.utils.logger import get_logger
from artemis.utils.notes import (
    APPEND_NOTE_ARG_CONTENT_DESC,
    APPEND_NOTE_ARG_KEY_DESC,
    APPEND_NOTE_DOCSTRING,
    LIST_NOTES_DOCSTRING,
    READ_NOTE_ARG_KEY_DESC,
    READ_NOTE_DOCSTRING,
    SAVE_NOTE_ARG_CONTENT_DESC,
    SAVE_NOTE_ARG_KEY_DESC,
    SAVE_NOTE_DOCSTRING,
    UPDATE_NOTE_ARG_KEY_DESC,
    UPDATE_NOTE_ARG_REPLACEMENT_DESC,
    UPDATE_NOTE_ARG_TARGET_DESC,
    UPDATE_NOTE_DOCSTRING,
    append_note_content,
    format_list_notes_failure,
    format_list_notes_success,
    format_read_note_failure,
    format_read_note_success,
    list_notes_info,
    read_note_content,
    save_note_content,
    update_note_content,
)

logger = get_logger(__name__)


class SaveNoteArgs(BaseModel):
    """Arguments schema for saving notes."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    key: str = Field(..., description=SAVE_NOTE_ARG_KEY_DESC)
    content: str = Field(..., description=SAVE_NOTE_ARG_CONTENT_DESC)


class AppendNoteArgs(BaseModel):
    """Arguments schema for appending notes."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    key: str = Field(..., description=APPEND_NOTE_ARG_KEY_DESC)
    content: str = Field(..., description=APPEND_NOTE_ARG_CONTENT_DESC)


class UpdateNoteArgs(BaseModel):
    """Arguments schema for updating notes."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    key: str = Field(..., description=UPDATE_NOTE_ARG_KEY_DESC)
    target: str = Field(..., description=UPDATE_NOTE_ARG_TARGET_DESC)
    replacement: str = Field(..., description=UPDATE_NOTE_ARG_REPLACEMENT_DESC)


class ReadNoteArgs(BaseModel):
    """Arguments schema for reading notes."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    key: str = Field(..., description=READ_NOTE_ARG_KEY_DESC)
    start_line: int | None = Field(None, description="Start line to read (1-indexed, inclusive)")
    end_line: int | None = Field(None, description="End line to read (1-indexed, inclusive)")


class ListNotesArgs(BaseModel):
    """Arguments schema for listing notes."""

    model_config = {"ignored_types": (CyFunctionDetector,)}


class SaveNoteTool(ArtemisTool):
    """Universal tool for saving notes to persistent memory."""

    def __init__(self):
        super().__init__(
            name="save_note",
            description=SAVE_NOTE_DOCSTRING,
            args_schema=SaveNoteArgs,
            category="memory",
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        key: str | None = None,
        content: str | None = None,
        tool_call_id: str | None = None,
        state: State | None = None,
        **kwargs: Any,
    ) -> Any:
        k = (
            key
            if key is not None
            else (
                kwargs.get("key")
                or kwargs.get("Key")
                or kwargs.get("title")
                or kwargs.get("Title")
                or ""
            )
        )
        c = (
            content
            if content is not None
            else (kwargs.get("content") or kwargs.get("Content") or "")
        )
        tcid = (tool_call_id if tool_call_id is not None else kwargs.get("tool_call_id")) or ""
        st = state if state is not None else kwargs.get("state")

        try:
            logger.info(f"save_note called with key='{k}'")
            base_dir = (
                ctx.data_engine.base_dir
                if (
                    ctx
                    and getattr(ctx, "data_engine", None)
                    and getattr(ctx.data_engine, "base_dir", None)
                )
                else "."
            )
            save_note_content(base_dir, k, c)
            agent_outcome = f"Successfully saved note to {k}.md."
            status = "success"
        except Exception as e:  # pylint: disable=broad-exception-caught
            agent_outcome = f"Failed to save note {k}.md: {e}"
            status = "error"

        if st and callable(getattr(st, "asanitize_update", None)):
            tool_message = ToolMessage(
                tool_call_id=tcid,
                content=agent_outcome,
                status=status,
            )

            current_agent = "operator"
            if hasattr(st, "current_agent") and st.current_agent:
                current_agent = st.current_agent

            return Command(
                update=await st.asanitize_update(
                    ctx=ctx,
                    update={
                        VALIDATOR_MESSAGES_KEY: [tool_message],
                    },
                    agent=current_agent,
                ),
            )

        return agent_outcome


# Universal tool instance & aliases
save_note = SaveNoteTool()
SaveNote = SaveNoteTool
ToolRegistry.register(save_note)


def get_save_note_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports save_note as a LangChain BaseTool."""
    return trace_langchain_tool(save_note.to_langchain_tool(ctx), ctx)


class AppendNoteTool(ArtemisTool):
    """Universal tool for appending text content to notes in persistent memory."""

    def __init__(self):
        super().__init__(
            name="append_note",
            description=APPEND_NOTE_DOCSTRING,
            args_schema=AppendNoteArgs,
            category="memory",
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        key: str | None = None,
        content: str | None = None,
        tool_call_id: str | None = None,
        state: State | None = None,
        **kwargs: Any,
    ) -> Any:
        k = (
            key
            if key is not None
            else (
                kwargs.get("key")
                or kwargs.get("Key")
                or kwargs.get("title")
                or kwargs.get("Title")
                or ""
            )
        )
        c = (
            content
            if content is not None
            else (kwargs.get("content") or kwargs.get("Content") or "")
        )
        tcid = (tool_call_id if tool_call_id is not None else kwargs.get("tool_call_id")) or ""
        st = state if state is not None else kwargs.get("state")

        try:
            logger.info(f"append_note called with key='{k}'")
            base_dir = (
                ctx.data_engine.base_dir
                if (
                    ctx
                    and getattr(ctx, "data_engine", None)
                    and getattr(ctx.data_engine, "base_dir", None)
                )
                else "."
            )
            append_note_content(base_dir, k, c)
            agent_outcome = f"Successfully appended note to {k}.md."
            status = "success"
        except Exception as e:  # pylint: disable=broad-exception-caught
            agent_outcome = f"Failed to append note {k}.md: {e}"
            status = "error"

        if st and callable(getattr(st, "asanitize_update", None)):
            tool_message = ToolMessage(
                tool_call_id=tcid,
                content=agent_outcome,
                status=status,
            )

            current_agent = "operator"
            if hasattr(st, "current_agent") and st.current_agent:
                current_agent = st.current_agent

            return Command(
                update=await st.asanitize_update(
                    ctx=ctx,
                    update={
                        VALIDATOR_MESSAGES_KEY: [tool_message],
                    },
                    agent=current_agent,
                ),
            )

        return agent_outcome


# Universal tool instance & aliases
append_note = AppendNoteTool()
AppendNote = AppendNoteTool
ToolRegistry.register(append_note)


def get_append_note_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports append_note as a LangChain BaseTool."""
    return trace_langchain_tool(append_note.to_langchain_tool(ctx), ctx)


class UpdateNoteTool(ArtemisTool):
    """Universal tool for updating notes by replacing a target string in persistent memory."""

    def __init__(self):
        super().__init__(
            name="update_note",
            description=UPDATE_NOTE_DOCSTRING,
            args_schema=UpdateNoteArgs,
            category="memory",
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        key: str | None = None,
        target: str | None = None,
        replacement: str | None = None,
        tool_call_id: str | None = None,
        state: State | None = None,
        **kwargs: Any,
    ) -> Any:
        k = (
            key
            if key is not None
            else (
                kwargs.get("key")
                or kwargs.get("Key")
                or kwargs.get("title")
                or kwargs.get("Title")
                or ""
            )
        )
        t = target if target is not None else (kwargs.get("target") or kwargs.get("Target") or "")
        r = (
            replacement
            if replacement is not None
            else (kwargs.get("replacement") or kwargs.get("Replacement") or "")
        )
        tcid = (tool_call_id if tool_call_id is not None else kwargs.get("tool_call_id")) or ""
        st = state if state is not None else kwargs.get("state")

        try:
            logger.info(f"update_note called with key='{k}'")
            base_dir = (
                ctx.data_engine.base_dir
                if (
                    ctx
                    and getattr(ctx, "data_engine", None)
                    and getattr(ctx.data_engine, "base_dir", None)
                )
                else "."
            )
            warning = update_note_content(base_dir, k, t, r)
            if warning:
                agent_outcome = f"Successfully updated note '{k}'.\nWARNING: {warning}"
            else:
                agent_outcome = f"Successfully updated note '{k}'."
            status = "success"
        except Exception as e:  # pylint: disable=broad-exception-caught
            agent_outcome = f"Failed to update note '{k}': {e}"
            status = "error"

        if st and callable(getattr(st, "asanitize_update", None)):
            tool_message = ToolMessage(
                tool_call_id=tcid,
                content=agent_outcome,
                status=status,
            )

            current_agent = "operator"
            if hasattr(st, "current_agent") and st.current_agent:
                current_agent = st.current_agent

            return Command(
                update=await st.asanitize_update(
                    ctx=ctx,
                    update={
                        VALIDATOR_MESSAGES_KEY: [tool_message],
                    },
                    agent=current_agent,
                ),
            )

        return agent_outcome


# Universal tool instance & aliases
update_note = UpdateNoteTool()
UpdateNote = UpdateNoteTool
ToolRegistry.register(update_note)


def get_update_note_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports update_note as a LangChain BaseTool."""
    return trace_langchain_tool(update_note.to_langchain_tool(ctx), ctx)


class ReadNoteTool(ArtemisTool):
    """Universal tool for reading previously saved notes from persistent memory."""

    def __init__(self):
        super().__init__(
            name="read_note",
            description=READ_NOTE_DOCSTRING,
            args_schema=ReadNoteArgs,
            category="memory",
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        key: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        tool_call_id: str | None = None,
        state: State | None = None,
        **kwargs: Any,
    ) -> Any:
        k = (
            key
            if key is not None
            else (
                kwargs.get("key")
                or kwargs.get("Key")
                or kwargs.get("title")
                or kwargs.get("Title")
                or ""
            )
        )
        sl = (
            start_line
            if start_line is not None
            else (kwargs.get("start_line") or kwargs.get("StartLine"))
        )
        el = end_line if end_line is not None else (kwargs.get("end_line") or kwargs.get("EndLine"))
        tcid = (tool_call_id if tool_call_id is not None else kwargs.get("tool_call_id")) or ""
        st = state if state is not None else kwargs.get("state")

        try:
            logger.info(f"read_note called with key='{k}'")
            base_dir = (
                ctx.data_engine.base_dir
                if (
                    ctx
                    and getattr(ctx, "data_engine", None)
                    and getattr(ctx.data_engine, "base_dir", None)
                )
                else "."
            )
            content = read_note_content(base_dir, k, sl, el)
            agent_outcome = read_note_wrapper.on_success_fn(k, content, sl, el)
            status = "success"
        except Exception as e:  # pylint: disable=broad-exception-caught
            if "not found" in str(e).lower():
                agent_outcome = read_note_wrapper.on_failure_fn(k)
            else:
                agent_outcome = f"Failed to read note '{k}': {e}"
            status = "error"

        if st and callable(getattr(st, "asanitize_update", None)):
            tool_message = ToolMessage(
                tool_call_id=tcid,
                content=agent_outcome,
                status=status,
            )

            current_agent = "operator"
            if hasattr(st, "current_agent") and st.current_agent:
                current_agent = st.current_agent

            return Command(
                update=await st.asanitize_update(
                    ctx=ctx,
                    update={
                        VALIDATOR_MESSAGES_KEY: [tool_message],
                    },
                    agent=current_agent,
                ),
            )

        return agent_outcome


# Universal tool instance & aliases
read_note = ReadNoteTool()
ReadNote = ReadNoteTool
ToolRegistry.register(read_note)


def get_read_note_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports read_note as a LangChain BaseTool."""
    return trace_langchain_tool(read_note.to_langchain_tool(ctx), ctx)


class ListNotesTool(ArtemisTool):
    """Universal tool for listing all stored note keys in persistent memory."""

    def __init__(self):
        super().__init__(
            name="list_notes",
            description=LIST_NOTES_DOCSTRING,
            args_schema=ListNotesArgs,
            category="memory",
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        tool_call_id: str | None = None,
        state: State | None = None,
        **kwargs: Any,
    ) -> Any:
        tcid = (tool_call_id if tool_call_id is not None else kwargs.get("tool_call_id")) or ""
        st = state if state is not None else kwargs.get("state")

        try:
            logger.info("list_notes called")
            base_dir = (
                ctx.data_engine.base_dir
                if (
                    ctx
                    and getattr(ctx, "data_engine", None)
                    and getattr(ctx.data_engine, "base_dir", None)
                )
                else "."
            )
            notes_info = list_notes_info(base_dir)
            agent_outcome = list_notes_wrapper.on_success_fn(notes_info)
            status = "success"
        except Exception as e:  # pylint: disable=broad-exception-caught
            agent_outcome = f"Failed to list notes: {e}"
            status = "error"

        if st and callable(getattr(st, "asanitize_update", None)):
            tool_message = ToolMessage(
                tool_call_id=tcid,
                content=agent_outcome,
                status=status,
            )

            current_agent = "operator"
            if hasattr(st, "current_agent") and st.current_agent:
                current_agent = st.current_agent

            return Command(
                update=await st.asanitize_update(
                    ctx=ctx,
                    update={
                        VALIDATOR_MESSAGES_KEY: [tool_message],
                    },
                    agent=current_agent,
                ),
            )

        return agent_outcome


# Universal tool instance & aliases
list_notes = ListNotesTool()
ListNotes = ListNotesTool
ToolRegistry.register(list_notes)


def get_list_notes_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports list_notes as a LangChain BaseTool."""
    return trace_langchain_tool(list_notes.to_langchain_tool(ctx), ctx)


class ReadNotePureTool(ArtemisTool):
    """Universal pure tool for reading previously saved notes from persistent memory."""

    def __init__(self):
        super().__init__(
            name="read_note_pure",
            description=READ_NOTE_DOCSTRING,
            args_schema=ReadNoteArgs,
            category="memory",
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        key: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        **kwargs: Any,
    ) -> str:
        k = (
            key
            if key is not None
            else (
                kwargs.get("key")
                or kwargs.get("Key")
                or kwargs.get("title")
                or kwargs.get("Title")
                or ""
            )
        )
        sl = (
            start_line
            if start_line is not None
            else (kwargs.get("start_line") or kwargs.get("StartLine"))
        )
        el = end_line if end_line is not None else (kwargs.get("end_line") or kwargs.get("EndLine"))

        try:
            logger.info(f"read_note_pure called with key='{k}'")
            base_dir = (
                ctx.data_engine.base_dir
                if (
                    ctx
                    and getattr(ctx, "data_engine", None)
                    and getattr(ctx.data_engine, "base_dir", None)
                )
                else "."
            )
            content = read_note_content(base_dir, k, sl, el)
            return format_read_note_success(k, content, sl, el)
        except Exception as e:  # pylint: disable=broad-exception-caught
            return format_read_note_failure(k, str(e))


# Universal pure tool instance & aliases
read_note_pure = ReadNotePureTool()
ReadNotePure = ReadNotePureTool
ToolRegistry.register(read_note_pure)


def get_read_note_tool_pure(ctx: ArtemisContext) -> BaseTool:
    """Exports read_note_pure as a LangChain BaseTool named 'read_note'."""
    return read_note_pure.to_langchain_tool(ctx, name="read_note")


class ListNotesPureTool(ArtemisTool):
    """Universal pure tool for listing all stored note keys in persistent memory."""

    def __init__(self):
        super().__init__(
            name="list_notes_pure",
            description=LIST_NOTES_DOCSTRING,
            args_schema=ListNotesArgs,
            category="memory",
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            logger.info("list_notes_pure called")
            base_dir = (
                ctx.data_engine.base_dir
                if (
                    ctx
                    and getattr(ctx, "data_engine", None)
                    and getattr(ctx.data_engine, "base_dir", None)
                )
                else "."
            )
            notes_info = list_notes_info(base_dir)
            return format_list_notes_success(notes_info)
        except Exception as e:  # pylint: disable=broad-exception-caught
            return format_list_notes_failure(str(e))


# Universal pure tool instance & aliases
list_notes_pure = ListNotesPureTool()
ListNotesPure = ListNotesPureTool
ToolRegistry.register(list_notes_pure)


def get_list_notes_tool_pure(ctx: ArtemisContext) -> BaseTool:
    """Exports list_notes_pure as a LangChain BaseTool named 'list_notes'."""
    return list_notes_pure.to_langchain_tool(ctx, name="list_notes")


class SaveNotePureTool(ArtemisTool):
    """Universal pure tool for saving notes to persistent memory."""

    def __init__(self):
        super().__init__(
            name="save_note_pure",
            description=SAVE_NOTE_DOCSTRING,
            args_schema=SaveNoteArgs,
            category="memory",
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        key: str | None = None,
        content: str | None = None,
        **kwargs: Any,
    ) -> str:
        k = (
            key
            if key is not None
            else (
                kwargs.get("key")
                or kwargs.get("Key")
                or kwargs.get("title")
                or kwargs.get("Title")
                or ""
            )
        )
        c = (
            content
            if content is not None
            else (kwargs.get("content") or kwargs.get("Content") or "")
        )

        try:
            logger.info(f"save_note_pure called with key='{k}'")
            base_dir = (
                ctx.data_engine.base_dir
                if (
                    ctx
                    and getattr(ctx, "data_engine", None)
                    and getattr(ctx.data_engine, "base_dir", None)
                )
                else "."
            )
            save_note_content(base_dir, k, c)
            return f"Successfully saved note to {k}.md."
        except Exception as e:  # pylint: disable=broad-exception-caught
            return f"Failed to save note {k}.md: {e}"


# Universal pure tool instance & aliases
save_note_pure = SaveNotePureTool()
SaveNotePure = SaveNotePureTool
ToolRegistry.register(save_note_pure)


def get_save_note_tool_pure(ctx: ArtemisContext) -> BaseTool:
    """Exports save_note_pure as a LangChain BaseTool named 'save_note'."""
    return save_note_pure.to_langchain_tool(ctx, name="save_note")


class UpdateNotePureTool(ArtemisTool):
    """Universal pure tool for updating notes by replacing a target string in persistent memory."""

    def __init__(self):
        super().__init__(
            name="update_note_pure",
            description=UPDATE_NOTE_DOCSTRING,
            args_schema=UpdateNoteArgs,
            category="memory",
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        key: str | None = None,
        target: str | None = None,
        replacement: str | None = None,
        **kwargs: Any,
    ) -> str:
        k = (
            key
            if key is not None
            else (
                kwargs.get("key")
                or kwargs.get("Key")
                or kwargs.get("title")
                or kwargs.get("Title")
                or ""
            )
        )
        t = target if target is not None else (kwargs.get("target") or kwargs.get("Target") or "")
        r = (
            replacement
            if replacement is not None
            else (kwargs.get("replacement") or kwargs.get("Replacement") or "")
        )

        try:
            logger.info(f"update_note_pure called with key='{k}'")
            base_dir = (
                ctx.data_engine.base_dir
                if (
                    ctx
                    and getattr(ctx, "data_engine", None)
                    and getattr(ctx.data_engine, "base_dir", None)
                )
                else "."
            )
            warning = update_note_content(base_dir, k, t, r)
            if warning:
                return f"Successfully updated note '{k}'.\nWARNING: {warning}"
            return f"Successfully updated note '{k}'."
        except Exception as e:  # pylint: disable=broad-exception-caught
            return f"Failed to update note '{k}': {e}"


# Universal pure tool instance & aliases
update_note_pure = UpdateNotePureTool()
UpdateNotePure = UpdateNotePureTool
ToolRegistry.register(update_note_pure)


def get_update_note_tool_pure(ctx: ArtemisContext) -> BaseTool:
    """Exports update_note_pure as a LangChain BaseTool named 'update_note'."""
    return update_note_pure.to_langchain_tool(ctx, name="update_note")


class AppendNotePureTool(ArtemisTool):
    """Universal pure tool for appending text content to notes in persistent memory."""

    def __init__(self):
        super().__init__(
            name="append_note_pure",
            description=APPEND_NOTE_DOCSTRING,
            args_schema=AppendNoteArgs,
            category="memory",
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        key: str | None = None,
        content: str | None = None,
        **kwargs: Any,
    ) -> str:
        k = (
            key
            if key is not None
            else (
                kwargs.get("key")
                or kwargs.get("Key")
                or kwargs.get("title")
                or kwargs.get("Title")
                or ""
            )
        )
        c = (
            content
            if content is not None
            else (kwargs.get("content") or kwargs.get("Content") or "")
        )

        try:
            logger.info(f"append_note_pure called with key='{k}'")
            base_dir = (
                ctx.data_engine.base_dir
                if (
                    ctx
                    and getattr(ctx, "data_engine", None)
                    and getattr(ctx.data_engine, "base_dir", None)
                )
                else "."
            )
            append_note_content(base_dir, k, c)
            return f"Successfully appended note to {k}.md."
        except Exception as e:  # pylint: disable=broad-exception-caught
            return f"Failed to append note {k}.md: {e}"


# Universal pure tool instance & aliases
append_note_pure = AppendNotePureTool()
AppendNotePure = AppendNotePureTool
ToolRegistry.register(append_note_pure)


def get_append_note_tool_pure(ctx: ArtemisContext) -> BaseTool:
    """Exports append_note_pure as a LangChain BaseTool named 'append_note'."""
    return append_note_pure.to_langchain_tool(ctx, name="append_note")


save_note_wrapper = ToolWrapper(
    tool_fn_getter=get_save_note_tool,
    on_success_fn=lambda key: f"Successfully saved note '{key}'.",
    on_failure_fn=lambda key: f"Failed to save note '{key}'.",
)

read_note_wrapper = ToolWrapper(
    tool_fn_getter=get_read_note_tool,
    on_success_fn=format_read_note_success,
    on_failure_fn=lambda key: format_read_note_failure(key, "not found"),
)

list_notes_wrapper = ToolWrapper(
    tool_fn_getter=get_list_notes_tool,
    on_success_fn=format_list_notes_success,
    on_failure_fn=lambda: format_list_notes_failure("error"),
)


update_note_wrapper = ToolWrapper(
    tool_fn_getter=get_update_note_tool,
    on_success_fn=lambda key: f"Successfully updated note '{key}'.",
    on_failure_fn=lambda key: f"Failed to update note '{key}'.",
)


append_note_wrapper = ToolWrapper(
    tool_fn_getter=get_append_note_tool,
    on_success_fn=lambda key: f"Successfully appended note '{key}'.",
    on_failure_fn=lambda key: f"Failed to append note '{key}'.",
)
