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

"""Runtime state coordination, temporary file lifecycles, and IPC synchronization."""

import os
from pathlib import Path
import time

from artemis.config.constants import (
    ENV_ANTIGRAVITY_LS_ADDRESS,
    ENV_ARTEMIS_IPC_PORT,
)
from artemis.config.paths import (
    get_ipc_port_file,
    get_ls_address_file,
    get_temp_dir,
)
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


def read_ipc_port() -> int | None:
    """Read the active ARTEMIS IPC port from environment or temporary state file."""
    env_port = os.getenv(ENV_ARTEMIS_IPC_PORT)
    if env_port and env_port.strip().isdigit():
        return int(env_port.strip())

    port_file = get_ipc_port_file()
    if port_file.exists():
        try:
            content = port_file.read_text(encoding="utf-8").strip()
            if content.isdigit():
                return int(content)
        except Exception as e:
            logger.warning(f"Failed to read IPC port from {port_file}: {e}")

    return None


def write_ipc_port(port: int) -> Path:
    """Save active IPC port to environment and temporary synchronization file."""
    os.environ[ENV_ARTEMIS_IPC_PORT] = str(port)
    port_file = get_ipc_port_file()
    try:
        port_file.parent.mkdir(parents=True, exist_ok=True)
        port_file.write_text(str(port), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to write IPC port file to {port_file}: {e}")
    return port_file


def clear_ipc_port() -> None:
    """Remove IPC port synchronization state file."""
    port_file = get_ipc_port_file()
    if port_file.exists():
        try:
            port_file.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Failed to remove IPC port file {port_file}: {e}")


def read_ls_address() -> str | None:
    """Read the active Language Server address from environment or state file."""
    env_addr = os.getenv(ENV_ANTIGRAVITY_LS_ADDRESS)
    if env_addr and env_addr.strip():
        return env_addr.strip()

    addr_file = get_ls_address_file()
    if addr_file.exists():
        try:
            content = addr_file.read_text(encoding="utf-8").strip()
            if content:
                return content
        except Exception as e:
            logger.warning(f"Failed to read LS address from {addr_file}: {e}")

    return None


def write_ls_address(address: str) -> Path:
    """Save Language Server address to environment and temporary synchronization file."""
    clean_addr = address.strip()
    os.environ[ENV_ANTIGRAVITY_LS_ADDRESS] = clean_addr
    addr_file = get_ls_address_file()
    try:
        addr_file.parent.mkdir(parents=True, exist_ok=True)
        addr_file.write_text(clean_addr, encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to write LS address file to {addr_file}: {e}")
    return addr_file


def clear_ls_address() -> None:
    """Remove Language Server address synchronization state file."""
    addr_file = get_ls_address_file()
    if addr_file.exists():
        try:
            addr_file.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Failed to remove LS address file {addr_file}: {e}")


def cleanup_temp_dir(subfolder: str | None = None, max_age_seconds: float | None = None) -> int:
    """Clean up expired temporary runtime files in the central temp directory.

    Args:
        subfolder: Optional subfolder within the temp directory.
        max_age_seconds: Maximum age in seconds before a file is deleted. If None, all files are purged.

    Returns:
        Number of files successfully deleted.
    """
    temp_dir = get_temp_dir(subfolder)
    if not temp_dir.exists():
        return 0

    now = time.time()
    deleted_count = 0

    for file_path in temp_dir.glob("*"):
        if file_path.is_file():
            try:
                if max_age_seconds is None or (now - file_path.stat().st_mtime) > max_age_seconds:
                    file_path.unlink(missing_ok=True)
                    deleted_count += 1
            except Exception as e:
                logger.warning(f"Could not delete temporary file {file_path}: {e}")

    return deleted_count
