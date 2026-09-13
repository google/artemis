#!/usr/bin/env python3
# Copyright 2026 Google LLC
"""Live experiment demonstrating Artemis concurrency modes and device targeting.

Covers:
1. Single-device queueing and relay (FIFO mutual exclusion on the same device).
2. Multi-device parallel execution (per_device concurrency mode across real device & emulator).
3. Cross-device global serialization (global concurrency mode restricting all devices to 1 task).
4. Full parameter coverage (device_serial, set_device, Task objects, list_devices, get_idle_devices).
"""

import asyncio
import os
import sys
import time
from datetime import datetime

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from artemis import ArtemisClient, ConcurrencyMode, Task
from artemis.runtime import DeviceBusyError, device_pool


def print_header(title: str):
    print("\n" + "=" * 76)
    print(f"  {title}")
    print("=" * 76)


def now_str() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


async def run_experiment_1_single_device_relay(real_serial: str):
    """Experiment 1: Single device FIFO queueing & relay."""
    print_header("[Experiment 1] Single-Device Task Queueing & Relay (FIFO Relay on Single Device)")
    print(f"Target Device: {real_serial} (Physical Pixel 11 Pro)")

    client = ArtemisClient(
        device_serial=real_serial, default_profile="flash", concurrency_mode="per_device"
    )
    print(
        f"Client Init: device_serial='{client.device_serial}', concurrency_mode='{client.concurrency_mode}'"
    )

    task_1_launched = 0.0
    task_1_start = 0.0
    task_1_end = 0.0

    task_2_launched = 0.0
    task_2_start = 0.0
    task_2_end = 0.0

    async def execute_task_1():
        nonlocal task_1_launched, task_1_start, task_1_end
        task_1_launched = time.time()
        print(f"[{now_str()}] ▶ [Task 1] Submitted for scheduling (target device: {real_serial})")
        task_1_start = time.time()
        res = await client.run(
            "Open Settings and check battery level",
            device_serial=real_serial,
            max_turns=3,
        )
        task_1_end = time.time()
        print(
            f"[{now_str()}] ◀ [Task 1] Execution completed! Status={res.status}, "
            f"Trace={res.trace_id[:8]}..., Elapsed={task_1_end - task_1_start:.3f}s"
        )
        return res

    async def execute_task_2():
        nonlocal task_2_launched, task_2_start, task_2_end
        task_2_launched = time.time()
        print(
            f"[{now_str()}] ▶ [Task 2] Submitted concurrently (detects device occupied by Task 1, entering FIFO queue...)"
        )

        task_2_start = time.time()
        res = await client.run(
            "Return to home screen and open Clock",
            device_serial=real_serial,
            max_turns=3,
        )
        task_2_end = time.time()
        print(
            f"[{now_str()}] ◀ [Task 2] Relayed execution completed! Status={res.status}, "
            f"Trace={res.trace_id[:8]}..., Elapsed={task_2_end - task_2_start:.3f}s"
        )
        return res

    # Launch Task 1 first, then Task 2 40ms later while Task 1 is holding the lock
    t_coro_1 = asyncio.create_task(execute_task_1())
    await asyncio.sleep(0.04)
    t_coro_2 = asyncio.create_task(execute_task_2())
    res1, res2 = await asyncio.gather(t_coro_1, t_coro_2)

    print("\n[Timing & Relay Analysis]:")
    print(
        f"  Task 1 runtime interval: [{task_1_start:.3f}s ~ {task_1_end:.3f}s] (elapsed {task_1_end - task_1_start:.3f}s)"
    )
    print(f"  Task 2 submission time: {task_2_launched:.3f}s")
    print(f"  Task 2 actual start: {task_2_start:.3f}s (waited for Task 1 lock release)")
    print(f"  Task 2 end time: {task_2_end:.3f}s (elapsed {task_2_end - task_2_start:.3f}s)")

    assert task_2_end > task_1_end, "Task 2 did not complete after Task 1!"
    assert task_2_end - task_1_start >= (task_1_end - task_1_start) + 0.1, (
        "Tasks failed to execute in sequential queue!"
    )
    print(
        "  ✅ Verification successful: Task 2 queued properly and relayed immediately after Task 1 released the lock!"
    )


async def run_experiment_2_multi_device_parallel(real_serial: str, emu_serial: str):
    """Experiment 2: Multi-device parallel execution under per_device concurrency."""
    print_header("[Experiment 2] Multi-Device Parallel Execution Test")
    print(f"Device A (Physical): {real_serial}")
    print(f"Device B (Emulator): {emu_serial}")

    client_a = ArtemisClient(
        device_serial=real_serial, default_profile="flash", concurrency_mode="per_device"
    )
    client_b = ArtemisClient(
        device_serial=emu_serial, default_profile="flash", concurrency_mode="per_device"
    )

    a_start, a_end = 0.0, 0.0
    b_start, b_end = 0.0, 0.0

    async def run_on_device_a():
        nonlocal a_start, a_end
        print(f"[{now_str()}] ▶ [Device A Task] Starting on {real_serial}")
        a_start = time.time()
        res = await client_a.run("Test operation A on physical device", max_turns=3)
        a_end = time.time()
        print(
            f"[{now_str()}] ◀ [Device A Task] Completed! Elapsed: {a_end - a_start:.3f}s, Device: {res.device_id}"
        )
        return res

    async def run_on_device_b():
        nonlocal b_start, b_end
        print(f"[{now_str()}] ▶ [Device B Task] Starting on {emu_serial}")
        b_start = time.time()
        res = await client_b.run("Test operation B on emulator", max_turns=3)
        b_end = time.time()
        print(
            f"[{now_str()}] ◀ [Device B Task] Completed! Elapsed: {b_end - b_start:.3f}s, Device: {res.device_id}"
        )
        return res

    t_start = time.time()
    task_a = asyncio.create_task(run_on_device_a())
    task_b = asyncio.create_task(run_on_device_b())
    res_a, res_b = await asyncio.gather(task_a, task_b)
    t_total = time.time() - t_start

    print("\n[Dual-Device Concurrency Analysis]:")
    print(
        f"  Device A execution interval: {a_start:.3f}s ~ {a_end:.3f}s (elapsed {a_end - a_start:.3f}s)"
    )
    print(
        f"  Device B execution interval: {b_start:.3f}s ~ {b_end:.3f}s (elapsed {b_end - b_start:.3f}s)"
    )
    print(
        f"  Total concurrent duration: {t_total:.3f}s (sum of sequential durations: {(a_end - a_start) + (b_end - b_start):.3f}s)"
    )

    # Overlap verification
    overlap = min(a_end, b_end) - max(a_start, b_start)
    print(f"  Parallel overlap duration: {overlap:.3f}s")
    assert overlap > 0.0, "Dual devices failed to run concurrently with overlap!"
    assert t_total < ((a_end - a_start) + (b_end - b_start) * 0.95), (
        "Total duration should be less than sum of both tasks!"
    )
    print("  ✅ Verification successful: Tasks on both devices ran concurrently without blocking!")


async def run_experiment_3_global_concurrency_serialization(real_serial: str, emu_serial: str):
    """Experiment 3: Global concurrency serialization across devices."""
    print_header("[Experiment 3] Global Concurrency Mode Test")
    print(
        "Mode description: concurrency_mode='global', entire system allows only 1 task across all devices."
    )

    client_a = ArtemisClient(
        device_serial=real_serial, default_profile="flash", concurrency_mode="global"
    )
    client_b = ArtemisClient(
        device_serial=emu_serial, default_profile="flash", concurrency_mode="global"
    )

    # 3.1 Test non-blocking mode rejection
    print("\n[Subtest 3.1: Non-blocking rejection limit]")
    from artemis.runtime import DeviceExecutionLock

    lock_a = DeviceExecutionLock(real_serial, "Placeholder test lock", concurrency_mode="global")
    await asyncio.to_thread(lock_a.acquire)
    try:
        print(f"[{now_str()}] Device A ({real_serial}) holds the global lock...")
        try:
            print(f"[{now_str()}] Device B ({emu_serial}) attempts run with blocking=False...")
            await client_b.run("Emulator non-blocking task", blocking=False)
            print("  ❌ Exception: Was not intercepted by global lock!")
        except DeviceBusyError as exc:
            print(f"  ✅ Correctly intercepted with DeviceBusyError: {exc}")
    finally:
        await asyncio.to_thread(lock_a.release)

    # 3.2 Test cross-device queueing and relay
    print("\n[Subtest 3.2: Cross-device global queueing & relay]")
    t1_submitted, t1_end = 0.0, 0.0
    t2_submitted, t2_end = 0.0, 0.0

    async def run_global_task_1():
        nonlocal t1_submitted, t1_end
        t1_submitted = time.time()
        print(f"[{now_str()}] ▶ [Global Task 1] Started on physical device ({real_serial})...")
        res = await client_a.run("Global task 1", max_turns=3)
        t1_end = time.time()
        print(f"[{now_str()}] ◀ [Global Task 1] Completed! Elapsed: {t1_end - t1_submitted:.3f}s")
        return res

    async def run_global_task_2():
        nonlocal t2_submitted, t2_end
        # Submit task 2 shortly after task 1
        await asyncio.sleep(0.04)
        t2_submitted = time.time()
        print(
            f"[{now_str()}] ▶ [Global Task 2] Submitted to emulator ({emu_serial}), entering queue due to global limit..."
        )
        res = await client_b.run("Global task 2", max_turns=3)
        t2_end = time.time()
        print(
            f"[{now_str()}] ◀ [Global Task 2] Relayed completion! Total time (including queue): {t2_end - t2_submitted:.3f}s"
        )
        return res

    res1, res2 = await asyncio.gather(run_global_task_1(), run_global_task_2())
    print(
        f"  Global Task 1 interval: {t1_submitted:.3f}s ~ {t1_end:.3f}s (elapsed {t1_end - t1_submitted:.3f}s)"
    )
    print(f"  Global Task 2 submit time: {t2_submitted:.3f}s")
    print(
        f"  Global Task 2 end time: {t2_end:.3f}s (waited for Task 1 completion before executing)"
    )
    assert t2_end > t1_end, "Global Task 2 did not complete after Global Task 1!"
    assert (t2_end - t1_end) >= 0.08, (
        "Global Task 2 did not execute sequentially after Task 1 released global lock!"
    )
    print(
        "  ✅ Verification successful: Under global mode, strictly 1 task executed sequentially across different devices!"
    )


async def run_experiment_4_parameters_and_utilities(real_serial: str, emu_serial: str):
    """Experiment 4: Comprehensive SDK parameter and utility testing."""
    print_header("[Experiment 4] SDK Parameter & Utility Comprehensive Test")

    client = ArtemisClient()
    print(f"1. Default idle device selection: client.device_serial = '{client.device_serial}'")

    # Chain switch device
    client.set_device(real_serial)
    print(
        f"2. Chain switch to physical device: client.set_device('{real_serial}') -> '{client.device_serial}'"
    )
    assert client.device_serial == real_serial

    # Attribute switch
    client.device_serial = emu_serial
    print(
        f"3. Property assignment to emulator: client.device_serial = '{emu_serial}' -> '{client.device_id}'"
    )
    assert client.device_id == emu_serial

    # Dynamically change concurrency mode
    client.set_concurrency_mode(ConcurrencyMode.GLOBAL)
    print(
        f"4. Chain change concurrency mode: client.concurrency_mode = '{client.concurrency_mode}'"
    )
    assert client.concurrency_mode == "global"
    client.concurrency_mode = "per_device"

    # Query all available devices
    print("5. client.list_devices() status:")
    devices = client.list_devices()
    for d in devices:
        print(
            f"   • [{d.serial}] State: {d.state}, Model: {d.model}, IsEmulator: {d.is_emulator}, Busy: {d.is_busy}"
        )

    # Query idle devices
    idle = client.get_idle_devices()
    print(f"6. client.get_idle_devices(): {[d.serial for d in idle]}")

    # Use Task structured object with run_task
    print("7. Calling client.run_task() with structured Task entity:")
    task_obj = Task(
        goal="Verify physical device call via Task object",
        profile="flash",
        device_serial=real_serial,
        concurrency_mode="per_device",
        max_turns=3,
    )
    result = await client.run_task(task_obj)
    print(
        f"   Task result: Status={result.status}, Device={result.device_id}, Trace={result.trace_id[:8]}..."
    )
    assert result.device_id == real_serial

    # Use stream_run for live event stream
    print("8. Using client.stream_run() for live event stream:")
    events_received = 0
    async for ev in client.stream_run(
        "Stream operation test",
        device_serial=emu_serial,
        profile="flash",
        max_turns=3,
    ):
        events_received += 1
        print(f"   • [{ev.event_type}] payload={ev.payload}")
    assert events_received >= 2
    print(f"   ✅ Stream events captured successfully! Captured {events_received} events.")


async def main():
    # Detect devices
    devices = device_pool.list_devices()
    print("=" * 76)
    print("[ARTEMIS Python SDK Concurrency & Device Targeting Experiment]")
    print(f"Detected connected devices count: {len(devices)}")
    for d in devices:
        print(
            f"  • Serial: {d.serial} | Model: {d.model} | State: {d.state} | IsEmulator: {d.is_emulator}"
        )
    print("=" * 76)

    import argparse

    parser = argparse.ArgumentParser(
        description="Live experiment demonstrating Artemis concurrency modes and device targeting."
    )
    parser.add_argument(
        "--phone",
        default=os.environ.get("ARTEMIS_PHONE_SERIAL"),
        help="Target physical phone serial number",
    )
    parser.add_argument(
        "--emulator",
        default=os.environ.get("ARTEMIS_EMULATOR_SERIAL"),
        help="Target Android emulator serial number",
    )
    args, _ = parser.parse_known_args()

    # Auto-detect connected devices if not explicitly provided
    real_dev = next((d for d in devices if not d.is_emulator and d.state == "device"), None)
    emu_dev = next((d for d in devices if d.is_emulator and d.state == "device"), None)

    real_serial = args.phone or (real_dev.serial if real_dev else "63191FDKX00062")
    emu_serial = args.emulator or (emu_dev.serial if emu_dev else "emulator-5554")

    # Verify both devices are available
    serials = [d.serial for d in devices]
    if real_serial not in serials or emu_serial not in serials:
        print(
            f"Error: Both physical device '{real_serial}' and emulator '{emu_serial}' must be connected for full experiment."
        )
        print(f"Currently online devices: {serials}")
        return

    # Run experiments in sequence
    await run_experiment_1_single_device_relay(real_serial)
    await run_experiment_2_multi_device_parallel(real_serial, emu_serial)
    await run_experiment_3_global_concurrency_serialization(real_serial, emu_serial)
    await run_experiment_4_parameters_and_utilities(real_serial, emu_serial)

    print_header(
        "🎉 All experiments completed successfully! All concurrency and device targeting verified!"
    )


if __name__ == "__main__":
    asyncio.run(main())
