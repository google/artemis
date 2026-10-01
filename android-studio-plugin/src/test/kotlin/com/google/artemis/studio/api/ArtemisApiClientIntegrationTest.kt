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

package com.google.artemis.studio.api

import com.google.artemis.studio.model.RunRequest
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import java.net.InetSocketAddress
import java.nio.charset.StandardCharsets
import java.util.UUID
import java.util.concurrent.CopyOnWriteArrayList
import org.junit.jupiter.api.AfterEach
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.assertThrows

/**
 * Integration tests of [ArtemisApiClient] against the JDK's built-in
 * [HttpServer], returning canned responses that mirror the fixtures in
 * packages/artemis-client/tests/test_client.py.
 */
class ArtemisApiClientIntegrationTest {

    private data class RecordedRequest(val method: String, val path: String, val body: String)

    private lateinit var server: HttpServer
    private lateinit var client: ArtemisApiClient
    private val recorded = CopyOnWriteArrayList<RecordedRequest>()

    @BeforeEach
    fun setUp() {
        server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.start()
        client = ArtemisApiClient("http://127.0.0.1:${server.address.port}")
    }

    @AfterEach
    fun tearDown() {
        server.stop(0)
    }

    private fun respond(path: String, bodyProvider: (RecordedRequest) -> Pair<Int, String>) {
        server.createContext(path) { exchange: HttpExchange ->
            val requestBody = exchange.requestBody.readBytes().toString(StandardCharsets.UTF_8)
            val recordedRequest =
                RecordedRequest(exchange.requestMethod, exchange.requestURI.path, requestBody)
            recorded.add(recordedRequest)
            val (code, body) = bodyProvider(recordedRequest)
            val bytes = body.toByteArray(StandardCharsets.UTF_8)
            exchange.responseHeaders.add("Content-Type", "application/json")
            exchange.sendResponseHeaders(code, bytes.size.toLong())
            exchange.responseBody.use { it.write(bytes) }
        }
    }

    @Test
    fun `submit poll and stop happy path`() {
        val taskId = "00000000-0000-4000-8000-000000000123"
        respond("/api/run") {
            200 to """{"status": "started", "tasks": [{"session_id": "$taskId", "status": "pending", "device_serial": "pixel-8"}]}"""
        }
        respond("/api/sessions/$taskId") {
            200 to """{"session_id": "$taskId", "status": "completed", "goal": "Open Settings", "current_turn": 4, "summary": "Battery page opened"}"""
        }
        respond("/api/stop") { 200 to """{"status": "stopped"}""" }

        val request = RunRequest(
            goal = "Open Settings",
            profile = "flash",
            taskId = taskId,
            deviceSerial = "pixel-8",
            options = mapOf("record_video" to true),
        )
        val handle = client.submit(request)
        assertEquals(taskId, handle.taskId)
        assertEquals("pending", handle.status)
        assertEquals("pixel-8", handle.deviceSerial)

        // The wire payload must match the Python client's submit() shape.
        val runBody = recorded.single { it.path == "/api/run" }.body.asJson()
        assertEquals(taskId, runBody.get("session_id").asString)
        assertEquals("Open Settings", runBody.get("goal").asString)
        assertEquals("android_studio_plugin", runBody.get("ingress").asString)
        assertTrue(runBody.getAsJsonObject("options").get("record_video").asBoolean)

        val result = client.getTask(taskId)
        assertTrue(result.done)
        assertTrue(result.succeeded)
        assertEquals(4, result.turns)
        assertEquals("Battery page opened", result.output!!.asString)

        assertTrue(client.stop(taskId))
        val stopBody = recorded.single { it.path == "/api/stop" }.body.asJson()
        assertEquals(taskId, stopBody.get("session_id").asString)
    }

    @Test
    fun `rejected submission raises task rejected with server detail`() {
        // Mirrors test_submit_rejected_task_raises_specific_error.
        respond("/api/run") { 200 to """{"status": "rejected", "error": "Device is offline", "tasks": []}""" }
        val error = assertThrows<ArtemisTaskRejectedException> {
            client.submit(RunRequest(goal = "Open Settings", profile = "flash"))
        }
        assertTrue(error.message!!.contains("Device is offline"))
    }

    @Test
    fun `devices endpoint accepts legacy wrapped shape`() {
        // Mirrors test_list_devices_accepts_legacy_shape.
        respond("/api/devices") {
            200 to """{"devices": [{"serial": "emulator-5554", "state": "device", "model": "Pixel_8", "busy": true}]}"""
        }
        val devices = client.listDevices()
        assertEquals(1, devices.size)
        assertEquals("emulator-5554", devices[0].serial)
        assertTrue(devices[0].busy)
    }

    @Test
    fun `getTask falls back to scheduler queue then launching`() {
        // Mirrors test_run_finds_queued_task_then_reads_terminal_session (404 branch)
        // and test_get_task_returns_launching_when_not_visible_yet.
        val queuedId = "00000000-0000-4000-8000-000000000124"
        respond("/api/sessions/$queuedId") { 404 to """{"detail": "not created yet"}""" }
        respond("/api/sessions/new-task") { 404 to """{"detail": "missing"}""" }
        respond("/api/status") {
            200 to """{"status": "running", "queue": [{"session_id": "$queuedId", "status": "pending"}]}"""
        }

        val queued = client.getTask(queuedId)
        assertEquals("pending", queued.status)
        assertFalse(queued.done)

        val launching = client.getTask("new-task")
        assertEquals("launching", launching.status)
        assertFalse(launching.done)
    }

    @Test
    fun `capabilities fall back to legacy baseline on 404`() {
        respond("/api/v1/capabilities") { 404 to """{"detail": "not implemented"}""" }
        val caps = client.capabilities()
        assertEquals("legacy", caps.apiVersion)
        assertTrue(caps.supports("tasks.submit"))
        assertTrue(caps.supports("devices.list"))
    }

    @Test
    fun `http 500 surfaces as readable api exception`() {
        respond("/api/status") { 500 to """{"detail": "boom"}""" }
        val error = assertThrows<ArtemisHttpException> { client.health() }
        assertEquals(500, error.statusCode)
        assertTrue(error.message!!.contains("HTTP 500"))
        assertTrue(error.message!!.contains("/api/status"))
    }

    @Test
    fun `unreachable server surfaces as unreachable exception`() {
        server.stop(0)
        val error = assertThrows<ArtemisServerUnreachableException> { client.health() }
        assertTrue(error.message!!.contains("Cannot reach ARTEMIS server"))
    }

    @Test
    fun `invalid devices payload is rejected`() {
        // Mirrors test_invalid_devices_payload_is_rejected.
        respond("/api/devices") { 200 to """{"devices": "not-a-list"}""" }
        assertThrows<ArtemisApiException> { client.listDevices() }
    }

    @Test
    fun `devices endpoint treats missing devices key as empty list`() {
        // Mirrors client.py: payload.get("devices", []) yields [] when the key
        // is absent, but an explicit null remains a protocol error.
        respond("/api/devices") { 200 to """{}""" }
        assertTrue(client.listDevices().isEmpty())
    }

    @Test
    fun `submit injects client session id when server returns only id`() {
        // Matches client.py setdefault("session_id", resolved_task_id): the
        // client-generated idempotency ID wins over a bare server-side "id".
        respond("/api/run") { 200 to """{"status": "started", "tasks": [{"id": "server-internal-1"}]}""" }
        val handle = client.submit(RunRequest(goal = "Open Settings", profile = "flash"))
        // The handle must NOT adopt the server-internal id as the task id.
        assertTrue(handle.taskId != "server-internal-1")
        UUID.fromString(handle.taskId) // client-generated UUID
    }

    @Test
    fun `malformed admitted task entry raises api exception not runtime error`() {
        // The entry has an empty session_id, which firstString treats as absent;
        // TaskHandle parsing then fails with ArtemisProtocolException, which must
        // be an ArtemisApiException so UI catch blocks surface it as a readable
        // error instead of leaking into the IDE error reporter.
        respond("/api/run") { 200 to """{"status": "started", "tasks": [{"session_id": "  ", "status": "pending"}]}""" }
        assertThrows<ArtemisApiException> {
            client.submit(RunRequest(goal = "Open Settings", profile = "flash"))
        }
    }

    private fun String.asJson(): JsonObject = JsonParser.parseString(this).asJsonObject
}
