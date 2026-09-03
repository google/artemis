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
  type: 'llm_stream' | 'step' | 'checker';
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

/** One check item's verdict as booked by the Checker (ledger record shape). */
export interface CheckerVerdict {
  item_text: string;
  kind: 'verify' | 'assert' | string;
  status: 'passed' | 'failed' | 'inconclusive' | 'superseded' | 'unchecked' | string;
  evidence: string;
  suggestion?: string;
}

export interface CheckerCheckItem {
  kind: string;
  text: string;
  when?: string;
}

/**
 * One streamed LLM turn of a multi-turn agent (the Checker's tool loop):
 * the text of one execution id with the time its first chunk arrived, so the
 * timeline can interleave it with the tool calls that followed.
 */
export interface StreamSegment {
  execution_id: string;
  stream_type: 'thinking' | 'text';
  text: string;
  timestamp: string;
  isCompleted?: boolean;
}

/**
 * Data of a `checker` timeline block: one Checker attempt (a midway checkpoint
 * of a completed subgoal or the exit final review), or the run outcome
 * (`phase: 'outcome'`). Streamed reasoning and tool traces land in the same
 * `operator_*_thinking` / `generic_tools` fields as an Operator step so the
 * timeline renders them with one code path.
 */
export interface CheckerBlockData {
  event?: string;
  attempt_id?: string;
  checkpoint_id?: string;
  phase: 'checkpoint' | 'final' | 'outcome' | string;
  subgoal_text?: string;
  status?: 'running' | 'done' | 'superseded' | 'unchecked' | 'error' | string;
  trace_id?: string | null;
  anchor_step_id?: string | null;
  items?: CheckerCheckItem[];
  verdicts?: CheckerVerdict[];
  findings?: string[];
  unmet_subgoals?: string[];
  reverted?: boolean;
  applicable?: boolean;
  repairs_used?: number;
  route?: string;
  error?: string;
  task_status?: 'completed' | 'partial' | 'blocked' | string;
  tests?: { passed: number; failed: number; inconclusive: number; unchecked: number };
  last_findings?: string[];
  started_at?: number;
  finished_at?: number;
  duration?: number;
  isCompleted?: boolean;
  generic_tools?: any[];
  /** Per-turn stream segments (live sessions only); the flat fields below hold the joined text. */
  stream_segments?: StreamSegment[];
  operator_native_thinking?: string;
  operator_raw_thinking?: string;
  [key: string]: any;
}

/** @deprecated legacy JSON-in-stream checker verdict (pre-ledger); kept for the parser. */
export interface CheckerResult {
  success: boolean;
  reason: string;
}

export interface StepReplayFrame {
  index: number;
  stepNumber: number;
  rawStepNumber?: number;
  stepId?: string;
  title: string;
  imageUrl: string;
  preImageUrl?: string | null;
  postImageUrl?: string | null;
  action?: any;
  actionType?: string;
  actionText?: string;
  targetText?: string;
  coords?: string;
  status?: 'success' | 'failed' | string;
  isPost?: boolean;
  timestamp?: number;
  phaseId?: string;
  summary?: string;
}

