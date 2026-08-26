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

export interface ActionParam {
  key: string;
  value: string;
}

export interface ActionExecution {
  id?: string;
  trace_id?: string;
  agent?: string;
  agent_name?: string;
  action: string;
  name?: string;
  args?: Record<string, any>;
  params?: Record<string, any>;
  payload?: any;
  pre_image_name?: string | null;
  post_image_name?: string | null;
  status?: 'success' | 'failed' | 'error' | 'running' | string;
  duration?: number;
  duration_ms?: number;
  error?: string | null;
  timestamp?: number;
}

export interface StepEvent {
  type: 'thinking' | 'text' | 'action' | 'tool';
  data: any;
  timestamp?: number;
}

export interface LLMStreamEventData {
  execution_id: string;
  step_id?: string;
  session_id?: string;
  text?: string;
  chunk?: string;
  stream_type?: 'thinking' | 'text' | string;
  isCompleted?: boolean;
}

export interface StepItemData {
  step_id: string;
  step_type?: string;
  step_number: number;
  session_id: string;
  timestamp: number;
  operator_native_thinking?: string;
  operator_raw_thinking?: string;
  action_taken?: any;
  generic_tools?: any[];
  last_execution_result?: any;
  pre_image_name?: string;
  post_image_name?: string;
  extra_metadata?: any;
  token_usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
  };
  total_tokens?: number;
  duration?: number;
}

export interface StepBlock {
  id: string;
  type: 'llm_stream' | 'step';
  timestamp: string;
  data: any;
}

export interface PhaseBlock {
  id: string;
  durationSeconds: number;
  tokens?: number;
  promptTokens?: number;
  completionTokens?: number;
  blocks: StepBlock[];
}

export interface CheckerResult {
  success: boolean;
  reason: string;
}

export interface StepReplayFrame {
  index: number;
  stepNumber: number;
  title: string;
  imageUrl: string;
  actionText?: string;
  isPost?: boolean;
  timestamp?: number;
  phaseId?: string;
}

