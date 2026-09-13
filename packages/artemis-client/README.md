# Artemis Client

`artemis-client` is the remote Python SDK that runs on a developer machine. It
communicates with the Artemis daemon on a device host over HTTP and contains no
ADB, device-driver, agent, LLM, or image-processing code.

Runtime dependencies: **zero**. HTTP, JSON, and asynchronous adapters use only
the Python standard library.

## Installation

Install from a local checkout:

```powershell
uv add --editable C:\path\to\artemis\packages\artemis-client
```

Install from the Git repository subdirectory:

```powershell
uv add "artemis-client @ git+https://github.com/google/artemis.git#subdirectory=packages/artemis-client"
```

## Connect to a Device Host

The current Artemis daemon administration API does not include built-in remote
authentication. Do not expose its port directly to the public internet. During
the initial rollout, connect through an SSH tunnel:

```powershell
ssh -L 8000:127.0.0.1:8000 user@artemis-host
```

Then connect the SDK to the local tunnel endpoint:

```python
from artemis_client import ArtemisClient

client = ArtemisClient("http://127.0.0.1:8000")
```

For production, place a reverse proxy in front of the daemon that enforces HTTPS
and bearer-token authentication and exposes only task-related APIs.

## Usage

```python
import asyncio
import os

from artemis_client import ArtemisClient


async def main() -> None:
    client = ArtemisClient(
        os.environ["ARTEMIS_BASE_URL"],
        token=os.environ.get("ARTEMIS_TOKEN"),
    )

    handle = await client.submit(
        "Open the Battery page in Settings and verify the battery percentage",
        profile="flash",
        device_serial="emulator-5554",
    )
    print("queued:", handle.task_id)

    result = await client.wait_for_task(handle.task_id, timeout=600)
    if not result.succeeded:
        raise RuntimeError(result.error or result.status)


asyncio.run(main())
```

You can also submit a task and wait for it in one call:

```python
result = await client.run("Open Android Settings and check the Wi-Fi status")
```

Main methods:

- `health()` quickly checks whether the server and scheduler are alive.
- `readiness()` runs full device and toolchain diagnostics and may take longer.
- `capabilities()` negotiates API features and falls back for older servers.
- `list_devices()` lists devices available on the host.
- `submit()` submits an idempotent task and returns immediately.
- `get_task()` retrieves a task.
- `wait_for_task()` waits for a terminal state.
- `run()` submits a task and waits for completion.
- `stop()` stops one task.

By default, `submit()` generates a UUID on the client and sends it as the current
server's `session_id`. Network retries reuse the same `task_id`, preventing the
server from creating duplicate tasks.

## Compatibility

Version 0.1 supports these current Artemis daemon endpoints:

- `POST /api/run`
- `GET /api/sessions/{session_id}`
- `GET /api/status`
- `POST /api/stop`
- `GET /api/devices`
- `GET /api/system/readiness`

Capability discovery first attempts the future `GET /api/v1/capabilities`
endpoint. If an older server returns 404, the SDK uses its known baseline
capability set.

See [DESIGN.md](DESIGN.md) for the architecture and protocol evolution plan.
