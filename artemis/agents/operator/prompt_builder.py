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

"""Dynamic prompt template builder for Operator Agent."""

from jinja2 import Template

OPERATOR_SYSTEM_PROMPT = """You are ARTEMIS Operator, an expert autonomous mobile device interaction agent.
Your objective is to observe the current Android mobile screen and take precise actions to achieve the user's goal.

Available action commands:
- click(target=[x, y]): Normalized coordinates 0-1000 scale.
- swipe(action="up"|"down"|"left"|"right"|[x1, y1, x2, y2]): Drag across screen.
- input_text(text="...", target=[x, y]): Click field and type.
- press_key(key="home"|"back"|"enter"|"delete"): Press hardware key.
- wait_for_delay(seconds=...): Pause execution.
"""

OPERATOR_HUMAN_TEMPLATE = """Goal: {{ goal }}
Active Sub-Goal: {{ sub_goal }}
Current Turn: {{ current_turn }}
Execution History:
{{ history }}
"""


class OperatorPromptBuilder:
    """Builds prompt messages and multimodal contents for the Operator Agent."""

    @classmethod
    def build_system_message(cls) -> str:
        return OPERATOR_SYSTEM_PROMPT

    @classmethod
    def build_human_message(
        cls,
        goal: str,
        sub_goal: str = "",
        current_turn: int = 1,
        history: str = "",
    ) -> str:
        template = Template(OPERATOR_HUMAN_TEMPLATE)
        return template.render(
            goal=goal,
            sub_goal=sub_goal,
            current_turn=current_turn,
            history=history,
        )
