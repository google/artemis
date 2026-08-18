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

"""Centralized workspace, configuration, temporary files, and directory management."""

import os
from pathlib import Path
import sys

from artemis.config.constants import (
    DATA_ENGINE_DB_FILENAME,
    ENV_ANTIGRAVITY_APP_DIR,
    ENV_ARTEMIS_APP_DIR,
    ENV_ARTEMIS_TRACES_DIR,
    ENV_ARTEMIS_USE_USER_DIR,
    IPC_PORT_FILENAME,
    LS_ADDRESS_FILENAME,
)
from artemis.platform import platform

# Project root directory (3 levels up from artemis/config/paths.py)
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = ROOT_DIR / "config"


def is_frozen_bundle() -> bool:
    """Check if running inside a PyInstaller / Nuitka standalone compiled binary."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def get_app_dir() -> Path:
    """Returns the central application directory for ARTEMIS configurations, DBs, and traces.

    Order of precedence:
    1. ARTEMIS_APP_DIR or ANTIGRAVITY_APP_DIR environment variable.
    2. Legacy ~/.gemini/jetski or ~/.artemis directory if it exists.
    3. OS-standard data directory via Platform Abstraction Layer (PAL).
    """
    return platform.paths.resolve_app_dir()


def get_default_traces_path() -> Path:
    """Returns default traces directory.

    Uses user app dir when running as a frozen binary or when app dir env is set.
    """
    env_traces = os.getenv(ENV_ARTEMIS_TRACES_DIR)
    if env_traces:
        traces_dir = Path(env_traces)
    elif (
        is_frozen_bundle()
        or os.getenv(ENV_ARTEMIS_USE_USER_DIR) == "true"
        or os.getenv(ENV_ARTEMIS_APP_DIR)
        or os.getenv(ENV_ANTIGRAVITY_APP_DIR)
    ):
        traces_dir = get_app_dir() / "traces"
    else:
        traces_dir = ROOT_DIR / "traces"

    traces_dir.mkdir(parents=True, exist_ok=True)
    return traces_dir


def get_traces_dir() -> Path:
    """Alias for get_default_traces_path."""
    return get_default_traces_path()


def get_temp_dir(subfolder: str | None = None) -> Path:
    """Returns a unified temporary directory for ARTEMIS runtime operations (e.g., screenshots, recordings).

    Args:
        subfolder: Optional subfolder name under the temp directory.

    Returns:
        Path object pointing to the initialized temporary directory.
    """
    return platform.paths.temp_dir(subfolder)


def get_data_engine_db_path() -> Path:
    """Returns the unified SQLite database path for ARTEMIS DataEngine."""
    return get_default_traces_path() / DATA_ENGINE_DB_FILENAME


def get_ipc_port_file() -> Path:
    """Returns the location of the IPC port synchronization file."""
    app_dir_file = get_app_dir() / IPC_PORT_FILENAME
    if app_dir_file.exists():
        return app_dir_file
    return ROOT_DIR / IPC_PORT_FILENAME


def get_ls_address_file() -> Path:
    """Returns the location of the Language Server address synchronization file."""
    app_dir_file = get_app_dir() / LS_ADDRESS_FILENAME
    if app_dir_file.exists():
        return app_dir_file
    return ROOT_DIR / LS_ADDRESS_FILENAME


def get_config_path(filename: str, default_bundled_path: Path | None = None) -> Path:
    """Resolve a configuration file path across env vars, config dir, project dir, app dir, and bundles.

    Resolution Order:
    1. Environment variable override: `ARTEMIS_<FILENAME_UPPER>`
    2. Central `config/` directory: `<ROOT_DIR>/config/<filename>`
    3. Project root directory: `<ROOT_DIR>/<filename>`
    4. Custom default bundled path (if provided and exists)
    5. User application/config directory via PAL: `<CONFIG_DIR>/<filename>` or `<APP_DIR>/<filename>`
    """
    env_var = f"ARTEMIS_{filename.upper().replace('.', '_').replace('-', '_')}"
    env_path_str = os.getenv(env_var)
    if env_path_str:
        env_path = Path(env_path_str)
        if env_path.exists():
            return env_path

    # Check config/ directory
    config_dir_path = CONFIG_DIR / filename
    if config_dir_path.exists():
        return config_dir_path

    # Check project root directory
    project_path = ROOT_DIR / filename
    if project_path.exists():
        return project_path

    # Check bundled fallback
    if default_bundled_path and default_bundled_path.exists():
        return default_bundled_path

    # Check user PAL config directory
    pal_config_path = platform.paths.config_dir / filename
    if pal_config_path.exists():
        return pal_config_path

    # Check user app directory
    app_dir_path = get_app_dir() / filename
    if app_dir_path.exists():
        return app_dir_path

    raise FileNotFoundError(
        f"Configuration file '{filename}' not found in config dir ({config_dir_path}), "
        f"project root ({project_path}), or app dir ({app_dir_path})."
    )


GLOBAL_APP_DIR = get_app_dir()
GLOBAL_JETSKI_DIR = GLOBAL_APP_DIR  # Backward-compatibility alias
