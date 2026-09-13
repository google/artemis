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

import { Component, ChangeDetectionStrategy, inject, computed, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { AgentService } from '../../services/agent.service';
import { Session } from '../../core/models/session.model';
import { MarkdownSegment, MarkdownLine, NoteMilestone, ParsedNote } from '../../core/models/markdown.model';
import { parseNote, parseNoteLines } from '../../utils/markdown-parser.util';

export type { MarkdownSegment, MarkdownLine, NoteMilestone, ParsedNote };

@Component({
  selector: 'app-chat-interface',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './chat-interface.component.html',
  styleUrl: './chat-interface.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class ChatInterfaceComponent {
  public agentService = inject(AgentService);

  public taskInput: string = '';
  // Signals so async completion handlers refresh this OnPush view.
  public isSubmitting = signal<boolean>(false);
  public errorMessage = signal<string | null>(null);

  // Device-serial resolution can require a JSON.parse of device_info; memoize
  // it per session object so template re-evaluation stays cheap.
  private deviceSerialCache = new WeakMap<Session, string | null>();

  /**
   * Filtered computed list of active tasks (running or pending) sorted by status and submission order
   */
  public activeQueue = computed(() => {
    const list = this.agentService.sessions().filter((s) => {
      const status = this.getTaskStatus(s);
      return status === 'running' || status === 'paused' || status === 'pending';
    });
    return list.sort((a, b) => {
      const statusA = this.getTaskStatus(a);
      const statusB = this.getTaskStatus(b);
      const isRunA = statusA === 'running' || statusA === 'paused';
      const isRunB = statusB === 'running' || statusB === 'paused';
      if (isRunA && !isRunB) return -1;
      if (!isRunA && isRunB) return 1;
      return (a.start_time || 0) - (b.start_time || 0); // stable FIFO order
    });
  });

  /**
   * Filtered computed list of historical/completed tasks (completed, failed, or cancelled)
   */
  public historyTasks = computed(() => {
    return this.agentService.sessions().filter((s) => {
      const status = this.getTaskStatus(s);
      return status === 'completed' || status === 'failed' || status === 'cancelled';
    });
  });

  /**
   * Submit a new task goal to the backend
   */
  public submitTask(): void {
    const goal = this.taskInput.trim();
    if (!goal) {
      return;
    }

    this.isSubmitting.set(true);
    this.errorMessage.set(null);

    this.agentService.runTask(goal).subscribe({
      next: (res) => {
        this.taskInput = '';
        this.isSubmitting.set(false);
        // Fetch status to refresh sessions list and select new session
        this.agentService.fetchStatus();
      },
      error: (err) => {
        console.error('Failed to submit task:', err);
        this.isSubmitting.set(false);
        this.errorMessage.set(err.error?.detail || 'The runner is busy. Please wait for the current task to finish.');
        // Auto-dismiss error banner after 5 seconds
        setTimeout(() => {
          this.errorMessage.set(null);
        }, 5000);
      }
    });
  }

  /**
   * Stop an individual task session
   */
  public stopTask(sessionId: string, event: MouseEvent): void {
    event.stopPropagation();
    this.isSubmitting.set(true);
    this.errorMessage.set(null);
    this.agentService.stopTask(sessionId, false);
    setTimeout(() => {
      this.isSubmitting.set(false);
    }, 400);
  }

  /**
   * Stop the currently running task and optionally clear the backend queue
   */
  public stopTasks(stopAll: boolean = false): void {
    this.isSubmitting.set(true);
    this.errorMessage.set(null);
    this.agentService.stopTask(stopAll);
    setTimeout(() => {
      this.isSubmitting.set(false);
    }, 400);
  }

  /**
   * Clear all database data/history to start fresh
   */
  public clearHistory(): void {
    if (!confirm('Are you sure you want to clear all tasks and history? This cannot be undone.')) {
      return;
    }
    this.isSubmitting.set(true);
    this.errorMessage.set(null);
    this.agentService.clearAllHistory().subscribe({
      next: () => {
        this.isSubmitting.set(false);
      },
      error: (err: any) => {
        console.error('Failed to clear history:', err);
        this.isSubmitting.set(false);
        this.errorMessage.set(err.error?.detail || 'Failed to clear history.');
      }
    });
  }

  /**
   * Delete an individual task / session
   */
  public deleteTask(sessionId: string, event: MouseEvent): void {
    event.stopPropagation();
    if (!confirm(`Are you sure you want to delete this task? This cannot be undone.`)) {
      return;
    }
    this.isSubmitting.set(true);
    this.errorMessage.set(null);
    this.agentService.deleteSession(sessionId).subscribe({
      next: () => {
        this.isSubmitting.set(false);
      },
      error: (err: any) => {
        console.error(`Failed to delete task ${sessionId}:`, err);
        this.isSubmitting.set(false);
        this.errorMessage.set(err.error?.detail || 'Failed to delete task.');
      }
    });
  }

  /**
   * Determine the current task execution status
   */
  public getTaskStatus(session: Session): 'running' | 'paused' | 'completed' | 'pending' | 'failed' | 'cancelled' {
    if (session.status) {
      const s = session.status.toLowerCase();
      if (s === 'completed' || s === 'success' || s === 'failed' || s === 'cancelled') {
        return (s === 'success' ? 'completed' : s) as any;
      }
      if (s === 'running' || s === 'paused' || s === 'pending') {
        return s as any;
      }
    }
    if (session.session_id === this.agentService.runningSessionId() && (this.agentService.agentStatus() === 'running' || this.agentService.agentStatus() === 'paused')) {
      return this.agentService.agentStatus() as 'running' | 'paused';
    }
    return 'completed';
  }

  /**
   * Determine the device serial number for the session
   */
  public getDeviceSerial(session: Session): string | null {
    if (this.deviceSerialCache.has(session)) {
      return this.deviceSerialCache.get(session) ?? null;
    }
    let resolved: string | null = null;
    const serial = session.device_serial || session.device_id;
    if (serial && serial !== 'pending' && serial !== 'null' && serial !== 'undefined') {
      resolved = serial;
    } else if (session.device_info) {
      try {
        const info = typeof session.device_info === 'string' ? JSON.parse(session.device_info) : session.device_info;
        const s = info?.device_id || info?.device_serial;
        if (s && s !== 'pending' && s !== 'null' && s !== 'undefined') {
          resolved = s;
        }
      } catch {
        // ignore
      }
    }
    this.deviceSerialCache.set(session, resolved);
    return resolved;
  }

  /**
   * Select a session in the UI to monitor its steps
   */
  public selectTask(sessionId: string): void {
    this.agentService.selectSession(sessionId, true);
  }

  // Notes Computed Properties
  public currentNoteContent = computed(() => {
    const notes = this.agentService.currentNotes();
    const key = this.agentService.selectedNoteKey();
    return notes[key] || '';
  });

  public noteKeys = computed(() => {
    return Object.keys(this.agentService.currentNotes()).filter(key => key.toLowerCase().endsWith('.md'));
  });

  public parsedNote = computed<ParsedNote>(() => {
    return this.getParsedNote(this.currentNoteContent());
  });

  public getParsedNote(content: string): ParsedNote {
    return parseNote(content);
  }

  public getParsedNoteLines(content: string): MarkdownLine[] {
    return parseNoteLines(content);
  }

  public selectNote(key: string): void {
    this.agentService.selectedNoteKey.set(key);
  }

  public trackSession(index: number, session: Session): string {
    return session.session_id;
  }

  public trackNoteKey(index: number, key: string): string {
    return key;
  }

  public trackMilestone(index: number, milestone: NoteMilestone): number {
    return milestone.index;
  }

  public trackMarkdownLine(index: number, line: MarkdownLine): number {
    return index;
  }
}
