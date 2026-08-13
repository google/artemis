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

"""Structured output schemas, event recording, and output file management."""

import json
import os
from pathlib import Path
from typing import Annotated, Any
import warnings

from pydantic import BaseModel, Field, model_validator

from artemis.config.constants import (
    ENV_EVENTS_OUTPUT_PATH,
    ENV_RESULTS_OUTPUT_PATH,
)
from artemis.config.llm import CyFunctionDetector
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class OutputConfig(BaseModel):
    """Configuration for structured output formatting and schemas."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    structured_output: Annotated[
        type[BaseModel] | dict | None,
        Field(
            default=None,
            description=(
                "Optional structured schema (as a BaseModel or dict) to shape the output. "
                "If provided, it takes precedence over 'output_description'."
            ),
        ),
    ]
    output_description: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional natural language description of the expected output format. "
                "Used only if 'structured_output' is not provided. "
                "Example: 'Output a JSON with 3 keys: color, price, websiteUrl'."
            ),
        ),
    ]
    enable_outputter: Annotated[
        bool | None,
        Field(
            default=None,
            description="Explicitly enable or disable Outputter post-execution synthesis.",
        ),
    ] = None
    force_synthesis: Annotated[
        bool,
        Field(
            default=False,
            description="Whether to run Outputter synthesis even without explicit output_description or schema.",
        ),
    ] = False

    def __str__(self) -> str:
        s_builder = ""
        if self.structured_output:
            s_builder += f"Structured Output: {self.structured_output}\n"
        if self.output_description:
            s_builder += f"Output Description: {self.output_description}\n"
        if self.output_description and self.structured_output:
            s_builder += (
                "Both 'structured_output' and 'output_description' are provided. "
                "'structured_output' will take precedence.\n"
            )
        return s_builder

    @model_validator(mode="after")
    def warn_if_both_outputs_provided(self) -> "OutputConfig":
        if self.structured_output and self.output_description:
            warnings.warn(
                "Both 'structured_output' and 'output_description' are provided. "
                "'structured_output' will take precedence.",
                stacklevel=2,
            )
        return self

    def needs_structured_format(self, default_enabled: bool = True) -> bool:
        """Returns True if Outputter should run to synthesize/structure the output."""
        if self.enable_outputter is False:
            return False
        if self.enable_outputter is True or self.force_synthesis:
            return True
        if not default_enabled:
            return False
        return bool(self.structured_output or self.output_description)


def prepare_output_files() -> tuple[str | None, str | None]:
    """Validate and prepare events and results output files from environment variables."""
    events_output_path = os.getenv(ENV_EVENTS_OUTPUT_PATH)
    results_output_path = os.getenv(ENV_RESULTS_OUTPUT_PATH)

    def validate_and_prepare_file(file_path: str) -> str | None:
        if not file_path:
            return None

        path_obj = Path(file_path)

        if path_obj.exists() and path_obj.is_dir():
            logger.error(f"Error: Path '{file_path}' points to an existing directory, not a file.")
            return None

        if not path_obj.suffix or file_path.endswith(("/", "\\")):
            logger.error(f"Error: Path '{file_path}' appears to be a directory path, not a file.")
            return None

        try:
            path_obj.parent.mkdir(parents=True, exist_ok=True)
            path_obj.touch(exist_ok=True)
            return file_path
        except OSError as e:
            logger.error(f"Error creating file '{file_path}': {e}")
            return None

    validated_events_path = (
        validate_and_prepare_file(events_output_path) if events_output_path else None
    )
    validated_results_path = (
        validate_and_prepare_file(results_output_path) if results_output_path else None
    )

    return validated_events_path, validated_results_path


def record_events(output_path: Path | None, events: list[str] | BaseModel | Any) -> None:
    """Serialize and write execution events to disk."""
    if not output_path:
        return

    if isinstance(events, str):
        events_content = events
    elif isinstance(events, BaseModel):
        events_content = events.model_dump_json(indent=2)
    else:
        events_content = json.dumps(events, indent=2)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(events_content)
