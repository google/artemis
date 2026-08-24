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

import { Component, HostListener, inject, computed, signal, ViewChild, ElementRef, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { AgentStreamComponent } from '../../components/agent-stream/agent-stream.component';
import { ChatInterfaceComponent } from '../../components/chat-interface/chat-interface.component';
import { FloatingVideoPlayerComponent } from '../../components/floating-video-player/floating-video-player.component';
import { AgentService } from '../../services/agent.service';

@Component({
  selector: 'app-workspace',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    AgentStreamComponent,
    ChatInterfaceComponent,
    FloatingVideoPlayerComponent
  ],
  templateUrl: './workspace.component.html',
  styleUrl: './workspace.component.scss'
})
export class WorkspaceComponent implements OnInit {
  public agentService = inject(AgentService);

  // Default right panel width to 1/3 of the screen (or 450px as fallback)
  public rightPanelWidth = typeof window !== 'undefined' ? Math.round(window.innerWidth / 3) : 450;
  public isDragging = false;

  // Floating Command Bar State
  public taskInput: string = '';
  public isSubmitting: boolean = false;
  public errorMessage: string | null = null;
  public selectedProfile = signal<'flash' | 'pro'>('flash');

  // Expand States (Signals for 0-latency reactivity)
  public isHoveringCard = signal<boolean>(false);
  public isInputFocused = signal<boolean>(false);

  @ViewChild('dockInput') public dockInputRef?: ElementRef<HTMLTextAreaElement>;

  ngOnInit(): void {
    if (typeof localStorage !== 'undefined') {
      const saved = localStorage.getItem('artemis_selected_profile');
      if (saved === 'flash' || saved === 'pro') {
        this.selectedProfile.set(saved);
      }
    }
  }

  /**
   * Set model profile ('flash' vs 'pro')
   */
  public setProfile(profile: 'flash' | 'pro', event?: MouseEvent): void {
    if (event) {
      event.stopPropagation();
    }
    this.selectedProfile.set(profile);
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem('artemis_selected_profile', profile);
    }
  }

  /**
   * Computed boolean whether a task is currently executing or active
   */
  public isTaskRunning = computed(() => {
    return this.agentService.isRunningTask();
  });

  /**
   * Computed boolean whether the command bar should be in its expanded state:
   * - Mouse is hovering directly on the command card
   * - Input textarea is focused
   * - User has entered task text (drafting)
   */
  public isBarExpanded = computed(() => {
    return (
      this.isHoveringCard() ||
      this.isInputFocused() ||
      this.taskInput.trim().length > 0
    );
  });

  /**
   * Focus input handler
   */
  public onInputFocus(): void {
    this.isInputFocused.set(true);
  }

  /**
   * Blur input handler
   */
  public onInputBlur(): void {
    this.isInputFocused.set(false);
  }

  /**
   * Click on the resting capsule or dock card to focus textarea
   */
  public onCardClick(event: MouseEvent): void {
    const target = event.target as HTMLElement;
    // Don't steal focus if clicking action buttons or textarea directly
    if (target.closest('button') || target.tagName.toLowerCase() === 'textarea') {
      return;
    }
    this.focusInput();
  }

  /**
   * Focus textarea programmatically
   */
  public focusInput(): void {
    setTimeout(() => {
      this.dockInputRef?.nativeElement?.focus();
    }, 30);
  }

  /**
   * Clear typed task input
   */
  public clearInput(event?: MouseEvent): void {
    if (event) {
      event.stopPropagation();
    }
    this.taskInput = '';
    if (this.dockInputRef?.nativeElement) {
      this.dockInputRef.nativeElement.style.height = 'auto';
      this.dockInputRef.nativeElement.focus();
    }
  }

  /**
   * Auto-resize textarea as user types multi-line tasks
   */
  public onTextareaInput(event: Event): void {
    const textarea = event.target as HTMLTextAreaElement;
    if (!textarea) return;
    textarea.style.height = 'auto';
    const newHeight = Math.min(Math.max(textarea.scrollHeight, 24), 120);
    textarea.style.height = `${newHeight}px`;
  }

  /**
   * Global keyboard shortcut (⌘K or Ctrl+K) to focus input bar from anywhere
   */
  @HostListener('window:keydown', ['$event'])
  public onGlobalKeyDown(event: KeyboardEvent): void {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      this.focusInput();
    }
  }

  /**
   * Handle enter key in floating dock textarea
   */
  public onKeyDown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      this.submitTask();
    }
  }

  /**
   * Submit a new task instruction
   */
  public submitTask(): void {
    const goal = this.taskInput.trim();
    if (!goal || this.isSubmitting) {
      return;
    }

    this.isSubmitting = true;
    this.errorMessage = null;

    if (this.dockInputRef?.nativeElement) {
      this.dockInputRef.nativeElement.blur();
    }
    this.isInputFocused.set(false);

    this.agentService.runTask(goal, this.selectedProfile()).subscribe({
      next: (res) => {
        this.taskInput = '';
        if (this.dockInputRef?.nativeElement) {
          this.dockInputRef.nativeElement.style.height = 'auto';
        }
        this.isSubmitting = false;
        this.agentService.fetchStatus();
      },
      error: (err) => {
        console.error('Failed to submit task:', err);
        this.isSubmitting = false;
        this.errorMessage = err.error?.detail || 'The runner is busy. Please wait for current task to finish.';
        setTimeout(() => {
          this.errorMessage = null;
        }, 5000);
      }
    });
  }

  /**
   * Stop currently running task
   */
  public stopTask(event?: MouseEvent): void {
    if (event) {
      event.stopPropagation();
    }
    if (!this.isTaskRunning()) {
      return;
    }
    this.isSubmitting = true;
    this.errorMessage = null;
    this.agentService.stopTask(false);
    setTimeout(() => {
      this.isSubmitting = false;
    }, 400);
  }

  /**
   * Handle mouse down on resizer bar to start dragging
   */
  public onDragStart(event: MouseEvent): void {
    this.isDragging = true;
    event.preventDefault();
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
