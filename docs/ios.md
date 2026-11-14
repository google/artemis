# iOS support

Artemis can run Flash and Pro tasks on iOS simulators and on paired physical
iPhones/iPads on macOS with Xcode 27 or newer. Select iOS explicitly: the
existing Android defaults still apply on a Mac. iOS tasks work through the CLI,
embedded Python SDK, Artemis Daemon task queue, Admin Console, and the Artemis
MCP tools — including native screen recording, video analysis/replay, device
discovery, live screen streaming, and per-device locking that cannot collide
with Android targets.

The iOS drivers use tools included with Xcode and Artemis's existing Python MCP
dependency. Simulators communicate with `xcrun mcpbridge` using an initialized
MCP session for screen observation and input, and `xcrun simctl` for lifecycle
operations. Physical devices use `xcrun devicectl` (CoreDevice) for lifecycle
plus **WebDriverAgent (WDA)** for observation and input — Xcode's
`DeviceInteraction*` MCP tools accept simulators only. WDA is the same
XCUITest HTTP bridge Appium uses and must be built and installed on the device
once (see below). Apple added native agent device interactions in Xcode 27;
see the
[Xcode 27 release notes](https://developer.apple.com/documentation/xcode-release-notes/xcode-27-release-notes).

## Prepare Xcode

1. Install Xcode 27 or newer, complete its first-launch setup, and install an iOS
   simulator runtime. Confirm that `xcodebuild -version` reports the intended
   version and that `xcode-select -p` points to that Xcode installation. You can
   also set `DEVELOPER_DIR` for the Artemis process when multiple Xcode versions
   are installed.
2. Enable external agent access using Apple's
   [Xcode MCP access instructions](https://developer.apple.com/documentation/xcode/giving-external-agents-access-to-xcode)
   and review any Xcode approval prompts. For the workspace-independent MCP
   server introduced in Xcode 27, follow Apple's enablement instructions in the
   [release notes](https://developer.apple.com/documentation/xcode-release-notes/xcode-27-release-notes).
   Artemis does not enable the server or grant permissions automatically.
3. For physical devices: connect the iPhone/iPad over USB (or enable network
   pairing), tap **Trust** on the device pairing prompt, and enable **Developer
   Mode** in Settings > Privacy & Security on iOS 16+. `xcrun devicectl list
   devices` should show the device `connected` (or `available (paired)`); the
   JSON fields are `pairingState: paired` and `tunnelState: connected`.
4. Check the toolchain and available simulators from the repository root:

   ```bash
   bash scripts/setup_ios_env.sh
   ```

   This check reads the Xcode version, tool locations, simulator and
   physical-device (`devicectl`) inventories, and `xcrun mcp-server status`. It
   does not install dependencies, boot a simulator, or change access settings. A passing check confirms prerequisites; the first
   driver connection checks native MCP tool availability and device access.
5. Install Artemis's Python dependencies and configure a model provider using
   the normal project configuration:

   ```bash
   uv sync --dev
   cp .env.example .env
   ```

   Fill in the provider credentials in `.env` and select the desired models in
   `config/artemis.jsonc`. The one-click Android startup and dependency installer
   are not needed for this iOS workflow.

## Run a task

List the simulators to find the UDID for an available iOS device:

```bash
xcrun simctl list devices available
```

Run a standalone task on that simulator:

```bash
uv run artemis run "Open Settings and inspect the General screen" \
  --platform ios --standalone --device-serial <SIMULATOR-UDID> --profile flash
```

An explicitly selected simulator is booted if necessary, and the driver waits
for it to finish booting. To use a simulator that is already running, omit
`--device-serial` or pass `--device-serial booted`. Exactly one available iOS
simulator must be booted; with zero or multiple booted devices, choose a UDID.
The driver resolves `booted` once and pins every later command to that UDID.

Use `--profile pro` to run the planner, operator, and verification workflow on
the same driver. Prompts and app identifiers should refer to iOS apps and bundle
identifiers, such as `com.apple.Preferences`.

To install a simulator build before the task, add
`--app-path /absolute/path/MyApp.app`. The bundle's `Info.plist` must provide
`CFBundleIdentifier`.

### Physical devices

A paired physical device is selected the same way — by UDID:

```bash
xcrun devicectl list devices          # find the device UDID
uv run artemis run "Open Settings" \
  --platform ios --standalone --device-serial <DEVICE-UDID> --profile flash
```

Physical UDIDs are routed to a separate driver: `devicectl` handles install,
launch, terminate, app listing, URL opening, and screenshot capture, while
the UI hierarchy and input (taps, swipes, text, buttons) go through
**WebDriverAgent**. `booted` remains a simulator-only selector — physical
tasks always require an explicit `--device-serial`.

#### WebDriverAgent setup (one time per device)

WDA is a signed XCTest runner app that exposes an HTTP automation endpoint on
port 8100. Build and install it once per device — Appium users can reuse an
existing WDA install:

```bash
git clone https://github.com/appium/WebDriverAgent.git
cd WebDriverAgent
xcodebuild build-for-testing -project WebDriverAgent.xcodeproj \
  -scheme WebDriverAgentRunner -destination id=<DEVICE-UDID> \
  -allowProvisioningUpdates DEVELOPMENT_TEAM=<TEAM-ID> \
  PRODUCT_BUNDLE_IDENTIFIER=com.example.WebDriverAgentRunner
xcrun devicectl device install app --device <DEVICE-UDID> \
  <DerivedData>/Build/Products/Debug-iphoneos/WebDriverAgentRunner-Runner.app
```

Any Apple development team works; a free Personal Team profile must be
re-signed every 7 days while paid-program profiles last a year.

When Artemis connects, it finds an installed `*WebDriverAgent*` runner on the
device, launches it via `devicectl device process launch`, and probes its HTTP
endpoint on the CoreDevice tunnel address and `127.0.0.1:8100` (for
`iproxy`/`pymobiledevice3` port forwards). Endpoint overrides:

- `ARTEMIS_IOS_WDA_URL` — full base URL, e.g. `http://127.0.0.1:8100` or
  `http://<device-LAN-ip>:8100`
- `ARTEMIS_IOS_WDA_HOST` — host only; port 8100 assumed
- `ARTEMIS_IOS_WDA_BUNDLE_ID` — nonstandard runner bundle identifier
- `ARTEMIS_IOS_WDA_XCTESTRUN` — path to a `WebDriverAgentRunner_*.xctestrun`
  bundle from `xcodebuild build-for-testing`; Artemis hosts it via
  `xcodebuild test-without-building` (the canonical WDA session — a bare
  runner app launch does not start the HTTP server)

Endpoint ownership limits: an explicitly configured or forwarded WDA
endpoint (`ARTEMIS_IOS_WDA_URL`/`ARTEMIS_IOS_WDA_HOST`) must belong to the
selected physical device. Before opening a session, Artemis reads WDA's
sessionless `GET /wda/device/info` and refuses to connect when it reports a
simulator or a device name different from the selected device — the name
corroborates the selection but is not a cryptographic proof of unique-device
identity (WDA's `uuid` is `identifierForVendor`, not the device UDID). Artemis
also refuses to replace a WDA session that belongs to another client, since
WDA's `POST /session` unconditionally kills the active session; close the
existing session first.

A WDA session is anchored to an app bundle — a bare session binds to a
transient `pid.0` and fails on first use, so Artemis always creates sessions
with an `alwaysMatch` `bundleId`. New and recovered sessions anchor to the
device's foreground app, falling back to `com.apple.Preferences` when the
foreground is SpringBoard (`com.apple.springboard` is not an activatable
session target) or cannot be determined. When a session dies mid-task — WDA
replies `invalid session id`/`session does not exist`, or `invalid element
state: The application under test ... is not running` when the anchor app
itself exits, while device-level screenshots keep working — the client drops
the zombie, rebinds under a lock,
rewrites the old session id in the request path, and retries the command once.
Requests that timed out are never replayed: a stalled input may have executed
device-side, so only reads recover transparently.

Protocol details verified against WebDriverAgent 16.14.0 (commit `d177824`,
checked 2026-10-07):
[FBSessionCommands.m](https://github.com/appium/WebDriverAgent/blob/d177824/WebDriverAgentLib/Commands/FBSessionCommands.m),
[FBResponsePayload.m](https://github.com/appium/WebDriverAgent/blob/d177824/WebDriverAgentLib/Routing/FBResponsePayload.m),
[FBCustomCommands.m](https://github.com/appium/WebDriverAgent/blob/d177824/WebDriverAgentLib/Commands/FBCustomCommands.m).

Simulator bridging note: `xcrun mcpbridge` is spawned with a minimal
environment; `DEVELOPER_DIR` and `MCP_XCODE_PID` are forwarded when set so a
specific Xcode toolchain can be pinned.

Differences from simulators:

- The device must already be paired, trusted, and connected; Artemis never
  boots or unlocks it — unlock the device before connecting, since WDA cannot
  inject touches while it is locked. The first XCTest attach may also show an
  on-device "Enable UI Automation" passcode prompt; approve it once, and expect
  resprings or restarts to require approving it again. For longer tasks, set
  **Auto-Lock** to *Never* (Settings > Display & Brightness) so the device
  does not re-lock mid-task.
- `--app-path` expects a device-signed artifact: an `.app` built for an arm64
  device destination (signed with a valid provisioning profile) or a `.ipa`.
  Simulator `.app` bundles are x86_64/arm64-simulator builds and cannot be
  installed on hardware.
- System apps are discoverable: `manage_app` resolves Apple apps like Safari
  (`com.apple.mobilesafari`) alongside installed apps — the physical driver
  lists default apps via `devicectl`, so preinstalled apps launch by name or
  bundle id without installation.
- Screen recording polls `devicectl device capture screenshot` and assembles
  timestamped MP4 segments (~1–3 fps). There is no `recordVideo`-equivalent
  stream on hardware, so motion fidelity is lower than simulator captures and
  brief gaps between frames are expected.

### First-run Xcode approval

The very first time a new Python interpreter asks Xcode for device access,
Xcode requires user approval for that interpreter and, if one is supplied, the
selected project folder. Point Artemis at an existing Xcode project or
workspace so the request can be recorded:

```bash
uv run artemis run "Open Settings" --platform ios --standalone \
  --ios-workspace /absolute/path/MyApp.xcodeproj
```

If approval is still pending, the run stops with an "Xcode Approval Required"
panel instead of retrying. Approve the interpreter and the selected folder from
the Xcode MCP menu bar icon, choosing **Always Allow** there if offered and you
want later runs to skip the prompt; then rerun the task. Advanced users can instead
inspect pending request IDs with `xcrun mcp-server status` and approve only
those entries via `sudo xcrun mcp-server approve <REQUEST-ID> --always` from
their own terminal; the CLI path requires admin rights and is performed by the
user, never by Artemis.

Granted approvals persist across fresh bridge processes; there is no need to
keep a bridge alive. A different interpreter path or build, a different project
folder, or an expiring grant can require approval again. The Always/persistent
choice belongs to you and Xcode; Artemis requests only scoped approval for its
interpreter and the folder you select and never enables global access.

Grant duration is Xcode's decision, not Artemis's: Xcode binds each agent
approval to the requesting binary's code signature. A binary signed with a
real signing identity can hold a persistent **Always Allow** grant, but an
unsigned or adhoc-signed interpreter — the common case for `python3` from uv,
Homebrew, or a virtual environment — receives a grant that expires after
roughly 24 hours, and upgrading or replacing that interpreter binary requires
re-approval either way. `xcrun mcp-server status` lists every grant with its
expiry. On headless or CI machines an administrator can run
`sudo xcrun mcp-server enable` to keep the service reachable while Xcode is
closed; per-agent approval still applies unless the administrator also passes
`--unsafe-always-allow-all-agents`, which lets any local process drive
reachable projects and should stay disabled outside CI.

## Embedded Python SDK

Configure iOS through the embedded SDK's builder:

```python
from artemis.sdk import Agent
from artemis.sdk.builders import AgentConfigBuilder

config = AgentConfigBuilder().for_ios_device("<SIMULATOR-UDID>").build()
agent = Agent(config=config)
```

To run the first-run approval flow against a specific project, pass the
existing project or workspace through the builder:

```python
config = (
    AgentConfigBuilder()
    .for_ios_device(
        "<SIMULATOR-UDID>",
        workspace_path="/absolute/path/MyApp.xcodeproj",
    )
    .build()
)
```

`with_ios_workspace(path)` applies the same setting, and `None` clears it.
The path is used only if Xcode refuses the initial session with an approval
error; already-approved runs never open a workspace.

The generic builder also accepts
`for_device(DevicePlatform.IOS, "<SIMULATOR-UDID>")`, with `DevicePlatform`
imported from `artemis.context`. Supplying only a `device_serial` without an iOS
configuration retains the existing Android selection behavior.

The dependency-free remote client submits iOS tasks to an iOS-capable host —
the remote host needs macOS, Xcode, and (for hardware) WDA set up as above;
`ios_workspace` is an optional host-side path, not resolved locally:

```python
from artemis_client import ArtemisClient

client = ArtemisClient(
    base_url="http://mac-host:8000",
    device_serial="<UDID>",
    platform="ios",
    # optional: ios_workspace="/abs/host/path/MyApp.xcworkspace",
)
result = await client.run("Open Settings and inspect the General page")
```

iOS submissions first check `GET /api/v1/capabilities` and refuse to POST
when the host does not advertise `platform.ios` — upgrade the host instead
of silently running on Android.

## Daemon, web console, and batch submission

Without `--standalone`, `artemis run --platform ios` forwards
`platform`/`ios_workspace`/`device_serial` to the running Artemis Daemon, which
queues the task against the selected iOS device under the shared `ios`
lock scope and spawns the worker with `--platform ios` — no ADB endpoint or Android readiness
probe is involved. `artemis batch` accepts the same `--platform`,
`--device-serial`, and `--ios-workspace` flags for goal lists.

The Admin Console `/api/run` accepts `platform: "ios"`, an iOS device UDID
(simulator or paired physical hardware) in `device_serial`, and an optional
`ios_workspace`; `/api/devices` lists Android
devices and iOS devices (simulators and paired physical hardware) together,
each tagged with its `platform`. The live screen view
(`/api/stream/device-live`) streams frames captured with `simctl io
screenshot` for simulators or `devicectl capture screenshot` for physical
devices when an iOS task holds the lock; `/api/stream/device-state` reports
the `platform` of the streamed device. Replay preserves the recorded session's
`mobile_platform`, so an iOS trace replays through the native iOS driver (the
device picker retargets iOS replays to a chosen UDID — simulator or paired
physical).

`mobile_run_task` accepts `platform="ios"`, `device_serial=<UDID>`, and
`ios_workspace=<path>`; it validates the UDID against the `simctl` and
`devicectl` inventories rather than ADB and queues the runner under the `ios`
lock scope. `mobile_get_device_state` and `mobile_diagnose` accept the same
`platform` switch — the latter runs a native screenshot/hierarchy smoke test
on the selected iOS device. The legacy
`Android_ADB_Controller` actuator server (tap/swipe/type tools) remains
Android-only.

A minimal run looks like:

```python
agent = Agent(config=config)
try:
    await agent.init()
    result = await agent.run_task(goal="Open Settings", profile="flash")
    screenshot = await agent.get_screenshot()
finally:
    await agent.clean()
```

`init` resolves the simulator without opening a native session. Public
`get_screenshot()` and `install_app()` calls acquire the device execution
lease and own a short-lived native session that is closed before they return,
so they work both right after `init` and between tasks. An app installation
requested inside `run_task` reuses the task's existing lease and session
instead of acquiring a second one.

## Supported operations and limits

| Operation | Simulator | Physical device |
| --- | --- | --- |
| Device selection and readiness | `simctl` inventory, `boot`, `bootstatus` | `devicectl` inventory; must be paired and connected |
| Screenshot and accessibility hierarchy | Xcode native device-interaction MCP session | WDA `/source` and `/screenshot` (devicectl screenshot fallback) |
| Tap, long press, and swipe | Native synthesized touch events | WDA W3C pointer actions |
| Text entry | Native keyboard synthesis with `clear_exist=false` | WDA `/wda/keys`, `clear_exist=false` |
| Enter, Home, Power, volume, and app switcher | Native keyboard and button synthesis | WDA `/wda/keys` (Enter), `/wda/homescreen`, and `/wda/pressButton` |
| App install, launch, and terminate | `simctl` with simulator `.app` bundles | `devicectl` with signed `.app`/`.ipa`; terminate re-verifies the running executable against the installed app URL (a cached launch PID is only a hint — recycled PIDs are never trusted) |
| Screen recording | `simctl io recordVideo` (VFR H.264) | `devicectl` screenshot polling assembled to timestamped MP4 |

The native hierarchy is normalized into the element tree used by Artemis's
perception and action tools. Screenshots and touch coordinates are kept in the
same coordinate space. Custom UI without accessible elements still relies on
visual targeting.

watchOS, tvOS, and visionOS are outside this implementation. Install a
simulator build on simulators and a device-signed build on hardware; neither
artifact is interchangeable with the other or with an Android `.apk`. The
drivers do not build Xcode projects.

iOS has no system Back button. Use the app's visible navigation controls.
The native driver supports `enter`, `home`, `power`, `volume_up`, `volume_down`,
and `app_switch`. The `erase_one_char` and `focus_and_clear_text` actions are
not exposed. The `back` and `delete` keys and automatic replacement of existing
text fail with an explicit unsupported-operation error. When calling the `input_text` action,
pass `clear_exist=false`; the default requests text replacement. In direct
driver or controller calls, the equivalent argument is `clear_existing=False`.
To replace text, clear the field using its visible UI first.
Android shell commands, Logcat, Android resource identifiers, Android package
discovery, and the Android Accessibility Helper are unavailable on iOS.
Platform-specific operations fail explicitly when unsupported.

Screen recording on simulators uses the native `xcrun simctl io recordVideo`
capture with no third-party device automation. Each segment is a
variable-frame-rate H.264 `.mov` anchored to its first captured frame; after
recording stops (or when a rotation or the duration limit rolls a segment),
the bundled FFmpeg post-processing finalizes a browser-safe 30 fps MP4 and a
`recording.json` manifest mapping every segment to its session-time offset.
On physical devices, recording polls `devicectl device capture screenshot`
into timestamped PNG frames and assembles the same manifest/MP4 output shape
at the real capture cadence. The video analyzer can clip the sealed portion of
an in-progress recording; request ranges that reach past the sealed boundary
are clipped with a warning. Limitations: iOS capture is silent (no audio),
pre-first-frame startup time is not captured, physical capture runs at ~1–3
fps rather than continuous video, and recorder restarts leave a brief gap in
the timeline rather than stretching recorded frames.

## Troubleshooting

- **Xcode version or tools are incorrect:** inspect `xcodebuild -version`,
  `xcode-select -p`, and any `DEVELOPER_DIR` override. Command Line Tools alone
  do not provide the full Xcode simulator and device-interaction environment.
- **MCP tool missing or access denied:** follow Apple's MCP access instructions,
  review approval prompts, and check `xcrun mcp-server status`. Xcode 27 release
  notes mention that some settings may require relaunching Xcode or restarting
  the Mac. If Xcode requests workspace approval before allowing a session, open
  and approve the intended workspace through Xcode's normal access flow.
- **CoreSimulator inventory fails:** run `xcrun simctl list devices available`
  in a local terminal. Sandboxed processes need access to the simulator service
  and its device data. Install a runtime and create a simulator if the iOS
  inventory is empty.
- **Multiple booted simulators:** supply `--device-serial` with the intended
  simulator's UDID.
- **Physical device not found:** confirm `xcrun devicectl list devices` lists
  it as `paired`; if pairing is absent, reconnect and approve the Trust prompt.
  A device shown as `disconnected`/offline needs USB reattachment or reachable
  network pairing.
- **Physical install fails:** the artifact must be signed for the device —
  check the signing team and provisioning profile, or build an `.ipa` with
  `xcodebuild -exportArchive` for the device destination.
- **Physical input fails on a locked device:** unlock the device; WDA cannot
  inject touches while locked. A device that re-locks mid-task suspends UI
  Automation — set Auto-Lock to Never while testing.
- **"Not authorized for performing UI testing actions" (Code=41):** the
  device's UI Automation authorization was reset — resprings and restarts can
  clear it. Unlock the device, re-enable **UI Automation** under
  Settings > Developer, approve any on-device prompt, and relaunch the runner
  (`devicectl device process launch --terminate-existing <runner-bundle>`).
- **WDA requests stall then recover:** WDA serializes HTTP requests, so a slow
  `/source` or text-input call queues every later request, including
  screenshots. Stalls clear when the queued request returns; they are not a
  sign the session died.
- **"invalid session id" mid-task:** the session's anchor app exited or another
  client replaced the session. Artemis rebinds automatically (see the session
  notes above); a failed rebind surfaces as a session-recovery error rather
  than an endless retry loop.
- **"No WebDriverAgent server answers":** no WDA endpoint responded on the
  CoreDevice tunnel or `127.0.0.1:8100`, and no installed runner matching
  `*WebDriverAgent*` was found to launch. Build and install the WDA runner
  (one-time steps above), or point `ARTEMIS_IOS_WDA_URL` at a forwarded or
  LAN-reachable server.
- **WDA runner launches but never answers:** unlock the device first — UI
  Automation, and therefore WDA's HTTP server, cannot start while it is
  locked. Otherwise the runner may be crashing on launch — check provisioning
  (`get-task-allow`, matching certificate) and whether a free-team profile
  expired; also try forwarding port 8100 (`iproxy 8100 8100`) and setting
  `ARTEMIS_IOS_WDA_URL`.
- **A queued iOS task runs on the wrong surface:** confirm the submission
  carried `platform: "ios"` (CLI `--platform ios`, web request `platform`,
  or the MCP `platform` argument); tasks default to Android.

## Contributing and validation

Keep native MCP and subprocess interactions behind the iOS driver. Unit tests
should use fake MCP sessions and command runners so the normal deterministic
suite remains usable on Linux and without Xcode or model credentials. Run
`make test`, `make lint`, and `make typecheck` before submitting a change.

For live acceptance on a configured Mac, run the prerequisite script and an
explicit standalone CLI task. Verify that the chosen simulator receives taps,
swipes, and text, and that the screenshot and hierarchy describe the same
screen. Test an ambiguous `booted` selection and denied MCP access as well as a
successful session, and confirm that the session is released when the task
ends or is cancelled. Record the Xcode version, simulator runtime, UDID, and
observed results in the pull request; unit tests alone do not establish live
device compatibility.
