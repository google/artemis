<!--
  Copyright 2026 Google LLC

  Licensed under the Apache License, Version 2.0 (the "License");
  you may not use this file except in compliance with the License.
  You may obtain a copy of the License at

      https://www.apache.org/licenses/LICENSE-2.0

  Unless required by applicable law or agreed to in writing, software
  distributed under the License is distributed on an "AS IS" BASIS,
  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
  See the License for the specific language governing permissions and
  limitations under the License.
  -->

# ARTEMIS Android Studio Plugin (Phase 1 MVP)

A thin-client Android Studio / IntelliJ plugin for
[ARTEMIS](../README.md), a natural-language Android on-device automation
framework. The plugin contains **no agent logic**: it talks to a running
ARTEMIS server over its HTTP API and nothing else.

## What it does

- **Settings** page (Settings → Tools → ARTEMIS): server base URL
  (default `http://localhost:8000`), default profile (`flash` / `pro`), and an
  optional default device serial.
- **ARTEMIS tool window** (right-hand side):
  - Server status indicator (`GET /api/status`).
  - Device dropdown populated from `GET /api/devices`, with a refresh button.
  - Prompt text area + **Run Task** button → `POST /api/run` (disabled while
    the server is unreachable).
  - Session task list with live status polling (`GET /api/sessions/{id}`, every
    ~2 s while a task is active, on background coroutines; the Swing UI is only
    ever updated on the EDT).
  - Detail pane for the selected task (status, turn counts, result summary,
    error text) and a **Stop** button → `POST /api/stop`.
- **Notifications** for task completion / failure (balloon group
  `ARTEMIS_TASKS`).

The Kotlin model classes mirror the Python client models in
[`packages/artemis-client`](../packages/artemis-client/src/artemis_client/models.py)
field-for-field, including the legacy payload fallbacks
(`session_id`/`task_id`/`trace_id`, `current_turn`/`turns`,
`output`/`result`/`summary`, wrapped or bare `/api/devices` lists, scheduler
queue fallback for `GET /api/sessions/{id}` 404s, and the legacy capabilities
baseline).

## Compatibility

| IDE | Version | Build range |
|---|---|---|
| Android Studio | Hedgehog 2023.1.1+ (Hedgehog → Narwhal and newer) | 231+ |
| IntelliJ IDEA (Community/Ultimate) | 2023.1+ | 231+ |

The plugin is declared with `since-build="231"` and **no** `until-build` cap.
It is compiled against IntelliJ IDEA Community 2024.3 (see
`gradle.properties`).

## Prerequisites

1. An ARTEMIS server running and reachable, e.g. the default
   `http://localhost:8000` (see the repo root README for how to start it).
2. At least one Android device attached/visible to that server
   (`GET /api/devices` non-empty), unless you rely on the server-side default.
3. JDK/JBR 17 to build (IntelliJ plugin development requires JVM 17). The
   checked-in `gradle.properties` points `org.gradle.java.home` at a JBR 17
   path; adjust it for your machine if needed.

## Build

```bash
cd android-studio-plugin
./gradlew buildPlugin
```

The first build downloads the IntelliJ Platform distribution (~1 GB+), so it
can take a while. The plugin ZIP is produced at:

```
build/distributions/artemis-android-studio-plugin-0.1.0.zip
```

Other useful tasks:

```bash
./gradlew test                  # unit + HTTP-integration tests (no IDE needed at runtime)
./gradlew verifyPluginStructure # sanity-check the assembled plugin layout
./gradlew runIde                # launch a sandbox IDE with the plugin installed
```

To build against a locally installed IDE instead of downloading the platform
(for example Android Studio itself):

```bash
./gradlew buildPlugin -PplatformLocalPath="/Applications/Android Studio.app/Contents"
```

## Install from disk

1. Build the ZIP (above).
2. In Android Studio: **Settings → Plugins → ⚙ → Install Plugin from Disk…**
3. Select `build/distributions/artemis-android-studio-plugin-0.1.0.zip` and restart the IDE.
4. Open the **ARTEMIS** tool window on the right, and (optionally) adjust the
   server URL under **Settings → Tools → ARTEMIS**.

## Architecture

```
+------------------+        HTTP (JSON)         +------------------+      adb       +----------+
| Android Studio   |  GET /api/status           | ARTEMIS server   |  ---------->   | Android  |
|  ARTEMIS plugin  |  GET /api/devices          | (scheduler +     |                | device   |
|  (thin client,   |  POST /api/run             |  agent runtime,  |                |          |
|   no agent code) |  GET  /api/sessions/{id}   |  LLM providers)  |                |          |
|                  |  POST /api/stop            |                  |                |          |
+------------------+                             +------------------+                +----------+
```

Package layout (`src/main/kotlin/com/google/artemis/studio/`):

| Package | Contents |
|---|---|
| `model` | Data classes mirroring the Python client models + `RunRequest` payload builder |
| `api` | `ArtemisApiClient` (JDK `java.net.http.HttpClient` + Gson) and typed exceptions |
| `settings` | `PersistentStateComponent` + `Configurable` (Settings → Tools → ARTEMIS) |
| `services` | `TaskPollingService` (2 s status polling, coroutine-based) + notifications |
| `toolwindow` | `ArtemisToolWindowFactory` + the tool window panel |

JSON uses Gson, which is bundled with the IntelliJ Platform — the plugin has
no external runtime dependencies.

## Testing

```bash
./gradlew test
```

The suite runs entirely outside the IDE:

- **Model parsing** (`ModelParsingTest`): canned payloads mirrored from
  `packages/artemis-client/tests/test_client.py` (legacy device shape,
  terminal session payload, capability list/map forms, field-name fallbacks).
- **Request building** (`RunRequestTest`): `POST /api/run` payload shape
  (`goal` / `profile` / `session_id` / `ingress`, optional-field omission,
  Pro tuning-knob normalization, early rejection of invalid values).
- **HTTP integration** (`ArtemisApiClientIntegrationTest`): the client against
  the JDK's built-in `com.sun.net.httpserver.HttpServer` — full
  submit → poll → stop flow with wire-payload assertions, task rejection,
  scheduler-queue fallback for session 404s, legacy capabilities fallback,
  HTTP 500, unreachable server, and malformed `/api/devices` payloads.
