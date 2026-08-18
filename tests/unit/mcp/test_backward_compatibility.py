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

"""Unit tests for backward compatibility layer with legacy 'jetski' imports."""

import importlib.util
import pytest

if not importlib.util.find_spec("jetski"):
    pytest.skip("Legacy 'jetski' package not installed", allow_module_level=True)


def test_legacy_jetski_mcp_imports():
    from jetski.mcp.base import mcp as jetski_mcp
    from mcp_server.base import mcp as mcp_server_mcp

    assert jetski_mcp is mcp_server_mcp


def test_legacy_jetski_tools_imports():
    from jetski.mcp.tools.task_runner import mobile_run_task as j_run
    from mcp_server.tools.task_runner import mobile_run_task as a_run

    assert j_run is a_run

    from jetski.mcp.tools.task_manager import mobile_manage_task as j_manage
    from mcp_server.tools.task_manager import mobile_manage_task as a_manage

    assert j_manage is a_manage

    from jetski.mcp.tools.device_state import mobile_get_device_state as j_dev
    from mcp_server.tools.device_state import mobile_get_device_state as a_dev

    assert j_dev is a_dev

    from jetski.mcp.tools.inspect_trace import mobile_inspect_trace as j_insp
    from mcp_server.tools.inspect_trace import mobile_inspect_trace as a_insp

    assert j_insp is a_insp


def test_legacy_jetski_utils_imports():
    from jetski.utils.trace_store import init_trace
    from mcp_server.utils.trace_store import init_trace as a_init

    assert init_trace is a_init

    from jetski.utils.notifier import notify, notify_jetski
    from mcp_server.notifiers import notify as a_notify

    assert notify is a_notify
    assert callable(notify_jetski)
