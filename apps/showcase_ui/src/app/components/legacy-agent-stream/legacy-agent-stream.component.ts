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

import { Component, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { AgentService } from '../../services/agent.service';

@Component({
  selector: 'app-legacy-agent-stream',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './legacy-agent-stream.component.html',
  styleUrl: './legacy-agent-stream.component.scss'
})
export class LegacyAgentStreamComponent {
  public agentService = inject(AgentService);

  /**
   * Handle dropdown session selection change
   */
  public onSessionChange(event: Event): void {
    const selectElement = event.target as HTMLSelectElement;
    const sessionId = selectElement.value;
    if (sessionId) {
      this.agentService.selectSession(sessionId, true);
    }
  }

  /**
   * Check if the event type represents a structured step record
   */
  public isStepEvent(eventType: string): boolean {
    return eventType === 'step_recorded' || eventType === 'step_updated';
  }

  /**
   * Determine if the content is short enough to remain open by default
   */
  public isShortContent(data: any): boolean {
    if (!data) return true;
    const str = typeof data === 'string' ? data : JSON.stringify(data);
    return str.length < 200;
  }

  /**
   * Format JSON data for display in HTML pre tag
   */
  public formatJson(data: any): string {
    if (typeof data === 'string') {
      try {
        return JSON.stringify(JSON.parse(data), null, 2);
      } catch (e) {
        return data;
      }
    }
    return JSON.stringify(data, null, 2);
  }
}
