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
}

export interface TaskQueueItem {
  session_id: string;
  goal: string;
  profile?: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  created_at?: number;
  start_time?: number;
}

export interface Session {
  session_id: string;
  initial_goal: string;
  start_time: number;
  end_time?: number;
  status?: string;
  video_url?: string;
  model_info?: ModelInfo;
}

export interface AgentStatusResponse {
  status: 'idle' | 'running' | 'paused' | 'completed' | 'offline';
  session_id?: string | null;
  goal?: string | null;
  pid?: number | null;
  queue?: (TaskQueueItem | string)[];
  background_tasks?: any[];
  model_info?: ModelInfo | null;
}

export type AgentStatus = 'idle' | 'running' | 'completed' | 'offline' | string;

export type TaskStatus = 'running' | 'completed' | 'pending' | 'failed' | 'cancelled';

