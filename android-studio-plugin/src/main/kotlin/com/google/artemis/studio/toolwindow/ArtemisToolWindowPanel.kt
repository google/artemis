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

package com.google.artemis.studio.toolwindow

import com.google.artemis.studio.api.ArtemisApiClient
import com.google.artemis.studio.api.ArtemisApiException
import com.google.artemis.studio.api.ArtemisTaskRejectedException
import com.google.artemis.studio.model.Device
import com.google.artemis.studio.model.RunRequest
import com.google.artemis.studio.services.ArtemisNotifications
import com.google.artemis.studio.services.TaskPollingService
import com.google.artemis.studio.settings.ArtemisSettings
import com.intellij.icons.AllIcons
import com.intellij.openapi.Disposable
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ModalityState
import com.intellij.openapi.project.Project
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBList
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.components.JBTextArea
import com.intellij.util.ui.JBUI
import java.awt.BorderLayout
import java.awt.GridBagConstraints
import java.awt.GridBagLayout
import javax.swing.DefaultListModel
import javax.swing.JButton
import javax.swing.JComboBox
import javax.swing.JPanel
import javax.swing.JScrollPane
import javax.swing.ListSelectionModel
import javax.swing.event.DocumentEvent
import javax.swing.event.DocumentListener

/**
 * Main panel of the ARTEMIS tool window: server status, device picker, prompt
 * submission, and the session task list with live status and a detail pane.
 *
 * All network calls run on pooled background threads; Swing state is only
 * touched on the EDT.
 */
class ArtemisToolWindowPanel(private val project: Project) :
    JPanel(BorderLayout()), TaskPollingService.Listener, Disposable {

    /** Combo entry: either a concrete device or "use the configured default". */
    private class DeviceChoice(val device: Device?) {
        override fun toString(): String = device?.toString() ?: "Default (from settings)"
    }

    private val service: TaskPollingService = TaskPollingService.getInstance(project)

    private val statusLabel = JBLabel("Checking ARTEMIS server…")
    private val refreshButton = JButton("Refresh", AllIcons.Actions.Refresh)
    private val deviceCombo = JComboBox<DeviceChoice>()
    private val promptArea = JBTextArea(4, 24)
    private val profileCombo = JComboBox(arrayOf("flash", "pro"))
    private val runButton = JButton("Run Task", AllIcons.Actions.Execute)
    private val taskListModel = DefaultListModel<TaskPollingService.TrackedTask>()
    private val taskList = JBList(taskListModel)
    private val detailArea = JBTextArea(8, 24)
    private val stopButton = JButton("Stop", AllIcons.Actions.Suspend)

    private var serverReachable = false

    init {
        border = JBUI.Borders.empty(8)

        promptArea.lineWrap = true
        promptArea.wrapStyleWord = true
        promptArea.emptyText.text = "Describe the on-device task, e.g. \"Open Settings and enable dark mode\""
        detailArea.isEditable = false
        detailArea.lineWrap = true
        detailArea.wrapStyleWord = true
        taskList.selectionMode = ListSelectionModel.SINGLE_SELECTION
        stopButton.isEnabled = false

        layoutComponents()
        wireActions()

        service.addListener(this)
        rebuildTaskList()
        updateRunButtonState()
        // Initial server/device discovery, off the EDT.
        service.pollOnceAsync()
        service.refreshDevices()
    }

    private fun layoutComponents() {
        val content = JPanel(GridBagLayout())
        val gc = GridBagConstraints().apply {
            gridx = 0
            gridy = 0
            fill = GridBagConstraints.HORIZONTAL
            weightx = 1.0
            insets = JBUI.insets(2)
        }

        val statusRow = JPanel(BorderLayout(JBUI.scale(4), 0))
        statusRow.add(statusLabel, BorderLayout.CENTER)
        statusRow.add(refreshButton, BorderLayout.EAST)
        content.add(statusRow, gc)

        val deviceRow = JPanel(BorderLayout(JBUI.scale(4), 0))
        deviceRow.add(JBLabel("Device:"), BorderLayout.WEST)
        deviceRow.add(deviceCombo, BorderLayout.CENTER)
        gc.gridy++
        content.add(deviceRow, gc)

        gc.gridy++
        content.add(JBLabel("Prompt:"), gc)

        gc.gridy++
        content.add(JBScrollPane(promptArea), gc)

        val runRow = JPanel(BorderLayout(JBUI.scale(4), 0))
        val profileRow = JPanel(BorderLayout(JBUI.scale(4), 0))
        profileRow.add(JBLabel("Profile:"), BorderLayout.WEST)
        profileRow.add(profileCombo, BorderLayout.CENTER)
        runRow.add(profileRow, BorderLayout.CENTER)
        runRow.add(runButton, BorderLayout.EAST)
        gc.gridy++
        content.add(runRow, gc)

        gc.gridy++
        content.add(JBLabel("Tasks (this session):"), gc)

        gc.gridy++
        gc.weighty = 0.6
        gc.fill = GridBagConstraints.BOTH
        content.add(JBScrollPane(taskList).apply { preferredSize = JBUI.size(220, 120) }, gc)

        gc.gridy++
        gc.weighty = 0.0
        gc.fill = GridBagConstraints.HORIZONTAL
        content.add(JBLabel("Task details:"), gc)

        gc.gridy++
        gc.weighty = 0.4
        gc.fill = GridBagConstraints.BOTH
        val detailScroll: JScrollPane = JBScrollPane(detailArea)
        content.add(detailScroll, gc)

        gc.gridy++
        gc.weighty = 0.0
        gc.fill = GridBagConstraints.HORIZONTAL
        gc.anchor = GridBagConstraints.EAST
        content.add(stopButton, gc)

        add(content, BorderLayout.CENTER)
    }

    private fun wireActions() {
        refreshButton.addActionListener {
            service.pollOnceAsync()
            service.refreshDevices()
        }

        runButton.addActionListener { submitPrompt() }

        stopButton.addActionListener {
            val selected = taskList.selectedValue ?: return@addActionListener
            stopButton.isEnabled = false
            service.requestStop(selected.taskId)
        }

        taskList.addListSelectionListener {
            if (!it.valueIsAdjusting) updateDetailPane()
        }

        promptArea.document.addDocumentListener(object : DocumentListener {
            override fun insertUpdate(e: DocumentEvent) = updateRunButtonState()
            override fun removeUpdate(e: DocumentEvent) = updateRunButtonState()
            override fun changedUpdate(e: DocumentEvent) = updateRunButtonState()
        })

        // Seed defaults from settings.
        profileCombo.selectedItem = ArtemisSettings.getInstance().defaultProfile
        deviceCombo.addItem(DeviceChoice(null))
    }

    private fun submitPrompt() {
        val goal = promptArea.text.trim()
        if (goal.isEmpty()) return
        val profile = profileCombo.selectedItem as? String ?: ArtemisSettings.DEFAULT_PROFILE
        val selectedDevice = (deviceCombo.selectedItem as? DeviceChoice)?.device?.serial
            ?: ArtemisSettings.getInstance().defaultDeviceSerial.ifBlank { null }

        val request = try {
            RunRequest(
                goal = goal,
                profile = profile,
                deviceSerial = selectedDevice,
            )
        } catch (e: IllegalArgumentException) {
            ArtemisNotifications.notifyError(project, "Invalid ARTEMIS request", e.message ?: "")
            return
        }

        runButton.isEnabled = false
        val client = ArtemisApiClient(ArtemisSettings.getInstance().serverUrl)
        ApplicationManager.getApplication().executeOnPooledThread {
            try {
                val handle = client.submit(request)
                onEdt {
                    service.track(handle, request.goal.trim(), request.profile)
                    promptArea.text = ""
                }
            } catch (e: ArtemisTaskRejectedException) {
                onEdt {
                    ArtemisNotifications.notifyError(project, "ARTEMIS task rejected", e.message ?: "")
                }
            } catch (e: ArtemisApiException) {
                onEdt {
                    ArtemisNotifications.notifyError(project, "ARTEMIS submission failed", e.message ?: "")
                }
            } finally {
                onEdt { updateRunButtonState() }
            }
        }
    }

    // ---- TaskPollingService.Listener (always called on the EDT) ----

    override fun onTasksUpdated() {
        rebuildTaskList()
        updateDetailPane()
    }

    override fun onServerStatusChanged(reachable: Boolean, detail: String) {
        serverReachable = reachable
        if (reachable) {
            statusLabel.text = "Server: reachable"
            statusLabel.icon = AllIcons.General.InspectionsOK
            statusLabel.toolTipText = null
        } else {
            statusLabel.text = "Server: unreachable"
            statusLabel.icon = AllIcons.General.Error
            statusLabel.toolTipText = detail
        }
        updateRunButtonState()
    }

    override fun onDevicesChanged(devices: List<Device>, error: String?) {
        val previousSerial = (deviceCombo.selectedItem as? DeviceChoice)?.device?.serial
        deviceCombo.removeAllItems()
        deviceCombo.addItem(DeviceChoice(null))
        val defaultSerial = ArtemisSettings.getInstance().defaultDeviceSerial
        var toSelect = 0
        devices.forEachIndexed { index, device ->
            deviceCombo.addItem(DeviceChoice(device))
            val wanted = previousSerial ?: defaultSerial
            if (wanted.isNotBlank() && device.serial == wanted) toSelect = index + 1
        }
        deviceCombo.selectedIndex = toSelect
        if (error != null) {
            statusLabel.toolTipText = "Device refresh failed: $error"
        }
    }

    // ---- Internals (EDT only) ----

    private fun rebuildTaskList() {
        val selectedId = taskList.selectedValue?.taskId
        taskListModel.clear()
        var reselect = -1
        service.trackedTasks().forEachIndexed { index, task ->
            taskListModel.addElement(task)
            if (task.taskId == selectedId) reselect = index
        }
        if (reselect >= 0) {
            taskList.selectedIndex = reselect
        } else if (taskListModel.size() > 0 && selectedId == null) {
            taskList.selectedIndex = taskListModel.size() - 1
        }
    }

    private fun updateDetailPane() {
        val task = taskList.selectedValue
        if (task == null) {
            detailArea.text = "No task selected."
            stopButton.isEnabled = false
            return
        }
        val result = task.latest
        detailArea.text = buildString {
            append("Task: ").append(task.taskId).append('\n')
            append("Goal: ").append(task.goal).append('\n')
            append("Profile: ").append(task.profile).append('\n')
            append("Device: ").append(task.deviceSerial ?: "server default").append('\n')
            append("Status: ").append(task.status).append('\n')
            result?.turns?.let { append("Turns: ").append(it).append('\n') }
            result?.outputSummary?.let { append("Result: ").append(it).append('\n') }
            result?.error?.let { append("Error: ").append(it).append('\n') }
        }
        detailArea.caretPosition = 0
        stopButton.isEnabled = !task.done
    }

    private fun updateRunButtonState() {
        runButton.isEnabled = serverReachable && promptArea.text.isNotBlank()
    }

    private fun onEdt(action: () -> Unit) {
        ApplicationManager.getApplication().invokeLater(
            { if (!project.isDisposed) action() },
            ModalityState.any(),
            project.disposed,
        )
    }

    override fun dispose() {
        service.removeListener(this)
    }
}
