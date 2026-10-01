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

import com.google.artemis.studio.api.ArtemisApiException
import com.google.gson.JsonObject

/**
 * A device reported by the remote ARTEMIS host.
 * Mirrors the Device dataclass in packages/artemis-client (models.py).
 */
data class Device(
    val serial: String,
    val state: String,
    val model: String? = null,
    val product: String? = null,
    val busy: Boolean = false,
) {
    companion object {
        private val BUSY_STATES = setOf("busy", "running", "locked")

        fun fromPayload(payload: JsonObject): Device {
            val serial = payload.firstString("serial", "device_serial", "device_id")
                ?: throw ArtemisProtocolException("Device response did not contain a serial number")
            val state = (payload.firstString("state", "status") ?: "unknown").lowercase()
            val busyFlag = payload.firstElement("busy", "is_busy")
                ?.let { runCatching { it.asBoolean }.getOrNull() } ?: false
            return Device(
                serial = serial,
                state = state,
                model = payload.firstString("model"),
                product = payload.firstString("product"),
                busy = busyFlag || state in BUSY_STATES,
            )
        }
    }

    override fun toString(): String = buildString {
        append(serial)
        append(" (").append(state)
        if (busy) append(", busy")
        append(")")
    }
}

/**
 * Thrown when the server response does not match the documented ARTEMIS payload
 * shapes. Extends [ArtemisApiException] so malformed-payload failures are
 * surfaced as readable UI errors by the same catch blocks that handle HTTP
 * failures, instead of bubbling into the IDE error reporter.
 */
class ArtemisProtocolException(message: String) : ArtemisApiException(message)
