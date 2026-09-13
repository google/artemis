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

/**
 * Pure helpers behind the run info popover (the Flash / Pro chip in the
 * task stream): elapsed time, compact token counts, context-window share
 * and the plain-language labels of the Pro tuning a run was started with.
 */

import {
  EXPLORER_MODES,
  VERIFICATION_LEVELS,
  levelIndex,
  DEFAULT_EXPLORER_MODE,
  DEFAULT_VERIFICATION_LEVEL
} from '../core/models/pro-tuning.model';
import { SessionRunTuning } from '../core/models/session.model';

/** Denominator for "context used" when the backend does not report one. */
export const OPERATOR_CONTEXT_WINDOW_TOKENS = 1_000_000;

/** `42s`, `12m 05s`, `1h 02m 05s`. Negative or invalid input reads as `0s`. */
export function formatElapsed(totalSeconds: number | null | undefined): string {
  const secs = Math.max(0, Math.floor(Number(totalSeconds) || 0));
  const hours = Math.floor(secs / 3600);
  const minutes = Math.floor((secs % 3600) / 60);
  const seconds = secs % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  if (hours > 0) return `${hours}h ${pad(minutes)}m ${pad(seconds)}s`;
  if (minutes > 0) return `${minutes}m ${pad(seconds)}s`;
  return `${seconds}s`;
}

/** `842`, `76.4k`, `1.2M` (no unit suffix; the caller labels the row). */
export function formatCompactTokens(tokens: number | null | undefined): string {
  const n = Math.max(0, Math.floor(Number(tokens) || 0));
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1).replace(/\.0$/, '')}k`;
  return `${(n / 1_000_000).toFixed(2).replace(/\.?0+$/, '')}M`;
}

/** Share of the context window in percent (one decimal), or null when unknown. */
export function contextPercent(
  usedTokens: number | null | undefined,
  windowTokens: number | null | undefined
): number | null {
  const used = Number(usedTokens);
  const window = Number(windowTokens) || OPERATOR_CONTEXT_WINDOW_TOKENS;
  if (!Number.isFinite(used) || used < 0 || usedTokens === null || usedTokens === undefined) {
    return null;
  }
  const pct = (used / window) * 100;
  return Math.min(100, Math.round(pct * 10) / 10);
}

/**
 * Seconds a session has been (or was) running.
 *
 * Live sessions count from `start_time` to `nowMs`; finished sessions use the
 * recorded `end_time`. A session with no `start_time` reads as 0.
 */
export function sessionElapsedSeconds(
  session: { start_time?: number; end_time?: number | null } | null | undefined,
  isRunning: boolean,
  nowMs: number
): number {
  if (!session?.start_time) return 0;
  const startMs = session.start_time * 1000;
  const endMs = !isRunning && session.end_time ? session.end_time * 1000 : nowMs;
  return Math.max(0, Math.round((endMs - startMs) / 1000));
}

/** Plain-language label for a tuning id, falling back to the ladder default. */
export function tuningLabel(kind: 'verify' | 'explore', id: string | null | undefined): string {
  if (kind === 'verify') {
    return VERIFICATION_LEVELS[levelIndex(VERIFICATION_LEVELS, id, DEFAULT_VERIFICATION_LEVEL)].label;
  }
  return EXPLORER_MODES[levelIndex(EXPLORER_MODES, id, DEFAULT_EXPLORER_MODE)].label;
}

/**
 * Reads `run_tuning` out of a session's `device_info` (which the sessions API
 * returns either as a JSON string or an already-parsed object).
 */
export function parseRunTuning(deviceInfo: unknown): SessionRunTuning | null {
  let info: unknown = deviceInfo;
  if (typeof info === 'string') {
    try {
      info = JSON.parse(info);
    } catch {
      return null;
    }
  }
  if (!info || typeof info !== 'object') return null;
  const tuning = (info as { run_tuning?: unknown }).run_tuning;
  if (!tuning || typeof tuning !== 'object') return null;
  const t = tuning as SessionRunTuning;
  if (!t.verification_level && !t.explorer_mode) return null;
  return {
    verification_level: t.verification_level ?? null,
    explorer_mode: t.explorer_mode ?? null
  };
}
