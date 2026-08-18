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

"""AgentAPI Notifier for Jetski / Antigravity environments."""

import json
import logging
import os
import shutil
import subprocess
from typing import Any

from mcp_server.notifiers.base import BaseNotifier

logger = logging.getLogger("mcp_server.notifiers.agentapi")


class AgentApiNotifier(BaseNotifier):
    """Notifier implementation for environments supporting the agentapi CLI (e.g. Jetski / Antigravity)."""

    @property
    def name(self) -> str:
        return "agentapi"

    def _find_agentapi_path(self) -> str | None:
        """Locates the agentapi executable."""
        which_path = shutil.which("agentapi")
        if which_path:
            return which_path

        candidates = [
            os.path.expanduser("~/.gemini/jetski/bin/agentapi"),
            os.path.expanduser("~/.artemis/bin/agentapi"),
            os.path.expanduser("~/bin/agentapi"),
            "/usr/local/bin/agentapi",
        ]
        for cand in candidates:
            if os.path.exists(cand) and os.access(cand, os.X_OK):
                return cand
        return None

    def is_available(self) -> bool:
        """Returns True if agentapi binary exists or LS address is detected."""
        if self._find_agentapi_path():
            return True
        if "ANTIGRAVITY_LS_ADDRESS" in os.environ:
            return True
        return False

    def _load_shared_env(self, force_proc_scan: bool = False) -> None:
        """Recovers and loads ANTIGRAVITY_LS_ADDRESS and ANTIGRAVITY_CSRF_TOKEN."""
        if not force_proc_scan:
            try:
                shared_candidates = [
                    os.path.expanduser("~/.gemini/jetski/.jetski_env"),
                    os.path.expanduser("~/.artemis/.artemis_env"),
                ]
                # Project root fallback
                parent_dir = os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                )
                shared_candidates.append(os.path.join(parent_dir, ".jetski_env"))

                for shared_file in shared_candidates:
                    if os.path.exists(shared_file):
                        with open(shared_file, encoding="utf-8") as f:
                            env_data = json.load(f)
                        for key, val in env_data.items():
                            if val:
                                os.environ[key] = val
                                logger.debug(f"[EnvRecovery] Loaded {key} from {shared_file}.")
                        return
            except Exception as e:
                logger.debug(f"[EnvRecovery] Error reading shared env file: {e}")

        # Fallback to psutil process scan
        logger.debug("[EnvRecovery] Scanning process table for active agent session...")
        found_addr = None
        found_token = None

        try:
            import psutil

            for proc in psutil.process_iter(["environ"]):
                try:
                    env = proc.info.get("environ")
                    if env and "ANTIGRAVITY_LS_ADDRESS" in env and "ANTIGRAVITY_CSRF_TOKEN" in env:
                        found_addr = env["ANTIGRAVITY_LS_ADDRESS"]
                        found_token = env["ANTIGRAVITY_CSRF_TOKEN"]
                        break
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    continue
        except ImportError:
            # Fallback on Linux /proc
            import glob

            for env_path in glob.glob("/proc/[0-9]*/environ"):
                try:
                    with open(env_path, "rb") as f:
                        content = f.read()
                    env_vars = content.split(b"\x00")
                    local_addr = None
                    local_token = None
                    for var in env_vars:
                        if var.startswith(b"ANTIGRAVITY_LS_ADDRESS="):
                            local_addr = var.split(b"=", 1)[1].decode("utf-8", errors="ignore")
                        elif var.startswith(b"ANTIGRAVITY_CSRF_TOKEN="):
                            local_token = var.split(b"=", 1)[1].decode("utf-8", errors="ignore")
                    if local_addr and local_token:
                        found_addr = local_addr
                        found_token = local_token
                        break
                except Exception:
                    pass

        if found_addr and found_token:
            os.environ["ANTIGRAVITY_LS_ADDRESS"] = found_addr
            os.environ["ANTIGRAVITY_CSRF_TOKEN"] = found_token
            logger.debug(f"[EnvRecovery] Recovered session from process. Address: {found_addr}")

    def notify(
        self,
        conversation_id: str,
        message: str,
        title: str | None = None,
        event_type: str = "completed",
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Dispatches a notification via the agentapi send-message command."""
        if not conversation_id or conversation_id.lower() in ("default", "none", ""):
            return False

        agentapi_path = self._find_agentapi_path()
        if not agentapi_path:
            return False

        if "ANTIGRAVITY_LS_ADDRESS" not in os.environ or "ANTIGRAVITY_CSRF_TOKEN" not in os.environ:
            self._load_shared_env()

        cmd = [agentapi_path, "send-message"]
        if title:
            cmd.extend(["--title", title])
        cmd.extend([conversation_id, message])

        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=10)
            logger.info("AgentAPI notification sent successfully.")
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, Exception):
            try:
                self._load_shared_env(force_proc_scan=True)
                if "ANTIGRAVITY_LS_ADDRESS" in os.environ:
                    subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=10)
                    logger.info("AgentAPI notification sent successfully on retry.")
                    return True
            except Exception as retry_err:
                logger.debug(f"AgentAPI notification retry failed: {retry_err}")
                return False
        return False
