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
from pathlib import Path
from artemis.agents.planner.planner import run_async_planner_validation


class DummyDataEngine:
    def get_agent_friendly_steps(self):
        return []


class DummyCtx:
    data_engine = DummyDataEngine()
    project_dir = str(Path(__file__).resolve().parent.parent.parent)


async def test():
    content_before = "- [/] Click the button\n- [ ] Complete task"
    content_after = (
        "- [/] Click the button\n- [ ] Complete task\n- [ ] New subgoal appended at bottom"
    )
    res = await run_async_planner_validation(
        ctx=DummyCtx(),
        initial_goal="Do the task",
        content_before=content_before,
        content_after=content_after,
        operator_raw_thinking="I need to add a new subgoal",
        operator_native_thinking="Thinking...",
    )
    print(res)


asyncio.run(test())
