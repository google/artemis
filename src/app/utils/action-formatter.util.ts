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
import { extractNumbersFromCoordinateValue, isPureDirectionString } from './image-overlay.util';

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
 * Check if the action execution failed
 */
export function isActionFailed(action: any, stepData?: any): boolean {
  const act = getActionObject(action);
  if (act) {
    if (act.status === 'failed' || act.status === 'error' || act.success === false) return true;
  }
  if (stepData && stepData.last_execution_result) {
    const res = stepData.last_execution_result;
    if (res.status === 'failed' || res.status === 'error' || res.success === false || res.is_successful === false) return true;
  }
  return false;
}

/**
 * Get failure message for an action
 */
export function getActionErrorMessage(action: any, stepData?: any): string {
  const act = getActionObject(action);
  if (act && (act.error || act.message)) return act.error || act.message;
  if (stepData && stepData.last_execution_result) {
    const res = stepData.last_execution_result;
    if (res.error || res.message || res.failure_reason) {
      return res.error || res.message || res.failure_reason;
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
 * Get pre-action screenshot URL
 */
export function getStepPreImageUrl(stepData: any, actionData?: any): string | null {
  const act = getActionObject(actionData);
  const candidate = act?.pre_image_name || act?.pre_screenshot || act?.before_screenshot || act?.screenshot || stepData?.pre_image_name || stepData?.pre_screenshot;
  return formatImageUrl(candidate);
}

/**
 * Get post-action screenshot URL
 */
export function getStepPostImageUrl(stepData: any, actionData?: any): string | null {
  const act = getActionObject(actionData);
  const candidate = act?.post_image_name || act?.post_screenshot || act?.after_screenshot || stepData?.post_image_name || stepData?.post_screenshot;
  const postUrl = formatImageUrl(candidate);
  const preUrl = getStepPreImageUrl(stepData, actionData);
  if (postUrl && preUrl && postUrl === preUrl) {
    return null;
  }
  return postUrl;
}
