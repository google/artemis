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

import { Injectable, signal, inject, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';

import { Session, ModelInfo, TaskQueueItem, AgentStatusResponse } from '../core/models/session.model';
import { StepItemData } from '../core/models/stream.model';
export type { Session, ModelInfo, TaskQueueItem, AgentStatusResponse, StepItemData };

@Injectable({
  providedIn: 'root'
})
export class AgentService {
  private http = inject(HttpClient);

  // Signals to expose state to components
  private rawSessions = signal<Session[]>([]);
  private pendingQueue = signal<Session[]>([]);

  public sessions = computed(() => {
    const raw = this.rawSessions();
    const pending = this.pendingQueue();
    const status = this.agentStatus();
    const goal = this.runningGoal();
    const runId = this.runningSessionId();

    const sessionMap = new Map<string, Session>();

    // 1. First add raw sessions from DB
    raw.forEach((s) => {
      const isCurrentRunning = status === 'running' && runId === s.session_id;
      const isPending = this.pendingQueue().some(p => p.session_id === s.session_id) && !isCurrentRunning;
      let sStatus = isCurrentRunning ? 'running' : (isPending ? 'pending' : s.status);
      if (sStatus === 'running' && !isCurrentRunning) {
        const activeRun = raw.find(r => r.session_id === runId);
        sStatus = (activeRun && (s.start_time || 0) > (activeRun.start_time || 0)) ? 'pending' : 'completed';
      }
      sessionMap.set(s.session_id, {
        ...s,
        status: sStatus,
        model_info: isCurrentRunning && this.activeModel() ? this.activeModel()! : s.model_info
      });
    });

    // 2. Add pending queue sessions if not yet in raw sessions
    pending.forEach((p) => {
      if (!sessionMap.has(p.session_id)) {
        const isCurrentRunning = status === 'running' && runId === p.session_id;
        sessionMap.set(p.session_id, {
          ...p,
          status: isCurrentRunning ? 'running' : 'pending'
        });
      }
    });

    // 3. Ensure currently active running session is present
    if (status === 'running' && runId && !sessionMap.has(runId)) {
      sessionMap.set(runId, {
        session_id: runId,
        initial_goal: goal || '',
        start_time: Date.now() / 1000,
        status: 'running',
        model_info: this.activeModel() || undefined
      });
    }

    return Array.from(sessionMap.values()).sort((a, b) => b.start_time - a.start_time);
  });

  public currentSessionId = signal<string | null>(null);
  public sessionLogs = signal<any[]>([]); // Dynamic array of all raw events received
  public agentStatus = signal<string>('idle'); // Status of the agent runner process
  public runningSessionId = signal<string | null>(null);
  public runningGoal = signal<string | null>(null);
  public isPaused = signal<boolean>(false);
  public pausedError = signal<string | null>(null);
  public isRetrying = signal<boolean>(false);
  public retryMessage = signal<string | null>(null);
  public activeModel = signal<{ name: string; id: string; provider: string } | null>(null);
  public userPinnedSessionId = signal<string | null>(null); // Pinned session if user explicitly selected a non-running task

  // Notes and Tab States
  public currentNotes = signal<Record<string, string>>({});
  public selectedNoteKey = signal<string>('task_plan.md');
  public activeTab = signal<'tasks' | 'notes'>('tasks');

  // Video Replay Floating Window States
  public isVideoWindowOpen = signal<boolean>(false);
  public isVideoMinimized = signal<boolean>(false);
  public activeVideoUrl = signal<string | null>(null);
  public activeVideoTitle = signal<string>('');
  public isVideoLoading = signal<boolean>(false);

  /**
   * Clear user-pinned selection so subsequent runs automatically follow active runner
   */
  public clearUserPinnedSession(): void {
    this.userPinnedSessionId.set(null);
  }

  /**
   * Computed active/viewed session
   */
  public currentSession = computed(() => {
    const curId = this.currentSessionId();
    if (!curId) return null;
    return this.sessions().find(s => s.session_id === curId) || null;
  });

  /**
   * Computed whether the currently viewed session is running
   */
  public isCurrentSessionRunning = computed(() => {
    const session = this.currentSession();
    if (session) return session.status === 'running';
    return this.agentStatus() === 'running';
  });

  /**
   * Computed video URL for the currently viewed session
   */
  public currentSessionVideoUrl = computed(() => {
    const curId = this.currentSessionId();
    if (!curId) return null;
    const session = this.sessions().find(s => s.session_id === curId);
    return session?.video_url || null;
  });

  private eventSource: EventSource | null = null;
  private statusInterval: any = null;

  constructor() {
    this.fetchSessions();
    this.startStatusPolling();
  }

  /**
   * Check if any task is currently running
   */
  public isRunningTask = computed(() => {
    if (this.agentStatus() === 'running') return true;
    return this.sessions().some(s => s.status === 'running');
  });

  /**
   * Run a new task by submitting to backend queue
   */
  public runTask(
    goal: string,
    profile: string = 'flash',
    expectedOutput?: string,
    enableOutputter?: boolean
  ): Observable<any> {
    return new Observable((obs) => {
      const payload: any = { goal, profile };
      if (expectedOutput && expectedOutput.trim()) {
        payload.expected_output = expectedOutput.trim();
      }
      if (enableOutputter !== undefined) {
        payload.enable_outputter = enableOutputter;
      }
      this.clearUserPinnedSession();
      this.http.post<any>('/api/run', payload).subscribe({
        next: (res) => {
          if (res && res.tasks && res.tasks.length > 0) {
            const newSessionId = res.tasks[0].session_id;
            if (newSessionId) {
              const isCurrentlyRunning =
                this.agentStatus() === 'running' ||
                this.sessions().some((s) => s.status === 'running');
              if (!isCurrentlyRunning) {
                this.selectSession(newSessionId, false);
              }
            }
          }
          obs.next(res);
          obs.complete();
        },
        error: (err) => {
          obs.error(err);
        }
      });
    });
  }

  /**
   * Stop the currently running task (and optionally clear queue)
   */
  public stopTask(stopAll: boolean = false): void {
    // Apply optimistic updates: only set idle if stopAll is true or pending queue is empty
    if (stopAll || this.pendingQueue().length === 0) {
      this.agentStatus.set('idle');
      this.runningSessionId.set(null);
      this.runningGoal.set(null);
    }
    this.isPaused.set(false);
    this.pausedError.set(null);
    this.isRetrying.set(false);
    if (stopAll) {
      this.pendingQueue.set([]);
    }

    // Mark active streaming / pending logs in current session as finished
    this.sessionLogs.update((logs) =>
      logs.map((l) => {
        if (l.type === 'llm_stream' && !l.data?.isCompleted) {
          return { ...l, data: { ...l.data, isCompleted: true } };
        }
        return l;
      })
    );

    this.http.post<any>(`/api/stop?all=${stopAll}`, {}).subscribe({
      next: () => {
        this.fetchStatus();
        this.fetchSessions();
      },
      error: (err) => {
        console.error('Failed to stop task:', err);
        this.fetchSessions();
        this.fetchStatus();
      }
    });
  }

  /**
   * Resume paused task
   */
  public resumeTask(): void {
    this.http.post('/api/resume', {}).subscribe({
      next: () => {
        this.isPaused.set(false);
        this.pausedError.set(null);
      },
      error: (err) => {
        console.error('Failed to resume task:', err);
      }
    });
  }

  /**
   * Fetch all past and active sessions from the backend
   */
  public fetchSessions(): void {
    this.http.get<Session[]>('/api/sessions').subscribe({
      next: (data) => {
        this.rawSessions.set(data);
        this.updateModelForCurrentView(data);
        // On initial load, if nothing is selected, not pinned, not running, and sessions exist, select latest
        if (!this.currentSessionId() && !this.userPinnedSessionId() && this.agentStatus() !== 'running' && data.length > 0) {
          this.selectSession(data[0].session_id, false);
        }
      },
      error: (err) => {
        console.error('Failed to fetch sessions from backend:', err);
      }
    });
  }

  /**
   * Delete an individual session / task
   */
  public deleteSession(sessionId: string): Observable<any> {
    // 1. Optimistically update local session state immediately
    this.rawSessions.update((list) => list.filter((s) => s.session_id !== sessionId));
    this.pendingQueue.update((list) => list.filter((s) => s.session_id !== sessionId));

    if (this.userPinnedSessionId() === sessionId) {
      this.userPinnedSessionId.set(null);
    }

    if (this.currentSessionId() === sessionId) {
      this.selectSession('', false);
      if (this.isVideoWindowOpen()) {
        this.closeVideoPlayer();
      }
      const runningId = this.runningSessionId();
      if (runningId && this.agentStatus() === 'running') {
        this.selectSession(runningId, false);
      }
    }

    // 2. Send API request
    return new Observable((obs) => {
      this.http.post<any>(`/api/sessions/${sessionId}/delete`, {}).subscribe({
        next: (res) => {
          this.fetchSessions();
          this.fetchStatus();
          obs.next(res);
          obs.complete();
        },
        error: (err) => {
          console.error(`Failed to delete session ${sessionId}:`, err);
          this.fetchSessions();
          this.fetchStatus();
          obs.error(err);
        }
      });
    });
  }

  /**
   * Clear all sessions, tasks, and history
   */
  public clearAllHistory(): Observable<any> {
    // 1. Optimistically clear all local session state immediately
    this.userPinnedSessionId.set(null);
    this.rawSessions.set([]);
    this.pendingQueue.set([]);
    this.selectSession('', false);
    if (this.isVideoWindowOpen()) {
      this.closeVideoPlayer();
    }

    // 2. Send API request
    return new Observable((obs) => {
      this.http.post<any>('/api/cleanup', {}).subscribe({
        next: (res) => {
          this.fetchSessions();
          this.fetchStatus();
          obs.next(res);
          obs.complete();
        },
        error: (err) => {
          console.error('Failed to cleanup history:', err);
          this.fetchSessions();
          this.fetchStatus();
          obs.error(err);
        }
      });
    });
  }

  /**
   * Connect to the Server-Sent Events stream for a specific session
   * @param sessionId The ID of the session to select
   * @param isUserAction Whether this selection was explicitly triggered by user interaction
   */
  public selectSession(sessionId: string, isUserAction: boolean = false): void {
    if (isUserAction) {
      if (sessionId && sessionId === this.runningSessionId() && this.agentStatus() === 'running') {
        // User explicitly clicked the currently running task -> follow live runner
        this.userPinnedSessionId.set(null);
      } else if (sessionId) {
        // User explicitly clicked a non-running or historical task -> pin to it
        this.userPinnedSessionId.set(sessionId);
      } else {
        this.userPinnedSessionId.set(null);
      }
    }

    if (!sessionId) {
      this.currentSessionId.set(null);
      this.sessionLogs.set([]);
      this.currentNotes.set({});
      if (this.eventSource) {
        this.eventSource.close();
        this.eventSource = null;
      }
      return;
    }

    if (this.currentSessionId() === sessionId) {
      return; // Already selected
    }

    this.currentSessionId.set(sessionId);
    this.sessionLogs.set([]); // Reset logs for new session selection
    this.isPaused.set(false);
    this.pausedError.set(null);
    this.fetchNotes(sessionId);
    this.updateModelForCurrentView();

    // If video window is currently open, dynamically sync/refresh video for new session
    if (this.isVideoWindowOpen()) {
      this.openVideoPlayer(sessionId);
    }

    // Clean up previous event stream connection if any
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
    }

    console.log(`Connecting to session: ${sessionId}`);
    this.http.get<StepItemData[]>(`/api/sessions/${sessionId}/steps`).subscribe({
      next: (steps) => {
        if (this.currentSessionId() !== sessionId) return;
        const historicalLogs: any[] = [];
        steps.forEach((step) => {
          const stepTimeIso = new Date((step.timestamp || Date.now() / 1000) * 1000).toISOString();
          historicalLogs.push({
            type: 'step_updated',
            session_id: sessionId,
            timestamp: stepTimeIso,
            data: step
          });
        });
        this.sessionLogs.set(historicalLogs);
      },
      error: (err) => {
        console.error('Failed to pre-load historical steps:', err);
      }
    });

    this.eventSource = new EventSource(`/api/stream/${sessionId}`);

    // Listen to standard SSE keep-alive
    this.eventSource.addEventListener('keep-alive', () => {
      // No-op keep-alive tick
    });

    // Listen to raw events and append them directly to the logs array
    const eventTypes = [
      'llm_stream',
      'trace_recorded',
      'step_recorded',
      'step_updated',
      'background_tasks_updated',
      'task_paused',
      'task_resumed',
      'llm_retrying',
      'session_started',
      'session_ended'
    ];

    eventTypes.forEach((eventType) => {
      this.eventSource?.addEventListener(eventType, (event: MessageEvent) => {
        try {
          const parsedData = JSON.parse(event.data);

          // If the event carries a session_id for a different session, update global states if applicable and ignore
          const evtSessionId = parsedData?.session_id;
          if (evtSessionId && String(evtSessionId) !== String(sessionId)) {
            if (eventType === 'session_started') {
              this.agentStatus.set('running');
              this.runningSessionId.set(parsedData.session_id);
              if (parsedData?.initial_goal) {
                this.runningGoal.set(parsedData.initial_goal);
              }
              this.fetchSessions();
              // Only auto-select if user hasn't explicitly chosen to inspect another task
              const pinnedId = this.userPinnedSessionId();
              if (!pinnedId && parsedData?.session_id) {
                this.selectSession(parsedData.session_id, false);
              }
            } else if (eventType === 'session_ended') {
              this.fetchStatus();
              this.fetchSessions();
              this.updateModelForCurrentView();
            }
            return;
          }

          // Guard against obsolete subscriptions
          if (this.currentSessionId() !== sessionId) {
            return;
          }

          if (eventType === 'session_started') {
            this.agentStatus.set('running');
            if (parsedData?.session_id) {
              this.runningSessionId.set(parsedData.session_id);
            }
            if (parsedData?.initial_goal) {
              this.runningGoal.set(parsedData.initial_goal);
            }
            this.fetchSessions();
            return;
          }

          if (eventType === 'session_ended') {
            this.agentStatus.set('idle');
            this.runningSessionId.set(null);
            this.runningGoal.set(null);
            this.isRetrying.set(false);
            this.isPaused.set(false);
            this.fetchSessions();
            this.fetchNotes(sessionId);
            this.updateModelForCurrentView();
            return;
          }

          if (eventType === 'llm_retrying') {
            this.isRetrying.set(true);
            const attempt = parsedData.attempt || 1;
            const max = parsedData.max_retries || 3;
            this.retryMessage.set(`Reconnecting to AI model (Attempt ${attempt}/${max})...`);
            return;
          }

          if (eventType === 'task_paused') {
            this.isPaused.set(true);
            this.isRetrying.set(false);
            this.pausedError.set(parsedData.error || 'AI call failed');
            return;
          }

          if (eventType === 'task_resumed') {
            this.isPaused.set(false);
            this.isRetrying.set(false);
            this.pausedError.set(null);
            return;
          }

          if (eventType === 'step_updated' || eventType === 'step_recorded' || eventType === 'llm_stream') {
            this.isRetrying.set(false);
          }

          if (eventType === 'llm_stream') {
            const execId = parsedData.execution_id;
            const stepId = parsedData.step_id;
            const chunk = parsedData.chunk;
            const streamType = parsedData.stream_type || 'text';

            this.sessionLogs.update((logs) => {
              // Find if there is an active stream log with same execution_id and stream_type
              const existingIndex = logs.findIndex(
                (l) => l.type === 'llm_stream' && 
                       l.data.execution_id === execId && 
                       (l.data.stream_type || 'text') === streamType
              );

              if (existingIndex > -1) {
                // Append text in place
                const updatedLogs = [...logs];
                const existingLog = updatedLogs[existingIndex];
                updatedLogs[existingIndex] = {
                  ...existingLog,
                  data: {
                    ...existingLog.data,
                    text: existingLog.data.text + chunk,
                    step_id: stepId || existingLog.data.step_id
                  }
                };
                return updatedLogs;
              } else {
                // First mark any other active streams of same stream_type as completed
                const updatedLogs = logs.map((l) => {
                  if (l.type === 'llm_stream' && 
                      (l.data.stream_type || 'text') === streamType && 
                      !l.data.isCompleted) {
                    return {
                      ...l,
                      data: { ...l.data, isCompleted: true }
                    };
                  }
                  return l;
                });

                // Add new stream log
                return [
                  ...updatedLogs,
                  {
                    type: 'llm_stream',
                    timestamp: new Date().toISOString(),
                    data: {
                      execution_id: execId,
                      step_id: stepId,
                      text: chunk,
                      stream_type: streamType,
                      isCompleted: false
                    }
                  }
                ];
              }
            });
          } else {
            // For other event types, mark any active stream as completed
            this.sessionLogs.update((logs) => {
              const updatedLogs = logs.map((l) => {
                if (l.type === 'llm_stream' && !l.data.isCompleted) {
                  return {
                    ...l,
                    data: { ...l.data, isCompleted: true }
                  };
                }
                return l;
              });

              const evtTime = typeof parsedData?.timestamp === 'number'
                ? new Date(parsedData.timestamp * 1000).toISOString()
                : new Date().toISOString();

              return [
                ...updatedLogs,
                {
                  type: eventType,
                  timestamp: evtTime,
                  data: parsedData
                }
              ];
            });
          }

          if (eventType === 'trace_recorded' && parsedData) {
            const trName = parsedData.name || '';
            if (['save_note', 'read_note', 'update_note', 'append_note', 'list_notes', 'outputter'].includes(trName.toLowerCase())) {
              const curSessionId = this.currentSessionId();
              if (curSessionId === sessionId) {
                this.fetchNotes(curSessionId);
              }
            }
          }
        } catch (e) {
          console.error(`Failed to parse ${eventType} event data:`, e);
          // Fallback to plain event data if parsing fails
          this.sessionLogs.update((logs) => [
            ...logs,
            {
              type: eventType,
              timestamp: new Date().toISOString(),
              data: event.data
            }
          ]);
        }
      });
    });

    this.eventSource.onerror = (err) => {
      console.error('EventSource connection error:', err);
    };
  }

  private pollCounter: number = 0;

  /**
   * Start periodic status polling from the backend
   */
  private startStatusPolling(): void {
    this.fetchStatus();
    this.statusInterval = setInterval(() => {
      this.fetchStatus();
      this.pollCounter++;
      // Periodically refresh sessions every 6 seconds (every 3 polling intervals of 2s) to stay in sync with external DB modifications
      if (this.pollCounter % 3 === 0) {
        this.fetchSessions();
      }
    }, 2000);
  }

  /**
   * Fetch current agent runner process status
   */
  public fetchStatus(): void {
    this.http.get<any>('/api/status').subscribe({
      next: (data) => {
        if (data && data.status) {
          const oldStatus = this.agentStatus();
          const oldRunningSessionId = this.runningSessionId();
          
          this.agentStatus.set(data.status);
          if (data.status === 'running') {
            this.runningSessionId.set(data.session_id || null);
            this.runningGoal.set(data.goal || null);
            if (data.model_info) {
              this.activeModel.set(data.model_info);
            }
          } else {
            this.runningSessionId.set(null);
            this.runningGoal.set(null);
            
            // If viewing a selected history session, use its model_info
            const curId = this.currentSessionId();
            if (curId) {
              const selected = this.sessions().find(s => s.session_id === curId);
              if (selected && selected.model_info) {
                this.activeModel.set(selected.model_info);
              } else if (data.model_info) {
                this.activeModel.set(data.model_info);
              }
            } else if (data.model_info) {
              this.activeModel.set(data.model_info);
            }
          }

          // Map backend pending queue to real Session / TaskQueueItem objects
          const pending: Session[] = (data.queue || []).map((item: any, index: number) => {
            if (typeof item === 'object' && item !== null) {
              return {
                session_id: item.session_id || `pending-task-${index}`,
                initial_goal: item.goal || '',
                start_time: item.start_time || item.created_at || (Date.now() / 1000 + index),
                status: item.status || 'pending'
              };
            }
            return {
              session_id: `task-queued-${index}`,
              initial_goal: String(item),
              start_time: (Date.now() / 1000) + index,
              status: 'pending'
            };
          });
          this.pendingQueue.set(pending);
          
          if (oldStatus !== data.status || oldRunningSessionId !== data.session_id) {
            this.fetchSessions();
          }

          // Auto-select the session if running and user hasn't explicitly chosen to inspect another task
          if (data.status === 'running' && data.session_id) {
            const currentId = this.currentSessionId();
            const pinnedId = this.userPinnedSessionId();
            if (!currentId) {
              this.selectSession(data.session_id, false);
            } else if (currentId !== data.session_id) {
              if (!pinnedId) {
                this.selectSession(data.session_id, false);
              }
            }
          }
        }
      },
      error: (err) => {
        console.error('Failed to fetch status from backend:', err);
        this.agentStatus.set('offline');
        this.runningSessionId.set(null);
        this.runningGoal.set(null);
      }
    });
  }

  /**
   * Update active model badge according to currently viewed session
   */
  private updateModelForCurrentView(sessionsList?: Session[]): void {
    const curId = this.currentSessionId();
    const allSessions = sessionsList || this.sessions();

    if (curId) {
      const selected = allSessions.find(s => s.session_id === curId);
      if (selected && selected.model_info) {
        this.activeModel.set(selected.model_info);
        return;
      }
    }
  }

  /**
   * Clean up event source connection and timers on service destruction
   */
  public destroy(): void {
    if (this.statusInterval) {
      clearInterval(this.statusInterval);
    }
    if (this.eventSource) {
      this.eventSource.close();
    }
  }

  /**
   * Fetch notes for a specific session
   */
  public fetchNotes(sessionId: string): void {
    if (!sessionId) {
      this.currentNotes.set({});
      return;
    }
    this.http.get<any>(`/api/sessions/${sessionId}/notes`).subscribe({
      next: (res) => {
        if (res && res.notes) {
          this.currentNotes.set(res.notes);
          
          // Default to task_plan.md if available, otherwise first note
          const keys = Object.keys(res.notes);
          if (keys.length > 0) {
            const currentSelected = this.selectedNoteKey();
            if (!keys.includes(currentSelected)) {
              if (keys.includes('task_plan.md')) {
                this.selectedNoteKey.set('task_plan.md');
              } else {
                this.selectedNoteKey.set(keys[0]);
              }
            }
          }
        }
      },
      error: (err) => {
        console.error(`Failed to fetch notes for session ${sessionId}:`, err);
      }
    });
  }

  /**
   * Open the video player for a given session or the current session
   */
  public openVideoPlayer(sessionId?: string, videoUrl?: string, title?: string): void {
    const targetSessionId = sessionId || this.currentSessionId();
    const session = this.sessions().find(s => s.session_id === targetSessionId);
    const targetUrl = videoUrl || session?.video_url || null;
    const goalTitle = title || session?.initial_goal || (targetSessionId ? `Task: ${targetSessionId.slice(0, 8)}...` : 'Screen Recording');

    this.activeVideoTitle.set(goalTitle);
    this.isVideoWindowOpen.set(true);
    this.isVideoMinimized.set(false);

    if (targetUrl) {
      this.activeVideoUrl.set(targetUrl);
    } else if (targetSessionId) {
      this.isVideoLoading.set(true);
      this.http.get<{ session_id: string; has_video: boolean; video_url: string | null }>(`/api/sessions/${targetSessionId}/video`).subscribe({
        next: (res) => {
          this.isVideoLoading.set(false);
          if (res && res.has_video && res.video_url) {
            this.activeVideoUrl.set(res.video_url);
            this.rawSessions.update((list) =>
              list.map((s) => s.session_id === targetSessionId ? { ...s, video_url: res.video_url || undefined } : s)
            );
          } else {
            this.activeVideoUrl.set(null);
          }
        },
        error: () => {
          this.isVideoLoading.set(false);
          this.activeVideoUrl.set(null);
        }
      });
    } else {
      this.activeVideoUrl.set(null);
    }
  }

  /**
   * Toggle the video player for the current session
   */
  public toggleVideoPlayer(): void {
    if (this.isVideoWindowOpen()) {
      this.isVideoWindowOpen.set(false);
    } else {
      this.openVideoPlayer();
    }
  }

  /**
   * Close the video player
   */
  public closeVideoPlayer(): void {
    this.isVideoWindowOpen.set(false);
  }
}
