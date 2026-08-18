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

"""Dynamic configuration variable interpolation engine (${env:VAR}, ${path:NAME})."""

import os
from pathlib import Path
import re
from typing import Any

from artemis.platform import platform

_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _resolve_variable(expr: str, workspace_root: Path | None = None) -> str:
    """Resolve a single ${namespace:key:-default} expression."""
    expr = expr.strip()
    namespace, _, rest = expr.partition(":")
    if not rest:
        # Defaults to environment variable lookup
        return os.getenv(namespace, f"${{{expr}}}")

    key, _, default = rest.partition(":-")

    if namespace == "env":
        return os.getenv(key, default)
    elif namespace == "path":
        paths = platform.paths
        if key == "config_dir":
            return str(paths.config_dir)
        elif key == "data_dir":
            return str(paths.data_dir)
        elif key == "cache_dir":
            return str(paths.cache_dir)
        elif key == "logs_dir":
            return str(paths.logs_dir)
        elif key == "temp_dir":
            return str(paths.temp_dir())
        elif key == "workspace_root" or key == "root_dir":
            return str(workspace_root or Path.cwd())
        return default or f"${{{expr}}}"
    elif namespace == "platform":
        if key == "os_type":
            return platform.os_type.value
        elif key == "path_sep":
            return platform.process.path_separator
        return default or f"${{{expr}}}"

    return os.getenv(expr, default or f"${{{expr}}}")


def interpolate_config_value(val: Any, workspace_root: Path | None = None) -> Any:
    """Recursively interpolate strings, lists, and dicts containing ${...} variables."""
    if isinstance(val, str):
        if "${" not in val:
            return val

        def replacer(match: re.Match) -> str:
            return _resolve_variable(match.group(1), workspace_root)

        return _VAR_PATTERN.sub(replacer, val)
    elif isinstance(val, dict):
        return {k: interpolate_config_value(v, workspace_root) for k, v in val.items()}
    elif isinstance(val, list):
        return [interpolate_config_value(item, workspace_root) for item in val]
    return val
