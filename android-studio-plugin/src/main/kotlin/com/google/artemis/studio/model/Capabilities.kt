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
 * Features advertised by an ARTEMIS host.
 * Mirrors the Capabilities dataclass in packages/artemis-client (models.py).
 */
data class Capabilities(
    val apiVersion: String,
    val features: Set<String>,
    val serverVersion: String? = null,
) {
    fun supports(feature: String): Boolean = feature in features

    companion object {
        /** Baseline assumed for legacy servers without /api/v1/capabilities. */
        val LEGACY_FEATURES: Set<String> = setOf(
            "tasks.submit",
            "tasks.get",
            "tasks.stop",
            "devices.list",
            "system.readiness",
        )

        fun fromPayload(payload: JsonObject): Capabilities {
            val rawFeatures = payload.get("features")
            val features: Set<String> = when {
                rawFeatures == null || rawFeatures.isJsonNull -> emptySet()
                rawFeatures.isJsonObject -> rawFeatures.asJsonObject.entrySet()
                    .filter { runCatching { it.value.asBoolean }.getOrDefault(false) }
                    .map { it.key }
                    .toSet()
                rawFeatures.isJsonArray -> rawFeatures.asJsonArray
                    .mapNotNull { runCatching { it.asString }.getOrNull() }
                    .toSet()
                else -> emptySet()
            }
            return Capabilities(
                apiVersion = payload.firstString("api_version") ?: "unknown",
                serverVersion = payload.firstString("server_version", "version"),
                features = features,
            )
        }
    }
}
