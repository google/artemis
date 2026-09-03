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
 * Launcher options for `/api/run` (`verification_level`, `explorer_mode`).
 * Keep ids in sync with `VERIFICATION_LEVEL_PRESETS` in `artemis/config/agent.py`
 * and `EXPLORER_TIERS` in `artemis/agents/explorer/tiers.py`.
 */

export type VerificationLevelId = 'off' | 'final' | 'checkpoints' | 'strict';
export type ExplorerModeId = 'flash' | 'pro' | 'ultra';

/** One notch on a tuning slider. */
export interface TuningLevel<TId extends string = string> {
  /** Wire value sent to the backend. */
  id: TId;
  /** Short name shown next to the slider title and as the hover card heading. */
  label: string;
  /** One-sentence summary shown in the hover card. */
  tagline: string;
  /** Plain-language time cost, e.g. "no extra time". */
  latency: string;
  /** Checks or searches performed at this level. */
  runs: string[];
  /** Checks or searches omitted at this level. */
  skips?: string[];
  /** When to pick this level. */
  bestFor: string;
}

export const VERIFICATION_LEVELS: readonly TuningLevel<VerificationLevelId>[] = [
  {
    id: 'off',
    label: 'Off',
    tagline: 'No checking. The run ends as soon as the task looks done.',
    latency: 'no extra time',
    runs: [
      'Each step is treated as finished the moment it is carried out.',
      'You get the full action trace, but no pass / fail verdict.'
    ],
    skips: ['Nothing is double-checked and nothing is retried.'],
    bestFor: 'Quick tries and demos, when you only want to watch what happens.'
  },
  {
    id: 'final',
    label: 'At the end',
    tagline: 'One check of the finished result against your goal. This is the default.',
    latency: 'adds about 20–60 s at the end',
    runs: [
      'When the task finishes, the final screen, the step history and the device state are compared with what you asked for.',
      'If the result does not match, the task goes back and tries to fix it, up to 3 times.'
    ],
    skips: ['Nothing is checked while the task is still running.'],
    bestFor: 'Everyday tasks: an honest pass / fail without slowing the run down.'
  },
  {
    id: 'checkpoints',
    label: 'Every step',
    tagline: 'Each step is checked as soon as it is done, plus the final check.',
    latency: 'a short check after each step, done in the background',
    runs: [
      'Every step is checked right after it completes, using the screenshots from that moment.',
      'If a step went wrong, it gets fixed before moving on (up to 2 tries per step).',
      'A failed test condition is written down and the task keeps going.',
      'The final check still runs at the end.'
    ],
    bestFor: 'Long tasks where one early mistake would spoil everything after it.'
  },
  {
    id: 'strict',
    label: 'Strict',
    tagline: 'Every step is checked, with more retries. The first failed test stops the run.',
    latency: 'slowest: more checks and more retries',
    runs: [
      'Each check takes longer and gets more attempts: 4 fixes per step and 5 at the end.',
      'The first failed test condition stops the run immediately, with the evidence attached.'
    ],
    bestFor: 'Release checks and regression runs, where a wrong pass is never acceptable.'
  }
];

export const EXPLORER_MODES: readonly TuningLevel<ExplorerModeId>[] = [
  {
    id: 'flash',
    label: 'Quick glance',
    tagline: 'Finds buttons and text on the screen in a single look.',
    latency: '1 look per search',
    runs: [
      'Something on screen is asked for by name, icon or colour and its position comes back straight away.',
      'Several things can be looked up at once.'
    ],
    skips: ['No zooming in and no second try.'],
    bestFor: 'Ordinary apps with clearly labelled buttons, icons and text.'
  },
  {
    id: 'pro',
    label: 'Second look',
    tagline: 'Takes up to 3 looks, thinking in between, before answering.',
    latency: 'up to 3 looks per search',
    runs: [
      'The screen layout is read first, then the picture is searched.',
      'If the first try misses, a different approach is tried within the 3 looks.'
    ],
    skips: ['Still no zooming into small areas, to keep searches short.'],
    bestFor: 'Things described by where they are ("the switch next to Wi-Fi") or with unclear labels.'
  },
  {
    id: 'ultra',
    label: 'Close-up',
    tagline: 'Zooms into parts of the screen and takes up to 8 looks.',
    latency: 'up to 8 looks per search (slowest)',
    runs: [
      'Parts of the screen can be cropped and magnified to read tiny text and crowded layouts piece by piece.',
      'Later looks reuse the earlier ones, so they cost less time than they sound.'
    ],
    bestFor: 'Crowded screens, tiny targets, charts and drawings, and checks where exact placement matters.'
  }
];

/** Per-run tuning sent with `/api/run` for the Pro profile. */
export interface ProTuningOptions {
  verificationLevel?: VerificationLevelId | string;
  explorerMode?: ExplorerModeId | string;
}

/** Effective defaults reported by `GET /api/run/defaults`. */
export interface ProTuningDefaults {
  verification_level?: string | null;
  explorer_mode?: string | null;
}

export const DEFAULT_VERIFICATION_LEVEL: VerificationLevelId = 'final';
export const DEFAULT_EXPLORER_MODE: ExplorerModeId = 'flash';

/** Index of a level id within its ladder; falls back to the default when unknown. */
export function levelIndex<TId extends string>(
  ladder: readonly TuningLevel<TId>[],
  id: string | null | undefined,
  fallback: TId
): number {
  const wanted = String(id ?? '').trim().toLowerCase();
  const idx = ladder.findIndex((l) => l.id === wanted);
  if (idx >= 0) return idx;
  return Math.max(0, ladder.findIndex((l) => l.id === fallback));
}

/** Slider fill percentage for a notch index on a ladder of `count` notches. */
export function notchPercent(index: number, count: number): number {
  if (count <= 1) return 0;
  const clamped = Math.min(Math.max(index, 0), count - 1);
  return (clamped / (count - 1)) * 100;
}
