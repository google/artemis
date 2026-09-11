"""Lifecycle of the Accessibility Helper: provision, attach, reattach, detach.

Every adb call goes through a scripted fake so the tests assert the exact
command sequence the manager issues, and the HTTP ping is a stub keyed by host
port so "the service answers" is a switch the test flips.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess

import pytest

from artemis.runtime import helper_manager as hm
from artemis.runtime.helper_manager import (
    DEVICE_PORT,
    SERVICE_NAME,
    AccessibilityHelperManager,
    BundledHelper,
    HelperUnavailable,
)

SERIAL = "pixel-1"
OTHER = "pixel-2"


class FakeAdb:
    """Scripted adb: records commands and answers from a small device model."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.installed: dict[str, int | None] = {SERIAL: None, OTHER: None}
        self.enabled_services: dict[str, str] = {SERIAL: "", OTHER: ""}
        self.transport: dict[str, str] = {SERIAL: "3", OTHER: "4"}
        self.forwards: list[tuple[str, int, int]] = []  # (serial, local, remote)
        self.next_port = 40000
        self.install_result = "Success"
        # Number of times the system "prunes" a freshly written service setting.
        self.prune_writes = 0
        # Serials whose dead service was re-bound by a settings toggle.
        self.revived: set[str] = set()
        # HTTP protocol the installed helper speaks (the bundled build speaks 2).
        self.protocol: dict[str, int] = {SERIAL: 2, OTHER: 2}
        # Last token delivered by `am broadcast`, per serial.
        self.tokens: dict[str, str] = {}
        # Process table lines (PID ARGS) of UiAutomation holders, per serial.
        self.holders: dict[str, list[str]] = {SERIAL: [], OTHER: []}
        self.killed: list[tuple[str, int]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        out, code = "", 0
        if args[:2] == ["devices", "-l"]:
            lines = ["List of devices attached"]
            for serial, tid in self.transport.items():
                lines.append(f"{serial}  device product:p model:m device:d transport_id:{tid}")
            out = "\n".join(lines) + "\n"
        elif args[:2] == ["forward", "--list"]:
            out = "".join(f"{s} tcp:{local} tcp:{r}\n" for s, local, r in self.forwards)
        elif args[0] == "-s":
            serial, rest = args[1], args[2:]
            if rest[:3] == ["shell", "dumpsys", "package"]:
                version = self.installed.get(serial)
                out = (
                    f"Package [{rest[3]}]\n    versionCode={version} minSdk=24\n"
                    if version is not None
                    else f"Unable to find package: {rest[3]}\n"
                )
            elif rest[:4] == ["shell", "settings", "get", "secure"]:
                key = rest[4]
                out = (
                    (self.enabled_services.get(serial) or "null") + "\n"
                    if key == "enabled_accessibility_services"
                    else "1\n"
                )
            elif rest[:4] == ["shell", "settings", "put", "secure"]:
                if rest[4] == "enabled_accessibility_services":
                    if self.prune_writes > 0:
                        self.prune_writes -= 1
                        self.enabled_services[serial] = ""
                    else:
                        before = self.enabled_services.get(serial, "")
                        self.enabled_services[serial] = rest[5]
                        # AccessibilityManager binds the service when it is (re)added.
                        if SERVICE_NAME in rest[5] and SERVICE_NAME not in before:
                            self.revived.add(serial)
            elif rest[0] == "install":
                if self.install_result == "Success":
                    self.installed[serial] = self.bundled_version
                    self.protocol[serial] = 2
                    out = "Success\n"
                else:
                    out, code = self.install_result, 1
            elif rest[0] == "uninstall":
                self.installed[serial] = None
                out = "Success\n"
            elif rest[:2] == ["forward", "--no-rebind"]:
                port = self.next_port
                self.next_port += 1
                self.forwards.append((serial, port, int(rest[3].split(":")[1])))
                out = f"{port}\n"
            elif rest[:2] == ["forward", "--remove"]:
                port = int(rest[2].split(":")[1])
                self.forwards = [f for f in self.forwards if not (f[0] == serial and f[1] == port)]
            elif rest[:2] == ["shell", "input"]:
                pass
            elif rest[:3] == ["shell", "am", "broadcast"]:
                assert rest[3:8] == ["-n", hm.TOKEN_RECEIVER, "-a", hm.TOKEN_ACTION, "--es"]
                self.tokens[serial] = rest[9]
                out = "Broadcasting: Intent { act=com.artemis.helper.SET_TOKEN }\nBroadcast completed: result=0\n"
            elif rest[:3] == ["shell", "am", "start"]:
                self.settings_opened = getattr(self, "settings_opened", 0) + 1
            elif rest[:2] == ["shell", "ps"]:
                out = "PID ARGS" + "".join(chr(10) + line for line in self.holders.get(serial, []))
            elif rest[:2] == ["shell", "kill"]:
                pid = int(rest[2])
                self.killed.append((serial, pid))
                self.holders[serial] = [
                    line for line in self.holders.get(serial, []) if not line.startswith(f"{pid} ")
                ]
            elif rest[:3] == ["shell", "am", "force-stop"]:
                pass
            else:
                raise AssertionError(f"unexpected adb call: {args}")
        else:
            raise AssertionError(f"unexpected adb call: {args}")
        return subprocess.CompletedProcess(args, code, out, "")

    bundled_version = 2


class FakePing:
    """Answers on host ports whose forward targets a device running the service."""

    def __init__(self, adb: FakeAdb):
        self.adb = adb
        self.dead: set[str] = set()  # serials whose service is down
        self.count = 0

    def __call__(self, port: int):
        self.count += 1
        for serial, local, remote in self.adb.forwards:
            if local == port and remote == DEVICE_PORT:
                if serial in self.dead and serial not in self.adb.revived:
                    return None
                if any(
                    not line.split(None, 1)[1].startswith("sh ")
                    for line in self.adb.holders.get(serial, [])
                ):
                    return None  # suppressed by UiAutomation
                if self.adb.installed.get(serial) is None:
                    return None
                if SERVICE_NAME not in (self.adb.enabled_services.get(serial) or ""):
                    return None
                return {
                    "success": True,
                    "version_code": self.adb.installed[serial],
                    "version_name": "1.1.0",
                    "protocol_version": self.adb.protocol.get(serial, 2),
                    "auth_required": self.adb.protocol.get(serial, 2) >= 2,
                    "token_set": serial in self.adb.tokens,
                }
        return None


@pytest.fixture
def bundled(tmp_path: Path) -> BundledHelper:
    apk = tmp_path / "helper.apk"
    apk.write_bytes(b"apk")
    return BundledHelper(apk_path=apk, version_code=2, version_name="1.1.0", sha256="x")


@pytest.fixture
def env(tmp_path: Path, monkeypatch, bundled):
    monkeypatch.setattr(hm, "get_temp_dir", lambda _name: tmp_path / "mutex")
    adb = FakeAdb()
    ping = FakePing(adb)
    auto = {"install": True}
    manager = AccessibilityHelperManager(
        run_adb=adb,
        ping=ping,
        bundled=bundled,
        sleep=lambda _s: None,
        token_path=tmp_path / "token" / "session.token",
        auto_install=lambda: auto["install"],
    )
    manager.auto_install_switch = auto  # test handle
    return adb, ping, manager


def _calls(adb: FakeAdb, prefix: list[str]) -> list[list[str]]:
    return [c for c in adb.calls if c[: len(prefix)] == prefix]


def test_status_and_session_repr_do_not_expose_token(env):
    _, _, manager = env
    session = manager.attach(SERIAL)
    status = manager.status(SERIAL)
    assert session.token
    assert "token" not in status["session"]
    assert session.token not in str(status)
    assert session.token not in repr(session)


def test_concurrent_managers_share_the_first_host_token(tmp_path, monkeypatch):
    monkeypatch.setattr(hm, "get_temp_dir", lambda _name: tmp_path / "mutex")
    token_path = tmp_path / "token" / "session.token"
    managers = [AccessibilityHelperManager(token_path=token_path) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        tokens = list(executor.map(lambda manager: manager.host_token(), managers))
    assert len(set(tokens)) == 1
    assert token_path.read_text() == tokens[0]


# --------------------------------------------------------------------------- #
# Provision
# --------------------------------------------------------------------------- #


def test_provision_installs_and_enables_when_missing(env):
    adb, _, manager = env
    result = manager.provision(SERIAL)

    assert result.ok and result.action == "installed"
    assert result.installed_version == 2 and result.bundled_version == 2
    install = _calls(adb, ["-s", SERIAL, "install"])
    assert install == [["-s", SERIAL, "install", "-r", "-g", str(manager.bundled.apk_path)]]
    assert adb.enabled_services[SERIAL] == SERVICE_NAME


def test_provision_upgrades_outdated_and_leaves_newer_alone(env):
    adb, _, manager = env
    adb.installed[SERIAL] = 1
    assert manager.provision(SERIAL).action == "upgraded"
    assert adb.installed[SERIAL] == 2

    adb.installed[SERIAL] = 5
    adb.calls.clear()
    result = manager.provision(SERIAL)
    assert result.action == "up_to_date" and result.installed_version == 5
    assert not _calls(adb, ["-s", SERIAL, "install"])


def test_provision_up_to_date_is_cheap_and_keeps_other_services(env):
    adb, _, manager = env
    adb.installed[SERIAL] = 2
    adb.enabled_services[SERIAL] = "com.android.talkback/.TalkBackService"
    result = manager.provision(SERIAL)

    assert result.ok and result.action == "up_to_date"
    assert adb.enabled_services[SERIAL] == f"com.android.talkback/.TalkBackService:{SERVICE_NAME}"
    assert not _calls(adb, ["-s", SERIAL, "install"])


def test_provision_without_bundled_apk_fails_only_when_not_installed(env):
    adb, ping, _ = env
    manager = AccessibilityHelperManager(
        run_adb=adb, ping=ping, bundled=None, sleep=lambda _s: None
    )
    result = manager.provision(SERIAL)
    assert not result.ok and result.action == "failed" and "no bundled APK" in result.error

    adb.installed[SERIAL] = 1
    result = manager.provision(SERIAL)
    assert result.ok and result.action == "up_to_date"


def test_provision_reports_install_failure(env):
    adb, _, manager = env
    adb.install_result = "Failure [INSTALL_FAILED_VERSION_DOWNGRADE]"
    result = manager.provision(SERIAL)
    assert not result.ok and result.action == "failed"
    assert "INSTALL_FAILED_VERSION_DOWNGRADE" in result.error


def test_provision_mutex_is_released_and_stale_lock_is_broken(env, tmp_path):
    adb, _, manager = env
    manager.provision(SERIAL)
    mutex_dir = tmp_path / "mutex"
    assert not list(mutex_dir.glob("*.mutex"))

    # A crashed process left its mutex behind long ago: provisioning proceeds.
    import hashlib
    import os
    import time

    digest = hashlib.sha256(SERIAL.encode()).hexdigest()[:16]
    stale = mutex_dir / f"{digest}.mutex"
    stale.write_text("1")
    old = time.time() - 1000
    os.utime(stale, (old, old))
    assert manager.provision(SERIAL).ok
    assert not stale.exists()


# --------------------------------------------------------------------------- #
# Attach / detach
# --------------------------------------------------------------------------- #


def test_attach_provisions_forwards_and_records_transport(env):
    adb, _, manager = env
    session = manager.attach(SERIAL)

    assert session.local_port == 40000 and session.owns_forward
    assert session.transport_id == "3" and session.version_code == 2
    assert adb.forwards == [(SERIAL, 40000, DEVICE_PORT)]
    forward_calls = _calls(adb, ["-s", SERIAL, "forward"])
    assert forward_calls == [
        ["-s", SERIAL, "forward", "--no-rebind", "tcp:0", f"tcp:{DEVICE_PORT}"]
    ]


def test_two_devices_get_distinct_host_ports(env):
    adb, _, manager = env
    first = manager.attach(SERIAL)
    second = manager.attach(OTHER)

    assert first.local_port != second.local_port
    assert {(s, local) for s, local, _ in adb.forwards} == {(SERIAL, 40000), (OTHER, 40001)}
    assert manager.session(SERIAL) is first and manager.session(OTHER) is second


def test_attach_reuses_forward_created_by_another_process(env):
    adb, _, manager = env
    adb.installed[SERIAL] = 2
    adb.enabled_services[SERIAL] = SERVICE_NAME
    adb.forwards.append((SERIAL, 39999, DEVICE_PORT))

    session = manager.attach(SERIAL)
    assert session.local_port == 39999 and not session.owns_forward
    assert not _calls(adb, ["-s", SERIAL, "forward", "--no-rebind"])

    manager.detach(SERIAL)
    assert adb.forwards == [(SERIAL, 39999, DEVICE_PORT)]  # not ours to remove


def test_attach_is_idempotent_while_session_is_healthy(env):
    adb, ping, manager = env
    first = manager.attach(SERIAL)
    adb.calls.clear()
    again = manager.attach(SERIAL)
    assert again is first
    assert not _calls(adb, ["-s", SERIAL, "install"])
    assert not _calls(adb, ["-s", SERIAL, "forward"])


def test_lazy_attach_never_installs(env):
    adb, _, manager = env
    with pytest.raises(HelperUnavailable):
        manager.attach(SERIAL, provision=False)
    assert not _calls(adb, ["-s", SERIAL, "install"])
    assert not _calls(adb, ["-s", SERIAL, "shell", "settings", "put"])
    # The dead forward it created for the probe is cleaned up again.
    assert adb.forwards == []
    assert manager.session(SERIAL) is None


def test_lazy_attach_connects_to_running_helper(env):
    adb, _, manager = env
    adb.installed[SERIAL] = 2
    adb.enabled_services[SERIAL] = SERVICE_NAME
    session = manager.attach(SERIAL, provision=False)
    assert session.local_port == 40000
    assert not _calls(adb, ["-s", SERIAL, "install"])


def test_replug_changes_transport_and_rebuilds_tunnel(env):
    adb, _, manager = env
    first = manager.attach(SERIAL)

    # Unplug: adb drops the forward and assigns a new transport on replug.
    adb.forwards.clear()
    adb.transport[SERIAL] = "9"
    adb.calls.clear()

    second = manager.attach(SERIAL)
    assert second is not first
    assert second.transport_id == "9" and second.local_port == 40001
    # The vanished forward is not "removed" (adb already dropped it).
    assert not _calls(adb, ["-s", SERIAL, "forward", "--remove"])
    assert adb.forwards == [(SERIAL, 40001, DEVICE_PORT)]


def test_reattach_after_request_failure_rebuilds_without_install(env):
    adb, ping, manager = env
    first = manager.attach(SERIAL)
    adb.calls.clear()

    session = manager.reattach(SERIAL)
    assert session.local_port != first.local_port
    assert _calls(adb, ["-s", SERIAL, "forward", "--remove"]) == [
        ["-s", SERIAL, "forward", "--remove", f"tcp:{first.local_port}"]
    ]
    assert not _calls(adb, ["-s", SERIAL, "install"])


def test_reattach_revives_a_killed_service_without_installing(env):
    adb, ping, manager = env
    first = manager.attach(SERIAL)
    ping.dead.add(SERIAL)  # force-stopped: enabled in settings, but not running
    adb.revived.discard(SERIAL)
    adb.calls.clear()

    session = manager.reattach(SERIAL)
    assert session.local_port != first.local_port
    assert not _calls(adb, ["-s", SERIAL, "install"])
    puts = _calls(
        adb, ["-s", SERIAL, "shell", "settings", "put", "secure", "enabled_accessibility_services"]
    )
    assert [p[-1] for p in puts] == ["null", SERVICE_NAME]  # removed, then re-added


def test_reattach_raises_when_revive_does_not_help(env):
    adb, ping, manager = env
    manager.attach(SERIAL)
    adb.calls.clear()
    ping.dead.add(SERIAL)
    adb.revived.discard(SERIAL)
    adb.installed[SERIAL] = None  # package vanished: nothing to re-bind
    with pytest.raises(HelperUnavailable):
        manager.reattach(SERIAL)
    assert manager.session(SERIAL) is None
    assert not _calls(adb, ["-s", SERIAL, "install"])


def test_lazy_first_attach_does_not_toggle_settings(env):
    adb, ping, manager = env
    adb.installed[SERIAL] = 2
    adb.enabled_services[SERIAL] = SERVICE_NAME
    ping.dead.add(SERIAL)
    adb.revived.discard(SERIAL)
    with pytest.raises(HelperUnavailable):
        manager.attach(SERIAL, provision=False)
    assert not _calls(adb, ["-s", SERIAL, "shell", "settings", "put"])


def test_detach_removes_only_owned_forward(env):
    adb, _, manager = env
    manager.attach(SERIAL)
    manager.attach(OTHER)
    manager.detach(SERIAL)
    assert adb.forwards == [(OTHER, 40001, DEVICE_PORT)]
    assert manager.session(SERIAL) is None and manager.session(OTHER) is not None


def test_status_is_read_only(env):
    adb, _, manager = env
    adb.installed[SERIAL] = 1
    status = manager.status(SERIAL)
    assert status["installed"] and status["installed_version"] == 1
    assert status["bundled_version"] == 2 and status["outdated"] is True
    assert status["enabled"] is False and status["reachable"] is False
    assert status["transport_id"] == "3"
    assert not _calls(adb, ["-s", SERIAL, "install"])
    assert not _calls(adb, ["-s", SERIAL, "forward"])


def test_uninstall_disables_service_and_removes_package(env):
    adb, _, manager = env
    manager.attach(SERIAL)
    assert manager.uninstall(SERIAL)
    assert adb.installed[SERIAL] is None
    assert SERVICE_NAME not in adb.enabled_services[SERIAL]
    assert adb.forwards == []


def test_transport_id_parsing_handles_missing_device():
    adb = FakeAdb()
    manager = AccessibilityHelperManager(run_adb=adb, ping=lambda _p: None, bundled=None)
    assert manager.transport_id("nope") is None
    assert manager.transport_id(SERIAL) == "3"


def test_enable_retries_when_the_system_prunes_the_setting(env):
    adb, _, manager = env
    adb.prune_writes = 2  # AccessibilityManager drops the first two writes
    result = manager.provision(SERIAL)
    assert result.ok and result.enabled
    puts = _calls(
        adb, ["-s", SERIAL, "shell", "settings", "put", "secure", "enabled_accessibility_services"]
    )
    assert len(puts) == 3
    assert adb.enabled_services[SERIAL] == SERVICE_NAME


def test_enable_gives_up_after_bounded_attempts(env):
    adb, _, manager = env
    adb.prune_writes = 99
    result = manager.provision(SERIAL)
    assert not result.ok and result.action == "installed"
    # The person gets the exact manual path and the settings screen is opened for them.
    assert hm.MANUAL_ENABLE_PATH in result.error
    assert adb.settings_opened == 1
    puts = _calls(
        adb, ["-s", SERIAL, "shell", "settings", "put", "secure", "enabled_accessibility_services"]
    )
    assert len(puts) == hm._ENABLE_ATTEMPTS


def test_provision_emits_install_and_upgrade_events_before_the_install(env):
    adb, _, manager = env
    events: list[tuple[str, dict]] = []

    def on_event(name, details):
        # Fired before adb install ran: nothing installed yet at this point.
        events.append((name, details, adb.installed[SERIAL]))

    manager.provision(SERIAL, on_event=on_event)
    assert [(e[0], e[2]) for e in events] == [("installing", None)]
    assert events[0][1]["to_version"] == 2 and events[0][1]["serial"] == SERIAL

    events.clear()
    adb.installed[SERIAL] = 1
    manager.provision(SERIAL, on_event=on_event)
    assert [(e[0], e[1]["from_version"], e[2]) for e in events] == [("upgrading", 1, 1)]

    events.clear()
    manager.provision(SERIAL, on_event=on_event)  # up to date: silent
    assert events == []


def test_attach_forwards_events_to_provision(env):
    adb, _, manager = env
    seen = []
    manager.attach(SERIAL, on_event=lambda name, details: seen.append(name))
    assert seen == ["installing"]


def test_status_probes_with_a_temporary_forward_when_no_tunnel_exists(env):
    adb, _, manager = env
    adb.installed[SERIAL] = 2
    adb.enabled_services[SERIAL] = SERVICE_NAME
    status = manager.status(SERIAL)
    assert status["reachable"] is True and status["tunnel"] == "probe"
    assert status["forward_port"] is None
    assert adb.forwards == []  # the probe forward was removed again
    assert _calls(adb, ["-s", SERIAL, "forward", "--remove"]) == [
        ["-s", SERIAL, "forward", "--remove", "tcp:40000"]
    ]


def test_status_reports_session_and_shared_tunnels(env):
    adb, _, manager = env
    manager.attach(SERIAL)
    assert manager.status(SERIAL)["tunnel"] == "session"
    manager.detach(SERIAL)
    adb.forwards.append((SERIAL, 39999, DEVICE_PORT))
    status = manager.status(SERIAL)
    assert status["tunnel"] == "shared" and status["forward_port"] == 39999


def test_status_does_not_probe_when_not_installed_or_disabled(env):
    adb, _, manager = env
    status = manager.status(SERIAL)
    assert status["reachable"] is False and status["tunnel"] is None
    assert not _calls(adb, ["-s", SERIAL, "forward"])


def test_status_flags_a_helper_newer_than_the_bundle(env):
    adb, _, manager = env
    adb.installed[SERIAL] = 7
    status = manager.status(SERIAL)
    assert status["newer_than_bundled"] is True and status["outdated"] is False


# --------------------------------------------------------------------------- #
# Session token, protocol gate, auto-install switch, per-device locking
# --------------------------------------------------------------------------- #


def test_attach_pushes_the_host_token_and_records_it(env, tmp_path):
    adb, _, manager = env
    session = manager.attach(SERIAL)
    token = manager.host_token()
    assert len(token) == 48 and session.token == token and session.protocol_version == 2
    assert adb.tokens[SERIAL] == token
    assert (tmp_path / "token" / "session.token").read_text() == token
    # A second manager on the same host (another process) presents the same token.
    other = AccessibilityHelperManager(
        run_adb=adb,
        ping=lambda _p: None,
        bundled=None,
        token_path=tmp_path / "token" / "session.token",
    )
    assert other.host_token() == token


def test_push_token_is_repeated_on_demand(env):
    adb, _, manager = env
    manager.attach(SERIAL)
    adb.tokens.clear()
    assert manager.push_token(SERIAL) is True
    assert adb.tokens[SERIAL] == manager.host_token()


def test_old_protocol_is_reinstalled_when_the_caller_may_provision(env):
    adb, _, manager = env
    adb.installed[SERIAL] = 9  # newer versionCode than the bundle, but the old contract
    adb.protocol[SERIAL] = 1
    session = manager.attach(SERIAL)
    assert _calls(adb, ["-s", SERIAL, "install"])
    assert session.protocol_version == 2 and session.version_code == 2


def test_old_protocol_blocks_a_lazy_attach_without_installing(env):
    adb, _, manager = env
    adb.installed[SERIAL] = 2
    adb.enabled_services[SERIAL] = SERVICE_NAME
    adb.protocol[SERIAL] = 1
    with pytest.raises(HelperUnavailable, match="protocol 1"):
        manager.attach(SERIAL, provision=False)
    assert not _calls(adb, ["-s", SERIAL, "install"])
    assert adb.forwards == []


def test_auto_install_off_never_installs(env):
    adb, _, manager = env
    manager.auto_install_switch["install"] = False
    with pytest.raises(HelperUnavailable, match="artemis helper install"):
        manager.attach(SERIAL)
    assert not _calls(adb, ["-s", SERIAL, "install"])

    # An outdated but present helper is used as it is.
    adb.installed[SERIAL] = 1
    session = manager.attach(SERIAL)
    assert session.version_code == 1
    assert not _calls(adb, ["-s", SERIAL, "install"])
    assert manager.status(SERIAL)["auto_install"] is False


def test_provisioning_one_device_does_not_block_another(env):
    import threading

    _, _, manager = env
    lock = manager._device_lock(SERIAL)
    lock.acquire()  # simulate a slow install in progress on SERIAL
    try:
        done = threading.Event()
        result: dict = {}

        def attach_other():
            result["session"] = manager.attach(OTHER)
            done.set()

        threading.Thread(target=attach_other, daemon=True).start()
        assert done.wait(2.0), "attach(OTHER) waited on SERIAL's lock"
        assert result["session"].serial == OTHER
    finally:
        lock.release()


def test_status_reports_protocol_and_token(env):
    adb, _, manager = env
    manager.attach(SERIAL)
    status = manager.status(SERIAL)
    assert status["protocol_version"] == 2 and status["protocol_supported"] is True
    assert status["token_set"] is True and status["auto_install"] is True


# --------------------------------------------------------------------------- #
# UiAutomation holders (uiautomator2 / Appium) mute the helper
# --------------------------------------------------------------------------- #

U2_LINES = [
    "24633 sh -c CLASSPATH=/data/local/tmp/u2.jar app_process / com.wetest.uia2.Main -p 9008",
    "24635 app_process / com.wetest.uia2.Main -p 9008",
]


def test_attach_stops_a_running_uiautomator2_server_and_binds(env):
    adb, _, manager = env
    adb.installed[SERIAL] = 2
    adb.enabled_services[SERIAL] = SERVICE_NAME
    adb.holders[SERIAL] = list(U2_LINES)
    session = manager.attach(SERIAL)
    assert session.local_port == 40000
    assert adb.killed == [(SERIAL, 24635)]  # the app_process, not its sh wrapper
    assert ["-s", SERIAL, "shell", "am", "force-stop", "com.github.uiautomator"] in adb.calls
    # No re-bind toggle was needed once UiAutomation released the services
    # (provisioning's idempotent accessibility_enabled=1 write is fine).
    assert not _calls(
        adb, ["-s", SERIAL, "shell", "settings", "put", "secure", "enabled_accessibility_services"]
    )


def test_lazy_attach_names_uiautomator2_instead_of_killing_it(env):
    adb, _, manager = env
    adb.installed[SERIAL] = 2
    adb.enabled_services[SERIAL] = SERVICE_NAME
    adb.holders[SERIAL] = list(U2_LINES)
    with pytest.raises(HelperUnavailable, match="suppressed by uiautomator2's device server"):
        manager.attach(SERIAL, provision=False)
    assert adb.killed == []


def test_appium_server_is_never_killed(env):
    adb, _, manager = env
    adb.installed[SERIAL] = 2
    adb.enabled_services[SERIAL] = SERVICE_NAME
    adb.holders[SERIAL] = ["31000 io.appium.uiautomator2.server.test"]
    with pytest.raises(HelperUnavailable, match="Appium.*disableSuppressAccessibilityServices"):
        manager.attach(SERIAL)
    assert adb.killed == []
    assert not _calls(
        adb, ["-s", SERIAL, "shell", "settings", "put", "secure", "enabled_accessibility_services"]
    )


def test_uiautomation_holders_classifies_processes(env):
    adb, _, manager = env
    adb.holders[SERIAL] = U2_LINES + ["31000 io.appium.uiautomator2.server.test", "7 sh"]
    holders = manager.uiautomation_holders(SERIAL)
    assert holders == {"uiautomator2": [24635], "appium": [31000]}
