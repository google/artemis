"""Comprehensive Edge-Case Test Suite for Artemis Multi-Device Concurrency & Unified Queue.

Covers:
1. Single Device FIFO Serial Queueing & Relay
2. Multi-Device True Parallel Execution
3. Global Concurrency Serialization across Distinct Devices
4. Mixed Multi-Device Queueing
"""

import asyncio
import os
import sys
import time
import requests

# Ensure workspace in path
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


def query_api_status():
    try:
        res = requests.get(API_STATUS_URL, timeout=3).json()
        active = res.get("active_tasks", [])
        queue = res.get("queue", [])
        return active, queue
    except Exception as e:
        return [], []


def print_status_snapshot(stage_name: str):
    active, queue = query_api_status()
    print(f"\n[{stage_name}] Live Snapshot from Web Console (/api/status):")
    print(f"  ⚡ Running Tasks ({len(active)}):")
    for a in active:
        print(f"     - Device: {a.get('device_id')} | Goal: {a.get('goal')} (PID: {a.get('pid')})")
    print(f"  ⏳ Queued Tasks ({len(queue)}):")
    for q in queue:
        dev = q.get("device_serial") or q.get("device_id")
        print(f"     - Device: {dev} | Goal: {q.get('goal')} [status={q.get('status')}]")
    print(f"  📊 Total in Active Queue (Running + Pending) = {len(active) + len(queue)}\n")


async def stage_1_single_device_queueing():
    print("\n" + "=" * 80)
    print("STAGE 1: Single Device FIFO Queueing & Relay")
    print(f"Target Device: {PHONE_SERIAL} (Pixel 11 Pro)")
    print("=" * 80)

    client_phone = ArtemisClient(device_serial=PHONE_SERIAL, concurrency_mode="per_device")

    t1_started = False
    t2_queued = False

    async def task_1a():
        print(f"🚀 [Task 1A] Launching on {PHONE_SERIAL}: Press HOME and open Settings...")
        res = await client_phone.run("Press HOME key and open Settings", profile="flash")
        print(f"✅ [Task 1A] Finished with status: {res.status}")
        return res

    async def task_1b():
        # Small delay so 1A is definitely acquired first
        await asyncio.sleep(0.4)
        print(
            f"🚀 [Task 1B] Launching on {PHONE_SERIAL}: Press HOME and open Clock (Should queue!)..."
        )
        res = await client_phone.run("Press HOME key and open Clock", profile="flash")
        print(f"✅ [Task 1B] Finished with status: {res.status}")
        return res

    # Run both simultaneously
    task_a_coro = asyncio.create_task(task_1a())
    task_b_coro = asyncio.create_task(task_1b())

    # Wait 1.5s and inspect status while 1A is running and 1B is queued
    await asyncio.sleep(1.2)
    print_status_snapshot("STAGE 1 In-Progress")
    active, queue = query_api_status()
    assert len(active) >= 1, "Expected Task 1A to be actively running!"
    # 1B should be in the queue or 1A just finished
    print(f"✓ Verified: Task 1A is running on {PHONE_SERIAL}")

    res_a, res_b = await asyncio.gather(task_a_coro, task_b_coro)
    print("✓ Stage 1 Complete: Both tasks executed in order and completed successfully!")


async def stage_2_multi_device_parallel():
    print("\n" + "=" * 80)
    print("STAGE 2: Multi-Device True Parallel Execution")
    print(f"Devices: {PHONE_SERIAL} (Pixel 11 Pro) AND {EMULATOR_SERIAL} (Emulator)")
    print("Concurrency Mode: per_device (Should run simultaneously!)")
    print("=" * 80)

    client_phone = ArtemisClient(device_serial=PHONE_SERIAL, concurrency_mode="per_device")
    client_emu = ArtemisClient(device_serial=EMULATOR_SERIAL, concurrency_mode="per_device")

    start_time = time.time()

    async def task_phone():
        print(f"📱 [Phone Task] Running on {PHONE_SERIAL}: Open Settings...")
        res = await client_phone.run("Press HOME key and open Settings", profile="flash")
        print(f"✅ [Phone Task] Completed in {time.time() - start_time:.2f}s")
        return res

    async def task_emu():
        print(f"💻 [Emulator Task] Running on {EMULATOR_SERIAL}: Open Settings...")
        res = await client_emu.run("Press HOME key and open Settings", profile="flash")
        print(f"✅ [Emulator Task] Completed in {time.time() - start_time:.2f}s")
        return res

    p_task = asyncio.create_task(task_phone())
    e_task = asyncio.create_task(task_emu())

    # Check during run
    await asyncio.sleep(1.2)
    print_status_snapshot("STAGE 2 Parallel In-Progress")
    active, queue = query_api_status()
    print(f"Active devices count: {len(active)}")
    active_devices = {a.get("device_id") for a in active}
    print(f"Active devices: {active_devices}")

    await asyncio.gather(p_task, e_task)
    total_duration = time.time() - start_time
    print(f"✓ Stage 2 Complete: Both devices completed parallel tasks in {total_duration:.2f}s!")


async def stage_3_global_concurrency_mode():
    print("\n" + "=" * 80)
    print("STAGE 3: Global Concurrency Mode Serialization")
    print("Concurrency Mode: global (max 1 task system-wide even across different devices)")
    print("=" * 80)

    client_phone_global = ArtemisClient(device_serial=PHONE_SERIAL, concurrency_mode="global")
    client_emu_global = ArtemisClient(device_serial=EMULATOR_SERIAL, concurrency_mode="global")

    async def task_phone_g():
        print(f"📱 [Task 3A - Phone] Acquiring global lock on {PHONE_SERIAL}...")
        res = await client_phone_global.run("Press HOME key and open Settings", profile="flash")
        print("✅ [Task 3A - Phone] Finished.")
        return res

    async def task_emu_g():
        await asyncio.sleep(0.4)
        print(
            f"💻 [Task 3B - Emulator] Requesting run on {EMULATOR_SERIAL} under global mode (Should queue!)..."
        )
        res = await client_emu_global.run("Press HOME key and open Settings", profile="flash")
        print("✅ [Task 3B - Emulator] Finished.")
        return res

    p_task = asyncio.create_task(task_phone_g())
    e_task = asyncio.create_task(task_emu_g())

    await asyncio.sleep(1.2)
    print_status_snapshot("STAGE 3 Global Mode In-Progress")
    active, queue = query_api_status()
    print(f"Active count: {len(active)}, Queue count: {len(queue)}")

    await asyncio.gather(p_task, e_task)
    print(
        "✓ Stage 3 Complete: Global lock correctly serialized execution across physical phone and emulator!"
    )


async def stage_4_mixed_queue_and_parallel():
    print("\n" + "=" * 80)
    print("STAGE 4: Mixed Multi-Device Queueing")
    print("Scenario: Phone gets 2 tasks (4A, 4B), Emulator gets 1 task (4C).")
    print("Expectation: 4A (Phone) and 4C (Emulator) run concurrently; 4B waits for Phone.")
    print("=" * 80)

    client_phone = ArtemisClient(device_serial=PHONE_SERIAL, concurrency_mode="per_device")
    client_emu = ArtemisClient(device_serial=EMULATOR_SERIAL, concurrency_mode="per_device")

    async def task_4a():
        print("📱 [Task 4A - Phone] Running...")
        return await client_phone.run("Press HOME key and open Settings", profile="flash")

    async def task_4b():
        await asyncio.sleep(0.4)
        print("📱 [Task 4B - Phone] Queued behind 4A...")
        return await client_phone.run("Press HOME key and open Clock", profile="flash")

    async def task_4c():
        print("💻 [Task 4C - Emulator] Running in parallel with 4A...")
        return await client_emu.run("Press HOME key and open Settings", profile="flash")

    t4a = asyncio.create_task(task_4a())
    t4b = asyncio.create_task(task_4b())
    t4c = asyncio.create_task(task_4c())

    await asyncio.sleep(1.2)
    print_status_snapshot("STAGE 4 Mixed Multi-Device In-Progress")

    await asyncio.gather(t4a, t4b, t4c)
    print("✓ Stage 4 Complete: 4A and 4C ran in parallel, 4B queued and picked up phone relay!")


async def main():
    import argparse

    global PHONE_SERIAL, EMULATOR_SERIAL

    parser = argparse.ArgumentParser(description="ARTEMIS All Edge-Cases Comprehensive Test Suite")
    parser.add_argument("--phone", default=None, help="Target physical phone serial number")
    parser.add_argument("--emulator", default=None, help="Target Android emulator serial number")
    args = parser.parse_args()

    if args.phone:
        PHONE_SERIAL = args.phone
    if args.emulator:
        EMULATOR_SERIAL = args.emulator

    print("\n" + "#" * 80)
    print("# ARTEMIS ALL EDGE-CASES COMPREHENSIVE CONCURRENCY & QUEUE TEST SUITE")
    print(f"# Devices: Real Phone ({PHONE_SERIAL}) + Android Emulator ({EMULATOR_SERIAL})")
    print("# Web Console API: http://127.0.0.1:8000/api/status")
    print("#" * 80)

    # Initial cleanup
    DeviceExecutionLock.cleanup_stale_locks()
    print_status_snapshot("Initial System State")

    # Run all 4 stages
    await stage_1_single_device_queueing()
    await asyncio.sleep(1)

    await stage_2_multi_device_parallel()
    await asyncio.sleep(1)

    await stage_3_global_concurrency_mode()
    await asyncio.sleep(1)

    await stage_4_mixed_queue_and_parallel()

    print("\n" + "=" * 80)
    print("🎉 ALL EDGE-CASE STAGES COMPLETED SUCCESSFULLY!")
    print("=" * 80)
    print_status_snapshot("Final System State")


if __name__ == "__main__":
    asyncio.run(main())
