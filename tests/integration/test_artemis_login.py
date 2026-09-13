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

import os
import select
import sys
import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.manual]

if sys.platform == "win32":
    pytest.skip("PTY-based SSH login test requires a POSIX host", allow_module_level=True)

import pty


@pytest.mark.skip(reason="Manual integration test requiring remote host")
def test_artemis_ssh_login():
    """Manual test to verify SSH login to remote Artemis instance."""
    run_artemis_ssh_login()


def run_artemis_ssh_login():
    host = os.getenv("ARTEMIS_SSH_TEST_HOST", "127.0.0.1")
    user = os.getenv("ARTEMIS_SSH_TEST_USER", "artemis")
    password = os.getenv("ARTEMIS_SSH_TEST_PASSWORD", "password")

    cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "PreferredAuthentications=password,keyboard-interactive",
        f"{user}@{host}",
        (
            "echo '=================================================='; echo '🚀 BINGO!! PURE"
            " PASSWORD LOGIN VICTORY 🚀'; echo"
            " '=================================================='; whoami; hostname; uptime"
        ),
    ]

    master, slave = pty.openpty()
    pid = os.fork()
    if pid == 0:
        os.close(master)
        os.execvp(cmd[0], cmd)
    else:
        os.close(slave)
        output = b""
        start = time.time()
        password_sent = False
        while time.time() - start < 15:
            r, _, _ = select.select([master], [], [], 0.3)
            if master in r:
                try:
                    data = os.read(master, 2048)
                    if not data:
                        break
                    output += data
                    sys.stdout.buffer.write(data)
                    sys.stdout.flush()
                    if (b"Password:" in data or b"password:" in data) and not password_sent:
                        time.sleep(0.2)
                        os.write(master, f"{password}\n".encode())
                        password_sent = True
                except OSError:
                    break
        os.close(master)
        os.waitpid(pid, 0)


if __name__ == "__main__":
    run_artemis_ssh_login()
