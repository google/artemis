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

package com.google.artemis.studio.services

import com.google.artemis.studio.model.TaskResult
import com.intellij.notification.NotificationGroupManager
import com.intellij.notification.NotificationType
import com.intellij.openapi.project.Project

/** Balloon notifications for ARTEMIS task lifecycle events. */
object ArtemisNotifications {
    const val GROUP_ID = "ARTEMIS_TASKS"

    fun notifyTaskFinished(project: Project, result: TaskResult) {
        val type: NotificationType
        val title: String
        when {
            result.succeeded -> {
                type = NotificationType.INFORMATION
                title = "ARTEMIS task completed"
            }
            result.status == "cancelled" || result.status == "canceled" -> {
                type = NotificationType.WARNING
                title = "ARTEMIS task cancelled"
            }
            else -> {
                type = NotificationType.ERROR
                title = "ARTEMIS task failed"
            }
        }
        val content = buildString {
            append(result.goal ?: result.taskId)
            result.outputSummary?.let { append(" — ").append(it.take(200)) }
            result.error?.let { append(" — ").append(it.take(200)) }
        }
        NotificationGroupManager.getInstance()
            .getNotificationGroup(GROUP_ID)
            .createNotification(title, content, type)
            .notify(project)
    }

    fun notifyError(project: Project, title: String, detail: String) {
        NotificationGroupManager.getInstance()
            .getNotificationGroup(GROUP_ID)
            .createNotification(title, detail.take(300), NotificationType.ERROR)
            .notify(project)
    }
}
