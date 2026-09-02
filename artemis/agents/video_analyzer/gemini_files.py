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

"""Gemini File API helpers: upload/poll lifecycle and abandoned-file cleanup."""

import asyncio
from datetime import datetime
from pathlib import Path
import time

from artemis.utils.logger import get_logger

try:
    from datetime import UTC
except ImportError:
    UTC = UTC

logger = get_logger(__name__)

_LAST_CLEANUP_TIME = 0.0
_CLEANUP_LOCK = asyncio.Lock()


async def cleanup_abandoned_gemini_files(client) -> None:
    """Scan and delete remaining cloud video files whose creation time has elapsed standard TTL."""
    global _LAST_CLEANUP_TIME
    if _CLEANUP_LOCK.locked():
        return

    async with _CLEANUP_LOCK:
        now_ts = time.time()
        if now_ts - _LAST_CLEANUP_TIME < 3600:
            return
        _LAST_CLEANUP_TIME = now_ts

    try:
        pager = await asyncio.wait_for(client.aio.files.list(), timeout=30)
        files = []
        async for f in pager:
            files.append(f)
        now = datetime.now(UTC)
        files_to_delete = []

        for f in files:
            display_name = getattr(f, "display_name", "") or ""

            if (
                display_name.startswith("compressed_")
                or display_name.startswith("audio_")
                or "artemis" in display_name
            ):
                create_time = getattr(f, "created_at", None) or getattr(f, "create_time", None)
                if not create_time:
                    continue

                if isinstance(create_time, str):
                    try:
                        parsed_time = datetime.fromisoformat(create_time.replace("Z", "+00:00"))
                    except Exception:
                        continue
                elif isinstance(create_time, datetime):
                    parsed_time = create_time
                else:
                    continue

                if parsed_time.tzinfo is None:
                    parsed_time = parsed_time.replace(tzinfo=UTC)

                age_seconds = (now - parsed_time).total_seconds()

                if age_seconds > 7200:
                    files_to_delete.append(f)
                    logger.info(
                        f"Marked expired cloud asset for deletion: {f.name}"
                        f" ({display_name}), Age: {age_seconds / 3600:.1f}h"
                    )

        if files_to_delete:
            logger.info(f"Purging {len(files_to_delete)} expired cloud assets in parallel...")
            tasks = [
                asyncio.wait_for(client.aio.files.delete(name=f.name), timeout=30)
                for f in files_to_delete
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        logger.warning(f"Routine cloud maintenance skipped: {e}")


async def upload_and_poll_file(
    client, compressed_path: Path, cloud_files_to_cleanup: set
) -> any:
    """Upload a media file to the Gemini File API and poll until it is ACTIVE."""
    logger.info(f"Uploading {compressed_path} to Gemini File API...")
    file_size_mb = compressed_path.stat().st_size / (1024 * 1024)
    upload_timeout = max(30.0, min(120.0, file_size_mb * 2.0))

    file = await asyncio.wait_for(
        client.aio.files.upload(file=compressed_path),
        timeout=upload_timeout,
    )
    cloud_files_to_cleanup.add(file.name)

    max_wait = max(60, min(180, 60 + int(file_size_mb * 2)))

    wait_interval = 0.5
    start_wait = time.time()
    retry_count = 0
    max_retries = 3

    while True:
        try:
            f_state = await asyncio.wait_for(
                client.aio.files.get(name=file.name), timeout=20
            )
            retry_count = 0
        except Exception as poll_error:
            retry_count += 1
            logger.warning(
                f"Temporary issue polling file {file.name} (attempt"
                f" {retry_count}/{max_retries}): {poll_error}"
            )
            if retry_count > max_retries:
                raise RuntimeError(
                    f"Failed to poll Gemini File API after {max_retries} attempts: {poll_error}"
                )
            await asyncio.sleep(2.0)
            continue

        if f_state.state.name == "ACTIVE":
            break
        elif f_state.state.name == "FAILED":
            raise RuntimeError(f"Gemini File API processing failed for {file.name}")

        if time.time() - start_wait > max_wait:
            raise TimeoutError(
                f"Gemini File API processing timeout for {file.name}"
                f" (Waited {max_wait}s for size {file_size_mb:.1f}MB)"
            )

        logger.info(f"File {file.name} is {f_state.state.name}, waiting {wait_interval}s...")
        await asyncio.sleep(wait_interval)
        wait_interval = min(3.0, wait_interval * 1.5)
    return file
