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

import java.util.UUID
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.assertThrows

/**
 * POST /api/run payload building tests, mirroring the submit() contract in
 * packages/artemis-client (client.py).
 */
class RunRequestTest {

    @Test
    fun `payload contains required fields and idempotent session id`() {
        // Mirrors test_submit_sends_idempotent_session_id.
        val taskId = "00000000-0000-4000-8000-000000000123"
        val payload = RunRequest(
            goal = "Open Settings",
            profile = "flash",
            taskId = taskId,
            deviceSerial = "pixel-8",
            options = mapOf("record_video" to true),
        ).toJsonObject()

        assertEquals("Open Settings", payload.get("goal").asString)
        assertEquals("flash", payload.get("profile").asString)
        assertEquals(taskId, payload.get("session_id").asString)
        assertEquals("android_studio_plugin", payload.get("ingress").asString)
        assertEquals("pixel-8", payload.get("device_serial").asString)
        assertTrue(payload.getAsJsonObject("options").get("record_video").asBoolean)
    }

    @Test
    fun `session id is generated as uuid when omitted`() {
        val request = RunRequest(goal = "Open Settings", profile = "flash")
        UUID.fromString(request.resolvedTaskId) // throws unless valid
        assertEquals(request.resolvedTaskId, request.toJsonObject().get("session_id").asString)
    }

    @Test
    fun `normalized pro tuning knobs are forwarded`() {
        // Mirrors test_submit_forwards_pro_tuning_knobs_normalised.
        val payload = RunRequest(
            goal = "Audit checkout",
            profile = "pro",
            verificationLevel = RunRequest.normalizeChoice(" Strict ", "verification_level", VERIFICATION_LEVELS),
            explorerMode = RunRequest.normalizeChoice("ULTRA", "explorer_mode", EXPLORER_MODES),
        ).toJsonObject()
        assertEquals("strict", payload.get("verification_level").asString)
        assertEquals("ultra", payload.get("explorer_mode").asString)
    }

    @Test
    fun `pro tuning knobs are omitted when unset or blank`() {
        // Mirrors test_submit_omits_pro_tuning_knobs_when_unset.
        val payload = RunRequest(
            goal = "Open Settings",
            profile = "flash",
            verificationLevel = RunRequest.normalizeChoice(null, "verification_level", VERIFICATION_LEVELS),
            explorerMode = RunRequest.normalizeChoice("  ", "explorer_mode", EXPLORER_MODES),
        ).toJsonObject()
        assertFalse(payload.has("verification_level"))
        assertFalse(payload.has("explorer_mode"))
    }

    @Test
    fun `unknown tuning values are rejected before any request`() {
        // Mirrors test_submit_rejects_unknown_pro_tuning_values_before_any_request.
        assertThrows<IllegalArgumentException> {
            RunRequest.normalizeChoice("paranoid", "verification_level", VERIFICATION_LEVELS)
        }
        assertThrows<IllegalArgumentException> {
            RunRequest.normalizeChoice("turbo", "explorer_mode", EXPLORER_MODES)
        }
        assertThrows<IllegalArgumentException> {
            RunRequest(goal = "x", profile = "pro", verificationLevel = "paranoid")
        }
    }

    @Test
    fun `blank goal and non-uuid task id are rejected`() {
        assertThrows<IllegalArgumentException> { RunRequest(goal = "  ", profile = "flash") }
        // Mirrors test_submit_rejects_non_uuid_task_id.
        val error = assertThrows<IllegalArgumentException> {
            RunRequest(goal = "Open Settings", profile = "flash", taskId = "not-a-uuid")
        }
        assertTrue(error.message!!.contains("valid UUID"))
    }

    @Test
    fun `optional fields are omitted when null`() {
        val payload = RunRequest(goal = "Open Settings", profile = "pro").toJsonObject()
        for (key in listOf(
            "device_serial", "expected_output", "enable_outputter", "locked_app_package",
            "app_path", "conversation_id", "verification_level", "explorer_mode", "options",
        )) {
            assertFalse(payload.has(key), "unexpected key: $key")
        }
    }

    @Test
    fun `optional fields are serialized when set`() {
        val payload = RunRequest(
            goal = "Install the app",
            profile = "pro",
            expectedOutput = "App installed",
            enableOutputter = true,
            lockedAppPackage = "com.example.app",
            appPath = "/tmp/app.apk",
            conversationId = "conv-1",
        ).toJsonObject()
        assertEquals("App installed", payload.get("expected_output").asString)
        assertTrue(payload.get("enable_outputter").asBoolean)
        assertEquals("com.example.app", payload.get("locked_app_package").asString)
        assertEquals("/tmp/app.apk", payload.get("app_path").asString)
        assertEquals("conv-1", payload.get("conversation_id").asString)
    }
}
