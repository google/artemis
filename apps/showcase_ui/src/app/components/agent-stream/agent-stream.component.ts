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

import { Component, signal, computed, effect, inject, untracked, DestroyRef, AfterViewInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { HttpClient } from '@angular/common/http';
import { AgentService } from '../../services/agent.service';
import { Session } from '../../core/models/session.model';
import { MarkdownSegment, MarkdownLine, NoteMilestone, ParsedNote } from '../../core/models/markdown.model';
import { StepBlock, PhaseBlock, StepEvent, ActionParam, CheckerResult } from '../../core/models/stream.model';

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

export type { MarkdownSegment, MarkdownLine, NoteMilestone, ParsedNote };

@Component({
  selector: 'app-agent-stream',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './agent-stream.component.html',
  styleUrl: './agent-stream.component.scss'
})
export class AgentStreamComponent implements AfterViewInit {
  public agentService = inject(AgentService);
  private http = inject(HttpClient);
  private destroyRef = inject(DestroyRef);

  // Auto-scroll state tracking
  public isUserAtBottom = true;
  private resizeObserver: ResizeObserver | null = null;

  // Dynamic Planning Loader Game-style Phrases
  public currentPlanningText = signal<string>(PLANNING_LOADER_PHRASES[0]);
  private planningIntervalId: any = null;
  private currentPhraseIndex = 0;

  // Top Nav Dropdown States
  public isTaskDropdownOpen = signal<boolean>(false);
  public isNotesDropdownOpen = signal<boolean>(false);

  // Zoom Modal State
  public selectedZoomImage = signal<string | null>(null);
  public selectedZoomAction = signal<any | null>(null);

  // Signals and properties for managing delayed collapsing of thought streams
  public collapsedStreams = signal<Set<string>>(new Set<string>());
  private scheduledCollapses = new Set<string>();

  // Signals and maps for typewriter effect simulation
  public typedTextsSignal = signal<Record<string, string>>({});
  private typingTimers = new Map<string, any>();

  // Set to track expanded and collapsed state of action cards
  public expandedActionCards = signal<Set<string>>(new Set<string>());
  public collapsedActionCards = signal<Set<string>>(new Set<string>());

  // Performance caches for parameter and event extractions
  private actionParamsCache = new WeakMap<any, ActionParam[]>();
  private toolParamsCache = new WeakMap<any, ActionParam[]>();
  private sortedEventsCache = new WeakMap<any, { length: number; actionTs: any; events: StepEvent[] }>();

  // Top Nav Task Queue Computed Properties
  public activeQueue = computed(() => {
    const list = this.agentService.sessions().filter((s) => {
      const status = this.getTaskStatus(s);
      return status === 'running' || status === 'paused' || status === 'pending';
    });
    return list.sort((a, b) => {
      const statusA = this.getTaskStatus(a);
      const statusB = this.getTaskStatus(b);
      if (statusA === 'running' || statusA === 'paused') return -1;
      if (statusB === 'running' || statusB === 'paused') return 1;
      return a.start_time - b.start_time;
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
    if (!this.agentService.isPaused()) return false;

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

  private retryablePauseTrace = computed<any | null>(() => {
    if (!this.agentService.isPaused()) return null;

    const blocks = this.consolidatedBlocks();
    for (let blockIndex = blocks.length - 1; blockIndex >= 0; blockIndex--) {
      const tools = blocks[blockIndex]?.data?.generic_tools;
      if (!Array.isArray(tools)) continue;

      for (let toolIndex = tools.length - 1; toolIndex >= 0; toolIndex--) {
        const tool = tools[toolIndex];
        if (
          tool?.type === 'llm_call'
          && tool?.status === 'failed'
          && tool?.payload?.pause === true
        ) {
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
    // Auto scroll stream box during active text streaming
    effect(() => {
      const logs = this.filteredLogs();
      const activeStream = logs.find(log => log.type === 'llm_stream' && !log.data?.isCompleted);
      if (activeStream) {
        setTimeout(() => {
          const elements = document.getElementsByClassName('stream-box');
          for (let i = 0; i < elements.length; i++) {
            const el = elements[i] as HTMLElement;
            el.scrollTop = el.scrollHeight;
          }
        }, 30);
      }
    });

    // Auto scroll main log list when new logs are added or typewriter updates
    effect(() => {
      this.consolidatedBlocks();
      this.typedTextsSignal();

      setTimeout(() => {
        const container = document.querySelector('.stream-logs-content');
        if (container && this.isUserAtBottom) {
          container.scrollTop = container.scrollHeight;
        }
      }, 50);
    });

    // Typewriter drive logic: triggers typing when block appears or expands
    effect(() => {
      const blocks = this.consolidatedBlocks();
      blocks.forEach(block => {
        const blockId = block.id;
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
              if (currentVal.length < rawText.length && !this.typingTimers.has(blockId)) {
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
              if (currentVal.length < nativeText.length && !this.typingTimers.has(nativeKey)) {
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
        this.typingTimers.forEach((timer) => clearInterval(timer));
        this.typingTimers.clear();
        this.typedTextsSignal.set({});
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
    });
  }

  public ngAfterViewInit(): void {
    const container = document.querySelector('.stream-logs-content');
    if (container && typeof ResizeObserver !== 'undefined') {
      this.resizeObserver = new ResizeObserver(() => {
        if (this.isUserAtBottom) {
          container.scrollTop = container.scrollHeight;
        }
      });
      this.resizeObserver.observe(container);
    }
  }

  public onStreamScroll(event: Event): void {
    const container = event.target as HTMLElement;
    if (!container) return;
    const threshold = 150;
    const distanceToBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
    this.isUserAtBottom = distanceToBottom <= threshold;
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
    if (session.session_id === this.agentService.runningSessionId() && (this.agentService.agentStatus() === 'running' || this.agentService.agentStatus() === 'paused')) {
      return this.agentService.agentStatus() as 'running' | 'paused';
    }
    if (session.status) {
      const s = session.status.toLowerCase();
      if (s === 'running' || s === 'paused' || s === 'completed' || s === 'pending' || s === 'failed' || s === 'cancelled') {
        return s as any;
      }
    }
    return 'completed';
  }

  public selectTask(sessionId: string, event?: Event): void {
    if (event) event.stopPropagation();
    this.agentService.selectSession(sessionId, true);
    this.isTaskDropdownOpen.set(false);
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
  private startTyping(key: string, targetText: string, isThinking: boolean = false, execId?: string) {
    if (this.typingTimers.has(key)) {
      clearInterval(this.typingTimers.get(key));
    }

    const interval = 12;
    const timer = setInterval(() => {
      const currentRecord = this.typedTextsSignal();
      const current = currentRecord[key] || '';
      
      const blocks = this.consolidatedBlocks();
      const blockId = key.endsWith('-native') ? key.replace('-native', '') : key;
      const block = blocks.find(b => b.id === blockId);
      let target = targetText;
      if (block) {
        target = isThinking ? (this.getNativeThinking(block) || targetText) : (this.getRawThinking(block) || targetText);
      }

      if (current.length < target.length) {
        const nextLength = Math.min(current.length + 8, target.length);
        const newText = target.slice(0, nextLength);
        this.typedTextsSignal.update(r => ({ ...r, [key]: newText }));
      } else {
        if (block?.data?.isCompleted) {
          clearInterval(timer);
          this.typingTimers.delete(key);
          
          if (isThinking && execId) {
            this.triggerDelayedCollapse(execId);
          }
        }
      }
    }, interval);

    this.typingTimers.set(key, timer);
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
  public isFirstSaveNoteForKey(tool: any): boolean {
    if (!tool || !tool.name || tool.name.toLowerCase() !== 'save_note') {
      return false;
    }
    const key = this.getToolKey(tool);
    if (!key) return true;

    const blocks = this.consolidatedBlocks();
    const saveNoteCalls: any[] = [];
    for (const block of blocks) {
      if (block.type === 'step' && block.data.generic_tools) {
        const uniqTools = this.getUniqueGenericTools(block.data.generic_tools);
        for (const t of uniqTools) {
          if (t.name && t.name.toLowerCase() === 'save_note' && this.getToolKey(t) === key) {
            saveNoteCalls.push(t);
          }
        }
      }
    }

    if (saveNoteCalls.length > 0) {
      const firstCall = saveNoteCalls[0];
      return (tool.trace_id && firstCall.trace_id)
        ? tool.trace_id === firstCall.trace_id
        : tool === firstCall;
    }
    return true;
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

  public onVideoToolClick(toolData: any): void {
    const curSessionId = this.agentService.currentSessionId();
    if (curSessionId) {
      this.agentService.openVideoPlayer(curSessionId);
    }
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

  public getNativeThinking(block: any): string | null {
    if (!block || !block.data) return null;
    const text = block.data.operator_native_thinking || (block.data.stream_type === 'thinking' ? block.data.text : null);
    return text && text.trim() ? text : null;
  }

  public getRawThinking(block: any): string | null {
    if (!block || !block.data) return null;
    const text = block.data.operator_raw_thinking || (block.data.stream_type === 'text' ? block.data.text : null);
    if (!text || !text.trim() || !this.isHumanThinking(text)) return null;
    return text;
  }

  public hasVisibleContent(block: any): boolean {
    if (!block) return false;
    const hasNative = Boolean(this.getNativeThinking(block));
    const hasRaw = Boolean(this.getRawThinking(block));
    const hasVisibleTools = Boolean(block.data?.generic_tools) && block.data.generic_tools.some((t: any) => this.shouldShowTool(t, block.data) || (t.type === 'llm_call' && (t.status === 'failed' || t.status === 'retrying')) || this.isReportStatusAction(t));
    const hasAndroidActions = Boolean(block.data?.action_taken) && (this.isAndroidAction(block.data.action_taken) || this.isReportStatusAction(block.data.action_taken));
    return hasNative || hasRaw || hasVisibleTools || hasAndroidActions;
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

  public resumePausedTask(event: Event): void {
    event.stopPropagation();
    this.agentService.resumeTask();
  }

  public getLLMErrorText(tool: any): string {
    if (!tool) return 'Unknown error';
    const rawError = tool.payload?.error || tool.error;
    if (!rawError) return 'Unknown error';
    return this.cleanErrorMessage(rawError);
  }

  public isLLMRetry(tool: any): boolean {
    return tool?.type === 'llm_call' && tool?.status === 'retrying';
  }

  public getLLMRetryEntries(tool: any): any[] {
    const retries = tool?.payload?.retries;
    return Array.isArray(retries) && retries.length > 0 ? retries : [tool?.payload || tool];
  }

  public getLLMRetryCount(tool: any): number {
    const count = Number(tool?.payload?.retry_count);
    return Number.isFinite(count) && count > 0 ? count : this.getLLMRetryEntries(tool).length;
  }

  public getLLMRetryTotalDelay(tool: any): string | null {
    const configuredTotal = Number(tool?.payload?.total_delay);
    const total = Number.isFinite(configuredTotal)
      ? configuredTotal
      : this.getLLMRetryEntries(tool).reduce(
          (sum: number, retry: any) => sum + (Number(retry?.delay) || 0),
          0
        );
    if (total <= 0) return null;
    return `${total.toFixed(2).replace(/\.00$/, '')}s`;
  }

  public getLLMRetryEntryError(entry: any): string {
    return this.cleanErrorMessage(entry?.error || 'Unknown error');
  }

  public getLLMRetryEntryDelay(entry: any): string | null {
    const delay = Number(entry?.delay);
    if (!Number.isFinite(delay) || delay <= 0) return null;
    return `${delay.toFixed(2).replace(/\.00$/, '')}s`;
  }

  public getLLMRetryProvider(tool: any): string | null {
    const providers = Array.isArray(tool?.payload?.providers)
      ? tool.payload.providers
      : [tool?.payload?.provider];
    const labels = providers
      .filter((provider: any) => typeof provider === 'string' && provider.trim())
      .map((provider: string) => provider.trim().replace(/^./, (value: string) => value.toUpperCase()));
    return labels.length > 0 ? Array.from(new Set(labels)).join(', ') : null;
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

  public renderMarkdown(text: string): string {
    return renderMarkdownToHtml(text);
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

  public trackStepEvent(index: number, item: { type: string; data: any }): string {
    if (!item || !item.data) return index.toString();
    return item.type + '-' + (item.data.timestamp || item.data.start_time || item.data.created_at || item.data.id || item.data.action || item.data.name || index);
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
    if (this.isReportStatusAction(block.data.action_taken) || block.data?.status === 'completed' || block.data?.status === 'failed' || block.data?.status === 'cancelled') return false;
    if (block.data.isCompleted === false) return true;
    const blocks = this.consolidatedBlocks();
    const lastBlock = blocks.length > 0 ? blocks[blocks.length - 1] : null;
    return lastBlock?.id === block.id;
  }
}
