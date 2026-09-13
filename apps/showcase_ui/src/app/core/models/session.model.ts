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

export interface ModelInfo {
  name: string;
  id: string;
  provider: string;
  architecture?: string;
}

export interface TaskQueueItem {
  session_id: string;
  goal: string;
  profile?: string;
  status: 'pending' | 'running' | 'paused' | 'completed' | 'failed' | 'cancelled';
  created_at?: number;
  start_time?: number;
  device_serial?: string | null;
  device_id?: string | null;
}

export interface Session {
  session_id: string;
  initial_goal: string;
  start_time: number;
  end_time?: number;
  status?: string;
  video_url?: string;
  recording_status?: 'recording' | 'finalizing' | 'processing' | 'ready' | 'failed' | 'unavailable';
  model_info?: ModelInfo;
  device_serial?: string | null;
  device_id?: string | null;
  device_info?: any;
}

export interface AgentStatusResponse {
  status: 'idle' | 'running' | 'paused' | 'completed' | 'offline';
  session_id?: string | null;
  goal?: string | null;
  pid?: number | null;
  queue?: (TaskQueueItem | string)[];
  background_tasks?: any[];
  model_info?: ModelInfo | null;
  paused_error?: string | null;
}

/** Per-run Pro tuning persisted with the session (`device_info.run_tuning`). */
export interface SessionRunTuning {
  verification_level?: string | null;
  explorer_mode?: string | null;
}

/** `GET /api/sessions/{id}/usage`: session-wide LLM usage and the live executor context. */
export interface SessionUsage {
  session_id: string;
  llm_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cached_tokens: number;
  /** Prompt size of the latest Operator / Flash runner call; null before the first call. */
  operator_context_tokens: number | null;
  operator_context_window_tokens: number;
  operator_context_updated_at?: number | null;
  profile?: string | null;
  run_tuning?: SessionRunTuning | null;
}

export type AgentStatus = 'idle' | 'running' | 'completed' | 'offline' | string;

export type TaskStatus = 'running' | 'paused' | 'completed' | 'pending' | 'failed' | 'cancelled';

