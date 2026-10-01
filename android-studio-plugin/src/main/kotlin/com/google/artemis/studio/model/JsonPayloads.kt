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
 * Task statuses considered terminal by the ARTEMIS client models.
 * Mirrors TERMINAL_TASK_STATUSES in packages/artemis-client (models.py).
 */
val TERMINAL_TASK_STATUSES: Set<String> =
    setOf("completed", "success", "failed", "cancelled", "canceled", "rejected")

/** Mirrors SUCCESS_TASK_STATUSES in packages/artemis-client (models.py). */
val SUCCESS_TASK_STATUSES: Set<String> = setOf("completed", "success")

internal fun JsonObject.firstString(vararg keys: String): String? {
    for (key in keys) {
        val element = get(key) ?: continue
        if (element.isJsonNull) continue
        val text = runCatching { element.asString }.getOrNull()?.trim()
        if (!text.isNullOrEmpty()) return text
    }
    return null
}

internal fun JsonObject.firstInt(vararg keys: String): Int? {
    for (key in keys) {
        val element = get(key) ?: continue
        if (element.isJsonNull) continue
        val value = runCatching { element.asInt }.getOrNull() ?: continue
        return value
    }
    return null
}

internal fun JsonObject.firstElement(vararg keys: String): JsonElement? {
    for (key in keys) {
        val element = get(key) ?: continue
        if (!element.isJsonNull) return element
    }
    return null
}

/** Resolves a device serial from the many shapes the ARTEMIS server can emit. */
internal fun deviceSerialFromPayload(payload: JsonObject): String? {
    payload.firstString("device_serial", "device_id")?.let { return it }
    var info = payload.get("device_info") ?: return null
    // models.py also tolerates device_info arriving as a JSON-encoded string.
    if (info.isJsonPrimitive && info.asJsonPrimitive.isString) {
        info = try {
            com.google.gson.JsonParser.parseString(info.asString)
        } catch (e: Exception) {
            return null
        }
    }
    if (info.isJsonObject) {
        return info.asJsonObject.firstString("device_serial", "device_id")
    }
    return null
}
