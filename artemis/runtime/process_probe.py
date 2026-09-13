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

"""Single source of truth for PID liveness probing across the Artemis codebase.

Semantics (decided by the maintainers):

- psutil only. No bare ``os.kill(pid, 0)`` fallback: on Windows, CPython
  implements ``os.kill(pid, 0)`` for non-CTRL signals via
  ``OpenProcess``/``TerminateProcess`` handle probing, which is both unreliable
  for dead pids and semantically surprising.
- ``psutil.NoSuchProcess`` -> dead. Zombie processes -> dead.
- ``psutil.AccessDenied`` / ``PermissionError`` -> **alive**. When liveness
  cannot be determined the probe defaults to "alive": it is always safer to
  skip reaping a resource than to reap one that is still owned.
- ``created_at`` (a ``psutil``-style process create time) enables PID-reuse
  protection: when provided and the probed process's create time differs by
  one second or more, the original process is dead and its PID was recycled.
- Any other unexpected error -> alive (default-alive), with a warning log.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def pid_is_alive(pid: int, created_at: float | None = None) -> bool:
    """Return whether the process identified by ``pid`` is still alive.

    Args:
        pid: The process id to probe. Non-positive or unparsable pids are dead.
        created_at: Optional expected process create time (as reported by
            ``psutil.Process.create_time()``). When given and > 0, a create-time
            mismatch of >= 1 second means the PID was recycled -> dead.

    Returns:
        True when the process is alive or liveness cannot be determined
        (default-alive); False when the process is definitively gone, a
        zombie, or its PID has been recycled by another process.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    try:
        import psutil
    except Exception as exc:  # pragma: no cover - psutil is a hard dependency
        logger.warning(f"psutil unavailable while probing pid {pid}: {exc}; assuming alive")
        return True

    try:
        process = psutil.Process(pid)
        if not process.is_running():
            return False

        try:
            if process.status() == psutil.STATUS_ZOMBIE:
                return False
        except psutil.NoSuchProcess:
            return False
        except (psutil.AccessDenied, PermissionError):
            # Cannot inspect the status; keep the default-alive verdict.
            pass

        if created_at is not None and created_at > 0:
            try:
                if abs(process.create_time() - float(created_at)) >= 1.0:
                    # Same PID, different birth time: the PID was recycled.
                    return False
            except psutil.NoSuchProcess:
                return False
            except (psutil.AccessDenied, PermissionError):
                pass

        return True
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, PermissionError):
        return True
    except Exception as exc:
        logger.warning(f"Could not determine liveness of pid {pid}: {exc}; assuming alive")
        return True
