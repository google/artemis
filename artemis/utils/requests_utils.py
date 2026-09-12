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

from artemis.utils.logger import get_logger
import requests

logger = get_logger(__name__)


def curl_from_request(req: requests.PreparedRequest) -> str:
    """Converts a requests.PreparedRequest object to a valid cURL command string."""
    command = ["curl", f"-X {req.method}"]

    for key, value in req.headers.items():
        command.append(f'-H "{key}: {value}"')

    if req.body:
        body = req.body
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        # Escape single quotes in the body for shell safety
        body = body.replace("'", "'\\''")
        command.append(f"-d '{body}'")

    command.append(f"'{req.url}'")

    return " ".join(command)


def logging_hook(response, *args, **kwargs):
    """Hook to log the request as a cURL command."""
    curl_command = curl_from_request(response.request)
    logger.debug(f"\n--- cURL Command ---\n{curl_command}\n--------------------")


def get_session_with_curl_logging() -> requests.Session:
    """Returns a requests.Session with cURL logging enabled."""
    session = requests.Session()
    session.hooks["response"] = [logging_hook]
    return session
