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

import { ModelInfo, Session } from '../core/models/session.model';

/** The model a task card shows, plus where that model came from. */
export interface TaskModelLabel {
  /** `provider · model`, or just whichever half is known. */
  label: string;
  /**
   * True when the label is what the task will resolve to rather than what it ran
   * with: the card has no session row (yet), so the model comes from the
   * override recorded on the queue item or from the globally active model.
   */
  scheduled: boolean;
}

/** `provider · model` from the halves that are known; null when neither is. */
function joinModelLabel(model: string, provider: string): string | null {
  if (model && provider) {
    return `${provider} · ${model}`;
  }
  return model || provider || null;
}

/**
 * Model shown on a task card, resolved in three steps: the per-task LLM
 * override recorded on the queue item, then the model persisted with the
 * session, then the globally active model. A queued task without an override
 * has no session row yet and resolves the configured default when the worker
 * dispatches it, which is what `activeModel` reports, so its card shows the
 * model the task will run with. Null when none of the three is known.
 */
export function getTaskModelLabel(
  session: Session,
  activeModel?: ModelInfo | null
): TaskModelLabel | null {
  const storedModel = (session.model_info?.id || '').trim();
  const storedProvider = (session.model_info?.provider || '').trim();
  // The override wins; a half-filled override still pairs with the stored half
  // so the label never loses half of it.
  const model = (session.llm_model || '').trim() || storedModel;
  const provider = (session.llm_provider || '').trim() || storedProvider;
  const label = joinModelLabel(model, provider);
  if (label) {
    return { label, scheduled: !storedModel && !storedProvider };
  }
  const activeLabel = joinModelLabel(
    (activeModel?.id || '').trim(),
    (activeModel?.provider || '').trim()
  );
  return activeLabel ? { label: activeLabel, scheduled: true } : null;
}
