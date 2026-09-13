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

"""Unit tests for the unified PID liveness probe."""

import os
from unittest.mock import MagicMock, patch

import psutil

from artemis.runtime.process_probe import pid_is_alive


def test_own_pid_is_alive():
    assert pid_is_alive(os.getpid()) is True


def test_own_pid_with_matching_create_time_is_alive():
    created_at = psutil.Process(os.getpid()).create_time()
    assert pid_is_alive(os.getpid(), created_at) is True


def test_recycled_pid_with_mismatched_create_time_is_dead():
    assert pid_is_alive(os.getpid(), 1.0) is False


def test_invalid_pids_are_dead():
    assert pid_is_alive(0) is False
    assert pid_is_alive(-5) is False
    assert pid_is_alive(None) is False  # type: ignore[arg-type]
    assert pid_is_alive("not-a-pid") is False  # type: ignore[arg-type]


def test_no_such_process_is_dead():
    with patch("psutil.Process", side_effect=psutil.NoSuchProcess(424242)):
        assert pid_is_alive(424242) is False


def test_zombie_process_is_dead():
    proc = MagicMock()
    proc.is_running.return_value = True
    proc.status.return_value = psutil.STATUS_ZOMBIE
    with patch("psutil.Process", return_value=proc):
        assert pid_is_alive(1234) is False


def test_access_denied_defaults_to_alive():
    with patch("psutil.Process", side_effect=psutil.AccessDenied(1234)):
        assert pid_is_alive(1234) is True


def test_unexpected_error_defaults_to_alive():
    with patch("psutil.Process", side_effect=RuntimeError("boom")):
        assert pid_is_alive(1234) is True
