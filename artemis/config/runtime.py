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
    IPC_PORT_FILENAME,
)
from artemis.config.paths import (
    ROOT_DIR,
    get_app_dir,
    get_ipc_port_file,
    get_ls_address_file,
    get_temp_dir,
)
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


def read_ipc_port() -> int | None:
    """Read the active ARTEMIS IPC port from environment, state files, or local server API."""
    env_port = os.getenv(ENV_ARTEMIS_IPC_PORT)
    if env_port and env_port.strip().isdigit():
        return int(env_port.strip())

    candidate_paths = [
        get_temp_dir() / "artemis-ipc-port",
        get_app_dir() / IPC_PORT_FILENAME,
        ROOT_DIR / IPC_PORT_FILENAME,
        get_ipc_port_file(),
    ]
    for p in candidate_paths:
        if p.exists():
            try:
                content = p.read_text(encoding="utf-8").strip()
                if content.isdigit():
                    return int(content)
            except Exception:
                pass

    # Dynamic fallback: check local admin console status endpoint
    try:
        import urllib.request
        import json

        req = urllib.request.Request(
            "http://127.0.0.1:8000/api/status",
            headers={"User-Agent": "Artemis-IPC-Discovery"},
        )
        with urllib.request.urlopen(req, timeout=0.2) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                ipc_port = data.get("ipc_port")
                if ipc_port and str(ipc_port).isdigit():
                    return int(ipc_port)
    except Exception:
        pass

    return None


def write_ipc_port(port: int) -> Path:
    """Save active IPC port to environment and temporary synchronization file."""
    os.environ[ENV_ARTEMIS_IPC_PORT] = str(port)
    port_file = get_ipc_port_file()
    all_targets = [
        port_file,
        get_temp_dir() / "artemis-ipc-port",
        get_app_dir() / IPC_PORT_FILENAME,
        ROOT_DIR / IPC_PORT_FILENAME,
    ]
    for target in all_targets:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(port), encoding="utf-8")
        except Exception as e:
            logger.debug(f"Could not write IPC port to {target}: {e}")
    return port_file


def clear_ipc_port() -> None:
    """Remove IPC port synchronization state from the process and filesystem."""
    os.environ.pop(ENV_ARTEMIS_IPC_PORT, None)
    all_targets = [
        get_ipc_port_file(),
        get_temp_dir() / "artemis-ipc-port",
        get_app_dir() / IPC_PORT_FILENAME,
        ROOT_DIR / IPC_PORT_FILENAME,
    ]
    for target in all_targets:
        if target.exists():
            try:
                target.unlink(missing_ok=True)
            except Exception as e:
                logger.debug(f"Failed to remove IPC port file {target}: {e}")


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


def init_ls_address() -> None:
    """Automatically write ANTIGRAVITY_LS_ADDRESS to shared synchronization file on startup.

    Allows decoupled background processes to communicate with Jetski.
    """
    ls_addr = os.environ.get(ENV_ANTIGRAVITY_LS_ADDRESS)
    if ls_addr:
        try:
            write_ls_address(ls_addr)
        except Exception as e:
            logger.debug(f"Failed to write LS address on startup: {e}")
