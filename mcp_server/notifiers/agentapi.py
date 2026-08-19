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

    def __init__(self):
        super().__init__()
        try:
            addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
            token = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
            if addr and token:
                self._save_shared_env(addr, token)
        except Exception:
            pass

    @property
    def name(self) -> str:
        return "agentapi"

    def _find_agentapi_path(self) -> str | None:
        """Locates the agentapi executable."""
        try:
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
        except Exception:
            pass
        return None

    def is_available(self) -> bool:
        """Returns True if agentapi binary exists or LS address is detected."""
        try:
            if self._find_agentapi_path():
                return True
            if "ANTIGRAVITY_LS_ADDRESS" in os.environ:
                return True
        except Exception:
            pass
        return False

    def _save_shared_env(self, addr: str, token: str) -> None:
        """Persists recovered environment to shared files for faster subsequent access."""
        shared_candidates = [
            os.path.expanduser("~/.gemini/jetski/.jetski_env"),
            os.path.expanduser("~/.artemis/.artemis_env"),
        ]
        try:
            parent_dir = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            shared_candidates.append(os.path.join(parent_dir, ".jetski_env"))
        except Exception:
            pass

        data = {
            "ANTIGRAVITY_LS_ADDRESS": addr,
            "ANTIGRAVITY_CSRF_TOKEN": token,
        }
        for shared_file in shared_candidates:
            try:
                os.makedirs(os.path.dirname(shared_file), exist_ok=True)
                with open(shared_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                logger.debug(f"[EnvRecovery] Saved session to {shared_file}.")
            except Exception as e:
                logger.debug(f"[EnvRecovery] Error writing shared env file {shared_file}: {e}")

    def _get_candidate_envs(self, force_proc_scan: bool = False) -> list[tuple[str, str]]:
        """Returns unique candidate (address, token) pairs ordered by priority and recency."""
        candidates: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def _add(addr: str | None, token: str | None) -> None:
            if addr and token and (addr, token) not in seen:
                seen.add((addr, token))
                candidates.append((addr, token))

        # 1. Current environment variables
        _add(
            os.environ.get("ANTIGRAVITY_LS_ADDRESS"),
            os.environ.get("ANTIGRAVITY_CSRF_TOKEN"),
        )

        # 2. Shared env files (if not forcing fresh process scan)
        if not force_proc_scan:
            shared_candidates = [
                os.path.expanduser("~/.gemini/jetski/.jetski_env"),
                os.path.expanduser("~/.artemis/.artemis_env"),
            ]
            try:
                parent_dir = os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                )
                shared_candidates.append(os.path.join(parent_dir, ".jetski_env"))
            except Exception:
                pass

            for shared_file in shared_candidates:
                if os.path.exists(shared_file):
                    try:
                        with open(shared_file, encoding="utf-8") as f:
                            env_data = json.load(f)
                        _add(
                            env_data.get("ANTIGRAVITY_LS_ADDRESS"),
                            env_data.get("ANTIGRAVITY_CSRF_TOKEN"),
                        )
                    except Exception as e:
                        logger.debug(
                            f"[EnvRecovery] Error reading shared env file {shared_file}: {e}"
                        )

        # 3. Scan process table for active agent sessions, sorted by newest creation time first
        proc_matches: list[tuple[float, str, str]] = []
        try:
            import psutil

            for proc in psutil.process_iter(["create_time", "environ"]):
                try:
                    env = proc.info.get("environ")
                    if (
                        env
                        and "ANTIGRAVITY_LS_ADDRESS" in env
                        and "ANTIGRAVITY_CSRF_TOKEN" in env
                    ):
                        ctime = proc.info.get("create_time", 0.0) or 0.0
                        proc_matches.append(
                            (
                                ctime,
                                env["ANTIGRAVITY_LS_ADDRESS"],
                                env["ANTIGRAVITY_CSRF_TOKEN"],
                            )
                        )
                except (psutil.AccessDenied, psutil.NoSuchProcess, Exception):
                    continue
        except ImportError:
            import glob

            for env_path in glob.glob("/proc/[0-9]*/environ"):
                try:
                    mtime = os.path.getmtime(env_path)
                    with open(env_path, "rb") as f:
                        content = f.read()
                    env_vars = content.split(b"\x00")
                    local_addr = None
                    local_token = None
                    for var in env_vars:
                        if var.startswith(b"ANTIGRAVITY_LS_ADDRESS="):
                            local_addr = (
                                var.split(b"=", 1)[1].decode("utf-8", errors="ignore")
                            )
                        elif var.startswith(b"ANTIGRAVITY_CSRF_TOKEN="):
                            local_token = (
                                var.split(b"=", 1)[1].decode("utf-8", errors="ignore")
                            )
                    if local_addr and local_token:
                        proc_matches.append((mtime, local_addr, local_token))
                except Exception:
                    pass

        # Sort matches descending by creation timestamp so active/newest sessions come first
        proc_matches.sort(key=lambda x: x[0], reverse=True)
        for _, addr, token in proc_matches:
            _add(addr, token)

        return candidates

    def _load_shared_env(self, force_proc_scan: bool = False) -> None:
        """Recovers and loads the newest candidate ANTIGRAVITY_LS_ADDRESS and ANTIGRAVITY_CSRF_TOKEN."""
        candidates = self._get_candidate_envs(force_proc_scan=force_proc_scan)
        if candidates:
            addr, token = candidates[0]
            os.environ["ANTIGRAVITY_LS_ADDRESS"] = addr
            os.environ["ANTIGRAVITY_CSRF_TOKEN"] = token
            logger.debug(f"[EnvRecovery] Loaded session candidate: {addr}")

    def notify(
        self,
        conversation_id: str,
        message: str,
        title: str | None = None,
        event_type: str = "completed",
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Dispatches a notification via the agentapi send-message command."""
        try:
            if not conversation_id or conversation_id.lower() in ("default", "none", ""):
                return False

            agentapi_path = self._find_agentapi_path()
            if not agentapi_path:
                return False

            cmd = [agentapi_path, "send-message"]
            if title:
                cmd.append(f"--title={title}")
            cmd.extend([conversation_id, message])

            candidates = self._get_candidate_envs(force_proc_scan=False)
            if not candidates:
                candidates = self._get_candidate_envs(force_proc_scan=True)

            for attempt, (addr, token) in enumerate(candidates):
                env = os.environ.copy()
                env["ANTIGRAVITY_LS_ADDRESS"] = addr
                env["ANTIGRAVITY_CSRF_TOKEN"] = token
                try:
                    subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=10,
                        env=env,
                    )
                    os.environ["ANTIGRAVITY_LS_ADDRESS"] = addr
                    os.environ["ANTIGRAVITY_CSRF_TOKEN"] = token
                    self._save_shared_env(addr, token)
                    logger.info(f"AgentAPI notification sent successfully via {addr}.")
                    return True
                except Exception as err:
                    logger.debug(
                        f"Candidate session {addr} failed (attempt {attempt + 1}): {err}"
                    )

            # If all initial candidates failed, force a fresh process scan in case a new session started
            fresh_candidates = self._get_candidate_envs(force_proc_scan=True)
            for addr, token in fresh_candidates:
                if (addr, token) in candidates:
                    continue
                env = os.environ.copy()
                env["ANTIGRAVITY_LS_ADDRESS"] = addr
                env["ANTIGRAVITY_CSRF_TOKEN"] = token
                try:
                    subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=10,
                        env=env,
                    )
                    os.environ["ANTIGRAVITY_LS_ADDRESS"] = addr
                    os.environ["ANTIGRAVITY_CSRF_TOKEN"] = token
                    self._save_shared_env(addr, token)
                    logger.info(
                        f"AgentAPI notification sent successfully on fresh scan via {addr}."
                    )
                    return True
                except Exception as err:
                    logger.debug(f"Fresh candidate session {addr} failed: {err}")

            return False
        except Exception as e:
            logger.debug(f"AgentApiNotifier encountered unhandled error: {e}")
            return False
