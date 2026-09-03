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

import { StepBlock, PhaseBlock, StepEvent, StreamSegment, StreamResetNotice, DEFAULT_STREAM_RESET_MESSAGE } from '../core/models/stream.model';
import { isAndroidAction, isReportStatusAction, getReportStatusExplanation, getReportStatusValue, getActionObject } from './action-formatter.util';
import { getUniqueGenericTools, isInternalPlumbingTool } from './tool-formatter.util';

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
 * Index of the `checker` block that owns a trace id (the Checker's own agent
 * trace). The Checker runs concurrently with Operator steps, so its streamed
 * reasoning and tool traces are routed by `parent_trace_id`, never by the
 * step that happens to be current.
 */
function findCheckerBlockByTrace(blocks: StepBlock[], parentTraceId: any): number {
  if (!parentTraceId) return -1;
  const key = String(parentTraceId);
  return blocks.findIndex(b => b.type === 'checker' && b.data?.trace_id && String(b.data.trace_id) === key);
}

/** Latest still-running checker block (fallback when only the agent name is known). */
function findRunningCheckerBlock(blocks: StepBlock[]): number {
  for (let i = blocks.length - 1; i >= 0; i--) {
    if (blocks[i].type === 'checker' && blocks[i].data?.isCompleted === false) return i;
  }
  return -1;
}

/**
 * Apply one `checker_event` (attempt_started / attempt_finished / run_outcome)
 * to the block list. Attempts are keyed by attempt id so live events and the
 * ledger backfill merge instead of duplicating.
 */
function applyCheckerLog(blocks: StepBlock[], log: any): void {
  const d = log.data || {};
  if (!d.event) return;

  if (d.event === 'run_outcome') {
    const id = 'checker-outcome';
    const idx = blocks.findIndex(b => b.id === id);
    const data = {
      ...(idx > -1 ? blocks[idx].data : {}),
      ...d,
      phase: 'outcome',
      status: 'done',
      isCompleted: true,
      generic_tools: []
    };
    const block: StepBlock = { id, type: 'checker', timestamp: log.timestamp, data };
    if (idx > -1) blocks[idx] = block; else blocks.push(block);
    return;
  }

  if (!d.attempt_id) return;
  const id = `checker-${d.attempt_id}`;
  const idx = blocks.findIndex(b => b.id === id);
  const existing = idx > -1 ? blocks[idx] : null;
  const ts = typeof d.ts === 'number' ? d.ts : (log.timestamp ? new Date(log.timestamp).getTime() / 1000 : undefined);

  if (d.event === 'attempt_started') {
    const data = {
      ...(existing?.data || {}),
      ...d,
      // A late "started" must not resurrect a finished attempt.
      status: existing?.data?.isCompleted ? existing.data.status : 'running',
      isCompleted: existing?.data?.isCompleted ?? false,
      started_at: existing?.data?.started_at ?? ts,
      verdicts: existing?.data?.verdicts || [],
      findings: existing?.data?.findings || [],
      generic_tools: existing?.data?.generic_tools || []
    };
    const block: StepBlock = { id, type: 'checker', timestamp: existing?.timestamp || log.timestamp, data };
    if (existing) blocks[idx] = block; else blocks.push(block);
    return;
  }

  if (d.event === 'attempt_finished') {
    const startedAt = existing?.data?.started_at ?? ts;
    const verdicts = Array.isArray(d.verdicts) ? d.verdicts : (existing?.data?.verdicts || []);
    const items = existing?.data?.items?.length
      ? existing.data.items
      : verdicts.map((v: any) => ({ kind: v.kind, text: v.item_text, when: v.when }));
    const data = {
      ...(existing?.data || {}),
      ...d,
      status: d.status || 'done',
      verdicts,
      items,
      findings: Array.isArray(d.findings) ? d.findings : (existing?.data?.findings || []),
      generic_tools: existing?.data?.generic_tools || [],
      started_at: startedAt,
      finished_at: ts,
      duration: startedAt !== undefined && ts !== undefined ? Math.max(0, ts - startedAt) : undefined,
      isCompleted: true
    };
    const block: StepBlock = { id, type: 'checker', timestamp: existing?.timestamp || log.timestamp, data };
    if (existing) blocks[idx] = block; else blocks.push(block);
  }
}

/**
 * Persisted steps list the Checker's tool traces (and its agent trace) under
 * the Operator step that was current while it ran, because traces are stored
 * by step id. Once the checker blocks exist (ledger backfill carries the
 * attempt's trace id), move those traces to the attempt they belong to and
 * drop the agent trace itself.
 */
function relocateCheckerTools(blocks: StepBlock[]): void {
  const checkerByTrace = new Map<string, number>();
  blocks.forEach((b, i) => {
    if (b.type === 'checker' && b.data?.trace_id) {
      checkerByTrace.set(String(b.data.trace_id), i);
    }
  });
  if (checkerByTrace.size === 0) return;

  blocks.forEach((block, i) => {
    if (block.type === 'checker') return;
    const tools: any[] = block.data?.generic_tools || [];
    if (tools.length === 0) return;
    const keep: any[] = [];
    let moved = false;
    for (const tool of tools) {
      const ownTrace = tool?.trace_id ? String(tool.trace_id) : '';
      const parent = tool?.parent_trace_id ? String(tool.parent_trace_id) : '';
      if (ownTrace && checkerByTrace.has(ownTrace) && tool?.type === 'agent') {
        moved = true; // the Checker's own agent trace: not a tool of the step
        continue;
      }
      const target = parent ? checkerByTrace.get(parent) : undefined;
      if (target === undefined) {
        keep.push(tool);
        continue;
      }
      const checker = blocks[target];
      const existing: any[] = checker.data.generic_tools || [];
      if (!existing.some((t) => t.trace_id && t.trace_id === tool.trace_id)) {
        blocks[target] = { ...checker, data: { ...checker.data, generic_tools: [...existing, tool] } };
      }
      moved = true;
    }
    if (moved) {
      blocks[i] = { ...block, data: { ...block.data, generic_tools: keep } };
    }
  });
}

/**
 * Consolidate raw SSE logs into deduplicated and ordered StepBlocks
 */
export function consolidateLogsToBlocks(rawLogs: any[]): StepBlock[] {
  if (!rawLogs || rawLogs.length === 0) return [];

  const blocks: StepBlock[] = [];

  rawLogs.forEach(log => {
    if (!log) return;

    if (log.type === 'checker_event') {
      applyCheckerLog(blocks, log);
      return;
    }

    if (log.type === 'llm_stream') {
      const execId = log.data?.execution_id || log.id;
      const stepId = log.data?.step_id;
      const streamType = log.data?.stream_type || 'text';
      const text = log.data?.text || '';
      const isCompleted = log.data?.isCompleted ?? false;
      const isReset = log.data?.isReset ?? false;
      const resetMessage = log.data?.resetMessage;

      // The Checker's own reasoning streams under its agent trace: route it to
      // the attempt block, never to the Operator step that is current.
      const checkerIndex = findCheckerBlockByTrace(blocks, log.data?.parent_trace_id);
      if (checkerIndex > -1) {
        const checkerBlock = blocks[checkerIndex];
        const checkerData: any = { ...checkerBlock.data };
        // The Checker is a multi-turn tool loop: every LLM turn streams under
        // its own execution id. Keep one segment per turn (with its first-seen
        // time) so the timeline can interleave the text with the tool calls it
        // triggered, instead of collapsing all turns into one early block.
        const segments: StreamSegment[] = Array.isArray(checkerData.stream_segments)
          ? [...checkerData.stream_segments]
          : [];
        const segmentIndex = segments.findIndex(
          (s) => s.execution_id === execId && s.stream_type === streamType
        );
        const segment: StreamSegment = {
          execution_id: execId,
          stream_type: streamType === 'thinking' ? 'thinking' : 'text',
          text,
          timestamp: segmentIndex > -1 ? segments[segmentIndex].timestamp : log.timestamp,
          isCompleted,
          isReset,
          resetMessage
        };
        if (segmentIndex > -1) segments[segmentIndex] = segment; else segments.push(segment);
        checkerData.stream_segments = segments;
        // Flat fields stay as the joined text for consumers that only know steps.
        const joined = (type: string) => segments
          .filter((s) => s.stream_type === type && s.text.trim())
          .map((s) => s.text)
          .join('\n\n');
        checkerData.operator_native_thinking = joined('thinking');
        checkerData.operator_raw_thinking = joined('text');
        if (streamType === 'thinking') {
          checkerData.operator_native_thinking_timestamp =
            checkerData.operator_native_thinking_timestamp || log.timestamp;
        } else {
          checkerData.operator_raw_thinking_timestamp =
            checkerData.operator_raw_thinking_timestamp || log.timestamp;
        }
        if (execId && !checkerData.execution_id) {
          checkerData.execution_id = execId;
        }
        blocks[checkerIndex] = { ...checkerBlock, data: checkerData };
        return;
      }

      // Find existing block: match by stepId if available, or by execId, or active incomplete block
      let existingIndex = -1;
      if (stepId) {
        existingIndex = blocks.findIndex(b => b.id === `step-${stepId}` || b.data?.step_id === stepId);
      }
      if (existingIndex === -1 && execId) {
        existingIndex = blocks.findIndex(b => b.data?.execution_id === execId || b.id === `stream-${execId}`);
      }
      // If no stepId and not matched by execId, check if there is an unattached stream block that was reset.
      // The retry stream supersedes that reset block rather than spawning an orphaned duplicate card.
      if (existingIndex === -1 && !stepId) {
        existingIndex = blocks.findIndex(b => b.type === 'llm_stream' && b.data?.isReset);
      }

      if (existingIndex > -1) {
        const existing = blocks[existingIndex];
        const updatedData = { ...existing.data };
        const existingResets: StreamResetNotice[] =
          Array.isArray(updatedData.stream_resets) ? [...updatedData.stream_resets] : [];

        if (isReset) {
          updatedData.isReset = true;
          updatedData.resetMessage = resetMessage;
          const msg = resetMessage || DEFAULT_STREAM_RESET_MESSAGE;
          const resetId = execId || `reset-${log.timestamp || updatedData.step_id || 'stream'}`;
          const existingResetIdx = existingResets.findIndex(r => r.id === resetId);
          if (existingResetIdx > -1) {
            existingResets[existingResetIdx] = { id: resetId, message: msg, isWaiting: true, streamType };
          } else {
            existingResets.push({ id: resetId, message: msg, isWaiting: true, streamType });
          }
          updatedData.stream_resets = existingResets;
        } else if (execId && updatedData.execution_id !== execId) {
          // New execution replacing a reset stream: mark prior resets as finished waiting
          updatedData.stream_resets = existingResets.map(r => ({ ...r, isWaiting: false }));
          updatedData.isReset = false;
          updatedData.resetMessage = undefined;
          updatedData.execution_id = execId;
        } else if (!isReset && text && text.length > 0 && existingResets.some(r => r.isWaiting)) {
          updatedData.stream_resets = existingResets.map(r => ({ ...r, isWaiting: false }));
          updatedData.isReset = false;
          updatedData.resetMessage = undefined;
        }

        if (streamType === 'thinking') {
          updatedData.operator_native_thinking = text;
          updatedData.operator_native_thinking_timestamp =
            updatedData.operator_native_thinking_timestamp || log.timestamp;
        } else {
          updatedData.operator_raw_thinking = text;
          updatedData.operator_raw_thinking_timestamp =
            updatedData.operator_raw_thinking_timestamp || log.timestamp;
        }
        if (stepId && !updatedData.step_id) {
          updatedData.step_id = stepId;
        }
        if (execId && !updatedData.execution_id) {
          updatedData.execution_id = execId;
        }
        updatedData.isCompleted = isCompleted;
        if (isCompleted && updatedData.stream_resets) {
          updatedData.stream_resets = updatedData.stream_resets.map((r: any) => ({ ...r, isWaiting: false }));
        }

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
          isReset: isReset,
          resetMessage: resetMessage,
          stream_resets: isReset ? [{
            id: execId || `reset-${log.timestamp || stepId || 'stream'}`,
            message: resetMessage || DEFAULT_STREAM_RESET_MESSAGE,
            isWaiting: true,
            streamType
          }] : [],
          generic_tools: []
        };
        if (streamType === 'thinking') {
          blockData.operator_native_thinking = text;
          blockData.operator_native_thinking_timestamp = log.timestamp;
        } else {
          blockData.operator_raw_thinking = text;
          blockData.operator_raw_thinking_timestamp = log.timestamp;
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

      // No adoption of untagged stream blocks here: the Operator's streams
      // always carry the step id (Perception / the Flash turn pre-allocate it
      // before the LLM call), so a stream without one belongs to another agent
      // (Planner, Checker...). A step whose Operator emitted only tool calls
      // and no text used to steal the latest such block -- typically the
      // Planner's -- and render its action inside it at the top of the timeline.

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
          operator_native_thinking_timestamp: existingBlock.data.operator_native_thinking_timestamp
            || log.data.operator_native_thinking_timestamp
            || (log.data.operator_native_thinking ? log.timestamp : undefined),
          operator_raw_thinking_timestamp: existingBlock.data.operator_raw_thinking_timestamp
            || log.data.operator_raw_thinking_timestamp
            || (log.data.operator_raw_thinking ? log.timestamp : undefined),
          action_taken: log.data.action_taken || existingBlock.data.action_taken || undefined,
          last_execution_result: log.data.last_execution_result || existingBlock.data.last_execution_result || undefined,
          generic_tools: mergedTools,
          isCompleted: true
        };
        if (mergedData.stream_resets) {
          mergedData.stream_resets = mergedData.stream_resets.map((r: any) => ({ ...r, isWaiting: false }));
        }

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
            operator_native_thinking_timestamp: log.data.operator_native_thinking_timestamp
              || (log.data.operator_native_thinking ? log.timestamp : undefined),
            operator_raw_thinking_timestamp: log.data.operator_raw_thinking_timestamp
              || (log.data.operator_raw_thinking ? log.timestamp : undefined),
            generic_tools: log.data.generic_tools || []
          }
        });
      }
    } else if (log.type === 'trace_recorded') {
      const isAction = log.data.type === 'action';
      const isTool = log.data.type === 'tool' || isAction;
      const isVisibleLLMEvent = log.data.type === 'llm_call'
        && (log.data.status === 'failed' || log.data.status === 'retrying');
      if (!isTool && !isVisibleLLMEvent) return;

      let stepId = log.data.step_id;
      let existingIndex = -1;

      // 0. Tools invoked by the Checker belong to its attempt block (routed by
      //    the parent trace, falling back to the running attempt by agent name).
      let checkerIndex = findCheckerBlockByTrace(blocks, log.data.parent_trace_id);
      if (checkerIndex === -1 && String(log.data.agent_name || '').toLowerCase() === 'checker') {
        checkerIndex = findRunningCheckerBlock(blocks);
      }
      if (checkerIndex > -1) {
        existingIndex = checkerIndex;
      }

      // 1. If this trace_id already exists in some block, update that block directly
      if (existingIndex === -1 && log.data.trace_id) {
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

      // 4. Untagged traces only: attach to the latest block that precedes
      //    them. A trace that names a step whose block does not exist yet
      //    (tool call before any streamed text, or an action recorded before
      //    the step in Flash) creates that step's block below instead of being
      //    glued to an earlier step, where the later `step_recorded` (which
      //    carries the same trace) would leave a duplicate card.
      if (existingIndex === -1 && !stepId) {
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

  relocateCheckerTools(blocks);

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
 * Cache for the token-estimate fallback. Tool payloads can carry multi-megabyte
 * base64 screenshots; serializing them on every recompute would block the main
 * thread, so each payload object is measured exactly once.
 */
const payloadSizeCache = new WeakMap<object, { isImage: boolean; length: number }>();

function estimatePayloadSize(payload: any): { isImage: boolean; length: number } {
  if (typeof payload === 'string') {
    return {
      isImage: payload.includes('data:image/') || payload.includes('base64,'),
      length: payload.length
    };
  }
  if (payload === null || typeof payload !== 'object') {
    return { isImage: false, length: String(payload).length };
  }
  const cached = payloadSizeCache.get(payload);
  if (cached) return cached;
  let payloadStr = '';
  try {
    payloadStr = JSON.stringify(payload) || '';
  } catch {
    payloadStr = String(payload);
  }
  const result = {
    isImage: payloadStr.includes('data:image/') || payloadStr.includes('base64,'),
    length: payloadStr.length
  };
  payloadSizeCache.set(payload, result);
  return result;
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
        const estimate = estimatePayloadSize(t.payload);
        if (estimate.isImage) {
          numImages += 1;
        } else {
          textLen += estimate.length;
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
  cache?: WeakMap<any, { signature: string; events: StepEvent[] }>
): StepEvent[] {
  if (!stepData || typeof stepData !== 'object') return [];
  const toolsLen = stepData.generic_tools ? stepData.generic_tools.length : 0;
  const toolsSignature = stepData.generic_tools && Array.isArray(stepData.generic_tools)
    ? stepData.generic_tools.map((t: any) => `${t?.trace_id || t?.name || ''}:${t?.status || ''}`).join(',')
    : '';
  const actionTs = stepData.action_taken ? (stepData.action_taken.timestamp || stepData.action_taken.start_time || stepData.action_taken.created_at) : null;
  const nativeText = typeof stepData.operator_native_thinking === 'string'
    ? stepData.operator_native_thinking.trim()
    : '';
  const rawText = typeof stepData.operator_raw_thinking === 'string'
    ? stepData.operator_raw_thinking.trim()
    : '';
  const segments: StreamSegment[] | null = Array.isArray(stepData.stream_segments) && stepData.stream_segments.length > 0
    ? stepData.stream_segments
    : null;
  const signature = [
    toolsLen,
    toolsSignature,
    actionTs ?? '',
    stepData.operator_native_thinking_timestamp ?? '',
    stepData.operator_raw_thinking_timestamp ?? '',
    nativeText.length,
    rawText.length,
    stepData.isReset ? '1' : '0',
    stepData.resetMessage ?? '',
    stepData.stream_resets ? stepData.stream_resets.map((r: any) => `${r.id}:${r.isWaiting ? 1 : 0}`).join(',') : '',
    segments ? segments.map((s) => `${s.execution_id}:${s.stream_type}:${s.text.length}`).join(',') : ''
  ].join('|');

  if (cache) {
    const cached = cache.get(stepData);
    if (cached?.signature === signature) {
      return cached.events;
    }
  }

  const events: Array<{ type: StepEvent['type']; data: any; timestamp: number; sequence: number }> = [];
  const fallbackTime = getItemTimestamp(stepData.timestamp, 0);
  let sequence = 0;

  // Text and tools share one timeline. Live streams carry their first-seen
  // timestamps; persisted steps use the step timestamp as a stable fallback.
  if (segments) {
    // Multi-turn agent (Checker): one event per streamed turn, each at the
    // time its first chunk arrived, so turns sort between the tool calls.
    segments.forEach((segment) => {
      if ((!segment.text || !segment.text.trim()) && !segment.isReset) return;
      events.push({
        type: segment.stream_type === 'thinking' ? 'thinking' : 'text',
        data: {
          text: segment.text,
          execution_id: segment.execution_id,
          segment: true,
          isReset: segment.isReset,
          resetMessage: segment.resetMessage
        },
        timestamp: getItemTimestamp(segment.timestamp, fallbackTime),
        sequence: sequence++
      });
    });
  } else {
    const hasThinkingResets = Array.isArray(stepData.stream_resets) && stepData.stream_resets.some((r: any) => r.streamType === 'thinking');
    if (nativeText || hasThinkingResets) {
      events.push({
        type: 'thinking',
        data: { text: stepData.operator_native_thinking || '' },
        timestamp: getItemTimestamp(stepData.operator_native_thinking_timestamp, fallbackTime),
        sequence: sequence++
      });
    }

    if (rawText || (stepData.isReset && stepData.resetMessage) || (Array.isArray(stepData.stream_resets) && stepData.stream_resets.length > 0)) {
      events.push({
        type: 'text',
        data: {
          text: stepData.operator_raw_thinking || '',
          isReset: stepData.isReset,
          resetMessage: stepData.resetMessage
        },
        timestamp: getItemTimestamp(stepData.operator_raw_thinking_timestamp, fallbackTime),
        sequence: sequence++
      });
    }
  }

  // 2. Single Source of Truth & Fallback Resolution:
  const uniqueTools = getUniqueGenericTools(stepData.generic_tools);
  const hasActionInTools = uniqueTools.some((tool: any) =>
    tool && (tool.type === 'action' || isAndroidAction(tool) || isReportStatusAction(tool))
  );

  // Fallback: only add action_taken if generic_tools did NOT already contain an action trace.
  // Pushed before uniqueTools so that when timestamps are missing/equal, the primary action
  // preserves deterministic sequence ordering ahead of generic fallback tools.
  if (!hasActionInTools && stepData.action_taken && (isAndroidAction(stepData.action_taken) || isReportStatusAction(stepData.action_taken))) {
    const actObj = getActionObject(stepData.action_taken);
    const actTime = getItemTimestamp(
      actObj?.timestamp ?? actObj?.start_time ?? actObj?.created_at,
      fallbackTime
    );
    events.push({
      type: 'action',
      data: stepData.action_taken,
      timestamp: actTime,
      sequence: sequence++
    });
  }

  // Add all real tool calls and actions from the event stream.
  uniqueTools.forEach((tool: any) => {
    if (isInternalPlumbingTool(tool) && !isReportStatusAction(tool)) {
      return;
    }

    const isAction = tool.type === 'action' || isAndroidAction(tool) || isReportStatusAction(tool);
    const toolTime = getItemTimestamp(
      tool.timestamp ?? tool.start_time ?? tool.created_at,
      fallbackTime
    );
    events.push({
      type: isAction ? 'action' : 'tool',
      data: tool,
      timestamp: toolTime,
      sequence: sequence++
    });
  });

  // Sort chronologically. The explicit sequence makes equal/missing timestamp
  // behavior deterministic across browsers and preserves the legacy fallback.
  events.sort((a, b) => (a.timestamp - b.timestamp) || (a.sequence - b.sequence));

  const result: StepEvent[] = events.map(e => ({
    type: e.type,
    data: e.data,
    timestamp: e.timestamp
  }));
  if (cache) {
    cache.set(stepData, { signature, events: result });
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
