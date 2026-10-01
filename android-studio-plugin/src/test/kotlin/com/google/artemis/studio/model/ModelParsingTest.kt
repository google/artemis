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

import com.google.gson.JsonParser
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/**
 * Parsing tests with canned payloads mirrored from
 * packages/artemis-client/tests/test_client.py.
 */
class ModelParsingTest {

    private fun parse(json: String) = JsonParser.parseString(json).asJsonObject

    @Test
    fun `device list legacy shape parses`() {
        // Mirrors test_list_devices_accepts_legacy_shape.
        val payload = parse(
            """{"serial": "emulator-5554", "state": "device", "model": "Pixel_8", "busy": true}"""
        )
        val device = Device.fromPayload(payload)
        assertEquals("emulator-5554", device.serial)
        assertEquals("device", device.state)
        assertEquals("Pixel_8", device.model)
        assertTrue(device.busy)
    }

    @Test
    fun `device busy is inferred from busy states`() {
        val device = Device.fromPayload(parse("""{"device_id": "pixel-8", "status": "RUNNING"}"""))
        assertEquals("pixel-8", device.serial)
        assertEquals("running", device.state)
        assertTrue(device.busy)
    }

    @Test
    fun `device without serial is rejected`() {
        val error = org.junit.jupiter.api.assertThrows<ArtemisProtocolException> {
            Device.fromPayload(parse("""{"state": "device"}"""))
        }
        assertTrue(error.message!!.contains("serial"))
    }

    @Test
    fun `task handle parses admission response entry`() {
        // Mirrors the /api/run task entry in test_submit_sends_idempotent_session_id.
        val taskId = "00000000-0000-4000-8000-000000000123"
        val handle = TaskHandle.fromPayload(
            parse("""{"session_id": "$taskId", "status": "PENDING", "device_serial": "pixel-8"}""")
        )
        assertEquals(taskId, handle.taskId)
        assertEquals("pending", handle.status)
        assertEquals("pixel-8", handle.deviceSerial)
        assertEquals(taskId, handle.sessionId)
    }

    @Test
    fun `task result parses terminal session payload`() {
        // Mirrors the terminal /api/sessions payload in
        // test_run_finds_queued_task_then_reads_terminal_session.
        val taskId = "00000000-0000-4000-8000-000000000124"
        val result = TaskResult.fromPayload(
            parse(
                """{
                  "session_id": "$taskId",
                  "status": "completed",
                  "goal": "Open Settings",
                  "current_turn": 4,
                  "summary": "Battery page opened"
                }"""
            )
        )
        assertTrue(result.done)
        assertTrue(result.succeeded)
        assertEquals(4, result.turns)
        assertEquals("Battery page opened", result.output!!.asString)
        assertEquals("Open Settings", result.goal)
    }

    @Test
    fun `task result falls back to alternative field names`() {
        val result = TaskResult.fromPayload(
            parse(
                """{
                  "trace_id": "abc-123",
                  "status": "FAILED",
                  "initial_goal": "Send a message",
                  "error_message": "device went offline",
                  "device_info": {"device_serial": "emu-1"}
                }"""
            )
        )
        assertEquals("abc-123", result.taskId)
        assertEquals("failed", result.status)
        assertTrue(result.done)
        assertFalse(result.succeeded)
        assertEquals("Send a message", result.goal)
        assertEquals("device went offline", result.error)
        assertEquals("emu-1", result.deviceSerial)
        assertNull(result.turns)
    }

    @Test
    fun `task result uses explicit task id fallback`() {
        val result = TaskResult.fromPayload(parse("""{"status": "launching"}"""), "new-task")
        assertEquals("new-task", result.taskId)
        assertEquals("launching", result.status)
        assertFalse(result.done)
    }

    @Test
    fun `device serial resolves from json-encoded device_info string`() {
        // Mirrors models.py _device_from_payload, which json.loads a string
        // device_info before reading device_serial from it.
        val result = TaskResult.fromPayload(
            parse(
                """{
                  "session_id": "abc-123",
                  "status": "running",
                  "device_info": "{\"device_serial\": \"emu-string-1\"}"
                }"""
            )
        )
        assertEquals("emu-string-1", result.deviceSerial)
    }

    @Test
    fun `protocol exception is an api exception subtype`() {
        // UI catch blocks only handle ArtemisApiException; malformed payloads
        // must not escape as an unrelated RuntimeException into the IDE.
        org.junit.jupiter.api.assertThrows<com.google.artemis.studio.api.ArtemisApiException> {
            Device.fromPayload(parse("""{"state": "device"}"""))
        }
    }

    @Test
    fun `capabilities parse list-shaped features`() {
        val caps = Capabilities.fromPayload(
            parse(
                """{
                  "api_version": "v1",
                  "server_version": "0.4.0",
                  "features": ["tasks.submit", "devices.list"]
                }"""
            )
        )
        assertEquals("v1", caps.apiVersion)
        assertEquals("0.4.0", caps.serverVersion)
        assertTrue(caps.supports("tasks.submit"))
        assertFalse(caps.supports("tasks.stop"))
    }

    @Test
    fun `capabilities parse map-shaped features keeping only enabled ones`() {
        val caps = Capabilities.fromPayload(
            parse(
                """{
                  "api_version": "v1",
                  "features": {"tasks.submit": true, "tasks.stop": false}
                }"""
            )
        )
        assertTrue(caps.supports("tasks.submit"))
        assertFalse(caps.supports("tasks.stop"))
        assertEquals("unknown", Capabilities.fromPayload(parse("""{}""")).apiVersion)
    }
}
