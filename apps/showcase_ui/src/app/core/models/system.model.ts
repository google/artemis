/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

export type ProbeStatus = 'pass' | 'warn' | 'fail' | 'skipped';

export type ProbeCategory = 'device' | 'auth' | 'toolchain' | 'runtime';

export interface ProbeAction {
  action_type: 'command' | 'hint' | 'link';
  label: string;
  payload: string;
}

export interface DeviceInfo {
  serial: string;
  state: string;
  model: string | null;
  product: string | null;
  android_version: string | null;
  screen_resolution: string | null;
  is_emulator: boolean;
  installed_packages?: string[];
}

export interface ProbeResult {
  id: string;
  category: ProbeCategory;
  title: string;
  status: ProbeStatus;
  is_blocker: boolean;
  summary: string;
  description: string;
  metadata: Record<string, any>;
  actions: ProbeAction[];
}

export interface SystemReadinessReport {
  overall_ready: boolean;
  blocker_count: number;
  passed_blocker_count: number;
  probes: ProbeResult[];
  active_device: DeviceInfo | null;
  os_type?: 'linux' | 'darwin' | 'windows' | string;
  timestamp: number;
}

export type EmulatorLaunchStage =
  | 'idle'
  | 'starting'
  | 'waiting_for_adb'
  | 'booting'
  | 'ready'
  | 'failed'
  | 'stopped';

export interface EmulatorLaunchState {
  avd_name: string | null;
  status: EmulatorLaunchStage;
  pid: number | null;
  serial: string | null;
  stage_message: string;
  progress_percent: number;
  started_at: number | null;
  elapsed_seconds: number;
  error: string | null;
  logs: string[];
  can_retry: boolean;
}
