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

import com.google.gson.Gson
import com.google.gson.JsonObject
import java.util.UUID

/** Verification levels accepted by /api/run (Pro-profile Checker presets). */
val VERIFICATION_LEVELS: Set<String> = setOf("off", "final", "checkpoints", "strict")

/** Explorer perception versions accepted by /api/run (Pro-only tuning knob). */
val EXPLORER_MODES: Set<String> = setOf("flash", "pro", "ultra")

/**
 * Payload builder for POST /api/run.
 * Mirrors ArtemisClient.submit() in packages/artemis-client (client.py):
 * required keys are goal/profile/session_id/ingress; every other key is only
 * sent when set.
 */
data class RunRequest(
    val goal: String,
    val profile: String,
    val deviceSerial: String? = null,
    val expectedOutput: String? = null,
    val enableOutputter: Boolean? = null,
    val lockedAppPackage: String? = null,
    val appPath: String? = null,
    val conversationId: String? = null,
    val taskId: String? = null,
    val verificationLevel: String? = null,
    val explorerMode: String? = null,
    val options: Map<String, Any?>? = null,
    val ingress: String = DEFAULT_INGRESS,
) {
    init {
        require(goal.isNotBlank()) { "goal must not be empty" }
        require(profile in setOf("flash", "pro")) { "profile must be flash or pro; got $profile" }
        verificationLevel?.let {
            require(it in VERIFICATION_LEVELS) {
                "verification_level must be one of ${VERIFICATION_LEVELS.joinToString()}; got $it"
            }
        }
        explorerMode?.let {
            require(it in EXPLORER_MODES) {
                "explorer_mode must be one of ${EXPLORER_MODES.joinToString()}; got $it"
            }
        }
    }

    /** Client-generated idempotency key sent as the legacy session_id. */
    val resolvedTaskId: String = taskId?.let {
        try {
            UUID.fromString(it).toString()
        } catch (e: IllegalArgumentException) {
            throw IllegalArgumentException("task_id must be a valid UUID string", e)
        }
    } ?: UUID.randomUUID().toString()

    fun toJsonObject(): JsonObject {
        val payload = JsonObject()
        payload.addProperty("goal", goal.trim())
        payload.addProperty("profile", profile)
        payload.addProperty("session_id", resolvedTaskId)
        payload.addProperty("ingress", ingress)
        deviceSerial?.let { payload.addProperty("device_serial", it) }
        expectedOutput?.let { payload.addProperty("expected_output", it) }
        enableOutputter?.let { payload.addProperty("enable_outputter", it) }
        lockedAppPackage?.let { payload.addProperty("locked_app_package", it) }
        appPath?.let { payload.addProperty("app_path", it) }
        conversationId?.let { payload.addProperty("conversation_id", it) }
        verificationLevel?.let { payload.addProperty("verification_level", it) }
        explorerMode?.let { payload.addProperty("explorer_mode", it) }
        options?.let { payload.add("options", GSON.toJsonTree(it)) }
        return payload
    }

    fun toJson(): String = toJsonObject().toString()

    companion object {
        const val DEFAULT_INGRESS = "android_studio_plugin"
        private val GSON = Gson()

        /** Strips + lower-cases an enumerated option, returning null for blank input. */
        fun normalizeChoice(value: String?, name: String, choices: Set<String>): String? {
            if (value == null) return null
            val normalized = value.trim().lowercase()
            if (normalized.isEmpty()) return null
            require(normalized in choices) {
                "$name must be one of ${choices.joinToString()}; got $value"
            }
            return normalized
        }
    }
}
