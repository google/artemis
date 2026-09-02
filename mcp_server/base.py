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

"""Shared FastMCP instance definition for the mobile automation MCP server."""

from mcp.server.fastmcp import FastMCP

try:
    from mcp.server.fastmcp.server import Settings as FastMCPSettings

    FastMCPSettings.model_rebuild()
except Exception:
    # Version-compat shim: older/newer FastMCP releases may not expose
    # Settings or need the rebuild; the server works without it.
    pass

# Define the shared FastMCP instance for external IDE and agent clients.
mcp = FastMCP(
    "artemis",
    instructions=(
        "ARTEMIS is an autonomous mobile AI agent and Android UI automation engine. "
        "Use mobile_run_task to launch autonomous UI workflows on connected Android devices or emulators, "
        "mobile_manage_task to check status or steer execution, "
        "mobile_get_device_state to inspect real-time device screen/hierarchy, "
        "and mobile_inspect_trace to inspect detailed execution steps and visual action overlays."
    ),
)

# Tool registration is intentionally NOT done here: mcp_server/__init__.py
# imports mcp_server.tools (which imports this module), so importing this
# module from anywhere already registers every tool. Importing tools here as
# well would create a circular import (base -> tools -> base).
