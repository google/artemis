# Mobile Testing Mindset (ARTEMIS Integration)

Write executable tests based on device behavior verified with **ARTEMIS**. Before authoring a test, explore the target application and confirm its screens, transitions, and interactions. Use ARTEMIS device actions or ADB commands to investigate software and hardware behavior instead of assuming how an interaction works.

### 1. The Runnable Code Principle & ARTEMIS Exploration
- **When tasked with authoring tests**, deliver runnable test code with verified interactions and explicit wait conditions.
- Before writing any test code, you must use the ARTEMIS MCP tools to interactively run and explore the target application. This allows you to discover the exact sequence of UI states, transitions, and required interactions.
- Analyze the user's target testing framework to exploit its native capabilities and maximize test stability.
- **Timing & Latency Management (Exploration vs. Execution)**:
  - **Precise Timing in Final Code**: While ARTEMIS's AI exploration inherently involves model latency and is not strictly time-precise, you must bridge this gap in your final deliverables. Use ARTEMIS to discover and verify the interaction path, then implement exact, deterministic timing and wait conditions (`sleep`, explicit/implicit waits) in your authored test scripts, as local test execution runs without LLM overhead.
  - **Compensating for Model Latency During Exploration**: When delegating exploratory tasks to ARTEMIS that involve waiting periods (e.g., waiting 30 seconds for a page load or timer), adjust the requested wait duration in your task description based on the selected model's natural step interval:
    - **Flash Model**: The average processing interval between steps is roughly **5 seconds**. For a required 30-second delay, instruct the agent to wait for approximately **25 seconds**.
    - **Pro Model**: The processing interval between turns is typically **~30 seconds** (due to multi-agent planning and verification). The natural pipeline delay often covers the required waiting time without adding long explicit delay commands.
  - **Pragmatic Timing Judgment**: When a user request mentions performing an action for a specific duration (e.g., "stay on this screen for 2 minutes"), evaluate whether exact timing is functionally critical. Often, these durations are rough guidelines rather than strict test constraints—exercise flexibility and pragmatic engineering judgment to achieve the verification goal efficiently.


### 2. ARTEMIS Closed-Loop Architecture Mastery
Deeply understand and select between ARTEMIS's dual execution models (**ARTEMIS Flash** and **ARTEMIS Pro**) based on the task scenario, and master their corresponding workflows:

- **ARTEMIS Flash (Fast / Reactive Model)**:
  - **Applicable Scenarios**: Designed for simple, highly deterministic, lightweight UI operations or direct automation workflows. There is no step cap by default (history is compressed rather than truncated), so task length alone is not a reason to choose Pro; choose Pro when the task needs a persistent plan, verified checkpoints, notes or a written report, ADB / log diagnostics, or multi-branch exploration.
  - **Execution Mechanism (Reactive Loop)**: Does not enter the LangGraph multi-node orchestration: there is no Planner, no pre-execution safety net, and no Checker. `FlashRunner` receives the user's goal, the latest UI element list, and the live screenshot, performs "Observe-Think-Act" with a single LLM, and calls action tools until it reports the task status. It chains taps into one `click_sequence` to catch transient UI (auto-fading control bars, toasts) before they expire, and it can call `ask_explorer` for element grounding, `search_history` / `replay_steps` / `get_step_screenshot` to look up compressed earlier steps, and `video_analyzer` over the session recording. History lives in the same session transcript ledger as Pro (session-relative `T+mm:ss` clock, screenshots folded into visual summaries, older steps chunked into eras).

- **ARTEMIS Pro (Deep / Multi-Agent Graph-Driven Closed-Loop Model)**:
  - **Applicable Scenarios**: Designed for long-range, highly complex, dynamic multi-branch tasks; continuous monitoring / polling loops; and tasks requiring deep system diagnostics (ADB, logs, video), verified checkpoints, or a detailed written report.
  - **Execution Mechanism (Plan-Execute-Verify-Summarize Closed-Loop)**:
    - **Planner**: Deconstructs complex, high-level testing goals into a living Markdown task plan with milestones and `verify` / `assert` check items. Later milestone edits get an advisory review (a hint plus reason back to the Operator; the plan is never rolled back).
    - **Operator**: Consumes the plan, analyzes the current screen state (screenshots plus the UI element list, with the Explorer for grounding at the `explorer_mode` perception depth: `flash` 1-shot, `pro` 3-turn, `ultra` deep zoom), keeps notes, and executes precise device interactions; it can also recall compressed history, analyze the session recording, and run ADB commands as a supplement to UI actions.
    - **Safety Net & Execution Incidents**: A single turn-ending action is vetted by a pre-execution safety net (XML-first, pixel fallback). A multi-action turn is a **fast-action burst** that fires back to back without the safety net, which is how the Operator defeats turn latency on transient UI. When an action is intercepted or fails, the system opens an **execution incident** that persists in the Operator's context (with the consecutive-failure count shown as a fact) until a later action executes successfully; recovery is the Operator's own decision and there is no separate repair agent.
    - **Checker (Verification)**: A read-only verifier with the same observation tools as the Operator audits plan-declared checkpoints and performs an exit final review against the original goal; `verification_level` selects the depth (`off`, `final` = exit review only and the default, `checkpoints` = every checkpoint plus exit review, `strict` = checkpoints with a larger repair budget where a failed assert halts the run).
    - **Outputter (Optional)**: Synthesizes the entire execution trace into a human-readable report detailing every action step and visual result.

### 3. Device & Environment Constraints & Multi-Device Management
- **Device Selection & Multi-Device Execution**: ARTEMIS supports multi-device execution and per-device concurrency. You can control device targeting via two modes:
  - **Direct Device Specification**: Explicitly provide the target phone's serial number via `device_serial` to `mobile_run_task` or `mobile_get_device_state`. Tasks targeting distinct devices run concurrently without blocking each other.
  - **Automatic Device Selection**: When `device_serial` is omitted or set to `None`, ARTEMIS automatically selects an available connected device or allocates an idle device from the device pool.
- **Prioritize User Choice for Device Selection**:
  - When multiple connected devices or emulators are detected, or whenever device selection is ambiguous, **YOU MUST PRIORITIZE ASKING THE USER** to select or confirm their preferred device serial before launching a task.
- **Device Diagnosis with `adb devices`**:
  - Use `adb devices` (or `adb devices -l`) via bash command execution to diagnose attached hardware, inspect connection status (`device`, `unauthorized`, `offline`), and retrieve device serials and models whenever preparing tasks or troubleshooting device issues.
- **ADB & File Transfer**: While ARTEMIS excels at device automation, it operates within the device boundary. To extract diagnostic files, logs, or test artifacts from the mobile device to the host PC for further analysis, you must manually execute appropriate `adb` commands (e.g., `adb -s <serial> pull ...`).
- **Hardware Prerequisites**: Running ARTEMIS requires at least one physically connected, fully authorized Android device (e.g., a Pixel phone) or an active emulator.
- **Per-Device Mutual Exclusion**: ARTEMIS manages per-device execution mutexes (`DeviceExecutionLock`). A device can only execute a single task at a time (FIFO queue), while different devices can execute tasks in parallel.

### 4. Robust Test Code Design (The "Dynamic-First, Coordinate-Fallback" Philosophy)
*If your task involves authoring test code, you must adhere to the following design principles for maximum reliability:*
- **Adapt to Framework Capabilities**: You must first assess what locating mechanisms the user's test framework supports (e.g., resource IDs, XPath, text matching, OCR, image template matching, or absolute/relative coordinates).
- **The Core Principle: Dynamic-First, Coordinate-Fallback**:
  - Wherever supported by the framework, **always prioritize dynamic element locating** (using IDs, text, OCR, etc.) to ensure the test code can withstand UI layout drifts and resolution changes.
  - Use **absolute coordinates as a reliable fallback** to guarantee execution success when dynamic locators fail or are unavailable.
- **Implementing Resilient Locating Patterns**:
  - **Dual-Capable Frameworks**: If the framework supports both dynamic and coordinate-based locating, implement the **Try-Catch Fallback Pattern**:
    1. **Try**: Attempt to interact with the element using dynamic locators (IDs, OCR, text) for maximum resilience against UI changes.
    2. **Catch**: If the dynamic attempt fails, fall back to the precise absolute coordinates verified during your ARTEMIS exploration.
  - **Coordinate-Only Frameworks**: If the framework only supports coordinates, ensure the coordinates are well-documented, and where possible, parameterized or made relative to screen boundaries to mitigate resolution differences.
  - **Dynamic-Only Frameworks**: If the framework does not support coordinate-based clicks, focus entirely on generating highly robust dynamic locators, leveraging ARTEMIS's element descriptions and XML tree analysis.

### 5. Environment Self-Diagnosis (`mobile_diagnose`)
- **Diagnose before guessing**: Whenever an ARTEMIS tool returns an error, a task fails to start or is rejected, no device is found, or the user reports that ARTEMIS "doesn't work" in the IDE, call `mobile_diagnose` first. Do not troubleshoot by re-running `mobile_run_task` on trial and error, and do not ask the user to run `adb` commands by hand before you have the report.
- **If `mobile_diagnose` is not in your tool list at all**, the MCP server itself failed to start: check the IDE's MCP server log and run `uv run artemis doctor` in the project directory (the CLI shares the same checks).
- **Follow `next_steps` in order**: The report lists fixes in dependency order (Python runtime → config → MCP host → LLM credentials → device → optional video toolchain). `Run:` lines are single, local, non-destructive shell commands (one command per line, no `&&`) you may execute yourself (ask before installing software with winget / brew / apt). `Guidance:` lines describe something only the user can do (unlock the phone, tap "Allow" on the USB-debugging prompt, plug in a cable, free a port, tell you which IDE they use) — relay them verbatim. `Docs:` lines are references.
- **Let ARTEMIS self-heal first**: Call `mobile_diagnose(attempt_fix=true)` before asking for manual ADB intervention; it regenerates corrupted ADB RSA keys, restarts the ADB server when that is safe (no device ready, no task holding a device), and removes stale device locks / queue tickets left behind by crashed runners.
- **Deeper checks on demand**: `verify_credentials=true` validates the configured API keys live (~12s; use when a key exists but tasks fail with auth / quota errors; result in `credentials`). `probe_device=true` runs a true end-to-end check — screenshot plus UI hierarchy through UIAutomator (~20s; use when the device shows connected but observation or tasks fail; result in `device_probe`).
- **Emulators**: `launch_avd="<name>"` starts an installed Android Virtual Device in the background via ARTEMIS's emulator manager and returns immediately; boot takes 1-3 minutes, so re-run `mobile_diagnose` after ~60s. Never run `emulator -avd` in the shell yourself — it is a foreground process and hangs the shell.
- **Busy devices**: `tasks: {active, queued}` lists tasks currently holding or waiting for devices. A busy device means wait, or stop the task with `mobile_manage_task(action="stop", trace_id=...)`.
- **Credentials never go through the chat**: If the LLM key is missing, ask the user to add it to the `.env` file at the path the report shows (`host.env_file`) or to the MCP server's `env` block, then to restart the MCP server. `artemis init` is interactive and cannot be driven from a tool call.
- **Restart after configuration changes**: Settings are read when the server starts. After editing `.env`, `config/artemis.jsonc`, or the MCP client configuration, reload the MCP server in the IDE before calling `mobile_diagnose` again; otherwise it reports the old state.
- **Failed tasks**: `logs.last_failed_task` gives the trace id, its `stderr_log`, and `recent_errors` (the error lines from that stderr, so you need not open the file); read them before re-running the task. Repeat `mobile_diagnose` until `verdict` is `ready` or `degraded` before delegating tasks again (`degraded` means tasks can run but something optional is off: video tools, the OCR key, a secondary credential, or a host warning such as an interpreter mismatch or a squatted daemon port; relay its `[OPTIONAL]` steps to the user, do not loop on them).
