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

"""The ``ask_explorer`` tool contract as seen by calling agents.

The contract is deliberately tier-agnostic: the Operator, Validator and Flash
runner receive the same tool name, description and argument schema whichever
Explorer tier the user configured.  Tier behavior lives in
:mod:`artemis.agents.explorer.tiers`.
"""

ASK_EXPLORER_TOOL_NAME = "ask_explorer"

#: Model used when the LLM configuration carries no Explorer entry.
DEFAULT_EXPLORER_MODEL = "gemini-3.8-flash"

ASK_EXPLORER_DESCRIPTION = (
    "[EXPLORER] Ask the UI Explorer to locate elements on the current screen"
    " that are missing from the indexed element list or whose listed"
    " coordinates look wrong. Every element it finds is appended to the"
    " indexed list with a new index and returned with its normalized [x, y]"
    " coordinate, so you can act on it right away."
)

ASK_EXPLORER_QUERY_DESCRIPTION = (
    "What to find, described the way you see it on the screenshot: visible"
    " text, icon shape, color, position, or nearby landmarks (e.g. 'gear icon"
    " top-right', 'blue Send button below the message box'). Several targets"
    " may be listed separated by ' | '."
)

ASK_EXPLORER_CONTEXT_FEEDBACK_DESCRIPTION = (
    "Optional. What went wrong with earlier attempts or extra hints, e.g."
    " which returned candidates were wrong and why."
)

#: Fed to the Explorer's own system prompt for loop engines.
EXECUTION_CONSTRAINT_TEMPLATE = (
    "You are running with a maximum of {max_turns} turns: on your final turn"
    " you will receive a warning and only `submit_answer` will be available."
)
