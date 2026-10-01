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

import com.google.artemis.studio.model.Capabilities
import com.google.artemis.studio.model.Device
import com.google.artemis.studio.model.RunRequest
import com.google.artemis.studio.model.TaskHandle
import com.google.artemis.studio.model.TaskResult
import com.google.gson.Gson
import com.google.gson.JsonElement
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import java.io.IOException
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.time.Duration

/**
 * Thin HTTP client for a running ARTEMIS server.
 *
 * Mirrors ArtemisClient in packages/artemis-client (client.py). All methods are
 * blocking and must be called off the Swing EDT (the plugin invokes them from
 * background coroutines). Every failure surfaces as an [ArtemisApiException]
 * subclass with a human-readable message; nothing is thrown raw onto the UI.
 */
class ArtemisApiClient(
    baseUrl: String,
    private val requestTimeout: Duration = Duration.ofSeconds(30),
    connectTimeout: Duration = Duration.ofSeconds(10),
) {
    private val baseUrl: String = baseUrl.trim().trimEnd('/')

    // Force HTTP/1.1: java.net.http defaults to HTTP/2 and attempts an h2c
    // upgrade on the first request. Uvicorn (the ARTEMIS server) does not
    // support cleartext upgrade and silently drops the POST body while
    // processing it, so FastAPI then fails validation with 422 "body: Field
    // required". Verified against a live server (2026-09-30).
    private val http: HttpClient = HttpClient.newBuilder()
        .version(HttpClient.Version.HTTP_1_1)
        .connectTimeout(connectTimeout)
        .build()
    private val gson = Gson()

    init {
        require(this.baseUrl.isNotEmpty()) { "ARTEMIS server URL must not be empty" }
    }

    /** Fast liveness check against the scheduler status API. */
    fun health(): JsonObject = requestObject("GET", "/api/status")

    /** Full device/toolchain readiness diagnostics (can be slow). */
    fun readiness(): JsonObject = requestObject("GET", "/api/system/readiness")

    /** Discover server features, falling back to the legacy baseline on HTTP 404. */
    fun capabilities(): Capabilities = try {
        Capabilities.fromPayload(requestObject("GET", "/api/v1/capabilities"))
    } catch (e: ArtemisNotFoundException) {
        Capabilities(apiVersion = "legacy", features = Capabilities.LEGACY_FEATURES)
    }

    /** List Android devices visible to the remote ARTEMIS host. */
    fun listDevices(): List<Device> {
        val payload = request("GET", "/api/devices")
        // Matches client.py: a bare array is accepted; for a wrapped payload a
        // missing "devices" key yields an empty list, while an explicit null or
        // a non-list value is a protocol error.
        val rawDevices: JsonElement? = when {
            payload == null || payload.isJsonNull -> null
            payload.isJsonObject -> {
                val obj = payload.asJsonObject
                if (!obj.has("devices")) return emptyList()
                obj.get("devices")
            }
            payload.isJsonArray -> payload
            else -> null
        }
        if (rawDevices == null || !rawDevices.isJsonArray) {
            throw ArtemisApiException("/api/devices response must contain a device list")
        }
        return rawDevices.asJsonArray.map { entry ->
            if (!entry.isJsonObject) {
                throw ArtemisApiException("/api/devices contained a non-object device entry")
            }
            Device.fromPayload(entry.asJsonObject)
        }
    }

    /**
     * Submit one task and return immediately after scheduler admission.
     * Throws [ArtemisTaskRejectedException] when the scheduler refuses the task.
     */
    fun submit(request: RunRequest): TaskHandle {
        val response = requestObject("POST", "/api/run", request.toJsonObject())
        val status = response.get("status")
            ?.takeUnless { it.isJsonNull }
            ?.let { runCatching { it.asString }.getOrNull() }
            ?.lowercase()
            ?: "unknown"
        val tasks = response.get("tasks")
        if (status == "rejected" || (tasks != null && tasks.isJsonArray && tasks.asJsonArray.size() == 0)) {
            val detail = response.get("error")
                ?.takeUnless { it.isJsonNull }
                ?.let { runCatching { it.asString }.getOrNull() }
                ?: "no reason given"
            throw ArtemisTaskRejectedException(detail)
        }
        if (tasks == null || !tasks.isJsonArray || tasks.asJsonArray.size() == 0 ||
            !tasks.asJsonArray[0].isJsonObject
        ) {
            throw ArtemisApiException("/api/run response did not contain an admitted task")
        }
        val taskPayload = tasks.asJsonArray[0].asJsonObject.deepCopy()
        // Match client.py's setdefault semantics exactly: the client-generated
        // idempotency ID is injected whenever the server omitted "session_id",
        // so it wins over a bare server-side "id" during TaskHandle parsing.
        if (!taskPayload.has("session_id")) {
            taskPayload.addProperty("session_id", request.resolvedTaskId)
        }
        if (!taskPayload.has("status")) {
            taskPayload.addProperty("status", status)
        }
        return TaskHandle.fromPayload(taskPayload)
    }

    /** Get a task from session storage, falling back to the live scheduler queue. */
    fun getTask(taskId: String): TaskResult {
        return try {
            TaskResult.fromPayload(requestObject("GET", "/api/sessions/$taskId"), taskId)
        } catch (e: ArtemisNotFoundException) {
            val scheduler = requestObject("GET", "/api/status")
            val liveTask = findLiveTask(scheduler, taskId)
            if (liveTask == null) {
                TaskResult(taskId = taskId, status = "launching")
            } else {
                TaskResult.fromPayload(liveTask, taskId)
            }
        }
    }

    /** Request cancellation of one remote task. Returns true when the server confirms. */
    fun stop(taskId: String): Boolean {
        val body = JsonObject().apply { addProperty("session_id", taskId) }
        val payload = requestObject("POST", "/api/stop", body)
        val status = payload.get("status")
            ?.takeUnless { it.isJsonNull }
            ?.let { runCatching { it.asString }.getOrNull() }
        return status?.lowercase() == "stopped"
    }

    private fun requestObject(method: String, path: String, body: JsonObject? = null): JsonObject {
        val payload = request(method, path, body)
        if (payload == null || !payload.isJsonObject) {
            throw ArtemisApiException("$path response must be a JSON object")
        }
        return payload.asJsonObject
    }

    private fun request(method: String, path: String, body: JsonObject? = null): JsonElement? {
        val builder = HttpRequest.newBuilder()
            .uri(URI.create(baseUrl + path))
            .timeout(requestTimeout)
        if (body == null) {
            builder.method(method, HttpRequest.BodyPublishers.noBody())
        } else {
            builder
                .header("Content-Type", "application/json")
                .method(method, HttpRequest.BodyPublishers.ofString(gson.toJson(body)))
        }
        val response: HttpResponse<String> = try {
            http.send(builder.build(), HttpResponse.BodyHandlers.ofString())
        } catch (e: IOException) {
            throw ArtemisServerUnreachableException(
                "Cannot reach ARTEMIS server at $baseUrl (${e.message ?: "connection failed"})", e
            )
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
            throw ArtemisServerUnreachableException("Request to $baseUrl$path was interrupted", e)
        } catch (e: IllegalArgumentException) {
            throw ArtemisApiException("Invalid ARTEMIS server URL: $baseUrl", e)
        }

        val responseBody = response.body()
        when {
            response.statusCode() == 404 -> throw ArtemisNotFoundException(path, responseBody)
            response.statusCode() !in 200..299 ->
                throw ArtemisHttpException(response.statusCode(), path, responseBody)
        }
        if (responseBody.isNullOrBlank()) return null
        return try {
            JsonParser.parseString(responseBody)
        } catch (e: Exception) {
            throw ArtemisApiException("$path returned invalid JSON: ${responseBody.take(200)}", e)
        }
    }

    companion object {
        /** Locate a task in the scheduler status payload (queue, active tasks, or top level). */
        internal fun findLiveTask(scheduler: JsonObject, taskId: String): JsonObject? {
            for (collectionName in listOf("queue", "active_tasks")) {
                val collection = scheduler.get(collectionName) ?: continue
                if (!collection.isJsonArray) continue
                for (item in collection.asJsonArray) {
                    if (!item.isJsonObject) continue
                    val itemId = item.asJsonObject.get("task_id") ?: item.asJsonObject.get("session_id")
                    if (itemId != null && !itemId.isJsonNull &&
                        runCatching { itemId.asString }.getOrNull() == taskId
                    ) {
                        return item.asJsonObject
                    }
                }
            }
            val activeId = scheduler.get("task_id") ?: scheduler.get("session_id")
            if (activeId != null && !activeId.isJsonNull &&
                runCatching { activeId.asString }.getOrNull() == taskId
            ) {
                val synthesized = scheduler.deepCopy()
                if (!synthesized.has("session_id")) synthesized.addProperty("session_id", taskId)
                return synthesized
            }
            return null
        }
    }
}
