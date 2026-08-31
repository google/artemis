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

import { Component, HostListener, ChangeDetectionStrategy } from '@angular/core';

import { LegacyAgentStreamComponent } from '../../components/legacy-agent-stream/legacy-agent-stream.component';
import { ChatInterfaceComponent } from '../../components/chat-interface/chat-interface.component';

@Component({
  selector: 'app-legacy-workspace',
  standalone: true,
  imports: [LegacyAgentStreamComponent, ChatInterfaceComponent],
  templateUrl: './legacy-workspace.component.html',
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: './legacy-workspace.component.scss'
})
export class LegacyWorkspaceComponent {
  public rightPanelWidth = typeof window !== 'undefined' ? Math.round(window.innerWidth / 3) : 450;
  public isDragging = false;

  public onDragStart(event: MouseEvent): void {
    this.isDragging = true;
    event.preventDefault();
  }

  @HostListener('document:mousemove', ['$event'])
  public onMouseMove(event: MouseEvent): void {
    if (!this.isDragging) {
      return;
    }

    const newWidth = window.innerWidth - event.clientX;
    const minWidth = 300;
    const maxWidth = window.innerWidth - 300;

    if (newWidth >= minWidth && newWidth <= maxWidth) {
      this.rightPanelWidth = newWidth;
    }
  }

  @HostListener('document:mouseup')
  public onMouseUp(): void {
    this.isDragging = false;
  }
}
