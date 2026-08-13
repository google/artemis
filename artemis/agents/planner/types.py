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

from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import BaseModel


class _CyFunctionDetectorMeta(type):
    def __instancecheck__(self, instance):
        name = type(instance).__name__
        return (
            name
            in (
                "cyfunction",
                "cython_function_or_method",
                "builtin_function_or_method",
            )
            or "cyfunction" in name.lower()
        )


class CyFunctionDetector(metaclass=_CyFunctionDetectorMeta):
    pass


class PlannerSubgoalOutput(BaseModel):
    model_config = {"ignored_types": (CyFunctionDetector,)}
    description: str


class PlannerOutput(BaseModel):
    model_config = {"ignored_types": (CyFunctionDetector,)}
    subgoals: list[PlannerSubgoalOutput]


class SubgoalStatus(Enum):
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


class Subgoal(BaseModel):
    model_config = {"ignored_types": (CyFunctionDetector,)}
    id: Annotated[str, "Unique identifier of the subgoal"]
    description: Annotated[str, "Description of the subgoal"]
    completion_reason: Annotated[
        str | None, "Reason why the subgoal was completed (failure or success)"
    ] = None
    status: SubgoalStatus
    started_at: Annotated[datetime | None, "When the subgoal started"] = None
    ended_at: Annotated[datetime | None, "When the subgoal ended"] = None

    def __str__(self):
        status_emoji = "❓"
        if self.status == SubgoalStatus.SUCCESS:
            status_emoji = "✅"
        elif self.status == SubgoalStatus.FAILURE:
            status_emoji = "❌"
        elif self.status == SubgoalStatus.PENDING:
            status_emoji = "⏳"
        elif self.status == SubgoalStatus.NOT_STARTED:
            status_emoji = "(not started yet)"

        output = f"- [ID:{self.id}]: {self.description} : {status_emoji}."
        if self.completion_reason:
            output += f" Completion reason: {self.completion_reason}"
        return output

    def __repr__(self):
        return str(self)
