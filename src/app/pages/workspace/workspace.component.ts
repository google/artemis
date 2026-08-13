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

import { Component, HostListener } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AgentStreamComponent } from '../../components/agent-stream/agent-stream.component';
import { ChatInterfaceComponent } from '../../components/chat-interface/chat-interface.component';
import { FloatingVideoPlayerComponent } from '../../components/floating-video-player/floating-video-player.component';

@Component({
  selector: 'app-workspace',
  standalone: true,
  imports: [CommonModule, AgentStreamComponent, ChatInterfaceComponent, FloatingVideoPlayerComponent],
  templateUrl: './workspace.component.html',
  styleUrl: './workspace.component.scss'
})
export class WorkspaceComponent {
  // Default right panel width to 1/3 of the screen (or 450px as fallback)
  public rightPanelWidth = typeof window !== 'undefined' ? Math.round(window.innerWidth / 3) : 450;
  public isDragging = false;

  /**
   * Handle mouse down on resizer bar to start dragging
   */
  public onDragStart(event: MouseEvent): void {
    this.isDragging = true;
    event.preventDefault(); // Prevent text selection highlight during resize drag
  }

  /**
   * Listen to global mousemove events to adjust the width dynamically
   */
  @HostListener('document:mousemove', ['$event'])
  public onMouseMove(event: MouseEvent): void {
    if (!this.isDragging) {
      return;
    }

    const newWidth = window.innerWidth - event.clientX;
    const minWidth = 250;
    const maxWidth = window.innerWidth - 300;

    // Apply boundary limits to prevent panels from shrinking too much
    if (newWidth >= minWidth && newWidth <= maxWidth) {
      this.rightPanelWidth = newWidth;
    }
  }

  /**
   * Listen to global mouseup events to stop resizing
   */
  @HostListener('document:mouseup')
  public onMouseUp(): void {
    this.isDragging = false;
  }
}
