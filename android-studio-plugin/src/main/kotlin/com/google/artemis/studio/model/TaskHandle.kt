/*
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.google.artemis.studio.model

import com.google.gson.JsonObject

/**
 * A task accepted by the remote ARTEMIS scheduler.
 * Mirrors the TaskHandle dataclass in packages/artemis-client (models.py).
 */
data class TaskHandle(
    val taskId: String,
    val status: String,
    val deviceSerial: String? = null,
) {
    /** Compatibility alias for servers that call a task a session. */
    val sessionId: String get() = taskId

    companion object {
        fun fromPayload(payload: JsonObject): TaskHandle {
            val taskId = payload.firstString("task_id", "session_id", "id")
                ?: throw ArtemisProtocolException(
                    "Task admission response did not contain a task/session ID"
                )
            return TaskHandle(
                taskId = taskId,
                status = (payload.firstString("status") ?: "queued").lowercase(),
                deviceSerial = deviceSerialFromPayload(payload),
            )
        }
    }
}
