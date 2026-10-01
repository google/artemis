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

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.components.PersistentStateComponent
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.State
import com.intellij.openapi.components.Storage

/** Application-level persisted settings for the ARTEMIS thin client. */
@Service(Service.Level.APP)
@State(name = "ArtemisSettings", storages = [Storage("artemis.xml")])
class ArtemisSettings : PersistentStateComponent<ArtemisSettings.State> {

    class State {
        var serverUrl: String = DEFAULT_SERVER_URL
        var defaultProfile: String = DEFAULT_PROFILE
        var defaultDeviceSerial: String = ""
    }

    private var state = State()

    override fun getState(): State = state

    override fun loadState(state: State) {
        this.state = state
    }

    var serverUrl: String
        get() = state.serverUrl.ifBlank { DEFAULT_SERVER_URL }
        set(value) {
            state.serverUrl = value.trim().trimEnd('/')
        }

    var defaultProfile: String
        get() = state.defaultProfile.ifBlank { DEFAULT_PROFILE }
        set(value) {
            state.defaultProfile = value
        }

    var defaultDeviceSerial: String
        get() = state.defaultDeviceSerial
        set(value) {
            state.defaultDeviceSerial = value.trim()
        }

    companion object {
        const val DEFAULT_SERVER_URL = "http://localhost:8000"
        const val DEFAULT_PROFILE = "flash"

        fun getInstance(): ArtemisSettings =
            ApplicationManager.getApplication().getService(ArtemisSettings::class.java)
    }
}
