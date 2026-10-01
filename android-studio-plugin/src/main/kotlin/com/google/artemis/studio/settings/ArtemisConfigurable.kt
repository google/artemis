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

package com.google.artemis.studio.settings

import com.intellij.openapi.options.Configurable
import com.intellij.ui.components.JBTextField
import com.intellij.util.ui.FormBuilder
import javax.swing.JComboBox
import javax.swing.JComponent
import javax.swing.JPanel

/** Settings page registered under Settings → Tools → ARTEMIS. */
class ArtemisConfigurable : Configurable {

    private val serverUrlField = JBTextField()
    private val profileCombo = JComboBox(arrayOf("flash", "pro"))
    private val deviceSerialField = JBTextField()
    private var panel: JPanel? = null

    override fun getDisplayName(): String = "ARTEMIS"

    override fun createComponent(): JComponent {
        val form = FormBuilder.createFormBuilder()
            .addLabeledComponent("Server base URL:", serverUrlField)
            .addTooltip("Base URL of a running ARTEMIS server, e.g. http://localhost:8000")
            .addLabeledComponent("Default profile:", profileCombo)
            .addTooltip("Agent profile used for new tasks: flash (fast) or pro (thorough)")
            .addLabeledComponent("Default device serial:", deviceSerialField)
            .addTooltip("Optional adb serial used when no device is selected in the tool window")
            .addComponentFillVertically(JPanel(), 0)
            .panel
        panel = form
        reset()
        return form
    }

    override fun isModified(): Boolean {
        val settings = ArtemisSettings.getInstance()
        return serverUrlField.text.trim().trimEnd('/') != settings.serverUrl ||
            profileCombo.selectedItem as? String != settings.defaultProfile ||
            deviceSerialField.text.trim() != settings.defaultDeviceSerial
    }

    override fun apply() {
        val settings = ArtemisSettings.getInstance()
        settings.serverUrl = serverUrlField.text
        settings.defaultProfile = profileCombo.selectedItem as? String ?: ArtemisSettings.DEFAULT_PROFILE
        settings.defaultDeviceSerial = deviceSerialField.text
    }

    override fun reset() {
        val settings = ArtemisSettings.getInstance()
        serverUrlField.text = settings.serverUrl
        profileCombo.selectedItem = settings.defaultProfile
        deviceSerialField.text = settings.defaultDeviceSerial
    }

    override fun disposeUIResources() {
        panel = null
    }
}
