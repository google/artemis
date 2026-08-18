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

import { ActionParam } from '../core/models/stream.model';
import { extractNumbersFromCoordinateValue, isPureDirectionString, parseSequenceCoordinates } from './image-overlay.util';
import { cleanErrorMessage } from './tool-formatter.util';

/**
 * Safely extract action object from possible array or nested structure
 */
export function getActionObject(action: any): any {
  if (!action) return null;
  if (Array.isArray(action)) {
    return action.length > 0 ? action[0] : null;
  }
  return action;
}

/**
 * Check if the action is a user interaction action on Android
 */
export function isAndroidAction(action: any): boolean {
  const act = getActionObject(action);
  if (!act) return false;
  const name = (act.name || act.action || '').toLowerCase();
  return [
    'tap', 'click', 'click_sequence', 'input', 'input_text', 'swipe', 'scroll', 'press_key', 
    'press_home', 'press_back', 'launch_app', 'manage_app', 'open_app',
    'focus_and_input_text', 'focus_and_clear_text', 'long_press', 'long_press_on',
    'wait_for_delay', 'wait'
  ].includes(name);
}

/**
 * Check if the action or tool is a task completion / submission report
 */
export function isReportStatusAction(action: any): boolean {
  const act = getActionObject(action);
  if (!act) return false;
  const name = (act.name || act.action || '').toLowerCase().replace(/^(_)?(self\.)?exec_/, '');
  return name === 'report_task_status' || name === 'report_status' || name === 'submit_task_status';
}

/**
 * Get status of report_task_status (completed / failed)
 */
export function getReportStatusValue(action: any): string {
  const act = getActionObject(action);
  if (!act) return 'completed';
  const args = act.args && typeof act.args === 'object' ? act.args : (act.payload?.args || act.payload || act);
  const status = args.status || act.status;
  if (typeof status === 'string') {
    return status.toLowerCase();
  }
  return 'completed';
}

/**
 * Get explanation of report_task_status
 */
export function getReportStatusExplanation(action: any): string {
  const act = getActionObject(action);
  if (!act) return '';
  const args = act.args && typeof act.args === 'object' ? act.args : (act.payload?.args || act.payload || act);
  return args.explanation || args.reason || args.summary || act.explanation || act.reason || act.summary || '';
}

/**
 * Get Material Symbol icon name for Android action
 */
export function getActionIcon(action: any): string {
  const act = getActionObject(action);
  if (!act) return 'ads_click';
  const name = (act.name || act.action || '').toLowerCase();
  switch (name) {
    case 'tap':
    case 'click':
    case 'click_sequence':
      return 'ads_click';
    case 'input':
    case 'input_text':
    case 'focus_and_input_text':
      return 'keyboard';
    case 'focus_and_clear_text':
    case 'clear_text':
      return 'backspace';
    case 'swipe':
    case 'scroll':
      return 'swipe';
    case 'drag':
    case 'drag_and_drop':
      return 'drag_indicator';
    case 'press_key':
    case 'press_home':
    case 'press_back':
      return 'keyboard_tab';
    case 'launch_app':
    case 'manage_app':
    case 'open_app':
      return 'open_in_new';
    case 'long_press':
    case 'long_press_on':
      return 'touch_app';
    case 'wait_for_delay':
    case 'delay':
    case 'wait':
      return 'hourglass_empty';
    default:
      return 'settings';
  }
}

/**
 * Get human-readable title for Android actions
 */
export function getActionTitle(action: any): string {
  const act = getActionObject(action);
  if (!act) return 'Action';
  const name = (act.name || act.action || '').toLowerCase();
  switch (name) {
    case 'tap':
    case 'click':
    case 'tap_element':
    case 'click_element':
      return 'Tapping Element';
    case 'input':
    case 'input_text':
    case 'focus_and_input_text':
      return 'Entering Text';
    case 'focus_and_clear_text':
    case 'clear_text':
      return 'Clearing Text';
    case 'swipe':
    case 'scroll': {
      const actObj = getActionObject(action);
      const args = actObj?.args && typeof actObj.args === 'object' ? actObj.args : {};
      const dir = actObj?.direction || actObj?.gesture || args.direction || args.gesture || (typeof args.action === 'string' ? args.action : '') || (typeof actObj?.action === 'string' && actObj.action !== name ? actObj.action : '');
      if (dir && isPureDirectionString(dir)) {
        return `Swiping Screen (${String(dir).toUpperCase()})`;
      }
      return 'Swiping Screen';
    }
    case 'drag':
    case 'drag_and_drop':
      return 'Dragging Screen';
    case 'press_key':
    case 'press_home':
    case 'press_back':
      return 'Pressing Hardware Key';
    case 'launch_app':
    case 'open_app':
      return 'Launching Application';
    case 'stop_app':
    case 'close_app':
      return 'Stopping Application';
    case 'manage_app': {
      const actObj = getActionObject(action);
      const actStr = (actObj?.action || actObj?.args?.action || '').toLowerCase();
      if (actStr === 'launch') return 'Launching Application';
      if (actStr === 'stop' || actStr === 'close') return 'Stopping Application';
      return 'Managing Application';
    }
    case 'wait_for_delay':
    case 'delay':
    case 'wait':
      return 'Waiting for Delay';
    case 'long_press':
    case 'long_press_on':
      return 'Long Pressing Element';
    case 'click_sequence':
      return 'Clicking Sequence';
    default:
      return name.replace(/_/g, ' ').replace(/\b\w/g, (c: string) => c.toUpperCase());
  }
}

/**
 * Get target text or element description for Android action
 */
export function getActionTargetText(action: any): string {
  const act = getActionObject(action);
  if (!act) return '';
  const name = (act.name || act.action || '').toLowerCase();
  const args = act.args && typeof act.args === 'object' ? act.args : {};

  if (name.includes('app')) {
    const rawApp = act.app_name || act.package_name || act.app || args.app_name || args.package_name || args.app || '';
    const app = rawApp ? (rawApp.charAt(0).toUpperCase() + rawApp.slice(1)) : '';
    const rawAction = act.action || args.action || '';
    const actionStr = rawAction ? (String(rawAction).charAt(0).toUpperCase() + String(rawAction).slice(1).toLowerCase()) : '';
    if (actionStr && app) {
      return `${actionStr} ${app}`;
    }
    return app || actionStr || '';
  }
  if (name.includes('delay') || name.includes('wait')) {
    const ms = act.time_in_ms || act.delay_ms || act.delay_seconds || args.time_in_ms || args.delay_ms || args.delay_seconds || args.duration;
    if (ms) return `${ms}ms`;
  }
  return act.target_text || act.target_class || act.element_id || args.target_text || args.element_id || '';
}

/**
 * Get input label for Android action
 */
export function getActionInputLabel(action: any): string {
  const act = getActionObject(action);
  if (!act) return 'Input';
  const name = (act.name || act.action || '').toLowerCase();
  if (name.includes('delay') || name.includes('wait')) {
    return 'Duration';
  }
  if (name === 'swipe' || name === 'scroll' || name === 'drag' || name === 'drag_and_drop') {
    const actObj = getActionObject(action);
    const args = actObj?.args && typeof actObj.args === 'object' ? actObj.args : {};
    const dir = actObj?.direction || actObj?.gesture || args.direction || args.gesture || (typeof args.action === 'string' ? args.action : '') || (typeof actObj?.action === 'string' && actObj.action !== name ? actObj.action : '');
    if (dir && isPureDirectionString(dir)) {
      return 'Direction';
    }
    return 'Input';
  }
  if (name === 'press_key' || name.includes('key')) {
    return 'Key';
  }
  if (name === 'input_text' || name.includes('input')) {
    return 'Input Text';
  }
  return 'Input';
}

/**
 * Get input text or value for Android action
 */
export function getActionInputText(action: any): string {
  const act = getActionObject(action);
  if (!act) return '';
  const name = (act.name || act.action || '').toLowerCase();
  const args = act.args && typeof act.args === 'object' ? act.args : {};

  if (name === 'press_key' || name.includes('key')) {
    return act.key || act.keycode || args.key || args.keycode || '';
  }
  if (name === 'swipe' || name === 'scroll' || name === 'drag' || name === 'drag_and_drop') {
    const dir = act.direction || act.gesture || args.direction || args.gesture || (typeof args.action === 'string' ? args.action : '') || (typeof act.action === 'string' && act.action !== name ? act.action : '');
    if (dir && isPureDirectionString(dir)) {
      return String(dir).toUpperCase();
    }
    return '';
  }
  if (name.includes('delay') || name.includes('wait')) {
    const ms = act.time_in_ms || act.delay_ms || act.delay_seconds || args.time_in_ms || args.delay_ms || args.delay_seconds || args.duration;
    if (ms) return `${ms}ms`;
  }
  return act.text || act.input_text || args.text || args.input_text || '';
}

/**
 * Get coordinates string representation for Android action
 */
export function getActionCoords(action: any): string {
  const act = getActionObject(action);
  if (!act) return '';
  const args = act.args && typeof act.args === 'object' ? act.args : {};
  const name = (act.name || act.action || '').toLowerCase();

  // Check sequence first if action is sequence or sequence arg is present
  const isSequenceAction = name === 'click_sequence' || name === 'tap_sequence' || Boolean(act.sequence || args.sequence || act.normalized_sequence || args.normalized_sequence);
  const rawSeq = act.normalized_sequence || args.normalized_sequence || act.sequence || args.sequence || act.targets || args.targets || (Array.isArray(act.coordinates) && Array.isArray(act.coordinates[0]) ? act.coordinates : null) || (Array.isArray(args.coordinates) && Array.isArray(args.coordinates[0]) ? args.coordinates : null);
  const seqPoints = parseSequenceCoordinates(rawSeq);
  if (seqPoints && seqPoints.length > 0 && (isSequenceAction || seqPoints.length > 1)) {
    return seqPoints.map(pt => `[${pt[0]}, ${pt[1]}]`).join(' → ');
  }

  // Check normalized start and end first
  const normStart = extractNumbersFromCoordinateValue(act.normalized_start_coordinates || args.normalized_start_coordinates);
  const normEnd = extractNumbersFromCoordinateValue(act.normalized_end_coordinates || args.normalized_end_coordinates);
  if (normStart && normEnd && normStart.length === 2 && normEnd.length === 2) {
    return `[${normStart[0]}, ${normStart[1]}] → [${normEnd[0]}, ${normEnd[1]}]`;
  }

  const startCoords = extractNumbersFromCoordinateValue(act.start_coordinates || act.start_point || act.start || act.from || args.start_coordinates || args.start_point || args.start || args.from);
  const endCoords = extractNumbersFromCoordinateValue(act.end_coordinates || act.end_point || act.end || act.to || args.end_coordinates || args.end_point || args.end || args.to);
  if (startCoords && endCoords && startCoords.length === 2 && endCoords.length === 2) {
    return `[${startCoords[0]}, ${startCoords[1]}] → [${endCoords[0]}, ${endCoords[1]}]`;
  }

  const normCoords = extractNumbersFromCoordinateValue(act.normalized_coordinates || args.normalized_coordinates);
  const coords = normCoords ||
                 extractNumbersFromCoordinateValue(act.coordinates) ||
                 extractNumbersFromCoordinateValue(args.coordinates) ||
                 extractNumbersFromCoordinateValue(act.coords) ||
                 extractNumbersFromCoordinateValue(args.coords) ||
                 extractNumbersFromCoordinateValue(act.target) ||
                 extractNumbersFromCoordinateValue(args.target) ||
                 extractNumbersFromCoordinateValue(args.action) ||
                 extractNumbersFromCoordinateValue(act.action) ||
                 extractNumbersFromCoordinateValue(args.gesture) ||
                 extractNumbersFromCoordinateValue(act.gesture);

  if (coords && Array.isArray(coords)) {
    if (coords.length === 4) {
      return `[${coords[0]}, ${coords[1]}] → [${coords[2]}, ${coords[3]}]`;
    }
    if (coords.length === 2) {
      return `[${coords[0]}, ${coords[1]}]`;
    }
    return coords.join(', ');
  }

  return '';
}

/**
 * Check if the action execution failed or encountered an execution failure/interception
 */
export function isActionFailed(action: any, stepData?: any): boolean {
  const act = getActionObject(action);
  if (act) {
    if (act.status === 'failed' || act.status === 'error' || act.success === false) return true;
  }
  if (stepData) {
    if (stepData.status === 'failed' || stepData.status === 'error') return true;
    if (stepData.last_execution_result) {
      const res = typeof stepData.last_execution_result === 'string'
        ? (() => { try { return JSON.parse(stepData.last_execution_result); } catch { return null; } })()
        : stepData.last_execution_result;

      if (res && typeof res === 'object') {
        if (res.status === 'failed' || res.status === 'error' || res.success === false || res.is_successful === false) {
          return true;
        }
        if (res.repair_status === 'fixed' || res.repair_status === 'failed' || res.repair_status === 'cannot_fix') {
          return true;
        }
        if (Array.isArray(res.execution) && res.execution.length > 0) {
          const firstExec = res.execution[0];
          if (firstExec && (firstExec.status === 'failed' || firstExec.status === 'error' || firstExec.error || (Array.isArray(firstExec.attempts) && firstExec.attempts.length > 0))) {
            return true;
          }
        }
      }
    }
  }
  return false;
}

/**
 * Get failure message or reason for an action
 */
export function getActionErrorMessage(action: any, stepData?: any): string {
  const act = getActionObject(action);
  if (act && (act.error || act.message || act.failure_reason)) {
    return cleanErrorMessage(act.error || act.message || act.failure_reason);
  }
  if (stepData && stepData.last_execution_result) {
    const res = typeof stepData.last_execution_result === 'string'
      ? (() => { try { return JSON.parse(stepData.last_execution_result); } catch { return null; } })()
      : stepData.last_execution_result;

    if (res && typeof res === 'object') {
      if (Array.isArray(res.execution) && res.execution.length > 0) {
        const firstExec = res.execution[0];
        if (firstExec) {
          if (Array.isArray(firstExec.attempts) && firstExec.attempts.length > 0) {
            return cleanErrorMessage(firstExec.attempts[0]);
          }
          if (firstExec.error || firstExec.failure_reason || firstExec.message) {
            return cleanErrorMessage(firstExec.error || firstExec.failure_reason || firstExec.message);
          }
        }
      }
      if (res.error || res.message || res.failure_reason) {
        return cleanErrorMessage(res.error || res.message || res.failure_reason);
      }
    }
  }
  return 'Action Failed';
}

/**
 * Get bounds string representation for Android action
 */
export function getActionBounds(action: any): string {
  const act = getActionObject(action);
  if (!act) return '';
  const bounds = act.target_bounds || act.bounds || (act.args && (act.args.target_bounds || act.args.bounds));
  if (Array.isArray(bounds)) {
    return bounds.join(', ');
  }
  return bounds ? String(bounds) : '';
}

/**
 * Get resource id for Android action target
 */
export function getActionResourceId(action: any): string {
  const act = getActionObject(action);
  if (!act) return '';
  return act.target_resource_id || act.resource_id || (act.args && (act.args.target_resource_id || act.args.resource_id)) || '';
}

/**
 * Get class name for Android action target
 */
export function getActionClass(action: any): string {
  const act = getActionObject(action);
  if (!act) return '';
  return act.target_class || act.class_name || (act.args && (act.args.target_class || act.args.class_name)) || '';
}

/**
 * Extract extra parameter key-value pairs for Android actions
 */
export function extractActionExtraParams(action: any, cache?: WeakMap<any, ActionParam[]>): ActionParam[] {
  const act = getActionObject(action);
  if (!act || typeof act !== 'object') return [];
  if (cache && cache.has(act)) {
    return cache.get(act)!;
  }

  const standardKeys = new Set([
    'action', 'name', 'type', 'target_text', 'text', 'input_text', 'target',
    'coordinates', 'coords', 'target_bounds', 'bounds', 'target_resource_id',
    'resource_id', 'target_class', 'class_name', 'normalized_coordinates',
    'normalized_start_coordinates', 'normalized_end_coordinates',
    'start_coordinates', 'end_coordinates', 'start', 'end', 'from', 'to',
    'pre_image_name', 'post_image_name', 'pre_screenshot', 'post_screenshot',
    'before_screenshot', 'after_screenshot', 'status', 'success', 'timestamp',
    'created_at', 'start_time', 'execution_id', 'controller', 'agent', 'session_id', 'step_id',
    'app_name', 'package_name', 'app', 'key', 'keycode', 'time_in_ms', 'delay_ms', 'delay_seconds', 'duration',
    'args', 'kwargs', 'parameters', 'extra_params', 'direction', 'gesture'
  ]);

  const result: ActionParam[] = [];

  const rawArgs = act.args || act.Args || act.kwargs || act.parameters;
  let parsedArgs: any = {};
  if (rawArgs) {
    if (typeof rawArgs === 'object') {
      parsedArgs = rawArgs;
    } else if (typeof rawArgs === 'string') {
      try {
        parsedArgs = JSON.parse(rawArgs);
      } catch {
        if (!rawArgs.includes('<') && !rawArgs.includes('object at')) {
          parsedArgs = { details: rawArgs };
        }
      }
    }
  }

  const mergedObj = { ...act, ...parsedArgs };

  // Explicitly check duration if available and not already formatted
  const dur = mergedObj.duration || mergedObj.duration_ms;
  if (dur !== undefined && dur !== null && dur !== '') {
    result.push({ key: 'Duration', value: typeof dur === 'number' ? `${dur}ms` : String(dur) });
  }

  for (const [k, v] of Object.entries(mergedObj)) {
    const lowerK = k.toLowerCase();
    if (standardKeys.has(lowerK)) continue;
    if (v === null || v === undefined || v === '') continue;

    let valStr = String(v);
    if (valStr.includes('object at 0x') || valStr.startsWith('<artemis.') || valStr.includes('<controller')) continue;

    if (typeof v === 'object') {
      try { valStr = JSON.stringify(v); } catch { valStr = String(v); }
    }

    let prettyKey = k.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    if (lowerK === 'time_in_ms' || lowerK === 'delay_ms') prettyKey = 'Delay';

    result.push({ key: prettyKey, value: valStr });
  }

  if (cache) {
    cache.set(act, result);
  }
  return result;
}

/**
 * Format image URL or path safely
 */
export function formatImageUrl(candidate: any): string | null {
  if (!candidate || candidate === 'None' || candidate === 'null' || candidate === 'undefined') return null;
  if (typeof candidate !== 'string') return null;
  const trimmed = candidate.trim();
  if (!trimmed || trimmed === 'None' || trimmed === 'null' || trimmed === 'undefined') return null;

  if (trimmed.startsWith('data:') || trimmed.startsWith('http://') || trimmed.startsWith('https://')) {
    return trimmed;
  }
  if (trimmed.startsWith('/local_file') || trimmed.startsWith('/images/') || trimmed.startsWith('/api/images/')) {
    return trimmed;
  }
  if (trimmed.startsWith('file://')) {
    return `/local_file?path=${encodeURIComponent(trimmed)}`;
  }
  if (trimmed.startsWith('/')) {
    return `/local_file?path=${encodeURIComponent('file://' + trimmed)}`;
  }
  return `/images/${trimmed}`;
}

/**
 * Helper to check if an item is a generic tool trace rather than the step's primary action
 */
export function isGenericToolTrace(actionData: any, stepData?: any): boolean {
  if (!actionData) return false;
  if (actionData === stepData?.action_taken) return false;
  if (Array.isArray(stepData?.action_taken)) {
    if (stepData.action_taken === actionData || stepData.action_taken.includes(actionData)) {
      return false;
    }
  }
  if (actionData.is_primary_step_action) return false;
  return Boolean(actionData.trace_id || actionData.payload || actionData.type === 'tool' || (actionData.type === 'action' && actionData.trace_id));
}

interface ResolvedImageEntry {
  pre: string | null;
  post: string | null;
  hasExplicitPost: boolean;
}

const stepImageMapCache = new WeakMap<any, { key: string; map: Map<any, ResolvedImageEntry> }>();

function safeParseJson(val: any): any {
  if (typeof val === 'string' && (val.trim().startsWith('{') || val.trim().startsWith('['))) {
    try {
      return JSON.parse(val);
    } catch {
      return val;
    }
  }
  return val;
}

export function extractItemPreImage(item: any): string | null {
  const act = getActionObject(item);
  if (!act) return null;
  const payload = safeParseJson(act.payload);
  const result = safeParseJson(act.result || payload?.result);
  const args = safeParseJson(act.args || payload?.args);

  const candidate =
    result?.pre_image_name || result?.pre_screenshot_name || result?.pre_screenshot ||
    payload?.pre_image_name || payload?.pre_screenshot_name || payload?.pre_screenshot ||
    args?.pre_image_name || args?.pre_screenshot_name || args?.pre_screenshot ||
    act.pre_image_name || act.pre_screenshot_name || act.pre_screenshot || act.before_screenshot || act.screenshot;

  return candidate ? String(candidate) : null;
}

export function extractItemPostImage(item: any): string | null {
  const act = getActionObject(item);
  if (!act) return null;
  const payload = safeParseJson(act.payload);
  const result = safeParseJson(act.result || payload?.result);
  const args = safeParseJson(act.args || payload?.args);

  const candidate =
    result?.post_image_name || result?.post_screenshot_name || result?.post_screenshot ||
    payload?.post_image_name || payload?.post_screenshot_name || payload?.post_screenshot ||
    args?.post_image_name || args?.post_screenshot_name || args?.post_screenshot ||
    act.post_image_name || act.post_screenshot_name || act.post_screenshot || act.after_screenshot;

  return candidate ? String(candidate) : null;
}

function getStepFailureScreenshot(stepData: any): string | null {
  if (!stepData) return null;
  if (stepData.failed_screenshot) return String(stepData.failed_screenshot);
  if (stepData.failure_screenshot) return String(stepData.failure_screenshot);
  if (stepData.failed_post_image_name) return String(stepData.failed_post_image_name);

  if (Array.isArray(stepData.generic_tools)) {
    for (const tool of stepData.generic_tools) {
      const act = getActionObject(tool);
      const payload = safeParseJson(act?.payload);
      const args = safeParseJson(act?.args || payload?.args);
      const name = String(act?.name || act?.agent_name || '').toLowerCase();
      if (args?.post_screenshot_name) return String(args.post_screenshot_name);
      if (args?.post_screenshot && (name.includes('failure') || name.includes('validator') || name.includes('diagnos'))) {
        return String(args.post_screenshot);
      }
      if (act?.post_screenshot_name && (name.includes('failure') || name.includes('validator'))) {
        return String(act.post_screenshot_name);
      }
    }
  }
  return null;
}

function getEventTimestamp(obj: any): number {
  if (!obj) return 0;
  const ts = obj.timestamp ?? obj.start_time ?? obj.created_at;
  if (typeof ts === 'number') {
    return ts < 1e11 ? ts * 1000 : ts;
  }
  if (typeof ts === 'string') {
    const parsed = new Date(ts).getTime();
    if (!isNaN(parsed) && parsed > 0) return parsed;
  }
  return 0;
}

export function resolveStepImageMap(stepData: any): Map<any, ResolvedImageEntry> {
  const emptyMap = new Map<any, ResolvedImageEntry>();
  if (!stepData || typeof stepData !== 'object') return emptyMap;

  const toolsLen = Array.isArray(stepData.generic_tools) ? stepData.generic_tools.length : 0;
  const cacheKey = `${stepData.step_id || ''}_${toolsLen}_${stepData.post_image_name || ''}_${stepData.pre_image_name || ''}`;

  const cached = stepImageMapCache.get(stepData);
  if (cached && cached.key === cacheKey) {
    return cached.map;
  }

  const map = new Map<any, ResolvedImageEntry>();
  const events: Array<{ item: any; act: any; timestamp: number; isPrimary: boolean }> = [];

  if (stepData.action_taken) {
    events.push({
      item: stepData.action_taken,
      act: getActionObject(stepData.action_taken),
      timestamp: getEventTimestamp(stepData.action_taken),
      isPrimary: true
    });
  }

  if (Array.isArray(stepData.generic_tools)) {
    stepData.generic_tools.forEach((t: any) => {
      if (t) {
        events.push({
          item: t,
          act: getActionObject(t),
          timestamp: getEventTimestamp(t),
          isPrimary: false
        });
      }
    });
  }

  events.sort((a, b) => a.timestamp - b.timestamp);

  const failureShot = getStepFailureScreenshot(stepData);
  const hadPrimaryFailure = Boolean(failureShot) || (stepData.action_taken && isActionFailed(stepData.action_taken, stepData));
  const hasSubsequentActions = events.some(e => !e.isPrimary && (isAndroidAction(e.act) || Boolean(extractItemPostImage(e.act))));

  let lastActionItem: any = null;
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (isAndroidAction(e.act) || extractItemPostImage(e.act) || e.isPrimary) {
      lastActionItem = e.item;
      break;
    }
  }

  let currentPreImage = stepData.pre_image_name || stepData.pre_screenshot || null;
  let switchedToFailureShot = false;

  for (const e of events) {
    const act = e.act;
    const explicitPre = extractItemPreImage(act);
    const explicitPost = extractItemPostImage(act);

    const agentName = String(
      act?.agent_name || act?.agent || safeParseJson(act?.payload)?.agent_name || act?.name || ''
    ).toLowerCase();
    const isRecoveryOrValidator = agentName.includes('failure') || agentName.includes('analyzer') || agentName.includes('validator') || agentName.includes('diagnos');

    if (failureShot && !switchedToFailureShot && (isRecoveryOrValidator || (!e.isPrimary && hadPrimaryFailure))) {
      currentPreImage = failureShot;
      switchedToFailureShot = true;
    }

    const resolvedPre = explicitPre || currentPreImage;
    let resolvedPost: string | null = null;
    let hasExplicitPost = false;

    if (explicitPost) {
      resolvedPost = explicitPost;
      hasExplicitPost = true;
    } else if (e.isPrimary && !hadPrimaryFailure && !hasSubsequentActions) {
      resolvedPost = stepData.post_image_name || stepData.post_screenshot || null;
    } else if (e.item === lastActionItem && !hadPrimaryFailure) {
      resolvedPost = stepData.post_image_name || stepData.post_screenshot || null;
    } else if (e.item === lastActionItem && isRecoveryOrValidator) {
      resolvedPost = stepData.post_image_name || stepData.post_screenshot || null;
    }

    // "下一次的决策前是上一次的动作后"
    if (resolvedPost) {
      currentPreImage = resolvedPost;
    }

    const entry: ResolvedImageEntry = { pre: resolvedPre, post: resolvedPost, hasExplicitPost };
    map.set(e.item, entry);
    if (act && act !== e.item) map.set(act, entry);
    if (act?.trace_id) map.set(act.trace_id, entry);
  }

  stepImageMapCache.set(stepData, { key: cacheKey, map });
  return map;
}

function lookupResolvedImages(stepData: any, actionData?: any): ResolvedImageEntry {
  const map = resolveStepImageMap(stepData);
  if (!actionData) {
    return {
      pre: stepData?.pre_image_name || stepData?.pre_screenshot || null,
      post: stepData?.post_image_name || stepData?.post_screenshot || null,
      hasExplicitPost: Boolean(stepData?.post_image_name || stepData?.post_screenshot)
    };
  }

  const act = getActionObject(actionData);

  if (map.has(actionData)) return map.get(actionData)!;
  if (act && map.has(act)) return map.get(act)!;
  if (act?.trace_id && map.has(act.trace_id)) return map.get(act.trace_id)!;

  const explicitPre = extractItemPreImage(act);
  const explicitPost = extractItemPostImage(act);
  const pre = explicitPre || stepData?.pre_image_name || stepData?.pre_screenshot || null;
  const post = explicitPost || (isGenericToolTrace(actionData, stepData) ? null : (stepData?.post_image_name || stepData?.post_screenshot || null));
  return { pre, post, hasExplicitPost: Boolean(explicitPost) };
}

/**
 * Get pre-action screenshot URL
 */
export function getStepPreImageUrl(stepData: any, actionData?: any): string | null {
  const resolved = lookupResolvedImages(stepData, actionData);
  return formatImageUrl(resolved.pre);
}

/**
 * Get post-action screenshot URL
 */
export function getStepPostImageUrl(stepData: any, actionData?: any): string | null {
  const resolved = lookupResolvedImages(stepData, actionData);
  const postUrl = formatImageUrl(resolved.post);
  const preUrl = formatImageUrl(resolved.pre);

  if (postUrl && preUrl && postUrl === preUrl && !resolved.hasExplicitPost) {
    return null;
  }
  return postUrl;
}
