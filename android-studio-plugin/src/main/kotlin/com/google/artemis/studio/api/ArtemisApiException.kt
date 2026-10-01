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

/** Base class for all ARTEMIS server communication failures. */
open class ArtemisApiException(message: String, cause: Throwable? = null) :
    RuntimeException(message, cause)

/** The server could not be reached at all (connection refused, timeout, DNS, ...). */
class ArtemisServerUnreachableException(message: String, cause: Throwable? = null) :
    ArtemisApiException(message, cause)

/** The server answered with a non-2xx status code. */
open class ArtemisHttpException(
    val statusCode: Int,
    val path: String,
    responseBody: String?,
) : ArtemisApiException(
    buildString {
        append("HTTP ").append(statusCode).append(" from ").append(path)
        val snippet = responseBody?.trim()?.take(200)
        if (!snippet.isNullOrEmpty()) append(": ").append(snippet)
    }
)

/** The requested resource was not found (HTTP 404). */
class ArtemisNotFoundException(path: String, responseBody: String?) :
    ArtemisHttpException(404, path, responseBody)

/** The scheduler refused to admit a task (status == "rejected" or an empty task list). */
class ArtemisTaskRejectedException(detail: String) :
    ArtemisApiException("Artemis host rejected the task: $detail")
