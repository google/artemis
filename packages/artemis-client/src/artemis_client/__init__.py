# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Public API for the lightweight Artemis remote client."""

from artemis_client.client import ArtemisClient
from artemis_client.errors import (
    ApiError,
    ArtemisClientError,
    AuthenticationError,
    ConflictError,
    NetworkError,
    NotFoundError,
    ProtocolError,
    TaskRejectedError,
    TaskTimeoutError,
)
from artemis_client.models import Capabilities, Device, TaskHandle, TaskResult
from artemis_client.transport import JsonTransport

__version__ = "0.1.0"

__all__ = [
    "ApiError",
    "ArtemisClient",
    "ArtemisClientError",
    "AuthenticationError",
    "Capabilities",
    "ConflictError",
    "Device",
    "JsonTransport",
    "NetworkError",
    "NotFoundError",
    "ProtocolError",
    "TaskHandle",
    "TaskRejectedError",
    "TaskResult",
    "TaskTimeoutError",
    "__version__",
]
