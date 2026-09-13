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

"""Environment and runtime discovery utilities for MCP."""

import os
import subprocess
import sys
from typing import Any


def get_project_root() -> str:
    """Returns the absolute path to the project root repository."""
    # Deliberate lazy import: this helper must keep working when the artemis
    # package (or its config bootstrap) is unavailable or broken, falling back
    # to filesystem-relative resolution below.
    try:
        from artemis.config.paths import ROOT_DIR

        return str(ROOT_DIR)
    except Exception:
        # mcp_server/utils/env_utils.py -> root is 3 levels up
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def resolve_python_executable(project_root: str | None = None) -> str:
    """Resolves the preferred Python executable (.venv or current sys.executable)."""
    root = project_root or get_project_root()

    if sys.platform == "win32":
        venv_python = os.path.join(root, ".venv", "Scripts", "python.exe")
    else:
        venv_python = os.path.join(root, ".venv", "bin", "python")

    if os.path.exists(venv_python):
        return venv_python

    return sys.executable


def get_detached_process_kwargs() -> dict[str, Any]:
    """Returns kwargs for subprocess.Popen to run a fully detached background process cross-platform."""
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        )
    else:
        kwargs["start_new_session"] = True
    return kwargs
