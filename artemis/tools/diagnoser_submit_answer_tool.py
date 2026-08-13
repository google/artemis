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

"""Universal tool for submitting final diagnosis and actionable recovery advice."""

from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from artemis.context import ArtemisContext
from artemis.data_engine.trace import trace_langchain_tool
from artemis.drivers.base import BaseDeviceDriver
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


# pylint: disable=too-few-public-methods
class SubmitAnswerArgs(BaseModel):
    """Arguments schema for submitting final diagnoser answer."""

    analysis: str = Field(
        ...,
        description=(
            "A detailed and concise explanation of the root cause of the"
            " failure or query. Answer in one full paragraph."
        ),
    )
    actionable_steps: list[str] = Field(
        default_factory=list,
        description=(
            "A list of concrete recovery instructions or next steps. Each"
            " step is a short sentence. If no actionable steps are"
            " available, return an empty list."
        ),
    )


class SubmitAnswerTool(ArtemisTool):
    """Universal tool for submitting final diagnosis and actionable recovery advice."""

    def __init__(self):
        super().__init__(
            name="submit_answer",
            description=(
                "[Finalization] Submits the final diagnosis and actionable recovery advice. "
                "Call this tool when you have pinpointed the root cause."
            ),
            args_schema=SubmitAnswerArgs,
            category="custom",
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,  # pylint: disable=unused-argument
        analysis: str | None = None,  # pylint: disable=unused-argument
        actionable_steps: list[str] | None = None,  # pylint: disable=unused-argument
        **kwargs: Any,
    ) -> str:
        logger.info("Diagnoser submitted answer via tool.")
        return "Answer submitted successfully."


# Universal tool instance & aliases
submit_answer = SubmitAnswerTool()
SubmitAnswer = SubmitAnswerTool
DiagnoserSubmitAnswerTool = SubmitAnswerTool
ToolRegistry.register(submit_answer)


def get_submit_answer_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports submit_answer as a LangChain BaseTool."""
    return trace_langchain_tool(submit_answer.to_langchain_tool(ctx), ctx)
