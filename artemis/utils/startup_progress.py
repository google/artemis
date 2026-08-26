# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Best-effort startup progress events for UI clients.

Startup happens before the normal DataEngine event bridge exists.  This small
sender lets the worker report those otherwise invisible phases over the same
local IPC protocol without making task startup depend on the UI server.
"""

from __future__ import annotations

import json
import os
import socket
import time
from typing import Any

from artemis.config import read_ipc_port


def publish_startup_progress(stage: str, message: str, session_id: str | None = None, **details: Any) -> None:
    """Publish one startup milestone, ignoring unavailable UI/IPC endpoints."""
    resolved_session_id = session_id or os.getenv("ARTEMIS_SESSION_ID") or os.getenv(
        "ARTEMIS_CLOUD_SESSION_ID"
    )
    ipc_port = read_ipc_port()
    if not resolved_session_id or not ipc_port:
        return

    event_data = {
        "session_id": str(resolved_session_id),
        "stage": stage,
        "message": message,
        "timestamp": time.time(),
        **details,
    }
    encoded = (
        json.dumps(
            {"event_type": "startup_progress", "data": event_data},
            default=str,
        )
        + "\n"
    ).encode("utf-8")

    try:
        with socket.create_connection(("127.0.0.1", ipc_port), timeout=0.25) as sock:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.sendall(encoded)
    except OSError:
        # Progress reporting is observability only and must never delay or fail
        # the automation task itself.
        return
