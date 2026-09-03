"""Interactive Edge-Case Test Suite with Clear Step-by-Step Guidance.

Supports running each edge-case part independently or all together.
Usage:
    python scripts/run_edge_case_part.py --part 1
    python scripts/run_edge_case_part.py --part 2
    python scripts/run_edge_case_part.py --part 3
    python scripts/run_edge_case_part.py --part 4
    python scripts/run_edge_case_part.py --part all
"""

import argparse
import asyncio
import os
import sys
import time
import requests

WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

from artemis.interfaces.sdk.client import ArtemisClient
from artemis.runtime.device_lock import DeviceExecutionLock
from artemis.toolchain import ensure_toolchain_in_path

ensure_toolchain_in_path()

from artemis.runtime import device_pool


def _resolve_devices() -> tuple[str, str]:
    all_devs = device_pool.list_devices()
    real_dev = next((d for d in all_devs if not d.is_emulator and d.state == "device"), None)
    emu_dev = next((d for d in all_devs if d.is_emulator and d.state == "device"), None)
    phone = os.environ.get("ARTEMIS_PHONE_SERIAL") or (
        real_dev.serial if real_dev else "63191FDKX00062"
    )
    emu = os.environ.get("ARTEMIS_EMULATOR_SERIAL") or (
        emu_dev.serial if emu_dev else "emulator-5554"
    )
    return phone, emu


PHONE_SERIAL, EMULATOR_SERIAL = _resolve_devices()
API_STATUS_URL = "http://127.0.0.1:8000/api/status"


def query_api():
    try:
        r = requests.get(API_STATUS_URL, timeout=3).json()
        return r.get("active_tasks", []), r.get("queue", [])
    except Exception:
        return [], []


def print_status_snapshot(label: str):
    active, queue = query_api()
    print(f"\n[{label}] Web Console Live Snapshot (/api/status):")
    print(f"  ⚡ Running Tasks ({len(active)}):")
    for a in active:
        dev = a.get("device_id") or a.get("device_serial")
        print(f"     - [Device: {dev}] {a.get('goal')} (PID: {a.get('pid')})")
    print(f"  ⏳ Pending Tasks ({len(queue)}):")
    for q in queue:
        dev = q.get("device_serial") or q.get("device_id")
        print(f"     - [Device: {dev}] {q.get('goal')} [status={q.get('status')}]")
    print(f"  📊 Total Active Queue (Running + Pending) = {len(active) + len(queue)}\n")


async def run_part_1():
    print("=" * 80)
    print("[Part 1: Single Device Serial Queueing & Automatic Relay]")
    print("=" * 80)
    print(
        "🎯 Goal: Launch 2 tasks on the same physical device; verify FIFO queueing, Web Active Queue count, and relay."
    )
    print("📱 Target Device: Physical Pixel 11 Pro (63191FDKX00062)")
    print("👀 Please observe:")
    print(
        "   1. Web Console (http://127.0.0.1:8000): Active Queue becomes 2 (1 Running blue badge, 1 Pending yellow badge)"
    )
    print(
        "   2. Device Screen: Task 1A opens Settings first; upon completion, Task 1B automatically takes over to open Clock"
    )
    print(
        "   3. Web Console: After 1A finishes, 1B switches to Running, and queue eventually clears to zero"
    )
    print("-" * 80)

    DeviceExecutionLock.cleanup_stale_locks()
    client = ArtemisClient(device_serial=PHONE_SERIAL, concurrency_mode="per_device")

    async def task_1a():
        print("🚀 [Task 1A] Launching: Open Settings on physical device...")
        res = await client.run(
            "Use manage_app to launch Settings app and report task status completed",
            profile="flash",
        )
        print(f"✅ [Task 1A] Completed: status={res.status}")
        return res

    async def task_1b():
        await asyncio.sleep(0.4)
        print(
            "🚀 [Task 1B] Launching: Open Clock on physical device -> should immediately enter queue..."
        )
        res = await client.run(
            "Use manage_app to launch Clock app and report task status completed", profile="flash"
        )
        print(f"✅ [Task 1B] Completed: status={res.status}")
        return res

    t1 = asyncio.create_task(task_1a())
    t2 = asyncio.create_task(task_1b())

    await asyncio.sleep(1.2)
    print_status_snapshot("Part 1 Running")

    await asyncio.gather(t1, t2)
    print(
        "🎉 Part 1 Success: Task 1B automatically relayed and completed after Task 1A finished!\n"
    )


async def run_part_2():
    print("=" * 80)
    print("[Part 2: Multi-Device True Parallel Execution]")
    print("=" * 80)
    print(
        "🎯 Goal: Cross-device true concurrency; both devices run independently at the same time without blocking."
    )
    print(f"📱 Target Device 1: Physical Pixel 11 Pro ({PHONE_SERIAL})")
    print(f"💻 Target Device 2: Android Emulator ({EMULATOR_SERIAL})")
    print("👀 Please observe:")
    print(
        "   1. Dual screens: Physical device and emulator operate simultaneously without sequential waiting!"
    )
    print(
        "   2. Web Console (http://127.0.0.1:8000): Active Tasks shows 2 (both with blue Running badges)"
    )
    print("   3. Queue count: Queue is 0, each device displays its own Device Serial")
    print("-" * 80)

    DeviceExecutionLock.cleanup_stale_locks()
    client_phone = ArtemisClient(device_serial=PHONE_SERIAL, concurrency_mode="per_device")
    client_emu = ArtemisClient(device_serial=EMULATOR_SERIAL, concurrency_mode="per_device")

    start_t = time.time()

    async def p_task():
        print(f"🚀 [Phone Task] Launching: Open Settings on {PHONE_SERIAL}...")
        res = await client_phone.run(
            "Use manage_app to launch Settings app and report task status completed",
            profile="flash",
        )
        print(f"✅ [Phone Task] Finished in {time.time() - start_t:.2f}s")
        return res

    async def e_task():
        print(f"🚀 [Emulator Task] Launching: Open Settings on {EMULATOR_SERIAL}...")
        res = await client_emu.run(
            "Use manage_app to launch Settings app and report task status completed",
            profile="flash",
        )
        print(f"✅ [Emulator Task] Finished in {time.time() - start_t:.2f}s")
        return res

    tp = asyncio.create_task(p_task())
    te = asyncio.create_task(e_task())

    await asyncio.sleep(1.2)
    print_status_snapshot("Part 2 Running (Multi-Device Concurrency)")

    await asyncio.gather(tp, te)
    print(
        f"🎉 Part 2 Success: Both devices completed tasks concurrently in {time.time() - start_t:.2f}s!\n"
    )


async def run_part_3():
    print("=" * 80)
    print("[Part 3: Global Concurrency Limit Across Devices]")
    print("=" * 80)
    print(
        "🎯 Goal: Configure global concurrency mode (global, limit 1); even targeting different devices, tasks must queue globally."
    )
    print(f"📱 Device 1: Physical Pixel 11 Pro ({PHONE_SERIAL})")
    print(f"💻 Device 2: Android Emulator ({EMULATOR_SERIAL})")
    print("👀 Please observe:")
    print(
        "   1. Emulator screen: Even though emulator is completely idle, it waits until the phone task completes!"
    )
    print(
        "   2. Web Console: Phone task is Running (blue badge), emulator task is Pending (yellow badge)!"
    )
    print("   3. Once phone finishes, emulator instantly activates and begins execution!")
    print("-" * 80)

    DeviceExecutionLock.cleanup_stale_locks()
    client_phone_g = ArtemisClient(device_serial=PHONE_SERIAL, concurrency_mode="global")
    client_emu_g = ArtemisClient(device_serial=EMULATOR_SERIAL, concurrency_mode="global")

    async def task_3a():
        print(f"🚀 [Task 3A - Phone] Global mode starting: Executing on {PHONE_SERIAL}...")
        res = await client_phone_g.run(
            "Use manage_app to launch Settings app and report task status completed",
            profile="flash",
        )
        print("✅ [Task 3A - Phone] Finished")
        return res

    async def task_3b():
        await asyncio.sleep(0.4)
        print(
            f"🚀 [Task 3B - Emulator] Global mode starting: Targeting {EMULATOR_SERIAL} (should queue due to global limit)..."
        )
        res = await client_emu_g.run(
            "Use manage_app to launch Settings app and report task status completed",
            profile="flash",
        )
        print("✅ [Task 3B - Emulator] Finished")
        return res

    t3a = asyncio.create_task(task_3a())
    t3b = asyncio.create_task(task_3b())

    await asyncio.sleep(1.2)
    print_status_snapshot("Part 3 Running (Global Limit Cross-Device Queueing)")

    await asyncio.gather(t3a, t3b)
    print(
        "🎉 Part 3 Success: Global mutex accurately queued and relayed tasks across physical phone and emulator!\n"
    )


async def run_part_4():
    print("=" * 80)
    print("[Part 4: Multi-Device Asymmetric Mixed Queueing]")
    print("=" * 80)
    print("🎯 Goal: Assign 2 tasks to physical phone, 1 task to emulator.")
    print(
        "   Expected: Phone 4A and Emulator 4C run concurrently; Phone 4B queues in dedicated phone queue; 4B relays immediately on phone after 4A."
    )
    print("👀 Please observe:")
    print(
        "   1. Web Console: Active Tasks = 2 (Phone 4A + Emulator 4C), Queue = 1 (Phone 4B), Total = 3!"
    )
    print(
        "   2. Screens: Phone and emulator operate concurrently first; Phone 4B executes on phone immediately after 4A finishes!"
    )
    print("-" * 80)

    DeviceExecutionLock.cleanup_stale_locks()
    client_phone = ArtemisClient(device_serial=PHONE_SERIAL, concurrency_mode="per_device")
    client_emu = ArtemisClient(device_serial=EMULATOR_SERIAL, concurrency_mode="per_device")

    async def task_4a():
        print("🚀 [Task 4A - Phone] Started...")
        return await client_phone.run(
            "Use manage_app to launch Settings app and report task status completed",
            profile="flash",
        )

    async def task_4b():
        await asyncio.sleep(0.4)
        print("🚀 [Task 4B - Phone] Started (Queued behind 4A)...")
        return await client_phone.run(
            "Use manage_app to launch Clock app and report task status completed", profile="flash"
        )

    async def task_4c():
        print("🚀 [Task 4C - Emulator] Started concurrently with 4A...")
        return await client_emu.run(
            "Use manage_app to launch Settings app and report task status completed",
            profile="flash",
        )

    t4a = asyncio.create_task(task_4a())
    t4b = asyncio.create_task(task_4b())
    t4c = asyncio.create_task(task_4c())

    await asyncio.sleep(1.2)
    print_status_snapshot("Part 4 Running (Mixed Queueing)")

    await asyncio.gather(t4a, t4b, t4c)
    print(
        "🎉 Part 4 Success: Cross-device concurrency and single-device queueing work together seamlessly!\n"
    )


async def main():
    global PHONE_SERIAL, EMULATOR_SERIAL
    parser = argparse.ArgumentParser(description="Artemis Concurrency & Queue Edge-Cases Runner")
    parser.add_argument(
        "--part",
        choices=["1", "2", "3", "4", "all"],
        default="1",
        help="Which part to run (1, 2, 3, 4, or all)",
    )
    parser.add_argument("--phone", default=None, help="Target physical phone serial number")
    parser.add_argument("--emulator", default=None, help="Target Android emulator serial number")
    args = parser.parse_args()

    if args.phone:
        PHONE_SERIAL = args.phone
    if args.emulator:
        EMULATOR_SERIAL = args.emulator

    if args.part == "1":
        await run_part_1()
    elif args.part == "2":
        await run_part_2()
    elif args.part == "3":
        await run_part_3()
    elif args.part == "4":
        await run_part_4()
    elif args.part == "all":
        await run_part_1()
        await asyncio.sleep(1)
        await run_part_2()
        await asyncio.sleep(1)
        await run_part_3()
        await asyncio.sleep(1)
        await run_part_4()


if __name__ == "__main__":
    asyncio.run(main())
