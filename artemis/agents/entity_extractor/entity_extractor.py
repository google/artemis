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

"""Lightweight entity extraction and package resolver for ARTEMIS.

Dispatches bounded LLM extraction turns to identify application package identifiers
and configuration attributes from raw device listings without manual parsing heuristics.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from jinja2 import Template
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from artemis.context import ArtemisContext
from artemis.data_engine.trace import trace
from artemis.services.llm import get_llm, invoke_llm_with_timeout_message, with_fallback
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class ExtractionResult(BaseModel):
    """Structured extraction outcome for entity resolution."""

    found: bool = Field(description="True if the requested entity was definitively identified.")
    output: str | None = Field(default=None, description="Exact extracted identifier or string value.")
    reason: str = Field(description="Brief justification of the match criteria applied.")


@trace(type="agent", name="entity_extractor")
async def extract_entity(
    ctx: ArtemisContext,
    request: str,
    data: str,
    use_fallback: bool = True,
) -> ExtractionResult:
    """Extract a specific identifier or entity from raw batch device data."""
    logger.info(f"Invoking Artemis Entity Extractor (fallback={use_fallback})")
    template_path = Path(__file__).parent / "entity_extractor.md"
    system_prompt = Template(template_path.read_text(encoding="utf-8")).render()

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"Target Query:\n{request}\n\nCandidate Raw Dataset:\n{data}"),
    ]

    primary_llm = get_llm(ctx=ctx, name="entity_extractor", is_utils=True).with_structured_output(
        ExtractionResult
    )

    try:
        if use_fallback:
            fallback_llm = get_llm(
                ctx=ctx, name="entity_extractor", is_utils=True, use_fallback=True
            ).with_structured_output(ExtractionResult)
            raw = await with_fallback(
                main_call=lambda: invoke_llm_with_timeout_message(primary_llm.ainvoke(messages)),
                fallback_call=lambda: invoke_llm_with_timeout_message(fallback_llm.ainvoke(messages)),
            )
        else:
            raw = await invoke_llm_with_timeout_message(primary_llm.ainvoke(messages))

        if isinstance(raw, ExtractionResult):
            return raw
        if isinstance(raw, dict):
            return ExtractionResult.model_validate(raw)
        return cast(ExtractionResult, raw)
    except Exception as exc:
        logger.error(f"Entity extraction turn failed: {exc}")
        return ExtractionResult(
            found=False,
            output=None,
            reason=f"LLM extraction error: {str(exc)[:400]}",
        )
