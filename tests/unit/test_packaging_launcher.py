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

"""The bundle entry point dispatches three ways before any Artemis import."""

import importlib.util
from pathlib import Path
import sys
import types

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRY = REPO_ROOT / "packaging" / "artemis_cli.py"


@pytest.fixture()
def launcher(monkeypatch):
    spec = importlib.util.spec_from_file_location("packaging_artemis_cli", ENTRY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", ["artemis-cli"])
    return module


def _install(monkeypatch, name: str, **attributes) -> types.ModuleType:
    module = types.ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def test_kernel_mode_launches_ipykernel_with_the_remaining_arguments(monkeypatch, launcher):
    launched: list[bool] = []
    kernelapp = _install(monkeypatch, "ipykernel.kernelapp")
    kernelapp.IPKernelApp = types.SimpleNamespace(launch_instance=lambda: launched.append(True))
    monkeypatch.setitem(sys.modules, "ipykernel", types.ModuleType("ipykernel"))
    monkeypatch.setattr(sys, "argv", ["artemis-cli", "--kernel", "-f", "connection.json"])

    launcher.main()

    assert launched == [True]
    assert sys.argv == ["artemis-cli", "-f", "connection.json"]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--daemon", "--port", "8123"], ("127.0.0.1", 8123)),
        (["--daemon", "--host", "0.0.0.0", "--port", "9001"], ("0.0.0.0", 9001)),
    ],
)
def test_daemon_mode_binds_the_host_and_port_it_is_given(monkeypatch, launcher, argv, expected):
    from apps.admin_console import server as admin_server

    monkeypatch.delenv("ARTEMIS_SERVER_HOST", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_SIDECAR_WEB_PORT", raising=False)
    bound: list[tuple[str, int]] = []
    monkeypatch.setattr(
        admin_server,
        "run_ui_server",
        lambda host, port: bound.append((host, port)),
    )
    monkeypatch.setattr(sys, "argv", ["artemis-cli", *argv])

    launcher.main()

    assert bound == [expected]


def test_everything_else_reaches_the_cli(monkeypatch, launcher):
    calls: list[bool] = []
    _install(monkeypatch, "artemis.interfaces.cli.main", cli=lambda: calls.append(True))
    for name in ("artemis", "artemis.interfaces", "artemis.interfaces.cli"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setattr(sys, "argv", ["artemis-cli", "doctor"])

    launcher.main()

    assert calls == [True]


def test_an_empty_argv_also_reaches_the_cli(monkeypatch, launcher):
    calls: list[bool] = []
    _install(monkeypatch, "artemis.interfaces.cli.main", cli=lambda: calls.append(True))
    for name in ("artemis", "artemis.interfaces", "artemis.interfaces.cli"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setattr(sys, "argv", [])

    launcher.main()

    assert calls == [True]
