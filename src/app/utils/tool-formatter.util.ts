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
import { isAndroidAction } from './action-formatter.util';
import { extractNumbersFromCoordinateValue, isPureDirectionString } from './image-overlay.util';

/**
 * Extract arguments from tool payload or direct args field
 */
export function getToolArgs(tool: any): any {
  if (!tool) return {};
  if (tool.payload) {
    let payloadObj = tool.payload;
    if (typeof payloadObj === 'string') {
      try { payloadObj = JSON.parse(payloadObj); } catch {}
    }
    if (payloadObj && typeof payloadObj === 'object') {
      if (payloadObj.args && typeof payloadObj.args === 'object') {
        return payloadObj.args;
      }
      return payloadObj;
    }
  }
  if (tool.args) {
    let argsObj = tool.args;
    if (typeof argsObj === 'string') {
      try { argsObj = JSON.parse(argsObj); } catch {}
    }
    if (argsObj && typeof argsObj === 'object') {
      return argsObj;
    }
  }
  return {};
}

/**
 * Check if a tool is one of the five note-related tools
 */
export function isNoteTool(tool: any): boolean {
  if (!tool || !tool.name) return false;
  const cleanName = tool.name.replace(/^(_)?(self\.)?exec_/, '');
  const nameLower = cleanName.toLowerCase();
  return ['save_note', 'read_note', 'list_notes', 'update_note', 'append_note'].includes(nameLower);
}

/**
 * Check if a tool is video analysis related
 */
export function isVideoTool(tool: any): boolean {
  if (!tool || !tool.name) return false;
  const cleanName = tool.name.replace(/^(_)?(self\.)?exec_/, '');
  const nameLower = cleanName.toLowerCase();
  return ['video_analyzer', 'video_analyzer_pure', 'extract_segment_metadata', 'spawn_sub_agent', 'analyze_audio_only'].includes(nameLower);
}

/**
 * Extract target video filename or description for video analysis tools
 */
export function getVideoToolTarget(tool: any): string | null {
  if (!tool) return null;
  const args = getToolArgs(tool);
  if (args.video_path || args.video_file || args.file_path || args.file) {
    const p = String(args.video_path || args.video_file || args.file_path || args.file);
    return p.split('/').pop() || p;
  }
  if (args.time_description) {
    return String(args.time_description);
  }
  if (args.start_time !== undefined || args.end_time !== undefined) {
    const start = args.start_time ?? 0;
    const end = args.end_time ? `${args.end_time}s` : 'end';
    return `${start}s - ${end}`;
  }
  if (args.purpose) {
    return String(args.purpose);
  }
  return 'screen_record.mp4';
}

/**
 * Check if a tool should be rendered in Action Card format (ONLY real Android device operations and ADB commands)
 */
export function isFailureAnalyzerActionTool(tool: any): boolean {
  if (!tool || !tool.name) return false;
  if (tool.type === 'llm_call') return false;
  const cleanName = tool.name.replace(/^(_)?(self\.)?exec_/, '').toLowerCase();
  
  const realActionTools = [
    'click', 'click_sequence', 'long_press', 'long_press_on', 'input', 'input_text', 
    'swipe', 'scroll', 'press_key', 'press_home', 'press_back', 'launch_app', 'manage_app', 'open_app',
    'focus_and_input_text', 'focus_and_clear_text', 'tap', 'wait_for_delay', 'wait',
    'run_adb_command', 'run_short_adb_command'
  ];
  return realActionTools.includes(cleanName);
}

/**
 * Check if a generic tool should be displayed in the UI
 */
export function shouldShowTool(tool: any, stepData?: any): boolean {
  if (!tool || !tool.name) return false;
  if (tool.type === 'llm_call') return false;
  const cleanName = tool.name.replace(/^(_)?(self\.)?exec_/, '');
  const nameLower = cleanName.toLowerCase();
  if (nameLower.includes('safety_net')) return false;

  // If step has an android action card rendered for action_taken, hide redundant generic tool text for atomic actions
  const atomicActions = [
    'click', 'click_sequence', 'long_press', 'long_press_on', 'input', 'input_text', 
    'swipe', 'scroll', 'press_key', 'press_home', 'press_back', 'launch_app', 'manage_app', 'open_app',
    'focus_and_input_text', 'focus_and_clear_text', 'tap', 'wait_for_delay', 'wait'
  ];
  if (stepData && stepData.action_taken && isAndroidAction(stepData.action_taken) && atomicActions.includes(nameLower)) {
    return false;
  }

  return true;
}

/**
 * Filter out nested child wrapper duplicates while preserving independent executions at the same level
 */
export function getUniqueGenericTools(tools: any[] | undefined): any[] {
  if (!tools) return [];

  const toolMap = new Map<string, any>();
  for (const t of tools) {
    if (t && t.trace_id) {
      toolMap.set(t.trace_id, t);
    }
  }

  const cleanName = (nameStr: string) => (nameStr || '').replace(/^(_)?(self\.)?exec_/, '').toLowerCase().trim();
  const hasMainVideoAnalyzer = tools.some(t => t && (cleanName(t.name) === 'video_analyzer' || cleanName(t.name) === 'video_analyzer_pure'));

  const result: any[] = [];
  for (const tool of tools) {
    if (!tool || !tool.name) continue;

    if (tool.type === 'llm_call' && tool.status === 'failed') {
      result.push(tool);
      continue;
    }

    const currClean = cleanName(tool.name);

    // Hide internal video sub-agent worker tools if main video analyzer trace is already present
    if (hasMainVideoAnalyzer && ['extract_segment_metadata', 'spawn_sub_agent', 'analyze_audio_only'].includes(currClean)) {
      continue;
    }

    // Check if this tool is a nested child execution of a parent tool with the exact same tool name
    let isNestedChildDuplicate = false;
    if (tool.parent_trace_id) {
      const parentTool = toolMap.get(tool.parent_trace_id);
      if (parentTool && cleanName(parentTool.name) === currClean) {
        isNestedChildDuplicate = true;
      }
    }

    if (!isNestedChildDuplicate) {
      result.push(tool);
    }
  }
  return result;
}

/**
 * Get note key for the tool if it exists
 */
export function getToolKey(tool: any): string | null {
  if (!tool || !tool.name) return null;
  const args = tool.payload?.args || tool.args;
  const key = args?.key;
  if (!key) return null;
  return key.toLowerCase().endsWith('.md') ? key : `${key}.md`;
}

/**
 * Get display label for tools (e.g. for pills or headers)
 */
export function getToolDisplayLabel(tool: any, isFirstSaveNote: boolean = false): string {
  if (!tool || !tool.name) return '';
  const cleanName = tool.name.replace(/^(_)?exec_/, '');
  const nameLower = cleanName.toLowerCase();
  const args = getToolArgs(tool);

  switch (nameLower) {
    case 'manage_app':
    case 'launch_app': {
      const rawApp = args.app_name || args.package_name || args.app || '';
      const app = rawApp ? (rawApp.charAt(0).toUpperCase() + rawApp.slice(1)) : 'Application';
      const rawAction = args.action ? String(args.action).toLowerCase() : '';
      const verb = rawAction === 'launch' ? 'Launching' : (rawAction === 'stop' || rawAction === 'close' ? 'Stopping' : 'Managing');
      return `${verb} "${app}"`;
    }

    case 'wait_for_delay':
    case 'wait_delay':
    case 'wait': {
      const delay = args.delay_seconds || args.seconds || args.delay || args.duration;
      return delay ? `Waiting for ${delay} second${Number(delay) > 1 ? 's' : ''}...` : 'Waiting for delay...';
    }
    case 'wait_for_text': {
      const text = args.text || args.target_text || '';
      return text ? `Waiting for text "${text}" to appear on screen` : 'Waiting for text on screen';
    }

    case 'input_text':
    case 'input': {
      const text = args.text || args.input_text || '';
      return text ? `Entering text "${text}" into field` : 'Entering text into input field';
    }
    case 'focus_and_input_text': {
      const text = args.text || args.input_text || '';
      return text ? `Focusing field and typing "${text}"` : 'Focusing field and entering text';
    }
    case 'focus_and_clear_text':
      return 'Focusing and clearing field text';

    case 'click':
    case 'tap': {
      const target = args.target_text || args.text || args.query || '';
      return target ? `Tapping on "${target}"` : 'Tapping on screen element';
    }
    case 'click_sequence':
      return 'Executing click sequence';
    case 'long_press': {
      const target = args.target_text || args.text || '';
      return target ? `Long pressing on "${target}"` : 'Long pressing screen element';
    }
    case 'swipe': {
      const dir = args.action || args.direction || '';
      return dir ? `Swiping ${String(dir).toUpperCase()} on screen` : 'Swiping screen';
    }
    case 'press_key': {
      const key = args.key || args.keycode || '';
      return key ? `Pressing key ${String(key).toUpperCase()}` : 'Pressing hardware key';
    }

    case 'save_note':
      return isFirstSaveNote ? 'Creating note' : 'Saving note';
    case 'read_note':
      return 'Reading note';
    case 'list_notes':
      return 'Browsing all saved notes';
    case 'update_note':
      return 'Updating note';
    case 'append_note':
      return 'Updating note';

    case 'object_detection': {
      const q = Array.isArray(args.queries) ? args.queries.join(', ') : (args.queries || '');
      return q ? `Locating on screen: "${q}"` : 'Locating elements on screen';
    }
    case 'ask_explorer': {
      const query = args.query || args.prompt || '';
      return query ? `Searching on screen: "${query}"` : 'Searching on screen';
    }
    case 'report_failure_analysis': {
      const reason = args.reason || args.analysis || '';
      return reason ? `Investigating issue: ${reason}` : 'Investigating execution issue';
    }
    case 'run_adb_command':
    case 'run_short_adb_command': {
      const cmd = args.command || args.cmd || '';
      return cmd ? `Running command: ${cmd}` : 'Running system command';
    }
    case 'search_logs':
    case 'read_logs': {
      const q = args.query || args.filter || '';
      return q ? `Searching logs for "${q}"` : 'Analyzing system logs';
    }
    case 'log_analyzer':
    case 'output_analyzer':
      return 'Analyzing logs';
    case 'diagnoser':
    case 'diagnose':
      return 'Diagnosing issue';
    case 'video_analyzer':
    case 'video_analyzer_pure':
      return 'Analyzing screen recording';
    case 'web_search': {
      const q = args.query || '';
      return q ? `Searching web for "${q}"` : 'Searching the web';
    }
    case 'read_url':
      return 'Fetching web page';

    default:
      return `Executing ${cleanName.split('_').map((w: string) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')}`;
  }
}

/**
 * Get Material Symbol icon for tools
 */
export function getToolIcon(tool: any): string {
  if (!tool || !tool.name) return 'settings';
  const name = tool.name.toLowerCase().replace(/^(_)?exec_/, '');
  switch (name) {
    case 'click':
    case 'tap':
      return 'ads_click';
    case 'click_sequence':
    case 'long_press':
      return 'touch_app';
    case 'input_text':
    case 'input':
      return 'keyboard';
    case 'swipe':
      return 'swipe';
    case 'press_key':
      return 'keyboard_tab';
    case 'manage_app':
      return 'open_in_new';
    case 'wait_for_delay':
      return 'timer';
    case 'wait_for_text':
      return 'hourglass_empty';
    case 'object_detection':
      return 'search';
    case 'ask_explorer':
      return 'search';
    case 'report_failure_analysis':
      return 'assessment';
    case 'run_adb_command':
    case 'run_short_adb_command':
      return 'terminal';
    case 'web_search':
      return 'travel_explore';
    case 'read_url':
      return 'language';
    case 'search_logs':
    case 'read_logs':
    case 'log_analyzer':
    case 'output_analyzer':
      return 'receipt_long';
    case 'diagnoser':
    case 'diagnose':
      return 'medical_services';
    case 'video_analyzer':
    case 'video_analyzer_pure':
      return 'video_camera_back';
    default:
      return 'build';
  }
}

/**
 * Get formatted title for a tool call card
 */
export function getToolTitle(tool: any): string {
  if (!tool || !tool.name) return 'Tool Call';
  const cleanName = tool.name.replace(/^(_)?exec_/, '');
  const name = cleanName.toLowerCase();
  switch (name) {
    case 'click':
    case 'tap':
      return 'Tapping Element';
    case 'click_sequence':
      return 'Executing Click Sequence';
    case 'long_press':
      return 'Long Pressing Element';
    case 'input_text':
    case 'input':
      return 'Entering Text';
    case 'swipe':
    case 'scroll': {
      const args = getToolArgs(tool);
      const dir = args.direction || args.gesture || (typeof args.action === 'string' ? args.action : '');
      if (dir && isPureDirectionString(dir)) return `Swiping Screen (${String(dir).toUpperCase()})`;
      return 'Swiping Screen';
    }
    case 'drag':
    case 'drag_and_drop':
      return 'Dragging Screen';
    case 'press_key':
      return 'Pressing Hardware Key';
    case 'manage_app':
    case 'launch_app': {
      const args = getToolArgs(tool);
      const rawAction = args.action ? String(args.action).toLowerCase() : '';
      if (rawAction === 'launch') return 'Launching Application';
      if (rawAction === 'stop' || rawAction === 'close') return 'Stopping Application';
      return 'Managing Application';
    }
    case 'wait_for_delay':
    case 'wait_delay':
      return 'Waiting for Delay';
    case 'wait_for_text':
      return 'Waiting for Text';
    case 'object_detection':
      return 'Locating Elements';
    case 'ask_explorer':
      return 'Searching on Screen';
    case 'report_failure_analysis':
      return 'Investigating Issue';
    case 'run_adb_command':
    case 'run_short_adb_command':
      return 'Running System Command';
    case 'web_search':
      return 'Web Search';
    case 'read_url':
      return 'Fetching Web Page';
    case 'search_logs':
    case 'read_logs':
      return 'Searching Logs';
    case 'log_analyzer':
    case 'output_analyzer':
      return 'Analyzing Logs';
    case 'diagnoser':
    case 'diagnose':
      return 'Diagnosing Issue';
    case 'video_analyzer':
    case 'video_analyzer_pure':
      return 'Analyzing Screen Recording';
    default:
      return cleanName.replace(/_/g, ' ').replace(/\b\w/g, (c: string) => c.toUpperCase());
  }
}

/**
 * Get target text description for tools
 */
export function getToolTargetText(tool: any): string {
  if (!tool || !tool.name) return '';
  const args = getToolArgs(tool);
  const name = tool.name.toLowerCase().replace(/^(_)?exec_/, '');
  if (name === 'manage_app' || name === 'launch_app') {
    const rawApp = args.app_name || args.package_name || args.app || '';
    const app = rawApp ? (rawApp.charAt(0).toUpperCase() + rawApp.slice(1)) : '';
    const rawAct = args.action ? String(args.action).toLowerCase() : '';
    const act = rawAct ? (rawAct.charAt(0).toUpperCase() + rawAct.slice(1)) : '';
    if (act && app) {
      return `${act} ${app}`;
    }
    return app || act || '';
  }
  if (name === 'swipe' && typeof args.action === 'string' && isPureDirectionString(args.action)) {
    return args.action;
  }
  if (name === 'wait_for_text') {
    return args.text || '';
  }
  if (name === 'object_detection') {
    if (Array.isArray(args.queries)) return args.queries.join(', ');
    return args.queries || '';
  }
  if (name === 'ask_explorer' || name === 'web_search') {
    return args.query || args.prompt || '';
  }
  if (name === 'read_url') {
    return args.url || '';
  }
  if (name === 'search_logs' || name === 'read_logs') {
    return args.query || args.filter || '';
  }
  if (name === 'report_failure_analysis') {
    return args.status || args.reason || args.analysis || '';
  }
  if (args.target && typeof args.target === 'string') {
    return args.target;
  }
  if (args.target && typeof args.target === 'number') {
    return `Element #${args.target}`;
  }
  if (args.index !== undefined) {
    return `Element #${args.index}`;
  }
  return args.target_text || args.target_class || args.element || args.element_text || (name !== 'input_text' ? args.text : '') || '';
}

/**
 * Get input label for tools
 */
export function getToolInputLabel(tool: any): string {
  if (!tool || !tool.name) return 'Input';
  const name = tool.name.toLowerCase().replace(/^(_)?exec_/, '');
  if (name === 'wait_for_delay' || name === 'wait_delay' || name === 'delay' || name === 'wait') {
    return 'Duration';
  }
  if (name === 'swipe' || name === 'scroll' || name === 'drag' || name === 'drag_and_drop') {
    const args = getToolArgs(tool);
    const dir = args.direction || args.gesture || (typeof args.action === 'string' ? args.action : '');
    if (dir && isPureDirectionString(dir)) {
      return 'Direction';
    }
    return 'Input';
  }
  if (name === 'press_key' || name === 'press_home' || name === 'press_back') {
    return 'Key';
  }
  if (name === 'input_text' || name === 'input') {
    return 'Input Text';
  }
  return 'Input';
}

/**
 * Get input value for tools
 */
export function getToolInputText(tool: any): string {
  if (!tool || !tool.name) return '';
  const args = getToolArgs(tool);
  const name = tool.name.toLowerCase().replace(/^(_)?exec_/, '');
  if (name === 'press_key') {
    return args.key || args.keycode || '';
  }
  if (name === 'swipe' || name === 'scroll' || name === 'drag' || name === 'drag_and_drop') {
    const dir = args.direction || args.gesture || (typeof args.action === 'string' ? args.action : '');
    if (dir && isPureDirectionString(dir)) {
      return String(dir).toUpperCase();
    }
    return '';
  }
  if (name === 'wait_for_delay' || name === 'wait_delay') {
    return args.time_in_ms ? `${args.time_in_ms}ms` : (args.delay_ms ? `${args.delay_ms}ms` : (args.time ? `${args.time}` : ''));
  }
  if (name === 'long_press' && args.duration) {
    return `Duration: ${args.duration}ms`;
  }
  if (name === 'input_text' || name === 'input') {
    return args.text || args.input_text || '';
  }
  return args.input_text || '';
}

/**
 * Get coordinates string representation for tools
 */
export function getToolCoords(tool: any): string {
  if (!tool || !tool.name) return '';
  const args = getToolArgs(tool);

  // Check normalized start and end first
  const normStart = extractNumbersFromCoordinateValue(args.normalized_start_coordinates || args.start_coordinates || args.start);
  const normEnd = extractNumbersFromCoordinateValue(args.normalized_end_coordinates || args.end_coordinates || args.end);
  if (normStart && normEnd && normStart.length === 2 && normEnd.length === 2) {
    return `[${normStart[0]}, ${normStart[1]}] → [${normEnd[0]}, ${normEnd[1]}]`;
  }

  const coords = extractNumbersFromCoordinateValue(args.normalized_coordinates) ||
                 extractNumbersFromCoordinateValue(args.coordinates) ||
                 extractNumbersFromCoordinateValue(args.target) ||
                 extractNumbersFromCoordinateValue(args.action) ||
                 extractNumbersFromCoordinateValue(args.gesture) ||
                 extractNumbersFromCoordinateValue(args.point);

  if (coords && Array.isArray(coords)) {
    if (coords.length === 4) {
      return `[${coords[0]}, ${coords[1]}] → [${coords[2]}, ${coords[3]}]`;
    }
    if (coords.length === 2) {
      return `[${coords[0]}, ${coords[1]}]`;
    }
    return coords.join(', ');
  }

  if (args.x !== undefined && args.y !== undefined) {
    return `${args.x}, ${args.y}`;
  }
  if (args.sequence && Array.isArray(args.sequence)) {
    return JSON.stringify(args.sequence);
  }
  return '';
}

/**
 * Get analysis / reasoning text for tools
 */
export function getToolAnalysisText(tool: any): string {
  if (!tool || !tool.name) return '';
  const args = getToolArgs(tool);
  return args.analysis || args.reasoning || args.summary || '';
}

/**
 * Check if tool is ADB command
 */
export function isAdbCommandTool(tool: any): boolean {
  if (!tool || !tool.name) return false;
  const name = tool.name.toLowerCase().replace(/^(_)?exec_/, '');
  return name === 'run_adb_command' || name === 'run_short_adb_command';
}

/**
 * Get ADB command line string
 */
export function getAdbCommandLine(tool: any): string {
  const args = getToolArgs(tool);
  return args.CommandLine || '';
}

/**
 * Get ADB working directory
 */
export function getAdbCwd(tool: any): string {
  const args = getToolArgs(tool);
  return args.Cwd || '';
}

/**
 * Get ADB requested terminal id
 */
export function getAdbTerminalId(tool: any): string {
  const args = getToolArgs(tool);
  return args.RequestedTerminalID || '';
}

/**
 * Get fallback generic details for unspecified tools
 */
export function getToolGenericDetails(tool: any): string {
  if (!tool) return '';
  if (isAdbCommandTool(tool)) {
    return '';
  }
  if (getToolTargetText(tool) || getToolInputText(tool) || getToolCoords(tool) || getToolAnalysisText(tool)) {
    return '';
  }
  const args = getToolArgs(tool);
  if (!args || typeof args !== 'object') return '';
  const ignoredKeys = new Set(['state', 'controller', 'ctx', 'session_id', 'step_id', 'trace_id', 'parent_trace_id', 'times', 'delay_ms']);
  const entries = Object.entries(args).filter(([k, v]) => !ignoredKeys.has(k) && v !== undefined && v !== null && v !== '');
  if (entries.length === 0) return '';
  return entries.map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join(', ');
}

/**
 * Check if tool execution failed
 */
export function isToolFailed(tool: any): boolean {
  if (!tool) return false;
  if (tool.status === 'failed' || tool.status === 'error') return true;
  const args = getToolArgs(tool);
  if (args.status === 'failed' || args.status === 'error' || args.status === 'cannot_fix') return true;
  return false;
}

/**
 * Get error message for a failed tool
 */
export function getToolErrorMessage(tool: any): string {
  if (!tool) return 'Tool Failed';
  const args = getToolArgs(tool);
  if (args.status === 'cannot_fix') return 'Status: cannot_fix';
  if (args.error || args.message || args.failure_reason) {
    return args.error || args.message || args.failure_reason;
  }
  if (tool.error || tool.message) return tool.error || tool.message;
  return 'Action Failed';
}

/**
 * Extract extra parameters for generic tools
 */
export function extractToolExtraParams(toolData: any, cache?: WeakMap<any, ActionParam[]>): ActionParam[] {
  if (!toolData) return [];
  const payload = toolData.payload || toolData.args || toolData;
  if (!payload || typeof payload !== 'object') return [];
  if (cache && cache.has(payload)) {
    return cache.get(payload)!;
  }

  const standardKeys = new Set([
    'action', 'name', 'type', 'target_text', 'text', 'input_text', 'target',
    'coordinates', 'coords', 'target_bounds', 'bounds', 'target_resource_id',
    'resource_id', 'target_class', 'class_name', 'normalized_coordinates',
    'pre_image_name', 'post_image_name', 'pre_screenshot', 'post_screenshot',
    'before_screenshot', 'after_screenshot', 'status', 'success', 'timestamp',
    'created_at', 'start_time', 'execution_id', 'controller', 'agent', 'session_id', 'step_id',
    'app_name', 'package_name', 'app', 'key', 'keycode', 'time_in_ms', 'delay_ms', 'delay_seconds', 'duration',
    'args', 'kwargs', 'parameters', 'extra_params', 'payload', 'trace_id', 'result', 'error', 'analysis', 'details', 'command', 'cwd', 'terminal_id'
  ]);

  const result: ActionParam[] = [];

  const rawArgs = toolData.args || toolData.Args || toolData.kwargs || toolData.parameters || toolData.payload;
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

  const mergedObj = { ...toolData, ...(typeof payload === 'object' ? payload : {}), ...parsedArgs };

  for (const [k, v] of Object.entries(mergedObj)) {
    const lowerK = k.toLowerCase();
    if (standardKeys.has(lowerK)) continue;
    if (v === null || v === undefined || v === '') continue;

    let valStr = String(v);
    if (valStr.includes('object at 0x') || valStr.startsWith('<artemis.') || valStr.includes('<controller') || valStr.includes('<android_world')) continue;

    if (typeof v === 'object') {
      try { valStr = JSON.stringify(v); } catch { valStr = String(v); }
    }

    let prettyKey = k.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    if (lowerK === 'time_in_ms' || lowerK === 'delay_ms') prettyKey = 'Delay';

    result.push({ key: prettyKey, value: valStr });
  }

  if (cache) {
    cache.set(payload, result);
  }
  return result;
}

/**
 * Check if text is genuine human thinking rather than raw JSON payload output
 */
export function isHumanThinking(text: string | null): boolean {
  if (!text) return false;
  let trimmed = text.trim();
  if (!trimmed) return false;

  // Strip markdown code blocks if wrapped: ```json ... ``` or ``` ... ```
  if (trimmed.startsWith('```')) {
    const match = trimmed.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/i);
    if (match) {
      trimmed = match[1].trim();
    }
  }

  // Direct object or array start
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    return false;
  }

  // Try parsing as JSON if it looks like JSON structures
  try {
    const parsed = JSON.parse(trimmed);
    if (typeof parsed === 'object' && parsed !== null) {
      return false;
    }
  } catch {
    // Not valid standalone JSON, continue checks
  }

  // Check for common JSON object detection / grounding / action / tool patterns
  if (/^\s*\{.*"label"\s*:.*"results"\s*:/s.test(trimmed) ||
      /^\s*\{.*"point"\s*:.*"label"\s*:/s.test(trimmed) ||
      /^\s*\{.*"action"\s*:/s.test(trimmed) ||
      /^\s*\{.*"name"\s*:/s.test(trimmed) ||
      /^\s*\{\s*"[^"]+"\s*:\s*/.test(trimmed) ||
      /^\s*\[\s*\{\s*"[^"]+"\s*:\s*/.test(trimmed)) {
    return false;
  }

  return true;
}

/**
 * Clean up raw error messages for display in the UI
 */
export function cleanErrorMessage(rawError: any): string {
  if (typeof rawError === 'object' && rawError !== null) {
    if (rawError.message && typeof rawError.message === 'string') {
      return cleanErrorMessage(rawError.message);
    }
    if (rawError.error?.message && typeof rawError.error?.message === 'string') {
      return cleanErrorMessage(rawError.error.message);
    }
    if (rawError.detail && typeof rawError.detail === 'string') {
      return cleanErrorMessage(rawError.detail);
    }
    try {
      return JSON.stringify(rawError);
    } catch {
      return String(rawError);
    }
  }

  const errorStr = String(rawError).trim();
  if (!errorStr) return 'Unknown error';

  // 1. Try regex extraction for "message": "..."
  const doubleQuoteMsgMatch = errorStr.match(/"message"\s*:\s*"((?:[^"\\]|\\.)*)"/i);
  if (doubleQuoteMsgMatch && doubleQuoteMsgMatch[1]) {
    const unescaped = doubleQuoteMsgMatch[1].replace(/\\"/g, '"').replace(/\\n/g, ' ').trim();
    if (unescaped) return unescaped;
  }

  // 2. Try regex extraction for 'message': '...'
  const singleQuoteMsgMatch = errorStr.match(/'message'\s*:\s*['"]((?:[^'\\]|\\.)*)['"]/i);
  if (singleQuoteMsgMatch && singleQuoteMsgMatch[1]) {
    const unescaped = singleQuoteMsgMatch[1].replace(/\\'/g, "'").replace(/\\n/g, ' ').trim();
    if (unescaped) return unescaped;
  }

  // 3. Try parsing JSON substrings inside the error string
  const jsonMatch = errorStr.match(/\{[\s\S]*\}/);
  if (jsonMatch) {
    try {
      const normalizedJsonStr = jsonMatch[0]
        .replace(/'/g, '"')
        .replace(/True/g, 'true')
        .replace(/False/g, 'false')
        .replace(/None/g, 'null');
      const parsed = JSON.parse(normalizedJsonStr);
      if (parsed?.message) return String(parsed.message);
      if (parsed?.error?.message) return String(parsed.error.message);
    } catch {
      // Ignore
    }
  }

  // 4. Fallback: truncate at trace dump headers or return clean message
  let fallback = errorStr;
  if (fallback.includes('=== Source Location Trace')) {
    fallback = fallback.split('=== Source Location Trace')[0];
  }
  if (fallback.includes('[type.googleapis.com')) {
    fallback = fallback.split('[type.googleapis.com')[0];
  }
  fallback = fallback.replace(/^ServerError:\s*/i, '').trim();

  return fallback || errorStr;
}
