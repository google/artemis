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

import com.google.artemis.studio.api.ArtemisApiClient
import com.google.artemis.studio.api.ArtemisApiException
import com.google.artemis.studio.model.Device
import com.google.artemis.studio.model.TaskHandle
import com.google.artemis.studio.model.TaskResult
import com.google.artemis.studio.settings.ArtemisSettings
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ModalityState
import com.intellij.openapi.components.Service
import com.intellij.openapi.project.Project
import java.util.concurrent.CopyOnWriteArrayList
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * Project-level service that tracks this session's submitted ARTEMIS tasks and
 * polls the server for their status. All network I/O happens on background
 * coroutines; listener callbacks are always delivered on the Swing EDT.
 */
@Service(Service.Level.PROJECT)
class TaskPollingService(private val project: Project, private val scope: CoroutineScope) {

    /** One task submitted from this IDE session. */
    class TrackedTask(
        val taskId: String,
        val goal: String,
        val profile: String,
        val deviceSerial: String?,
        @Volatile var latest: TaskResult? = null,
    ) {
        val status: String get() = latest?.status ?: "queued"
        val done: Boolean get() = latest?.done ?: false

        override fun toString(): String = "[$status] $goal"
    }

    /** UI callbacks. Every method is invoked on the EDT. */
    interface Listener {
        /** The tracked-task list or one of its entries changed. */
        fun onTasksUpdated()

        /** Server reachability was determined for the first time or changed. */
        fun onServerStatusChanged(reachable: Boolean, detail: String)

        /** A device refresh completed; [error] is non-null on failure. */
        fun onDevicesChanged(devices: List<Device>, error: String?)
    }

    private val tracked = CopyOnWriteArrayList<TrackedTask>()
    private val listeners = CopyOnWriteArrayList<Listener>()
    private val pollLock = Any()
    private val pollCycleLock = Any()
    private var pollJob: Job? = null

    @Volatile
    var serverReachable: Boolean = false
        private set

    @Volatile
    private var serverStatusReported: Boolean = false

    fun trackedTasks(): List<TrackedTask> = tracked.toList()

    fun addListener(listener: Listener) {
        listeners.addIfAbsent(listener)
    }

    fun removeListener(listener: Listener) {
        listeners.remove(listener)
    }

    /** Register a freshly admitted task and start polling for its status. */
    fun track(handle: TaskHandle, goal: String, profile: String) {
        tracked.add(TrackedTask(handle.taskId, goal, profile, handle.deviceSerial))
        notifyTasksUpdated()
        startPolling()
        pollOnceAsync()
    }

    /** Ask the server to cancel a task, then refresh its status. */
    fun requestStop(taskId: String) {
        scope.launch(Dispatchers.IO) {
            try {
                newClient().stop(taskId)
            } catch (e: ArtemisApiException) {
                notifyEdt {
                    ArtemisNotifications.notifyError(project, "ARTEMIS stop failed", e.message ?: "")
                }
            }
            pollOnce()
        }
    }

    /** Fetch the device list once, off the EDT. */
    fun refreshDevices() {
        scope.launch(Dispatchers.IO) {
            try {
                val devices = newClient().listDevices()
                notifyEdt { listeners.forEach { it.onDevicesChanged(devices, null) } }
            } catch (e: ArtemisApiException) {
                notifyEdt {
                    listeners.forEach { it.onDevicesChanged(emptyList(), e.message ?: "request failed") }
                }
            }
        }
    }

    /**
     * Start the 2-second polling loop (idempotent). The loop exits on its own
     * once every tracked task is terminal, and is cancelled when the project
     * (and therefore this service's scope) is disposed.
     */
    fun startPolling() {
        synchronized(pollLock) {
            if (pollJob?.isActive == true) return
            pollJob = scope.launch(Dispatchers.IO) {
                while (isActive) {
                    pollOnce()
                    // Stop polling when nothing is left to poll. The check and
                    // the pollJob reset happen under pollLock so a concurrent
                    // track() + startPolling() either sees this job as still
                    // active (and the loop then sees the new task) or sees
                    // pollJob == null and starts a fresh loop.
                    val keepPolling = synchronized(pollLock) {
                        if (tracked.any { !it.done }) {
                            true
                        } else {
                            pollJob = null
                            false
                        }
                    }
                    if (!keepPolling) break
                    delay(POLL_INTERVAL_MS)
                }
            }
        }
    }

    /** Run one polling cycle in the background (health + active task statuses). */
    fun pollOnceAsync() {
        scope.launch(Dispatchers.IO) { pollOnce() }
    }

    // Serialized with pollCycleLock so the periodic loop, manual refreshes and
    // stop-follow-up polls never run concurrently; that keeps the
    // read-modify-write of TrackedTask.latest (and the "finished" notification
    // decision based on it) race-free.
    private fun pollOnce() = synchronized(pollCycleLock) {
        val client = newClient()
        var reachable = true
        var detail = "Connected to ${ArtemisSettings.getInstance().serverUrl}"
        try {
            client.health()
        } catch (e: ArtemisApiException) {
            reachable = false
            detail = e.message ?: "Server unreachable"
        }
        val changed = reachable != serverReachable
        serverReachable = reachable
        // Always report the first poll outcome (the UI starts in a "Checking…"
        // state), then only on transitions — repeating the same unreachable
        // state every cycle would just churn the EDT.
        if (changed || !serverStatusReported) {
            serverStatusReported = true
            val d = detail
            val r = reachable
            notifyEdt { listeners.forEach { it.onServerStatusChanged(r, d) } }
        }
        if (!reachable) return@synchronized

        var anyUpdated = false
        for (task in tracked) {
            if (task.done) continue
            try {
                val result = client.getTask(task.taskId)
                val wasDone = task.latest?.done ?: false
                task.latest = result
                anyUpdated = true
                if (result.done && !wasDone) {
                    notifyEdt { ArtemisNotifications.notifyTaskFinished(project, result) }
                }
            } catch (e: ArtemisApiException) {
                // Transient poll failures keep the previous state; the health check
                // above already reports a fully unreachable server.
                if (task.latest == null) {
                    task.latest = TaskResult(task.taskId, "unknown", goal = task.goal, error = e.message)
                    anyUpdated = true
                }
            }
        }
        if (anyUpdated) notifyTasksUpdated()
    }

    private fun notifyTasksUpdated() {
        notifyEdt { listeners.forEach { it.onTasksUpdated() } }
    }

    private fun newClient(): ArtemisApiClient =
        ArtemisApiClient(ArtemisSettings.getInstance().serverUrl)

    private fun notifyEdt(action: () -> Unit) {
        val application = ApplicationManager.getApplication() ?: return
        application.invokeLater(
            { if (!project.isDisposed) action() },
            ModalityState.any(),
            project.disposed,
        )
    }

    companion object {
        const val POLL_INTERVAL_MS = 2_000L

        fun getInstance(project: Project): TaskPollingService =
            project.getService(TaskPollingService::class.java)
    }
}
