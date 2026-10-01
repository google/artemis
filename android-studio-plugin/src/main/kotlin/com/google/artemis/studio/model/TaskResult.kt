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

import com.google.gson.JsonElement
import com.google.gson.JsonObject

/**
 * Current or terminal state of a remote ARTEMIS task.
 * Mirrors the TaskResult dataclass in packages/artemis-client (models.py).
 */
data class TaskResult(
    val taskId: String,
    val status: String,
    val goal: String? = null,
    val profile: String? = null,
    val deviceSerial: String? = null,
    val output: JsonElement? = null,
    val error: String? = null,
    val turns: Int? = null,
) {
    val sessionId: String get() = taskId
    val done: Boolean get() = status in TERMINAL_TASK_STATUSES
    val succeeded: Boolean get() = status in SUCCESS_TASK_STATUSES

    /** Human-readable one-line rendering of the output payload, if any. */
    val outputSummary: String?
        get() = output?.let { if (it.isJsonPrimitive) it.asString else it.toString() }

    companion object {
        fun fromPayload(payload: JsonObject, taskId: String? = null): TaskResult {
            val resolvedId = payload.firstString("task_id", "session_id", "trace_id", "id")
                ?: taskId
                ?: throw ArtemisProtocolException("Task response did not contain a task/session ID")
            return TaskResult(
                taskId = resolvedId,
                status = (payload.firstString("status") ?: "unknown").lowercase(),
                goal = payload.firstString("goal", "initial_goal"),
                profile = payload.firstString("profile"),
                deviceSerial = deviceSerialFromPayload(payload),
                output = payload.firstElement("output", "result", "summary"),
                error = payload.firstString("error", "error_message"),
                turns = payload.firstInt("turns", "current_turn"),
            )
        }
    }
}
