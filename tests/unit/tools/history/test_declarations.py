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

"""One args schema, two contracts: the Flash ``ToolDeclaration`` and the
LangChain tool of every history tool are derived from the same Pydantic model."""

from types import SimpleNamespace

import pytest

from artemis.core.tool_declaration import ToolDeclaration
from artemis.tools.history import (
    GET_STEP_SCREENSHOT_TOOL,
    HISTORY_TOOL_DECLARATIONS,
    HISTORY_TOOL_NAMES,
    HISTORY_TOOL_WRAPPERS,
    HISTORY_TOOLS,
    REPLAY_STEPS_TOOL,
    SEARCH_HISTORY_TOOL,
    get_history_tools,
    history_tool_by_name,
    history_tool_declarations,
)


@pytest.mark.parametrize("tool", HISTORY_TOOLS, ids=lambda t: t.name)
def test_tool_declaration_mirrors_the_args_schema(tool):
    declaration = tool.to_tool_declaration()
    assert isinstance(declaration, ToolDeclaration)
    assert declaration.name == tool.name
    assert declaration.description == tool.description

    properties = declaration.parameters["properties"]
    assert list(properties) == list(tool.args_schema.model_fields)
    for field_name, info in tool.args_schema.model_fields.items():
        assert properties[field_name]["description"] == info.description
    assert declaration.parameters["required"] == [
        name for name, info in tool.args_schema.model_fields.items() if info.is_required()
    ]

    # The LangChain export binds the identical schema.
    lc_tool = tool.to_langchain_tool(SimpleNamespace(data_engine=None))
    assert lc_tool.name == tool.name
    assert lc_tool.args_schema is tool.args_schema

    # And the GenAI export agrees on names and required fields.
    genai = tool.to_genai_declaration()
    assert set(genai.parameters.properties) == set(properties)
    assert list(genai.parameters.required or []) == declaration.parameters["required"]


def test_declared_types_and_enums():
    search = SEARCH_HISTORY_TOOL.parameters["properties"]
    assert search["query"]["type"] == "string"
    assert search["step_range"] == {
        "type": "array",
        "items": {"type": "integer"},
        "description": search["step_range"]["description"],
    }
    assert search["max_results"]["type"] == "integer"
    assert search["max_results"]["default"] == 5
    assert SEARCH_HISTORY_TOOL.parameters["required"] == []

    replay = REPLAY_STEPS_TOOL.parameters["properties"]
    assert replay["start_step"]["type"] == "integer"
    assert replay["end_step"]["type"] == "integer"
    assert REPLAY_STEPS_TOOL.parameters["required"] == ["start_step"]

    shot = GET_STEP_SCREENSHOT_TOOL.parameters["properties"]
    assert shot["step_number"]["type"] == "integer"
    assert shot["which"]["type"] == "string"
    assert shot["which"]["enum"] == ["pre", "post", "overlay"]
    assert shot["which"]["default"] == "pre"
    assert GET_STEP_SCREENSHOT_TOOL.parameters["required"] == ["step_number"]


def test_exports_are_consistent():
    assert HISTORY_TOOL_NAMES == {"search_history", "replay_steps", "get_step_screenshot"}
    assert [d.name for d in HISTORY_TOOL_DECLARATIONS] == [t.name for t in HISTORY_TOOLS]
    assert len(HISTORY_TOOL_WRAPPERS) == 3
    assert history_tool_by_name("default_api:replay_steps").name == "replay_steps"
    assert history_tool_by_name("nope") is None

    ctx = SimpleNamespace(data_engine=object())
    expected = [t.name for t in HISTORY_TOOLS]
    assert [t.name for t in get_history_tools(ctx)] == expected
    assert [d.name for d in history_tool_declarations(ctx)] == expected
    assert history_tool_declarations(SimpleNamespace(data_engine=None)) == []
