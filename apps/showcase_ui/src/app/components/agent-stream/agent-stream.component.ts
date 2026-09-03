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

import { Component, ChangeDetectionStrategy, NgZone, signal, computed, effect, inject, untracked, DestroyRef, AfterViewInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { HttpClient } from '@angular/common/http';
import { AgentService, StartupProgressEvent } from '../../services/agent.service';
import { Session, ModelInfo } from '../../core/models/session.model';
import { MarkdownSegment, MarkdownLine, NoteMilestone, ParsedNote } from '../../core/models/markdown.model';
import { OverlayModule, ConnectedPosition } from '@angular/cdk/overlay';
import { StepBlock, PhaseBlock, StepEvent, ActionParam, CheckerResult, StreamResetNotice, DEFAULT_STREAM_RESET_MESSAGE } from '../../core/models/stream.model';

export const PLANNING_LOADER_PHRASES: string[] = [
  'Planning next step...',
  'Analyzing screen coordinates...',
  'Consulting neural network...',
  'Formulating tactical action plan...',
  'Deciphering UI state & elements...',
  'Synthesizing decision pathways...',
  'Calibrating next move...',
  'Aligning logical vectors...',
  'Evaluating optimal sub-goals...',
  'Gathering sensory inputs...',
  'Computing next interaction...',
  'Optimizing execution strategy...',
  'Simulating probable outcomes...',
  'Summoning AI intuition...',
  'Brewing the next command...',
  'Strategizing tactical moves...'
];

export interface StartupWorkItem extends StartupProgressEvent {
  isActive: boolean;
  elapsed: string;
}

interface StartupWorkStage {
  started: string;
  completed: string;
  completedMessage: string;
}

const STARTUP_WORK_STAGES: StartupWorkStage[] = [
  {
    started: 'device_check',
    completed: 'device_ready',
    completedMessage: 'Android device connected'
  },
  {
    started: 'uiautomator',
    completed: 'uiautomator_ready',
    completedMessage: 'UI Automator is ready'
  },
  {
    started: 'environment',
    completed: 'environment_ready',
    completedMessage: 'Device environment is ready'
  }
];

function formatStartupElapsed(seconds: number): string {
  const safeSeconds = Math.max(0, seconds);
  return safeSeconds < 10
    ? `${safeSeconds.toFixed(1)}s`
    : `${Math.round(safeSeconds)}s`;
}

/**
 * Collapse noisy process-level startup events into the three device preparation
 * operations that are useful to someone watching a run.
 */
export function buildStartupWorkItems(
  events: StartupProgressEvent[],
  nowSeconds: number,
  executionHasOutput: boolean,
  agentIsActive: boolean
): StartupWorkItem[] {
  const byStage = new Map(events.map((event) => [event.stage, event]));
  const firstResponse = byStage.get('first_response');

  return STARTUP_WORK_STAGES.flatMap((stage, stageIndex) => {
    const started = byStage.get(stage.started);
    const explicitlyCompleted = byStage.get(stage.completed);
    if (!started && !explicitlyCompleted) return [];

    const nextStageStarted = STARTUP_WORK_STAGES
      .slice(stageIndex + 1)
      .map((nextStage) => byStage.get(nextStage.started) || byStage.get(nextStage.completed))
      .find((event): event is StartupProgressEvent => Boolean(event));
    const inferredCompletion = stage.started === 'environment'
      ? firstResponse
      : nextStageStarted;
    const completed = explicitlyCompleted || inferredCompletion;
    const isActive = !completed && !executionHasOutput && agentIsActive;
    const startTimestamp = started?.timestamp || explicitlyCompleted?.timestamp || nowSeconds;
    const endTimestamp = completed?.timestamp
      || (isActive ? nowSeconds : events[events.length - 1]?.timestamp || startTimestamp);

    return [{
      ...(explicitlyCompleted || started!),
      message: explicitlyCompleted?.message
        || (completed ? stage.completedMessage : started!.message),
      isActive,
      elapsed: formatStartupElapsed(endTimestamp - startTimestamp)
    }];
  });
}

import {
  parseNote,
  parseNoteLines,
  extractCheckerResult,
  renderMarkdownToHtml
} from '../../utils/markdown-parser.util';

import {
  getActionObject,
  isAndroidAction,
  getActionIcon,
  getActionTitle,
  getActionTargetText,
  getActionInputLabel,
  getActionInputText,
  getActionCoords,
  getActionBounds,
  getActionResourceId,
  getActionClass,
  isActionFailed,
  getActionErrorMessage,
  extractActionExtraParams,
  getStepPreImageUrl,
  getStepPostImageUrl,
  isReportStatusAction,
  getReportStatusValue,
  getReportStatusExplanation
} from '../../utils/action-formatter.util';

import {
  getToolArgs,
  isNoteTool,
  isVideoTool,
  getVideoAnalysisView,
  formatVideoTime,
  getVideoToolTarget,
  isDeviceActionTool,
  isFailureAnalyzerActionTool,
  getToolAgentName,
  shouldShowTool,
  getUniqueGenericTools,
  getToolKey,
  getToolDisplayLabel,
  getToolIcon,
  getToolTitle,
  getToolTargetText,
  getToolInputLabel,
  getToolInputText,
  getToolCoords,
  getToolAnalysisText,
  getToolGenericDetails,
  isAdbCommandTool,
  getAdbCommandLine,
  getAdbCwd,
  getAdbTerminalId,
  isToolFailed,
  getToolErrorMessage,
  extractToolExtraParams,
  isHumanThinking,
  cleanErrorMessage
} from '../../utils/tool-formatter.util';

import { drawActionCoordinatesOnOverlay } from '../../utils/image-overlay.util';

import {
  consolidateLogsToBlocks,
  groupBlocksToPhases,
  extractBlockTokens,
  formatTokenCount,
  getSortedStepEvents,
  compileSessionSummary,
  computeSessionDuration,
  checkPlanningLoader
} from '../../utils/stream-aggregator.util';

export type { MarkdownSegment, MarkdownLine, NoteMilestone, ParsedNote, StreamResetNotice };

@Component({
  selector: 'app-agent-stream',
  standalone: true,
  imports: [CommonModule, FormsModule, OverlayModule],
  templateUrl: './agent-stream.component.html',
  styleUrl: './agent-stream.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class AgentStreamComponent implements AfterViewInit {
  public agentService = inject(AgentService);
  private http = inject(HttpClient);
  private destroyRef = inject(DestroyRef);
  private zone = inject(NgZone);

  // Auto-scroll state tracking
  public isUserAtBottom = true;
  private resizeObserver: ResizeObserver | null = null;
  private streamLogsContainer: HTMLElement | null = null;
  private autoScrollTimer: ReturnType<typeof setTimeout> | null = null;
  private autoScrollStreamBoxes = false;
  private onContainerScroll = () => {
    const container = this.streamLogsContainer;
    if (!container) return;
    const threshold = 150;
    const distanceToBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
    this.isUserAtBottom = distanceToBottom <= threshold;
  };

  // Dynamic Planning Loader Game-style Phrases
  public currentPlanningText = signal<string>(PLANNING_LOADER_PHRASES[0]);
  private planningIntervalId: any = null;
  private currentPhraseIndex = 0;

  // Top Nav Dropdown States
  public isTaskDropdownOpen = signal<boolean>(false);
  public isNotesDropdownOpen = signal<boolean>(false);

  // CDK Connected Overlay Positioning Strategy
  public readonly dropdownPositions: ConnectedPosition[] = [
    {
      originX: 'end',
      originY: 'bottom',
      overlayX: 'end',
      overlayY: 'top',
      offsetY: 6,
    },
    {
      originX: 'end',
      originY: 'top',
      overlayX: 'end',
      overlayY: 'bottom',
      offsetY: -6,
    },
    {
      originX: 'start',
      originY: 'bottom',
      overlayX: 'start',
      overlayY: 'top',
      offsetY: 6,
    }
  ];

  // Zoom Modal State
  public selectedZoomImage = signal<string | null>(null);
  public selectedZoomAction = signal<any | null>(null);

  // Signals and properties for managing delayed collapsing of thought streams
  public collapsedStreams = signal<Set<string>>(new Set<string>());
  private scheduledCollapses = new Set<string>();

  // Signals and maps for typewriter effect simulation. All active typewriters
  // are driven by ONE requestAnimationFrame loop (run outside the Angular
  // zone) that batches every text advance into a single signal update.
  public typedTextsSignal = signal<Record<string, string>>({});
  private typingTargets = new Map<string, {
    blockId: string;
    isThinking: boolean;
    execId?: string;
    fallbackText: string;
  }>();
  private typingRafId: number | null = null;
  private lastTypingTimestamp = 0;
  // ≈ legacy pace of 8 characters every 12ms interval tick.
  private static readonly TYPING_CHARS_PER_SECOND = 667;
  private static readonly TYPING_MIN_FRAME_MS = 28;

  // Active reset notices per block (displayed after typewriter rewind finishes)
  public activeBlockResets = signal<Map<string, StreamResetNotice[]>>(new Map());
  public activeResetNotices = signal<Map<string, string>>(new Map());
  private rewindTargets = new Map<string, {
    blockId: string;
    isThinking: boolean;
    execId?: string;
    message: string;
  }>();
  // Fast rewind pace (backspace speed ≈ 4x normal typing speed for snappy deletion)
  private static readonly REWIND_CHARS_PER_SECOND = 2600;

  // Rendered-markdown memo per template slot: returning the identical string
  // instance for unchanged text lets the [innerHTML] binding skip re-sanitizing
  // and re-parsing the whole fragment on every change-detection pass.
  private markdownHtmlCache = new Map<string, { source: string; html: string }>();
  // Thinking-text extraction memo (includes the JSON-detection heuristics).
  private thinkingTextCache = new WeakMap<any, { native: string | null; raw: string | null }>();
  private deviceSerialCache = new WeakMap<Session, string | null>();

  // Set to track expanded and collapsed state of action cards
  public expandedActionCards = signal<Set<string>>(new Set<string>());
  public collapsedActionCards = signal<Set<string>>(new Set<string>());
  public retryClock = signal<number>(Date.now());

  // Performance caches for parameter and event extractions
  private actionParamsCache = new WeakMap<any, ActionParam[]>();
  private toolParamsCache = new WeakMap<any, ActionParam[]>();
  private sortedEventsCache = new WeakMap<any, { signature: string; events: StepEvent[] }>();

  // Top Nav Task Queue Computed Properties
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
      return (a.start_time || 0) - (b.start_time || 0);
    });
  });

  public historyTasks = computed(() => {
    return this.agentService.sessions().filter((s) => {
      const status = this.getTaskStatus(s);
      return status === 'completed' || status === 'failed' || status === 'cancelled';
    });
  });

  // Top Nav Notes Computed Properties
  public currentNoteContent = computed(() => {
    const notes = this.agentService.currentNotes();
    const key = this.agentService.selectedNoteKey();
    return notes[key] || '';
  });

  public noteKeys = computed(() => {
    return Object.keys(this.agentService.currentNotes()).filter(key => key.toLowerCase().endsWith('.md'));
  });


  public outputterReport = computed(() => {
    const notes = this.agentService.currentNotes();
    return notes['output.md'] || null;
  });

  // Memoized so the Task Report card is not re-parsed (and its DOM rebuilt)
  // on every change-detection pass while streams are typing.
  public parsedOutputReport = computed<ParsedNote | null>(() => {
    const report = this.outputterReport();
    return report ? parseNote(report) : null;
  });

  public parsedNote = computed<ParsedNote>(() => {
    return this.getParsedNote(this.currentNoteContent());
  });

  // Stream Log Filtering & Aggregation Computed Properties
  public filteredLogs = computed(() => {
    const logs = this.agentService.sessionLogs();
    const currentSessionId = this.agentService.currentSessionId();
    if (!currentSessionId) return logs;
    return logs.filter((log: any) => {
      const logSessionId = log.session_id || log.data?.session_id;
      return !logSessionId || logSessionId === currentSessionId;
    });
  });

  public sessionSummary = computed(() => {
    return compileSessionSummary(this.filteredLogs());
  });

  public showPlanningLoader = computed(() => {
    const isRunning = this.agentService.agentStatus() === 'running';
    const currentSessionId = this.agentService.currentSessionId();
    const runningSessionId = this.agentService.runningSessionId();
    const isCurrentRunningSession = !currentSessionId || currentSessionId === runningSessionId;
    return checkPlanningLoader(this.filteredLogs(), isRunning, isCurrentRunningSession);
  });

  public isViewingPausedTask = computed(() => {
    // A live task_paused event and the polled runner status must agree before
    // recovery controls are shown. This prevents a stale failure trace from
    // offering Resume while the active task is still reported as running.
    if (!this.agentService.isPaused() || this.agentService.agentStatus() !== 'paused') return false;

    const currentSessionId = this.agentService.currentSessionId();
    const pausedSessionId = this.agentService.runningSessionId();
    return !currentSessionId || currentSessionId === pausedSessionId;
  });

  public sessionDuration = computed(() => {
    const currentSession = this.agentService.sessions().find(s => s.session_id === this.agentService.currentSessionId());
    return computeSessionDuration(currentSession?.start_time || 0, this.filteredLogs());
  });

  public consolidatedBlocks = computed<StepBlock[]>(() => {
    return consolidateLogsToBlocks(this.filteredLogs());
  });

  public phases = computed<PhaseBlock[]>(() => {
    const currentSession = this.agentService.sessions().find(s => s.session_id === this.agentService.currentSessionId());
    return groupBlocksToPhases(this.consolidatedBlocks(), currentSession?.start_time || 0);
  });

  public startupWorkItems = computed(() => {
    this.retryClock();
    const events = this.agentService.currentStartupProgress();
    const agentIsActive = ['running', 'paused'].includes(this.agentService.agentStatus());
    return buildStartupWorkItems(
      events,
      Date.now() / 1000,
      this.consolidatedBlocks().length > 0,
      agentIsActive
    );
  });

  public startupPreparationIsRunning = computed(() => {
    return this.startupWorkItems().some((item) => item.isActive);
  });

  public startupPreparationIsComplete = computed(() => {
    const items = this.startupWorkItems();
    return items.length > 0 && !items.some((item) => item.isActive);
  });

  public startupPreparationDuration = computed(() => {
    const items = this.startupWorkItems();
    if (items.length === 0) return 0;

    const events = this.agentService.currentStartupProgress();
    const startedAt = events[0]?.timestamp || items[0].timestamp;
    const completedAt = events.find((event) => event.stage === 'environment_ready')?.timestamp
      || events.find((event) => event.stage === 'first_response')?.timestamp;
    const endedAt = this.startupPreparationIsRunning()
      ? Date.now() / 1000
      : completedAt || Math.max(...items.map((item) => item.timestamp));

    return Math.max(1, Math.round(endedAt - startedAt));
  });

  private retryablePauseTrace = computed<any | null>(() => {
    if (!this.isViewingPausedTask()) return null;

    const blocks = this.consolidatedBlocks();
    for (let blockIndex = blocks.length - 1; blockIndex >= 0; blockIndex--) {
      const tools = blocks[blockIndex]?.data?.generic_tools;
      if (!Array.isArray(tools)) continue;

      for (let toolIndex = tools.length - 1; toolIndex >= 0; toolIndex--) {
        const tool = tools[toolIndex];
        // Older persisted traces used the llm_pause name without adding the
        // newer payload.pause marker. Treat both contracts consistently.
        if (this.isDisplayableLLMFailure(tool)) {
          return tool;
        }
      }
    }
    return null;
  });

  public hasVisibleBlocks(phase: any): boolean {
    if (!phase || !phase.blocks) return false;
    return phase.blocks.some((b: any) => this.hasVisibleContent(b));
  }

  constructor() {
    // The retry clock only needs to tick while something on screen is timing
    // (startup progress, LLM retry countdowns). An idle page stays quiet.
    this.zone.runOutsideAngular(() => {
      const retryClockInterval = setInterval(() => {
        const status = this.agentService.agentStatus();
        if (status === 'running' || status === 'paused' || this.agentService.isRetrying()) {
          this.retryClock.set(Date.now());
        }
      }, 1000);
      this.destroyRef.onDestroy(() => clearInterval(retryClockInterval));
    });

    // Auto scroll stream box during active text streaming
    effect(() => {
      const logs = this.filteredLogs();
      const activeStream = logs.find(log => log.type === 'llm_stream' && !log.data?.isCompleted);
      if (activeStream) {
        this.scheduleAutoScroll(true);
      }
    });

    // Auto scroll main log list when new logs are added or typewriter updates
    effect(() => {
      this.consolidatedBlocks();
      this.typedTextsSignal();
      this.scheduleAutoScroll(false);
    });

    // Listen for stream reset events to trigger fast typewriter rewind
    effect(() => {
      const resetEvent = this.agentService.streamResetEvent();
      if (!resetEvent) return;
      untracked(() => {
        this.triggerStreamRewind(
          resetEvent.stream_execution_id || resetEvent.stream_exec_id,
          resetEvent.message || DEFAULT_STREAM_RESET_MESSAGE
        );
      });
    });

    // Typewriter drive logic: triggers typing when block appears or expands
    effect(() => {
      const blocks = this.consolidatedBlocks();
      blocks.forEach(block => {
        // Check blocks render their per-turn segments directly (no typewriter).
        if (block.type === 'checker') return;
        const blockId = block.id;

        // If the block is flagged as reset, ensure typewriter rewind is active or reset notice is set
        if (block.data?.isReset) {
          untracked(() => {
            const resetMsg = block.data.resetMessage || DEFAULT_STREAM_RESET_MESSAGE;
            const currentRecord = this.typedTextsSignal();
            const hasText = (currentRecord[blockId]?.length || 0) > 0 || (currentRecord[blockId + '-native']?.length || 0) > 0;
            if (hasText) {
              if (!this.rewindTargets.has(blockId) && !this.rewindTargets.has(blockId + '-native')) {
                this.triggerStreamRewind(block.data.execution_id, resetMsg);
              }
            } else {
              const activeResets = this.activeBlockResets().get(blockId) || [];
              if (activeResets.length === 0) {
                this.upsertBlockResetNotice(blockId, {
                  id: block.data.execution_id || `${blockId}-text`,
                  message: resetMsg,
                  isWaiting: !block.data?.isCompleted,
                  streamType: 'text'
                });
              }
              if (!this.activeResetNotices().has(blockId)) {
                this.activeResetNotices.update(m => {
                  const next = new Map(m);
                  next.set(blockId, resetMsg);
                  return next;
                });
              }
            }
          });
          return;
        }

        const rawText = this.getRawThinking(block) || '';
        const nativeText = this.getNativeThinking(block) || '';

        untracked(() => {
          // Handle Raw Thinking (Work)
          const currentRecord = this.typedTextsSignal();
          if (rawText) {
            if (!(blockId in currentRecord)) {
              if (block.data?.isCompleted) {
                this.typedTextsSignal.update(r => ({ ...r, [blockId]: rawText }));
              } else {
                this.typedTextsSignal.update(r => ({ ...r, [blockId]: '' }));
                this.startTyping(blockId, rawText, false);
              }
            } else {
              const currentVal = currentRecord[blockId] || '';
              if (currentVal.length < rawText.length && !this.typingTargets.has(blockId)) {
                if (block.data?.isCompleted) {
                  this.typedTextsSignal.update(r => ({ ...r, [blockId]: rawText }));
                } else {
                  this.startTyping(blockId, rawText, false);
                }
              }
            }
          }

          // Handle Native Thinking (Thought)
          const nativeKey = blockId + '-native';
          if (nativeText) {
            const execId = block.data?.execution_id || block.data?.step_id || blockId;
            if (!(nativeKey in currentRecord)) {
              if (block.data?.isCompleted) {
                this.typedTextsSignal.update(r => ({ ...r, [nativeKey]: nativeText }));
                this.collapsedStreams.update(set => {
                  const newSet = new Set(set);
                  newSet.add(execId);
                  return newSet;
                });
              } else {
                this.typedTextsSignal.update(r => ({ ...r, [nativeKey]: '' }));
                this.startTyping(nativeKey, nativeText, true, execId);
              }
            } else {
              const currentVal = currentRecord[nativeKey] || '';
              if (currentVal.length < nativeText.length && !this.typingTargets.has(nativeKey)) {
                if (block.data?.isCompleted) {
                  this.typedTextsSignal.update(r => ({ ...r, [nativeKey]: nativeText }));
                  this.collapsedStreams.update(set => {
                    const newSet = new Set(set);
                    newSet.add(execId);
                    return newSet;
                  });
                } else {
                  this.startTyping(nativeKey, nativeText, true, execId);
                }
              }
            }
          }
        });
      });
    });

    // Reset typewriter and collapse states when session changes
    effect(() => {
      this.agentService.currentSessionId();
      untracked(() => {
        this.stopTypingLoop();
        this.typedTextsSignal.set({});
        this.activeResetNotices.set(new Map());
        this.activeBlockResets.set(new Map());
        this.markdownHtmlCache.clear();
        this.scheduledCollapses.clear();
        this.collapsedStreams.set(new Set<string>());
        this.isUserAtBottom = true;
        
        setTimeout(() => {
          const container = document.querySelector('.stream-logs-content');
          if (container) {
            container.scrollTop = container.scrollHeight;
          }
        }, 100);
      });
    });

    // Periodically rotate loading text like game loading screen hints
    effect(() => {
      const isLoading = this.showPlanningLoader();
      if (isLoading) {
        if (!this.planningIntervalId) {
          this.currentPhraseIndex = Math.floor(Math.random() * PLANNING_LOADER_PHRASES.length);
          this.currentPlanningText.set(PLANNING_LOADER_PHRASES[this.currentPhraseIndex]);

          this.planningIntervalId = setInterval(() => {
            this.currentPhraseIndex = (this.currentPhraseIndex + 1) % PLANNING_LOADER_PHRASES.length;
            this.currentPlanningText.set(PLANNING_LOADER_PHRASES[this.currentPhraseIndex]);
          }, 2800);
        }
      } else {
        if (this.planningIntervalId) {
          clearInterval(this.planningIntervalId);
          this.planningIntervalId = null;
        }
      }
    });

    this.destroyRef.onDestroy(() => {
      if (this.planningIntervalId) {
        clearInterval(this.planningIntervalId);
        this.planningIntervalId = null;
      }
      if (this.resizeObserver) {
        this.resizeObserver.disconnect();
        this.resizeObserver = null;
      }
      this.stopTypingLoop();
      if (this.autoScrollTimer) {
        clearTimeout(this.autoScrollTimer);
        this.autoScrollTimer = null;
      }
      if (this.streamLogsContainer) {
        this.streamLogsContainer.removeEventListener('scroll', this.onContainerScroll);
        this.streamLogsContainer = null;
      }
    });
  }

  public ngAfterViewInit(): void {
    const container = document.querySelector('.stream-logs-content') as HTMLElement | null;
    this.streamLogsContainer = container;
    if (!container) return;

    // The scroll listener lives outside the Angular zone: tracking the
    // user's scroll position must not trigger change detection per frame.
    this.zone.runOutsideAngular(() => {
      container.addEventListener('scroll', this.onContainerScroll, { passive: true });
    });

    if (typeof ResizeObserver !== 'undefined') {
      this.resizeObserver = new ResizeObserver(() => {
        if (this.isUserAtBottom) {
          this.scheduleAutoScroll(false);
        }
      });
      this.resizeObserver.observe(container);
    }
  }

  /**
   * Coalesce every auto-scroll request into one deferred pass that batches all
   * DOM reads before all writes, instead of thrashing layout per update.
   */
  private scheduleAutoScroll(includeStreamBoxes: boolean): void {
    this.autoScrollStreamBoxes = this.autoScrollStreamBoxes || includeStreamBoxes;
    if (this.autoScrollTimer) return;
    this.zone.runOutsideAngular(() => {
      this.autoScrollTimer = setTimeout(() => {
        this.autoScrollTimer = null;
        const includeBoxes = this.autoScrollStreamBoxes;
        this.autoScrollStreamBoxes = false;

        const boxes = includeBoxes
          ? (Array.from(document.getElementsByClassName('stream-box')) as HTMLElement[])
          : [];
        const boxTargets = boxes.map((el) => el.scrollHeight);
        const container = this.streamLogsContainer;
        const containerTarget = container && this.isUserAtBottom ? container.scrollHeight : null;

        boxes.forEach((el, i) => { el.scrollTop = boxTargets[i]; });
        if (container && containerTarget !== null) {
          container.scrollTop = containerTarget;
        }
      }, 50);
    });
  }

  // Top Nav Action Methods
  public toggleTaskDropdown(event: Event): void {
    event.stopPropagation();
    this.isTaskDropdownOpen.update(v => !v);
    if (this.isTaskDropdownOpen()) {
      this.isNotesDropdownOpen.set(false);
    }
  }

  public toggleNotesDropdown(event: Event): void {
    event.stopPropagation();
    this.isNotesDropdownOpen.update((v) => !v);
    if (this.isNotesDropdownOpen()) {
      this.isTaskDropdownOpen.set(false);
    }
  }

  public openOutputterNote(event?: Event): void {
    if (event) event.stopPropagation();
    this.agentService.selectedNoteKey.set('output.md');
    this.isNotesDropdownOpen.set(true);
    this.isTaskDropdownOpen.set(false);
  }

  public closeAllDropdowns(): void {
    this.isTaskDropdownOpen.set(false);
    this.isNotesDropdownOpen.set(false);
  }

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

  public selectTask(sessionId: string, event?: Event): void {
    if (event) event.stopPropagation();
    this.agentService.selectSession(sessionId, true);
    this.isTaskDropdownOpen.set(false);
  }

  public stopTask(sessionId: string, event?: Event): void {
    if (event) event.stopPropagation();
    this.agentService.stopTask(sessionId, false);
  }

  public deleteTask(sessionId: string, event?: Event): void {
    if (event) event.stopPropagation();
    if (!confirm(`Are you sure you want to delete this task? This cannot be undone.`)) {
      return;
    }
    this.agentService.deleteSession(sessionId).subscribe({
      error: (err) => {
        console.error(`Failed to delete session ${sessionId}:`, err);
      }
    });
  }

  public selectNote(key: string, event?: Event): void {
    if (event) event.stopPropagation();
    this.agentService.selectedNoteKey.set(key);
  }

  public getParsedNote(content: string): ParsedNote {
    return parseNote(content);
  }

  public getParsedNoteLines(content: string): MarkdownLine[] {
    return parseNoteLines(content);
  }

  // Typewriter & Delayed Collapse
  private upsertBlockResetNotice(blockId: string, notice: StreamResetNotice): void {
    this.activeBlockResets.update(m => {
      const next = new Map(m);
      const list = [...(next.get(blockId) || [])];
      const existingIdx = list.findIndex(r => r.id === notice.id);
      if (existingIdx > -1) {
        list[existingIdx] = notice;
      } else {
        list.push(notice);
      }
      next.set(blockId, list);
      return next;
    });
  }

  private startTyping(key: string, targetText: string, isThinking: boolean = false, execId?: string) {
    const blockId = key.endsWith('-native') ? key.replace('-native', '') : key;
    // If there was an active rewind, cancel it, reset text to empty, and record its reset notice as completed waiting
    const activeRewind = this.rewindTargets.get(key);
    if (activeRewind) {
      this.rewindTargets.delete(key);
      this.typedTextsSignal.update(r => ({ ...r, [key]: '' }));
      this.upsertBlockResetNotice(blockId, {
        id: activeRewind.execId || `${blockId}-${isThinking ? 'thinking' : 'text'}`,
        message: activeRewind.message,
        isWaiting: false,
        streamType: isThinking ? 'thinking' : 'text'
      });
    }

    // Cancel any active rewind on this key
    this.rewindTargets.delete(key);

    // When retry stream starts outputting content, change waiting dot to normal dot (isWaiting: false)
    // The previous prompt sentence DOES NOT disappear.
    this.activeBlockResets.update(m => {
      const list = m.get(blockId);
      if (!list || list.length === 0) return m;
      const targetType = isThinking ? 'thinking' : 'text';
      const updated = list.map(r => r.streamType === targetType ? { ...r, isWaiting: false } : r);
      const next = new Map(m);
      next.set(blockId, updated);
      return next;
    });

    this.typingTargets.set(key, { blockId, isThinking, execId, fallbackText: targetText });
    this.ensureTypingLoop();
  }

  private ensureTypingLoop(): void {
    if (this.typingRafId !== null) return;
    this.lastTypingTimestamp = performance.now();
    this.zone.runOutsideAngular(() => {
      const step = (now: number) => {
        this.typingRafId = null;
        const elapsed = now - this.lastTypingTimestamp;
        if (elapsed >= AgentStreamComponent.TYPING_MIN_FRAME_MS) {
          this.lastTypingTimestamp = now;
          this.advanceTyping(elapsed);
        }
        if (this.typingTargets.size > 0 || this.rewindTargets.size > 0) {
          this.typingRafId = requestAnimationFrame(step);
        }
      };
      this.typingRafId = requestAnimationFrame(step);
    });
  }

  private stopTypingLoop(): void {
    this.typingTargets.clear();
    this.rewindTargets.clear();
    if (this.typingRafId !== null) {
      cancelAnimationFrame(this.typingRafId);
      this.typingRafId = null;
    }
  }

  /**
   * Advance every active typewriter by the elapsed wall time and publish all
   * changes as one signal update (one render pass per animation frame at most).
   */
  private advanceTyping(elapsedMs: number): void {
    const blocks = untracked(() => this.consolidatedBlocks());
    const currentRecord = untracked(() => this.typedTextsSignal());
    const chars = Math.max(
      1,
      Math.round(AgentStreamComponent.TYPING_CHARS_PER_SECOND * elapsedMs / 1000)
    );
    const rewindChars = Math.max(
      2,
      Math.round(AgentStreamComponent.REWIND_CHARS_PER_SECOND * elapsedMs / 1000)
    );
    const updates: Record<string, string> = {};
    let hasUpdates = false;

    // 1. Advance forward typing targets
    for (const [key, typing] of this.typingTargets) {
      const block = blocks.find((b) => b.id === typing.blockId);
      let target = typing.fallbackText;
      if (block) {
        target = (typing.isThinking ? this.getNativeThinking(block) : this.getRawThinking(block))
          || typing.fallbackText;
      }

      const current = currentRecord[key] || '';
      if (current.length < target.length) {
        updates[key] = target.slice(0, Math.min(current.length + chars, target.length));
        hasUpdates = true;
      } else if (block?.data?.isCompleted) {
        this.typingTargets.delete(key);
        if (typing.isThinking && typing.execId) {
          this.triggerDelayedCollapse(typing.execId);
        }
      }
    }

    // 2. Advance fast rewind targets (backspacing for mid-stream resets)
    const completedRewinds: Array<{ blockId: string; execId?: string; isThinking: boolean; message: string }> = [];
    for (const [key, rewind] of this.rewindTargets) {
      const current = updates[key] !== undefined ? updates[key] : (currentRecord[key] || '');
      if (current.length > 0) {
        const nextLen = Math.max(0, current.length - rewindChars);
        updates[key] = current.slice(0, nextLen);
        hasUpdates = true;
        if (nextLen === 0) {
          this.rewindTargets.delete(key);
          completedRewinds.push({
            blockId: rewind.blockId,
            execId: rewind.execId,
            isThinking: rewind.isThinking,
            message: rewind.message
          });
        }
      } else {
        this.rewindTargets.delete(key);
        completedRewinds.push({
          blockId: rewind.blockId,
          execId: rewind.execId,
          isThinking: rewind.isThinking,
          message: rewind.message
        });
      }
    }

    if (completedRewinds.length > 0) {
      for (const item of completedRewinds) {
        this.upsertBlockResetNotice(item.blockId, {
          id: item.execId || `${item.blockId}-${item.isThinking ? 'thinking' : 'text'}`,
          message: item.message,
          isWaiting: true,
          streamType: item.isThinking ? 'thinking' : 'text'
        });
      }
      this.activeResetNotices.update((notices) => {
        const next = new Map(notices);
        for (const item of completedRewinds) {
          next.set(item.blockId, item.message);
        }
        return next;
      });
    }

    if (hasUpdates) {
      this.typedTextsSignal.update((r) => ({ ...r, ...updates }));
    }
  }

  /**
   * Triggers fast typewriter rewind on a stream execution when a mid-stream reset occurs.
   * Once characters rewind to zero, the block displays the graceful retry notification.
   */
  public triggerStreamRewind(execId: string | undefined, message: string): void {
    const blocks = untracked(() => this.consolidatedBlocks());
    const currentTexts = untracked(() => this.typedTextsSignal());

    const targetBlock = blocks.find(b =>
      b.data?.execution_id === execId
      || b.id === `stream-${execId}`
      || b.data?.step_id === execId
      || (Array.isArray(b.data?.stream_segments) && b.data.stream_segments.some((s: any) => s.execution_id === execId))
    ) || blocks.find(b => b.data?.isReset);

    if (!targetBlock) return;
    const blockId = targetBlock.id;

    const keysToCheck: Array<{ key: string; isThinking: boolean }> = [
      { key: blockId, isThinking: false },
      { key: blockId + '-native', isThinking: true }
    ];

    let startedRewind = false;
    for (const { key, isThinking } of keysToCheck) {
      this.typingTargets.delete(key);

      const currentText = currentTexts[key] || '';
      if (currentText.length > 0) {
        this.rewindTargets.set(key, {
          blockId,
          isThinking,
          execId,
          message
        });
        startedRewind = true;
      }
    }

    if (startedRewind) {
      this.ensureTypingLoop();
    } else {
      this.upsertBlockResetNotice(blockId, {
        id: execId || `${blockId}-text`,
        message,
        isWaiting: true,
        streamType: 'text'
      });
      this.activeResetNotices.update(notices => {
        const next = new Map(notices);
        next.set(blockId, message);
        return next;
      });
    }
  }

  public getBlockResets(block: any, isThinking: boolean = false): StreamResetNotice[] {
    if (!block) return [];
    const blockId = block.id;
    const targetStreamType = isThinking ? 'thinking' : 'text';

    // 1. From activeBlockResets signal
    const activeList = (this.activeBlockResets().get(blockId) || [])
      .filter(r => r.streamType === targetStreamType);

    // 2. From block.data.stream_resets (aggregated from logs)
    const blockDataResets = (Array.isArray(block.data?.stream_resets) ? block.data.stream_resets : [])
      .filter((r: any) => (r.streamType || 'text') === targetStreamType);

    // Fast path: if both lists are empty, check fallback or return empty array immediately
    if (activeList.length === 0 && blockDataResets.length === 0) {
      if (!isThinking && block.data?.isReset && block.data?.resetMessage) {
        return [{
          id: block.data.execution_id || blockId,
          message: block.data.resetMessage,
          isWaiting: !block.data?.isCompleted,
          streamType: 'text'
        }];
      }
      return [];
    }

    // Merge by id, giving precedence to activeList
    const mergedMap = new Map<string, StreamResetNotice>();
    for (const r of blockDataResets) {
      mergedMap.set(r.id, {
        id: r.id,
        message: r.message,
        isWaiting: block.data?.isCompleted ? false : (r.isWaiting ?? false),
        streamType: r.streamType || targetStreamType
      });
    }
    for (const r of activeList) {
      mergedMap.set(r.id, {
        ...r,
        isWaiting: block.data?.isCompleted ? false : r.isWaiting
      });
    }

    return Array.from(mergedMap.values());
  }

  public hasWaitingReset(block: any, isThinking: boolean = false): boolean {
    const resets = this.getBlockResets(block, isThinking);
    return resets.some(r => r.isWaiting);
  }

  public hasBlockResets(block: any, isThinking: boolean = false): boolean {
    return this.getBlockResets(block, isThinking).length > 0;
  }

  public formatResetMessage(message: string, isWaiting: boolean): string {
    if (!message) return '';
    if (isWaiting) return message;
    return message
      .replace(/[,.\s]*(?:Retrying automatically(?:\.{3}|\.\.\.)?)\s*$/i, '')
      .trim();
  }

  public getCheckerSegmentResetMessage(item: any): string {
    return item?.data?.resetMessage || DEFAULT_STREAM_RESET_MESSAGE;
  }

  public getResetNotice(block: any): string | null {
    if (!block) return null;
    const resets = this.getBlockResets(block, false);
    if (resets.length > 0) {
      const last = resets[resets.length - 1];
      return this.formatResetMessage(last.message, last.isWaiting);
    }
    const blockId = block.id;
    const active = this.activeResetNotices().get(blockId);
    if (active) return this.formatResetMessage(active, !block.data?.isCompleted);
    if (block.data?.isReset && block.data?.resetMessage) {
      return this.formatResetMessage(block.data.resetMessage, !block.data?.isCompleted);
    }
    return null;
  }

  public isStreamRewinding(key: string): boolean {
    return this.rewindTargets.has(key);
  }

  public isCheckerSegmentResetNotice(item: any): boolean {
    if (!item?.data?.isReset) return false;
    const text = item.data.text;
    return !text || text.trim().length === 0;
  }

  public getStreamDisplayText(key: string, fallback: string = ''): string {
    const typed = this.typedTextsSignal()[key];
    return typed !== undefined ? typed : fallback;
  }

  private triggerDelayedCollapse(execId: string) {
    if (!this.scheduledCollapses.has(execId)) {
      this.scheduledCollapses.add(execId);
      setTimeout(() => {
        this.collapsedStreams.update((set: Set<string>) => {
          const newSet = new Set(set);
          newSet.add(execId);
          return newSet;
        });
      }, 1000);
    }
  }

  public onDetailsToggle(event: Event, execId: string): void {
    const details = event.target as HTMLDetailsElement;
    const isOpen = details.open;
    this.collapsedStreams.update((set: Set<string>) => {
      const newSet = new Set(set);
      if (isOpen) {
        newSet.delete(execId);
      } else {
        newSet.add(execId);
      }
      return newSet;
    });
  }

  public toggleCollapse(execId: string): void {
    this.collapsedStreams.update((set: Set<string>) => {
      const newSet = new Set(set);
      if (newSet.has(execId)) {
        newSet.delete(execId);
      } else {
        newSet.add(execId);
      }
      return newSet;
    });
  }

  // Android Action Delegation Methods
  public getActionObject(action: any): any {
    return getActionObject(action);
  }

  public isAndroidAction(action: any): boolean {
    return isAndroidAction(action);
  }

  public getActionIcon(action: any): string {
    return getActionIcon(action);
  }

  public getActionTitle(action: any): string {
    return getActionTitle(action);
  }

  public getActionTargetText(action: any): string {
    return getActionTargetText(action);
  }

  public getActionInputLabel(action: any): string {
    return getActionInputLabel(action);
  }

  public getActionInputText(action: any): string {
    return getActionInputText(action);
  }

  public getActionCoords(action: any): string {
    return getActionCoords(action);
  }

  public isActionFailed(action: any, stepData?: any): boolean {
    return isActionFailed(action, stepData);
  }

  public getActionErrorMessage(action: any, stepData?: any): string {
    return getActionErrorMessage(action, stepData);
  }

  public isActionExpanded(cardId: string, defaultExpanded: boolean = false): boolean {
    if (this.collapsedActionCards().has(cardId)) return false;
    if (this.expandedActionCards().has(cardId)) return true;
    return defaultExpanded;
  }

  public toggleActionExpanded(cardId: string, event?: Event, defaultExpanded: boolean = false): void {
    if (event) {
      event.stopPropagation();
    }
    const isCurrentlyExpanded = this.isActionExpanded(cardId, defaultExpanded);
    if (isCurrentlyExpanded) {
      this.expandedActionCards.update(set => {
        const newSet = new Set(set);
        newSet.delete(cardId);
        return newSet;
      });
      this.collapsedActionCards.update(set => {
        const newSet = new Set(set);
        newSet.add(cardId);
        return newSet;
      });
    } else {
      this.collapsedActionCards.update(set => {
        const newSet = new Set(set);
        newSet.delete(cardId);
        return newSet;
      });
      this.expandedActionCards.update(set => {
        const newSet = new Set(set);
        newSet.add(cardId);
        return newSet;
      });
    }
  }

  public getActionBounds(action: any): string {
    return getActionBounds(action);
  }

  public getActionResourceId(action: any): string {
    return getActionResourceId(action);
  }

  public getActionClass(action: any): string {
    return getActionClass(action);
  }

  public getActionExtraParams(action: any): ActionParam[] {
    return extractActionExtraParams(action, this.actionParamsCache);
  }

  public getToolExtraParams(toolData: any): ActionParam[] {
    return extractToolExtraParams(toolData, this.toolParamsCache);
  }

  public getStepPreImageUrl(stepData: any, actionData?: any): string | null {
    return getStepPreImageUrl(stepData, actionData);
  }

  public getStepPostImageUrl(stepData: any, actionData?: any): string | null {
    return getStepPostImageUrl(stepData, actionData);
  }

  // Zoom Modal & Overlay Methods
  public openImageModal(url: string, actionObj?: any, event?: Event): void {
    if (event) {
      event.stopPropagation();
    }
    this.selectedZoomImage.set(url);
    this.selectedZoomAction.set(actionObj || null);
  }

  public closeImageModal(event?: Event): void {
    if (event) {
      event.stopPropagation();
    }
    this.selectedZoomImage.set(null);
    this.selectedZoomAction.set(null);
  }

  public onImageLoad(img: HTMLImageElement, overlay: HTMLElement, actionObj: any): void {
    if (!img || !overlay) return;
    try {
      this.drawActionCoordinatesOnOverlay(img, overlay, actionObj);
    } catch (e) {
      console.warn('Failed to draw action overlay:', e);
    }
  }

  public drawActionCoordinatesOnOverlay(img: HTMLImageElement, overlay: HTMLElement, actionObj: any): void {
    drawActionCoordinatesOnOverlay(img, overlay, actionObj);
  }

  // Generic Tool Delegation Methods

  /**
   * First save_note call per note key, derived once per blocks change instead
   * of re-scanning every block for every rendered tool row.
   */
  private firstSaveNoteByKey = computed<Map<string, any>>(() => {
    const firstByKey = new Map<string, any>();
    for (const block of this.consolidatedBlocks()) {
      if (block.type === 'step' && block.data.generic_tools) {
        for (const t of this.getUniqueGenericTools(block.data.generic_tools)) {
          if (t.name && t.name.toLowerCase() === 'save_note') {
            const key = this.getToolKey(t);
            if (key && !firstByKey.has(key)) {
              firstByKey.set(key, t);
            }
          }
        }
      }
    }
    return firstByKey;
  });

  public isFirstSaveNoteForKey(tool: any): boolean {
    if (!tool || !tool.name || tool.name.toLowerCase() !== 'save_note') {
      return false;
    }
    const key = this.getToolKey(tool);
    if (!key) return true;

    const firstCall = this.firstSaveNoteByKey().get(key);
    if (!firstCall) return true;
    return (tool.trace_id && firstCall.trace_id)
      ? tool.trace_id === firstCall.trace_id
      : tool === firstCall;
  }

  public getToolDisplayLabel(tool: any): string {
    return getToolDisplayLabel(tool, this.isFirstSaveNoteForKey(tool));
  }

  public isNoteTool(tool: any): boolean {
    return isNoteTool(tool);
  }

  public isVideoTool(tool: any): boolean {
    return isVideoTool(tool);
  }

  public getVideoToolTarget(tool: any): string | null {
    return getVideoToolTarget(tool);
  }

  public getVideoAnalysisLabel(tool: any): string {
    return getVideoAnalysisView(tool)?.title || 'Analyzing screen recording';
  }

  public getVideoAnalysisRangeLabel(tool: any): string {
    const range = getVideoAnalysisView(tool)?.requestedRange;
    if (!range) return 'Screen recording';
    return `${formatVideoTime(range.start)}–${formatVideoTime(range.end)}`;
  }

  public getVideoAnalysisDetail(tool: any): string {
    const view = getVideoAnalysisView(tool);
    if (!view || view.totalCount <= 1) return '';
    if (view.outcome === 'running' || view.outcome === 'recovering') {
      return view.completedCount > 0
        ? `${view.completedCount}/${view.totalCount} segments saved`
        : '';
    }
    if (view.outcome === 'partial') {
      return `${view.completedCount}/${view.totalCount} segments saved`;
    }
    return '';
  }

  public isVideoAnalysisAttention(tool: any): boolean {
    const outcome = getVideoAnalysisView(tool)?.outcome;
    return outcome === 'partial' || outcome === 'failed';
  }

  public onVideoToolClick(toolData: any): void {
    const curSessionId = this.agentService.currentSessionId();
    if (curSessionId) {
      const start = getVideoAnalysisView(toolData)?.requestedRange?.start;
      this.agentService.openVideoPlayer(curSessionId, undefined, undefined, start);
    }
  }

  public getScreenRecordingButtonTitle(): string {
    if (this.agentService.isCurrentSessionRunning()) {
      return 'Task is currently running (Recording screen)';
    }
    if (this.agentService.currentSessionRecordingStatus() === 'processing') {
      return 'Preparing screen recording...';
    }
    if (this.agentService.currentSessionVideoUrl()) {
      return 'Play Screen Recording Video';
    }
    if (this.agentService.hasCurrentSessionStepFrames()) {
      return this.agentService.currentSessionRecordingStatus() === 'failed'
        ? 'Video recording failed — Click to replay step-by-step screenshots'
        : 'Play step-by-step screenshots replay';
    }
    if (this.agentService.currentSessionRecordingStatus() === 'failed') {
      return 'Screen recording generation failed';
    }
    return 'Screen Recording';
  }

  public isDeviceActionTool(tool: any): boolean {
    return isDeviceActionTool(tool);
  }

  public isFailureAnalyzerActionTool(tool: any): boolean {
    return isDeviceActionTool(tool);
  }

  public getToolAgentName(tool: any): string | null {
    return getToolAgentName(tool);
  }

  public shouldShowTool(tool: any, stepData?: any): boolean {
    return shouldShowTool(tool, stepData);
  }

  public getUniqueGenericTools(tools: any[] | undefined): any[] {
    return getUniqueGenericTools(tools);
  }

  public getSortedStepEvents(stepData: any): StepEvent[] {
    return getSortedStepEvents(stepData, this.sortedEventsCache);
  }

  /**
   * Memoized per block-data object: templates call these many times per
   * change-detection pass, and the raw-thinking path runs JSON-detection
   * heuristics over the full text.
   */
  private getThinkingTexts(data: any): { native: string | null; raw: string | null } {
    let entry = this.thinkingTextCache.get(data);
    if (!entry) {
      const nativeText = data.operator_native_thinking || (data.stream_type === 'thinking' ? data.text : null);
      const rawText = data.operator_raw_thinking || (data.stream_type === 'text' ? data.text : null);
      entry = {
        native: nativeText && nativeText.trim() ? nativeText : null,
        raw: rawText && rawText.trim() && this.isHumanThinking(rawText) ? rawText : null
      };
      this.thinkingTextCache.set(data, entry);
    }
    return entry;
  }

  public getNativeThinking(block: any): string | null {
    if (!block || !block.data) return null;
    return this.getThinkingTexts(block.data).native;
  }

  public getRawThinking(block: any): string | null {
    if (!block || !block.data) return null;
    return this.getThinkingTexts(block.data).raw;
  }

  // --- Check blocks (verifier lane) ----------------------------------------------
  public isCheckerBlock(block: any): boolean {
    return block?.type === 'checker';
  }

  public isCheckerPhase(phase: any): boolean {
    const blocks: any[] = Array.isArray(phase?.blocks) ? phase.blocks : [];
    return blocks.length > 0 && blocks.every((b) => this.isCheckerBlock(b));
  }

  public getCheckerPhaseLabel(block: any): string {
    switch (block?.data?.phase) {
      case 'final':
        return 'Final check';
      case 'outcome':
        return 'Result';
      default:
        return 'Check';
    }
  }

  public getCheckerVerdicts(block: any): any[] {
    return Array.isArray(block?.data?.verdicts) ? block.data.verdicts : [];
  }

  /** Declared check items; persisted attempts only carry verdicts, so derive them. */
  public getCheckerItems(block: any): any[] {
    const d = block?.data || {};
    if (Array.isArray(d.items) && d.items.length > 0) return d.items;
    return this.getCheckerVerdicts(block).map((v) => ({ kind: v.kind, text: v.item_text, when: v.when }));
  }

  /** How the item is judged, in plain words (verify = must pass, assert = recorded test result). */
  public getCheckMethodLabel(item: any, block: any): string {
    const parts: string[] = [item?.kind === 'assert' ? 'Test assertion' : 'Must pass'];
    if (item?.when === 'at_end' && block?.data?.phase !== 'final') parts.push('at the end');
    return parts.join(' · ');
  }

  /** Superseded / unchecked attempts collapse to their header line. */
  public isCheckerMuted(block: any): boolean {
    const status = block?.data?.status;
    return status === 'superseded' || status === 'unchecked';
  }

  /** Number of the check item a verdict belongs to (1-based, matches the list above the stream). */
  public getVerdictItemNumber(block: any, verdict: any, fallbackIndex: number): number {
    const idx = this.getCheckerItems(block).findIndex((i) => i.text === verdict?.item_text);
    return (idx > -1 ? idx : fallbackIndex) + 1;
  }

  public getVerdictStatusText(status: string): string {
    switch (status) {
      case 'passed':
        return 'Passed';
      case 'failed':
        return 'Failed';
      case 'inconclusive':
        return 'Could not be confirmed';
      case 'superseded':
        return 'Superseded by a newer check';
      case 'unchecked':
        return 'Not checked';
      default:
        return status || '';
    }
  }

  public hasCheckerResult(block: any): boolean {
    if (!this.isCheckerBlock(block) || this.isBlockRunning(block) || this.isCheckerMuted(block)) return false;
    if (block.data?.phase === 'outcome') return this.getCheckerNotes(block).length > 0;
    return this.getCheckerVerdicts(block).length > 0 || this.getCheckerNotes(block).length > 0;
  }

  /** Follow-ups not already visible in the verdict rows (the findings text repeats them). */
  public getCheckerNotes(block: any): string[] {
    const d = block?.data || {};
    if (d.phase === 'outcome') return Array.isArray(d.last_findings) ? d.last_findings : [];
    const notes: string[] = [];
    if (Array.isArray(d.unmet_subgoals)) {
      for (const text of d.unmet_subgoals) notes.push(`Not finished yet: ${text}`);
    }
    if (d.reverted) notes.push('The step was sent back to be redone.');
    if (d.error) notes.push(String(d.error));
    return notes;
  }

  // Streamed turns of a check: the last one is live while the block runs;
  // finished Thought turns fold unless the reader opens them.
  public openCheckSegments = signal<Set<string>>(new Set<string>());

  public isCheckerSegmentRunning(block: any, itemIdx: number): boolean {
    if (!this.isBlockRunning(block)) return false;
    return itemIdx === this.getSortedStepEvents(block.data).length - 1;
  }

  public isCheckerSegmentCollapsed(block: any, itemIdx: number): boolean {
    if (this.isCheckerSegmentRunning(block, itemIdx)) return false;
    return !this.openCheckSegments().has(`${block.id}-${itemIdx}`);
  }

  public toggleCheckerSegment(block: any, itemIdx: number): void {
    const key = `${block.id}-${itemIdx}`;
    this.openCheckSegments.update((set) => {
      const next = new Set(set);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  }

  public isCheckerTextVisible(item: any): boolean {
    if (item?.data?.isReset) return true;
    const text = item?.data?.text;
    return typeof text === 'string' && text.trim().length > 0 && this.isHumanThinking(text);
  }

  public getCheckerTone(block: any): string {
    const d = block?.data || {};
    if (d.phase === 'outcome') {
      return d.task_status === 'completed' ? 'passed' : (d.task_status === 'blocked' ? 'failed' : 'inconclusive');
    }
    if (d.isCompleted === false) return 'running';
    if (d.status === 'superseded' || d.status === 'unchecked') return 'muted';
    if (d.status === 'error') return 'inconclusive';
    const verdicts = this.getCheckerVerdicts(block);
    if (verdicts.some((v) => v.status === 'failed')) return 'failed';
    if (verdicts.some((v) => v.status === 'inconclusive')) return 'inconclusive';
    return 'passed';
  }

  public getCheckerStatusLabel(block: any): string {
    const d = block?.data || {};
    if (d.phase === 'outcome') {
      const t = d.tests || {};
      const label = d.task_status === 'completed' ? 'Goal completed'
        : (d.task_status === 'blocked' ? 'Blocked' : 'Partially completed');
      const counts = [
        [t.passed, 'passed'],
        [t.failed, 'failed'],
        [t.inconclusive, 'inconclusive'],
        [t.unchecked, 'not checked']
      ]
        .filter(([n]) => Number(n) > 0)
        .map(([n, word]) => `${n} ${word}`);
      return counts.length > 0 ? `${label} · ${counts.join(' · ')}` : label;
    }
    if (d.isCompleted === false) return 'Checking…';
    switch (d.status) {
      case 'superseded':
        return 'Superseded';
      case 'unchecked':
        return 'Not checked';
      case 'error':
        return 'No verdict';
    }
    const verdicts = this.getCheckerVerdicts(block);
    const failed = verdicts.filter((v) => v.status === 'failed').length;
    if (failed > 0) return `${failed} failed`;
    if (verdicts.some((v) => v.status === 'inconclusive')) return 'Inconclusive';
    return verdicts.length > 0 ? 'Passed' : 'Done';
  }

  public getVerdictIcon(status: string): string {
    switch (status) {
      case 'passed':
        return 'check_circle';
      case 'failed':
        return 'cancel';
      case 'inconclusive':
        return 'help';
      case 'superseded':
        return 'history';
      case 'unchecked':
        return 'radio_button_unchecked';
      default:
        return 'pending';
    }
  }

  public hasVisibleContent(block: any): boolean {
    if (!block) return false;
    if (block.type === 'checker') return true;
    const hasNative = Boolean(this.getNativeThinking(block));
    const hasRaw = Boolean(this.getRawThinking(block));
    const hasReset = this.hasBlockResets(block, false) || this.hasBlockResets(block, true);
    const hasVisibleTools = Boolean(block.data?.generic_tools) && block.data.generic_tools.some((t: any) =>
      this.shouldShowTool(t, block.data)
      || this.isDisplayableLLMFailure(t)
      || this.isLLMRetry(t)
      || this.isReportStatusAction(t)
    );
    const hasAndroidActions = Boolean(block.data?.action_taken) && (this.isAndroidAction(block.data.action_taken) || this.isReportStatusAction(block.data.action_taken));
    return hasNative || hasRaw || hasReset || hasVisibleTools || hasAndroidActions;
  }

  public isReportStatusAction(action: any): boolean {
    return isReportStatusAction(action);
  }

  public getReportStatusValue(action: any): string {
    return getReportStatusValue(action);
  }

  public getReportStatusExplanation(action: any): string {
    return getReportStatusExplanation(action);
  }

  public isHumanThinking(text: string | null): boolean {
    return isHumanThinking(text);
  }

  public isTerminalLLMFailure(tool: any): boolean {
    return !!tool && this.retryablePauseTrace() === tool;
  }

  public isDisplayableLLMFailure(tool: any): boolean {
    return tool?.type === 'llm_call'
      && tool?.status === 'failed'
      && (tool?.name === 'llm_pause' || tool?.payload?.pause === true);
  }

  public resumePausedTask(event: Event): void {
    event.stopPropagation();
    this.agentService.resumeTask();
  }

  public getLLMErrorText(tool: any): string {
    const noDetails = 'The AI provider did not return error details after the request failed.';
    if (!tool) return noDetails;
    const rawError = tool.payload?.error || tool.error;
    if (!rawError) return noDetails;
    const cleaned = this.cleanErrorMessage(rawError);
    return cleaned === 'Unknown error' ? noDetails : cleaned;
  }

  public isLLMRetry(tool: any): boolean {
    return tool?.type === 'llm_call'
      && tool?.status === 'retrying'
      && tool?.payload?.source === 'provider_sdk'
      && ['google', 'gemini'].includes(String(tool?.payload?.provider || '').toLowerCase());
  }

  public getLLMRetryEntries(tool: any): any[] {
    const retries = tool?.payload?.retries;
    return Array.isArray(retries) && retries.length > 0 ? retries : [tool?.payload || tool];
  }

  public getLLMFailureRetryEntries(tool: any): any[] {
    const retries = tool?.payload?.retries;
    if (!Array.isArray(retries)) return [];
    return retries.filter((retry: any) =>
      retry?.source === 'provider_sdk'
      && ['google', 'gemini'].includes(String(retry?.provider || '').toLowerCase())
    );
  }

  public hasLLMFailureRetryEntries(tool: any): boolean {
    return this.getLLMFailureRetryEntries(tool).length > 0;
  }

  public getLLMRetryEntryDelay(entry: any): string | null {
    const delay = Number(entry?.delay);
    if (!Number.isFinite(delay) || delay <= 0) return null;
    return `${delay.toFixed(2).replace(/\.00$/, '')}s`;
  }

  public getLLMRetryEntryWaited(entry: any): string {
    const delay = Number(entry?.delay);
    if (!Number.isFinite(delay) || delay <= 0) return '0s';

    const rawStartedAt = Number(entry?.scheduled_at ?? entry?.timestamp);
    if (!Number.isFinite(rawStartedAt) || rawStartedAt <= 0) {
      return this.formatLLMDuration(delay);
    }
    const startedAtMs = rawStartedAt < 1e11 ? rawStartedAt * 1000 : rawStartedAt;
    const elapsed = Math.min(delay, Math.max(0, (this.retryClock() - startedAtMs) / 1000));
    return this.formatLLMDuration(elapsed);
  }

  public isLLMRetryEntryWaiting(entry: any): boolean {
    const delay = Number(entry?.delay);
    const rawStartedAt = Number(entry?.scheduled_at ?? entry?.timestamp);
    if (!Number.isFinite(delay) || delay <= 0 || !Number.isFinite(rawStartedAt)) return false;
    const startedAtMs = rawStartedAt < 1e11 ? rawStartedAt * 1000 : rawStartedAt;
    return this.retryClock() < startedAtMs + delay * 1000;
  }

  public getLLMFailureWaited(tool: any): string | null {
    const waited = Number(tool?.payload?.waited_seconds);
    if (!Number.isFinite(waited) || waited < 0) return null;
    return this.formatLLMDuration(waited);
  }

  public formatLLMDuration(seconds: number): string {
    if (seconds < 0.05) return '0s';
    if (seconds < 10) return `${seconds.toFixed(1).replace(/\.0$/, '')}s`;
    return `${Math.round(seconds)}s`;
  }

  public cleanErrorMessage(rawError: any): string {
    return cleanErrorMessage(rawError);
  }

  public getToolKey(tool: any): string | null {
    return getToolKey(tool);
  }

  public getToolCallName(action: any): string {
    const act = this.getActionObject(action);
    if (!act) return 'Tool';
    const label = this.getToolDisplayLabel(act);
    const key = this.getToolKey(act);
    return key ? `${label}: ${key}` : label;
  }

  public getToolCallLabel(action: any): string {
    return this.getToolCallName(action);
  }

  public isToolFailed(tool: any): boolean {
    return isToolFailed(tool);
  }

  public getToolErrorMessage(tool: any): string {
    return getToolErrorMessage(tool);
  }

  public getToolArgs(tool: any): any {
    return getToolArgs(tool);
  }

  public getToolIcon(tool: any): string {
    return getToolIcon(tool);
  }

  public getToolTitle(tool: any): string {
    return getToolTitle(tool);
  }

  public getToolTargetText(tool: any): string {
    return getToolTargetText(tool);
  }

  public getToolInputLabel(tool: any): string {
    return getToolInputLabel(tool);
  }

  public getToolInputText(tool: any): string {
    return getToolInputText(tool);
  }

  public getToolCoords(tool: any): string {
    return getToolCoords(tool);
  }

  public getToolAnalysisText(tool: any): string {
    return getToolAnalysisText(tool);
  }

  public getToolGenericDetails(tool: any): string {
    return getToolGenericDetails(tool);
  }

  public isAdbCommandTool(tool: any): boolean {
    return isAdbCommandTool(tool);
  }

  public getAdbCommandLine(tool: any): string {
    return getAdbCommandLine(tool);
  }

  public getAdbCwd(tool: any): string {
    return getAdbCwd(tool);
  }

  public getAdbTerminalId(tool: any): string {
    return getAdbTerminalId(tool);
  }

  public onSessionChange(event: Event): void {
    const selectElement = event.target as HTMLSelectElement;
    const sessionId = selectElement.value;
    if (sessionId) {
      this.agentService.selectSession(sessionId, true);
    }
  }

  public isStepEvent(eventType: string): boolean {
    return eventType === 'step_recorded' || eventType === 'step_updated';
  }

  public getCheckerResult(text: string): CheckerResult | null {
    return extractCheckerResult(text);
  }

  public isShortContent(data: any): boolean {
    if (!data) return true;
    const str = typeof data === 'string' ? data : JSON.stringify(data);
    return str.length < 200;
  }

  public formatJson(data: any): string {
    if (typeof data === 'string') {
      try {
        return JSON.stringify(JSON.parse(data), null, 2);
      } catch {
        return data;
      }
    }
    return JSON.stringify(data, null, 2);
  }

  public getStatusLabel(status: string): string {
    switch (status) {
      case 'idle':
        return 'Idle';
      case 'running':
        return 'Running';
      case 'completed':
        return 'Completed';
      case 'offline':
        return 'Offline';
      default:
        return status.toUpperCase();
    }
  }

  /**
   * Render markdown with a per-slot memo. Returning the identical cached
   * string instance for unchanged text means the [innerHTML] binding sees the
   * same reference and skips DOM/sanitizer work entirely.
   */
  public renderMarkdown(text: string, cacheKey?: string): string {
    if (!text) return '';
    if (!cacheKey) {
      return renderMarkdownToHtml(text);
    }
    const cached = this.markdownHtmlCache.get(cacheKey);
    if (cached && cached.source === text) {
      return cached.html;
    }
    const html = renderMarkdownToHtml(text);
    this.markdownHtmlCache.set(cacheKey, { source: text, html });
    return html;
  }

  public onToolKeyClick(key: string): void {
    this.agentService.selectedNoteKey.set(key);
    this.agentService.activeTab.set('notes');
    
    const sessionId = this.agentService.currentSessionId();
    if (sessionId) {
      this.agentService.fetchNotes(sessionId);
    }
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

  public trackPhase(index: number, phase: any): string {
    return phase.id;
  }

  public trackBlock(index: number, block: any): string {
    return block.id;
  }

  public trackTool(index: number, tool: any): string {
    return tool.trace_id || index.toString();
  }

  public trackStepEvent(index: number, item: { type: string; data: any; timestamp?: number }): string {
    if (!item || !item.data) return index.toString();
    // A step has at most one thinking and one text event; keying them by type
    // alone keeps their DOM stable while the streamed text grows (keying by
    // text length would tear down and rebuild the node on every chunk).
    if (item.type === 'thinking' || item.type === 'text') {
      return item.type;
    }
    return item.type + '-' + (item.data.trace_id
      || item.data.timestamp || item.data.start_time || item.data.created_at || item.data.id
      || ((item.data.action || item.data.name || '') + '-' + index));
  }

  public trackParam(index: number, param: { key: string; value: string }): string {
    return param.key;
  }

  public getModelDisplayName(name: string | undefined | null): string {
    if (!name) return 'Flash';
    const lower = name.toLowerCase();
    if (lower.includes('pro')) return 'Pro';
    if (lower.includes('flash')) return 'Flash';
    return name.replace(/^artemis\s+/i, '');
  }

  public getModelIcon(name: string | undefined | null): string {
    if (!name) return 'bolt';
    const lower = name.toLowerCase();
    if (lower.includes('pro')) return 'diamond';
    if (lower.includes('flash')) return 'bolt';
    return 'smart_toy';
  }

  public getModelClass(name: string | undefined | null): string {
    if (!name) return 'is-flash';
    const lower = name.toLowerCase();
    if (lower.includes('pro')) return 'is-pro';
    if (lower.includes('flash')) return 'is-flash';
    return '';
  }

  public getArchitectureTooltip(model?: ModelInfo | null): string {
    if (!model) return 'Agent Architecture: ARTEMIS Flash (Reactive Fast Loop)';
    const name = this.getModelDisplayName(model.name);
    const isPro = name.toLowerCase().includes('pro');
    const archDesc = isPro
      ? 'ARTEMIS Pro (Multi-Agent Cognitive State Graph)'
      : 'ARTEMIS Flash (Reactive Fast Loop)';
    if (model.id) {
      return `Agent Architecture: ${archDesc} · LLM: ${model.id} (${model.provider || 'google'})`;
    }
    return `Agent Architecture: ${archDesc}`;
  }

  public formatTokenCount(tokens?: number): string {
    return formatTokenCount(tokens);
  }

  public getTokenTooltip(phase: PhaseBlock): string {
    if (!phase.tokens) return '';
    if (phase.promptTokens && phase.completionTokens) {
      return `Consumed: ${phase.tokens.toLocaleString()} tokens (${phase.promptTokens.toLocaleString()} in / ${phase.completionTokens.toLocaleString()} out)`;
    }
    return `Consumed: ${phase.tokens.toLocaleString()} tokens`;
  }

  public isCurrentSessionActive(): boolean {
    const status = this.agentService.agentStatus();
    if (status !== 'running') return false;
    const currentId = this.agentService.currentSessionId();
    const runningId = this.agentService.runningSessionId();
    if (currentId && runningId && currentId !== runningId) {
      return false;
    }
    const currentSession = this.agentService.sessions().find(s => s.session_id === currentId);
    if (currentSession && (currentSession.status === 'completed' || currentSession.status === 'failed' || currentSession.status === 'cancelled')) {
      return false;
    }
    return true;
  }

  public isItemRunning(item: any, block: any): boolean {
    if (!item || !item.data) return false;
    if (!this.isCurrentSessionActive()) return false;
    if (item.data.status === 'success' || item.data.status === 'failed' || item.data.status === 'cancelled') return false;
    if (this.isReportStatusAction(item.data) || this.isReportStatusAction(item.data?.action_taken)) return false;
    if (item.data.status === 'running') return true;

    // Check if session is currently running and this is the active trailing item
    const blocks = this.consolidatedBlocks();
    const lastBlock = blocks.length > 0 ? blocks[blocks.length - 1] : null;
    if (lastBlock && (lastBlock.id === block?.id || !block?.data?.isCompleted)) {
      const events = this.getSortedStepEvents(block.data);
      const lastEvent = events.length > 0 ? events[events.length - 1] : null;
      if (lastEvent && (lastEvent.data?.trace_id === item.data?.trace_id || lastEvent === item)) {
        return true;
      }
    }
    return false;
  }

  public isBlockRunning(block: any): boolean {
    if (!block || !block.data) return false;
    if (!this.isCurrentSessionActive()) return false;
    if (block.type === 'checker') return block.data.isCompleted === false;
    if (this.isReportStatusAction(block.data.action_taken) || block.data?.status === 'completed' || block.data?.status === 'failed' || block.data?.status === 'cancelled') return false;
    if (block.data.isCompleted === false) return true;
    const blocks = this.consolidatedBlocks();
    const lastBlock = blocks.length > 0 ? blocks[blocks.length - 1] : null;
    return lastBlock?.id === block.id;
  }
}
