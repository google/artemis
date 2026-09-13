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

from datetime import datetime, timedelta
import time
from artemis.context import ArtemisContext
from artemis.controllers.platform_specific_commands_controller import get_adb_device
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


def parse_log_time(time_str: str) -> datetime:
    """Parses a log timestamp string into a datetime object."""
    now = datetime.now()
    current_year = now.year

    def _try_parse(year: int) -> datetime | None:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(f"{year}-{time_str}", fmt)
            except ValueError:
                pass
        return None

    dt = _try_parse(current_year)
    if dt is None:
        raise ValueError(f"Unsupported time format: {time_str}")

    # If parsed time is in the future (allowing 1 hour grace for clock drift),
    # we assume it belongs to the previous year.
    if dt > now + timedelta(hours=1):
        prev_dt = _try_parse(current_year - 1)
        if prev_dt is not None:
            dt = prev_dt

    return dt


def resolve_time(
    time_input: str | float | None, session_start: float | None
) -> tuple[str | None, datetime | None]:
    """Resolves relative or absolute time input into formatted string and datetime."""
    if time_input is None:
        return None, None

    if isinstance(time_input, (int, float)) or (
        isinstance(time_input, str) and time_input.replace(".", "", 1).isdigit()
    ):
        # It's relative seconds
        if session_start is None:
            raise ValueError("Cannot resolve relative time without session start time.")
        rel_sec = float(time_input)
        abs_ts = session_start + rel_sec

        t_struct = time.localtime(abs_ts)
        t_str = time.strftime("%m-%d %H:%M:%S", t_struct)
        ms = int((abs_ts - int(abs_ts)) * 1000)
        formatted_str = f"{t_str}.{ms:03d}"

        dt = datetime.fromtimestamp(abs_ts)
        return formatted_str, dt

    # Assume it's absolute time string "MM-DD HH:MM:SS.ms"
    try:
        dt = parse_log_time(str(time_input))
    except ValueError as e:
        raise ValueError(f"Unsupported time format: {time_input}") from e
    return str(time_input), dt


# pylint: disable=too-many-locals
def fetch_and_filter_logs(
    ctx: ArtemisContext,
    lines: int = 1000,
    since_time: str | None = None,
    until_time: str | None = None,
) -> str:
    """Fetches logs from ADB device and filters them by time if needed."""
    device = get_adb_device(ctx)

    session_start = ctx.data_engine.session_start_time if ctx.data_engine else None

    since_str, since_dt = resolve_time(since_time, session_start)
    _, until_dt = resolve_time(until_time, session_start)

    if since_str:
        try:
            logger.info(f"Attempting to fetch logs since {since_str}...")
            logs = device.shell(f'logcat -v threadtime -t "{since_str}"')
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning(
                f"Failed to fetch logs since time directly: {e}. Falling back"
                " to reading lines and filtering."
            )
            logs = device.shell(f"logcat -v threadtime -t {lines}")
    else:
        logger.info(f"Reading last {lines} lines of logs...")
        logs = device.shell(f"logcat -v threadtime -t {lines}")

    if since_dt or until_dt:
        filtered_lines = []
        for line in logs.splitlines():
            if len(line) >= 18:
                log_time_str = line[:18]
                try:
                    log_dt = parse_log_time(log_time_str)

                    if since_dt and log_dt < since_dt:
                        continue
                    if until_dt and log_dt > until_dt:
                        continue

                    filtered_lines.append(line)
                except ValueError:
                    pass
        # Cap to the requested line limit so timestamp fetches don't overflow context
        if 0 < lines < len(filtered_lines):
            filtered_lines = filtered_lines[-lines:]
        logs = "\n".join(filtered_lines)
    else:
        # Ensure fallback capping even when no datetime parsing occurred
        log_lines = logs.splitlines()
        if 0 < lines < len(log_lines):
            logs = "\n".join(log_lines[-lines:])

    return logs
