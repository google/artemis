# Copyright 2025-2026 Minitap, Inc.
# Modifications Copyright 2026 Google LLC
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
#
# Originally from mobile-use (https://github.com/minitap-ai/mobile-use).
# See third_party/mobile_use/METADATA for the upstream source and local modifications.

"""State reducer and convergence node used when building the agent graph."""

from collections.abc import Callable, Hashable
from typing import Any

from langgraph.graph import StateGraph

CONVERGENCE_NODE = "convergence"


def take_last(a, b):
    """Reducer function keeping the latest value."""
    return b


def convergence_node(state: Any):
    """Convergence point for parallel execution paths."""
    return {}


def add_convergence_node(graph_builder: StateGraph) -> None:
    """Add the deferred node where parallel branches join."""
    graph_builder.add_node(node=CONVERGENCE_NODE, action=convergence_node, defer=True)


def add_convergence_edges(
    graph_builder: StateGraph,
    gate: Callable[..., Hashable],
    path_map: dict[Hashable, str],
) -> None:
    """Add the conditional edges leaving the convergence node, chosen by ``gate``."""
    graph_builder.add_conditional_edges(
        source=CONVERGENCE_NODE,
        path=gate,
        path_map=path_map,
    )
