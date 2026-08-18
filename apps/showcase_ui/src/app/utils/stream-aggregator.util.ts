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

import { StepBlock, PhaseBlock, StepEvent } from '../core/models/stream.model';
import { isAndroidAction, isReportStatusAction, getReportStatusExplanation, getReportStatusValue } from './action-formatter.util';
import { getUniqueGenericTools } from './tool-formatter.util';

/**
 * Helper to safely extract milliseconds timestamp
 */
export function getItemTimestamp(ts: any, fallback: number = 0): number {
  if (ts !== undefined && ts !== null) {
    if (typeof ts === 'number') {
      return ts < 1e11 ? ts * 1000 : ts;
    }
    const parsed = new Date(ts).getTime();
    if (!isNaN(parsed) && parsed > 0) return parsed;
  }
  return fallback;
}

/**
 * Consolidate raw SSE logs into deduplicated and ordered StepBlocks
 */
export function consolidateLogsToBlocks(rawLogs: any[]): StepBlock[] {
  if (!rawLogs || rawLogs.length === 0) return [];

  const blocks: StepBlock[] = [];

  rawLogs.forEach(log => {
    if (!log) return;

    if (log.type === 'llm_stream') {
      const execId = log.data?.execution_id || log.id;
      const stepId = log.data?.step_id;
      const streamType = log.data?.stream_type || 'text';
      const text = log.data?.text || '';
      const isCompleted = log.data?.isCompleted ?? false;

      // Find existing block: match by stepId if available, or by execId, or active incomplete block
      let existingIndex = -1;
      if (stepId) {
        existingIndex = blocks.findIndex(b => b.id === `step-${stepId}` || b.data?.step_id === stepId);
      }
      if (existingIndex === -1 && execId) {
        existingIndex = blocks.findIndex(b => b.data?.execution_id === execId || b.id === `stream-${execId}`);
      }

      if (existingIndex > -1) {
        const existing = blocks[existingIndex];
        const updatedData = { ...existing.data };
        if (streamType === 'thinking') {
          updatedData.operator_native_thinking = text;
        } else {
          updatedData.operator_raw_thinking = text;
        }
        if (stepId && !updatedData.step_id) {
          updatedData.step_id = stepId;
        }
        if (execId && !updatedData.execution_id) {
          updatedData.execution_id = execId;
        }
        updatedData.isCompleted = isCompleted;

        blocks[existingIndex] = {
          ...existing,
          data: updatedData
        };
      } else {
        const blockId = stepId ? `step-${stepId}` : `stream-${execId}`;
        const blockData: any = {
          execution_id: execId,
          step_id: stepId,
          isCompleted: isCompleted,
          generic_tools: []
        };
        if (streamType === 'thinking') {
          blockData.operator_native_thinking = text;
        } else {
          blockData.operator_raw_thinking = text;
        }

        blocks.push({
          id: blockId,
          type: stepId ? 'step' : 'llm_stream',
          timestamp: log.timestamp,
          data: blockData
        });
      }
    } else if (log.type === 'step_recorded' || log.type === 'step_updated') {
      const stepId = log.data.step_id || log.data.step_number || 'unknown';
      // Find if there is an existing block for this step or an unattached stream block from this turn
      let existingIndex = blocks.findIndex(b => b.id === `step-${stepId}` || b.data?.step_id === stepId);

      // If not found by step_id, check if there is an open stream block from the same execution turn
      if (existingIndex === -1) {
        for (let i = blocks.length - 1; i >= 0; i--) {
          if (blocks[i].type === 'llm_stream' && (!blocks[i].data?.step_id || blocks[i].data?.step_id === stepId)) {
            existingIndex = i;
            break;
          }
        }
      }

      if (existingIndex > -1) {
        const existingBlock = blocks[existingIndex];
        const existingTools = existingBlock.data.generic_tools || [];
        const newTools = log.data.generic_tools || [];
        const mergedTools = [...existingTools];
        newTools.forEach((nt: any) => {
          const matchIdx = mergedTools.findIndex(et => et.trace_id && nt.trace_id && et.trace_id === nt.trace_id);
          if (matchIdx > -1) {
            mergedTools[matchIdx] = { ...mergedTools[matchIdx], ...nt };
          } else {
            mergedTools.push(nt);
          }
        });

        const mergedData = {
          ...existingBlock.data,
          ...log.data,
          operator_native_thinking: log.data.operator_native_thinking || existingBlock.data.operator_native_thinking || undefined,
          operator_raw_thinking: log.data.operator_raw_thinking || existingBlock.data.operator_raw_thinking || undefined,
          action_taken: log.data.action_taken || existingBlock.data.action_taken || undefined,
          last_execution_result: log.data.last_execution_result || existingBlock.data.last_execution_result || undefined,
          generic_tools: mergedTools,
          isCompleted: true
        };

        blocks[existingIndex] = {
          ...existingBlock,
          id: `step-${stepId}`,
          type: 'step',
          timestamp: existingBlock.timestamp || log.timestamp,
          data: mergedData
        };
      } else {
        blocks.push({
          id: `step-${stepId}`,
          type: 'step',
          timestamp: log.timestamp,
          data: {
            ...log.data,
            isCompleted: true,
            generic_tools: log.data.generic_tools || []
          }
        });
      }
    } else if (log.type === 'trace_recorded') {
      const isAction = log.data.type === 'action';
      const isTool = log.data.type === 'tool' || isAction;
      const isFailedLLM = log.data.type === 'llm_call' && log.data.status === 'failed';
      if (!isTool && !isFailedLLM) return;

      let stepId = log.data.step_id;
      let existingIndex = -1;

      // 1. If this trace_id already exists in some block, update that block directly
      if (log.data.trace_id) {
        existingIndex = blocks.findIndex(b =>
          b.data?.generic_tools?.some((t: any) => t.trace_id === log.data.trace_id)
        );
      }

      // 2. If not found by trace_id and stepId is present, find by step_id
      if (existingIndex === -1 && stepId) {
        existingIndex = blocks.findIndex(b => b.id === `step-${stepId}` || b.data?.step_id === stepId);
      }

      // 3. If still not found and no stepId (e.g. pre-planning / initial planning phase)
      if (existingIndex === -1 && !stepId) {
        existingIndex = blocks.findIndex(b => b.id === 'step-pre-planning' || b.data?.step_id === 'pre-planning');
      }

      // 4. Otherwise find by timestamp among existing step blocks
      if (existingIndex === -1) {
        const logTime = log.timestamp ? new Date(log.timestamp).getTime() : 0;
        let maxStepTime = -1;
        for (let i = 0; i < blocks.length; i++) {
          const b = blocks[i];
          const bTime = b.timestamp ? new Date(b.timestamp).getTime() : 0;
          if (bTime <= logTime && bTime >= maxStepTime) {
            maxStepTime = bTime;
            existingIndex = i;
          }
        }
      }

      if (existingIndex > -1) {
        const stepData = blocks[existingIndex].data;
        const genericTools = stepData.generic_tools ? [...stepData.generic_tools] : [];
        const existingToolIdx = log.data.trace_id
          ? genericTools.findIndex((t: any) => t.trace_id === log.data.trace_id)
          : -1;

        if (existingToolIdx > -1) {
          genericTools[existingToolIdx] = {
            ...genericTools[existingToolIdx],
            ...log.data
          };
        } else {
          genericTools.push(log.data);
        }

        blocks[existingIndex] = {
          ...blocks[existingIndex],
          data: {
            ...stepData,
            generic_tools: genericTools
          }
        };
      } else {
        const fallbackStepId = stepId || 'pre-planning';
        blocks.push({
          id: `step-${fallbackStepId}`,
          type: 'step',
          timestamp: log.timestamp,
          data: {
            step_id: fallbackStepId,
            isCompleted: true,
            generic_tools: [log.data]
          }
        });
      }
    }
  });

  blocks.sort((a, b) => {
    const timeA = a.timestamp ? new Date(a.timestamp).getTime() : 0;
    const timeB = b.timestamp ? new Date(b.timestamp).getTime() : 0;
    if (Math.abs(timeA - timeB) > 50) {
      return timeA - timeB;
    }
    // Order by step_number if available (0 is planning phase)
    const getStepOrder = (block: StepBlock): number => {
      if (block.data?.step_number !== undefined && block.data.step_number !== null) {
        return Number(block.data.step_number);
      }
      if (block.data?.step_id === 'pre-planning' || block.data?.step_type === 'planning') {
        return 0;
      }
      return 99999;
    };

    const stepNumA = getStepOrder(a);
    const stepNumB = getStepOrder(b);
    if (stepNumA !== stepNumB && stepNumA !== 99999 && stepNumB !== 99999) {
      return stepNumA - stepNumB;
    }
    return timeA - timeB;
  });

  return blocks;
}

/**
 * Safely extract and aggregate token usage for a step block
 */
export function extractBlockTokens(block: StepBlock): { total: number; prompt: number; completion: number } {
  if (!block || !block.data) return { total: 0, prompt: 0, completion: 0 };
  const data = block.data;

  // 1. Direct total_tokens or token_usage object
  if (data.total_tokens !== undefined && data.total_tokens !== null && Number(data.total_tokens) > 0) {
    const pr = data.token_usage?.prompt_tokens || data.token_usage?.input_tokens || 0;
    const co = data.token_usage?.completion_tokens || data.token_usage?.output_tokens || 0;
    return { total: Number(data.total_tokens), prompt: Number(pr), completion: Number(co) };
  }
  if (data.token_usage && typeof data.token_usage === 'object') {
    const pr = data.token_usage.prompt_tokens || data.token_usage.input_tokens || 0;
    const co = data.token_usage.completion_tokens || data.token_usage.output_tokens || 0;
    const to = data.token_usage.total_tokens || (pr + co);
    if (to > 0) {
      return { total: Number(to), prompt: Number(pr), completion: Number(co) };
    }
  }
  if (data.extra_metadata?.token_usage && typeof data.extra_metadata.token_usage === 'object') {
    const u = data.extra_metadata.token_usage;
    const pr = u.prompt_tokens || u.prompt_token_count || u.input_tokens || 0;
    const co = u.completion_tokens || u.candidates_token_count || u.output_tokens || 0;
    const to = u.total_tokens || u.total_token_count || (pr + co);
    if (to > 0) {
      return { total: Number(to), prompt: Number(pr), completion: Number(co) };
    }
  }

  // 2. Generic tools traces (e.g. llm_call traces or tool payloads)
  let sumPrompt = 0;
  let sumCompletion = 0;
  let sumTotal = 0;
  if (data.generic_tools && Array.isArray(data.generic_tools)) {
    data.generic_tools.forEach((t: any) => {
      if (t && t.payload) {
        const u = t.payload.token_usage || t.payload.usage_metadata;
        if (u) {
          const pr = u.prompt_tokens || u.prompt_token_count || u.input_tokens || 0;
          const co = u.completion_tokens || u.candidates_token_count || u.output_tokens || 0;
          const to = u.total_tokens || u.total_token_count || (pr + co);
          sumPrompt += Number(pr);
          sumCompletion += Number(co);
          sumTotal += Number(to);
        }
      }
    });
  }
  if (sumTotal > 0) {
    return { total: sumTotal, prompt: sumPrompt, completion: sumCompletion };
  }

  // 3. Fallback estimate from thoughts/messages/actions text if no tokens stored
  let textLen = 0;
  let numImages = 0;
  if (data.operator_native_thinking) textLen += data.operator_native_thinking.length;
  if (data.operator_raw_thinking) textLen += data.operator_raw_thinking.length;
  if (data.generic_tools && Array.isArray(data.generic_tools)) {
    data.generic_tools.forEach((t: any) => {
      if (t.payload) {
        const payloadStr = typeof t.payload === 'string' ? t.payload : JSON.stringify(t.payload);
        if (payloadStr.includes('data:image/') || payloadStr.includes('base64,')) {
          numImages += 1;
        } else {
          textLen += payloadStr.length;
        }
      }
    });
  }
  if (textLen > 20 || numImages > 0) {
    const estimated = Math.round(textLen / 4) + (numImages * 258);
    return { total: estimated, prompt: 0, completion: estimated };
  }

  return { total: 0, prompt: 0, completion: 0 };
}

/**
 * Format token count with clean units (e.g. "842 tokens", "76.4k tokens", "1.2M tokens")
 */
export function formatTokenCount(tokens?: number): string {
  if (!tokens || tokens <= 0) return '0 tokens';
  if (tokens < 1000) {
    return `${tokens} tokens`;
  }
  if (tokens < 1_000_000) {
    const inK = (tokens / 1000).toFixed(1).replace(/\.0$/, '');
    return `${inK}k tokens`;
  }
  const inM = (tokens / 1_000_000).toFixed(2).replace(/\.?0+$/, '');
  return `${inM}M tokens`;
}

/**
 * Group step blocks into chronological execution phases (segmented per work unit)
 */
export function groupBlocksToPhases(blocks: StepBlock[], sessionStartTime: number): PhaseBlock[] {
  if (!blocks || blocks.length === 0) return [];
  const phases: PhaseBlock[] = [];
  
  // Base start timestamp in ms
  let previousEndTime = sessionStartTime ? sessionStartTime * 1000 : 0;

  blocks.forEach((block, index) => {
    const blockTime = block.timestamp ? new Date(block.timestamp).getTime() : Date.now();
    
    if (previousEndTime === 0 && blockTime > 0) {
      previousEndTime = blockTime;
    }

    let durationSeconds = 0;
    if (block.data?.duration && typeof block.data.duration === 'number' && block.data.duration > 0) {
      durationSeconds = Math.max(1, Math.round(block.data.duration));
    } else if (blockTime >= previousEndTime && previousEndTime > 0) {
      durationSeconds = Math.max(1, Math.round((blockTime - previousEndTime) / 1000));
    } else {
      durationSeconds = 1;
    }

    const tokensInfo = extractBlockTokens(block);

    phases.push({
      id: `phase-${block.id || index}`,
      durationSeconds,
      tokens: tokensInfo.total > 0 ? tokensInfo.total : undefined,
      promptTokens: tokensInfo.prompt > 0 ? tokensInfo.prompt : undefined,
      completionTokens: tokensInfo.completion > 0 ? tokensInfo.completion : undefined,
      blocks: [block]
    });

    previousEndTime = blockTime;
  });

  return phases;
}

/**
 * Returns all actions and tools inside a step card, sorted in true chronological sequence by timestamp
 */
export function getSortedStepEvents(
  stepData: any, 
  cache?: WeakMap<any, { length: number; actionTs: any; events: StepEvent[] }>
): StepEvent[] {
  if (!stepData || typeof stepData !== 'object') return [];
  const toolsLen = stepData.generic_tools ? stepData.generic_tools.length : 0;
  const actionTs = stepData.action_taken ? (stepData.action_taken.timestamp || stepData.action_taken.start_time || stepData.action_taken.created_at) : null;

  if (cache) {
    const cached = cache.get(stepData);
    if (cached && cached.length === toolsLen && cached.actionTs === actionTs) {
      return cached.events;
    }
  }

  const events: Array<{ type: 'action' | 'tool'; data: any; timestamp: number }> = [];
  const fallbackTime = getItemTimestamp(stepData.timestamp, 0);

  // 1. Add action_taken if present and valid
  if (stepData.action_taken && (isAndroidAction(stepData.action_taken) || isReportStatusAction(stepData.action_taken))) {
    const actTime = getItemTimestamp(
      stepData.action_taken.timestamp ?? stepData.action_taken.start_time ?? stepData.action_taken.created_at,
      fallbackTime
    );
    events.push({
      type: 'action',
      data: stepData.action_taken,
      timestamp: actTime
    });
  }

  // 2. Add generic tools if present
  const uniqueTools = getUniqueGenericTools(stepData.generic_tools);
  uniqueTools.forEach((tool: any) => {
    const toolTime = getItemTimestamp(
      tool.timestamp ?? tool.start_time ?? tool.created_at,
      fallbackTime
    );
    events.push({
      type: 'tool',
      data: tool,
      timestamp: toolTime
    });
  });

  // 3. Sort chronologically by timestamp
  events.sort((a, b) => a.timestamp - b.timestamp);

  const result: StepEvent[] = events.map(e => ({ type: e.type, data: e.data }));
  if (cache) {
    cache.set(stepData, { length: toolsLen, actionTs, events: result });
  }
  return result;
}

/**
 * Compile human-readable session summary from logs
 */
export function compileSessionSummary(logs: any[]): string {
  if (!logs || logs.length === 0) return 'No logs available.';

  // Check for goal completed / checker response / cancellation / report_task_status
  for (let i = logs.length - 1; i >= 0; i--) {
    const log = logs[i];
    if (log.type === 'session_ended') {
      if (log.data?.status === 'cancelled' || log.data?.was_stopped_manually) {
        return 'Task stopped manually.';
      }
    }
    if (log.type === 'llm_stream' && log.data?.text) {
      const text = log.data.text;
      if (text.includes('```json') && text.includes('"success"')) {
        return 'Execution completed and verified by checker.';
      }
    }
    if (log.type === 'step_recorded' || log.type === 'step_updated') {
      const act = log.data?.action_taken;
      if (act && isReportStatusAction(act)) {
        const exp = getReportStatusExplanation(act);
        if (exp) return exp;
      }
      if (log.data?.status === 'completed') {
        return log.data.message || 'Task completed successfully.';
      }
    }
  }

  return 'Execution session in progress or ended.';
}

/**
 * Calculate total session duration in seconds
 */
export function computeSessionDuration(sessionStartTime: number, logs: any[]): number {
  if (!sessionStartTime || !logs || logs.length === 0) return 0;
  const startMs = sessionStartTime * 1000;
  const lastLog = logs[logs.length - 1];
  const endMs = lastLog?.timestamp ? new Date(lastLog.timestamp).getTime() : Date.now();
  return Math.max(0, Math.round((endMs - startMs) / 1000));
}

/**
 * Check if the planning loader should be visible (while running, waiting for next step/action and not actively typing a stream)
 */
export function checkPlanningLoader(
  logs: any[], 
  isRunning: boolean, 
  isCurrentRunningSession: boolean = true
): boolean {
  if (!isRunning || !isCurrentRunningSession) return false;
  if (!logs || logs.length === 0) return true;

  // If the latest step or event indicates task completion, do not show loader
  for (let i = logs.length - 1; i >= Math.max(0, logs.length - 3); i--) {
    const log = logs[i];
    if (log.type === 'session_ended') return false;
    if (log.type === 'step_recorded' || log.type === 'step_updated') {
      const act = log.data?.action_taken;
      if (act && (act.action === 'report_task_status' || act === 'report_task_status' || isReportStatusAction(act))) {
        return false;
      }
      if (log.data?.status === 'completed' || log.data?.status === 'failed') {
        return false;
      }
    }
  }

  // If there is an active incomplete stream currently streaming chunks, hide the planning loader
  const hasActiveStream = logs.some(
    l => l && l.type === 'llm_stream' && l.data && l.data.isCompleted === false
  );

  if (hasActiveStream) {
    return false;
  }

  return true;
}
