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

import {
  Component,
  inject,
  signal,
  computed,
  ElementRef,
  ViewChild,
  HostListener,
  effect,
  OnDestroy
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { AgentService } from '../../services/agent.service';
import { StepReplayFrame } from '../../core/models/stream.model';
import { drawActionCoordinatesOnOverlay } from '../../utils/image-overlay.util';
import { getActionIcon } from '../../utils/action-formatter.util';

@Component({
  selector: 'app-floating-video-player',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './floating-video-player.component.html',
  styleUrl: './floating-video-player.component.scss'
})
export class FloatingVideoPlayerComponent implements OnDestroy {
  public agentService = inject(AgentService);

  @ViewChild('videoRef') videoRef?: ElementRef<HTMLVideoElement>;
  @ViewChild('stepImgRef') stepImgRef?: ElementRef<HTMLImageElement>;
  @ViewChild('stepOverlayRef') stepOverlayRef?: ElementRef<HTMLElement>;

  // Playback state signals
  public isPlaying = signal<boolean>(false);
  public playbackRate = signal<number>(1.0);
  public isLooping = signal<boolean>(false);
  public currentTime = signal<number>(0);
  public duration = signal<number>(0);
  public isMuted = signal<boolean>(false);
  public isTheaterMode = signal<boolean>(false);
  public videoLoadError = signal<boolean>(false);
  public liveStreamUrl = signal<string>('/api/stream/device-live');
  public liveStreamError = signal<boolean>(false);
  public activeSegmentIndex = signal<number>(0);
  public currentVideoUrl = computed(() => {
    const segments = this.agentService.activeVideoSegments();
    return segments[this.activeSegmentIndex()]?.url || this.agentService.activeVideoUrl();
  });
  private pendingLocalTime: number | null = null;
  private pendingAutoplay = false;
  private pendingAbsoluteSeek: number | null = null;

  // Step Replay state signals
  public activeStepIndex = signal<number>(0);
  public isStepPlaying = signal<boolean>(false);
  public viewMode = signal<'pre' | 'post'>('pre'); // 'pre' = action preview with coords, 'post' = action result
  public isCardHovered = signal<boolean>(false);
  public isCardPinned = signal<boolean>(false);
  public isCardVisible = computed(() => this.isCardHovered() || this.isCardPinned());
  public isStepImageLoading = signal<boolean>(false);
  public stepImageError = signal<boolean>(false);
  private stepTimer: any = null;

  public stepFrames = computed(() => this.agentService.currentSessionStepFrames());
  public totalStepFrames = computed(() => this.stepFrames().length);
  public currentStepFrame = computed<StepReplayFrame | null>(() => {
    const frames = this.stepFrames();
    const idx = this.activeStepIndex();
    return frames[idx] || null;
  });
  public currentStepImageUrl = computed<string>(() => {
    const frame = this.currentStepFrame();
    if (!frame) return '';
    if (this.viewMode() === 'post' && frame.postImageUrl) {
      return frame.postImageUrl;
    }
    return frame.preImageUrl || frame.imageUrl || '';
  });
  public stepProgressPercent = computed(() => {
    const total = this.totalStepFrames();
    if (total <= 1) return 0;
    return (this.activeStepIndex() / (total - 1)) * 100;
  });

  // Available speed options
  public speedOptions = [0.5, 1.0, 1.5, 2.0, 4.0];

  // Dragging and window positioning state
  public posX = signal<number>(
    typeof window !== 'undefined' ? Math.max(20, window.innerWidth - 420) : 100
  );
  public posY = signal<number>(
    typeof window !== 'undefined' ? Math.max(60, window.innerHeight - 700) : 100
  );

  private isDragging = false;
  private dragStartX = 0;
  private dragStartY = 0;
  private initialPosX = 0;
  private initialPosY = 0;

  constructor() {
    // Reset video load error whenever active video URL changes
    effect(
      () => {
        const url = this.agentService.activeVideoUrl();
        const segments = this.agentService.activeVideoSegments();
        this.videoLoadError.set(false);
        this.activeSegmentIndex.set(0);
        this.currentTime.set(0);
        this.duration.set(segments.reduce((sum, segment) => sum + segment.duration, 0));
        this.isPlaying.set(false);
      },
      { allowSignalWrites: true }
    );

    // Reset step playback state only when session actually changes
    let lastTrackedSessionId: string | null = null;
    effect(
      () => {
        const curSessionId = this.agentService.currentSessionId();
        const total = this.totalStepFrames();
        if (curSessionId !== lastTrackedSessionId) {
          lastTrackedSessionId = curSessionId;
          this.activeStepIndex.set(0);
          this.viewMode.set('pre');
          this.pauseStepPlay();
        } else if (total > 0 && this.activeStepIndex() >= total) {
          this.activeStepIndex.set(total - 1);
        }
      },
      { allowSignalWrites: true }
    );

    // Track step screenshot URL changes and reset loading/error state
    effect(() => {
      const url = this.currentStepImageUrl();
      if (url) {
        this.isStepImageLoading.set(true);
        this.stepImageError.set(false);
      } else {
        this.isStepImageLoading.set(false);
      }
    }, { allowSignalWrites: true });

    // Re-draw overlay whenever activeStepIndex or viewMode changes
    effect(() => {
      this.activeStepIndex();
      this.viewMode();
      setTimeout(() => this.drawStepOverlay(), 40);
    });

    effect(() => {
      const request = this.agentService.videoSeekRequest();
      if (!request) return;
      this.pendingAbsoluteSeek = request.seconds;
      if (this.agentService.recordingPlaybackStatus() !== 'ready') return;
      const video = this.videoRef?.nativeElement;
      if (video && video.readyState >= 1) {
        const target = this.pendingAbsoluteSeek;
        this.pendingAbsoluteSeek = null;
        this.seek(target, true);
      }
    });

    effect(() => {
      const request = this.agentService.stepSeekRequest();
      if (!request) return;
      this.seekStepFrame(request.index);
    });
  }

  public onLiveStreamError(): void {
    this.liveStreamError.set(true);
  }

  public retryLiveStream(): void {
    this.liveStreamError.set(false);
    this.liveStreamUrl.set(`/api/stream/device-live?t=${Date.now()}`);
  }

  /**
   * Format seconds into mm:ss format
   */
  public formatTime(seconds: number): string {
    if (isNaN(seconds) || seconds < 0) return '00:00';
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    const mStr = mins < 10 ? '0' + mins : String(mins);
    const sStr = secs < 10 ? '0' + secs : String(secs);
    return `${mStr}:${sStr}`;
  }

  /**
   * Progress percentage for timeline scrubber (0 - 100)
   */
  public progressPercent = computed(() => {
    const dur = this.duration();
    if (dur <= 0) return 0;
    return Math.min(100, Math.max(0, (this.currentTime() / dur) * 100));
  });

  /**
   * Start dragging the window from header
   */
  public startDrag(event: MouseEvent): void {
    if (this.isTheaterMode()) return;
    this.isDragging = true;
    this.dragStartX = event.clientX;
    this.dragStartY = event.clientY;
    this.initialPosX = this.posX();
    this.initialPosY = this.posY();
    event.preventDefault();
  }

  @HostListener('window:mousemove', ['$event'])
  public onDrag(event: MouseEvent): void {
    if (!this.isDragging) return;
    const deltaX = event.clientX - this.dragStartX;
    const deltaY = event.clientY - this.dragStartY;

    const minX = 10;
    const minY = 10;
    const maxX = Math.max(10, window.innerWidth - 240);
    const maxY = Math.max(10, window.innerHeight - 80);

    const targetX = Math.max(minX, Math.min(maxX, this.initialPosX + deltaX));
    const targetY = Math.max(minY, Math.min(maxY, this.initialPosY + deltaY));

    this.posX.set(targetX);
    this.posY.set(targetY);
  }

  @HostListener('window:mouseup')
  public stopDrag(): void {
    this.isDragging = false;
  }

  @HostListener('window:resize')
  public onResize(): void {
    // Keep window within viewport bounds if window resized
    const maxX = Math.max(10, window.innerWidth - 240);
    const maxY = Math.max(10, window.innerHeight - 80);
    if (this.posX() > maxX) this.posX.set(maxX);
    if (this.posY() > maxY) this.posY.set(maxY);
  }

  @HostListener('window:keydown', ['$event'])
  public onKeyDown(event: KeyboardEvent): void {
    if (!this.agentService.isVideoWindowOpen()) return;

    if (event.key === 'Escape') {
      if (this.isTheaterMode()) {
        this.isTheaterMode.set(false);
      } else {
        this.agentService.closeVideoPlayer();
      }
    }
  }

  /**
   * Video element event handlers
   */
  public onLoadedMetadata(): void {
    const v = this.videoRef?.nativeElement;
    if (v) {
      const segments = this.agentService.activeVideoSegments();
      if (!segments.length) {
        this.duration.set(v.duration || 0);
      }
      this.videoLoadError.set(false);
      v.playbackRate = this.playbackRate();
      v.loop = this.isLooping() && !segments.length;
      v.muted = this.isMuted();
      if (this.pendingAbsoluteSeek !== null) {
        const target = this.pendingAbsoluteSeek;
        this.pendingAbsoluteSeek = null;
        this.seek(target, true);
        // A seek into another finalized segment changes the video source. Let
        // that segment's metadata event apply pendingLocalTime.
        if (this.pendingLocalTime !== null) return;
      }
      if (this.pendingLocalTime !== null) {
        v.currentTime = Math.max(0, Math.min(v.duration || this.pendingLocalTime, this.pendingLocalTime));
        this.pendingLocalTime = null;
      }
      if (this.pendingAutoplay) {
        this.pendingAutoplay = false;
        v.play().catch(() => {});
      }
      if (this.agentService.consumeVideoAutoplay()) {
        this.isMuted.set(true);
        v.muted = true;
        v.play().catch(() => {
          this.isPlaying.set(false);
        });
      }
    }
  }

  public onTimeUpdate(): void {
    const v = this.videoRef?.nativeElement;
    if (v) {
      const segments = this.agentService.activeVideoSegments();
      const segment = segments[this.activeSegmentIndex()];
      this.currentTime.set((segment?.start || 0) + v.currentTime);
      if (!segments.length && v.duration && v.duration !== this.duration()) {
        this.duration.set(v.duration);
      }
    }
  }

  public onPlay(): void {
    this.isPlaying.set(true);
  }

  public onPause(): void {
    this.isPlaying.set(false);
  }

  public onEnded(): void {
    const segments = this.agentService.activeVideoSegments();
    if (segments.length && this.activeSegmentIndex() < segments.length - 1) {
      this.pendingLocalTime = 0;
      this.pendingAutoplay = true;
      this.activeSegmentIndex.update(index => index + 1);
      return;
    }
    if (segments.length && this.isLooping()) {
      this.seek(0, true);
      return;
    }
    this.isPlaying.set(false);
  }

  public onError(): void {
    this.videoLoadError.set(true);
    this.isPlaying.set(false);
  }

  /**
   * Playback actions
   */
  public togglePlay(): void {
    const v = this.videoRef?.nativeElement;
    if (!v) return;
    if (v.paused) {
      v.play().catch(() => {});
    } else {
      v.pause();
    }
  }

  public restart(): void {
    this.seek(0, true);
  }

  public seek(seconds: number, autoplay = false): void {
    const v = this.videoRef?.nativeElement;
    if (!v) return;
    const target = Math.max(0, Math.min(this.duration(), seconds));
    const segments = this.agentService.activeVideoSegments();
    if (!segments.length) {
      v.currentTime = target;
      if (autoplay) v.play().catch(() => {});
      return;
    }
    let index = segments.findIndex(segment => target < segment.start + segment.duration);
    if (index < 0) index = segments.length - 1;
    const localTime = Math.max(0, target - segments[index].start);
    if (index === this.activeSegmentIndex()) {
      v.currentTime = localTime;
      if (autoplay) v.play().catch(() => {});
    } else {
      this.pendingLocalTime = localTime;
      this.pendingAutoplay = autoplay || !v.paused;
      this.activeSegmentIndex.set(index);
    }
    this.currentTime.set(target);
  }

  public onScrub(event: Event): void {
    const input = event.target as HTMLInputElement;
    const targetTime = parseFloat(input.value);
    this.seek(targetTime);
  }

  public setSpeed(rate: number): void {
    this.playbackRate.set(rate);
    const v = this.videoRef?.nativeElement;
    if (v) {
      v.playbackRate = rate;
    }
    if (this.isStepPlaying()) {
      this.scheduleNextStepFrame();
    }
  }

  public toggleLoop(): void {
    const next = !this.isLooping();
    this.isLooping.set(next);
    const v = this.videoRef?.nativeElement;
    if (v) {
      v.loop = next && !this.agentService.activeVideoSegments().length;
    }
  }

  public toggleMute(): void {
    const next = !this.isMuted();
    this.isMuted.set(next);
    const v = this.videoRef?.nativeElement;
    if (v) {
      v.muted = next;
    }
  }

  public toggleTheater(): void {
    this.isTheaterMode.set(!this.isTheaterMode());
  }

  public toggleMinimize(): void {
    this.agentService.isVideoMinimized.set(!this.agentService.isVideoMinimized());
  }

  public openInNewTab(): void {
    const url = this.agentService.activeVideoUrl();
    if (url) {
      window.open(url, '_blank');
    }
  }

  /**
   * Step Replay Controls
   */
  public toggleStepPlay(): void {
    if (this.isStepPlaying()) {
      this.pauseStepPlay();
    } else {
      this.startStepPlay();
    }
  }

  public startStepPlay(): void {
    if (this.totalStepFrames() <= 0) return;
    if (this.activeStepIndex() >= this.totalStepFrames() - 1) {
      this.activeStepIndex.set(0);
    }
    this.isStepPlaying.set(true);
    this.scheduleNextStepFrame();
  }

  public pauseStepPlay(): void {
    this.isStepPlaying.set(false);
    if (this.stepTimer) {
      clearTimeout(this.stepTimer);
      this.stepTimer = null;
    }
  }

  private scheduleNextStepFrame(): void {
    if (this.stepTimer) clearTimeout(this.stepTimer);
    const delay = Math.max(300, 1500 / this.playbackRate());
    this.stepTimer = setTimeout(() => {
      if (!this.isStepPlaying()) return;
      if (this.activeStepIndex() < this.totalStepFrames() - 1) {
        this.activeStepIndex.update((i) => i + 1);
        this.scheduleNextStepFrame();
      } else {
        if (this.isLooping()) {
          this.activeStepIndex.set(0);
          this.scheduleNextStepFrame();
        } else {
          this.pauseStepPlay();
        }
      }
    }, delay);
  }

  public prevStepFrame(): void {
    this.pauseStepPlay();
    this.viewMode.set('pre');
    this.activeStepIndex.update((i) => Math.max(0, i - 1));
  }

  public nextStepFrame(): void {
    this.pauseStepPlay();
    this.viewMode.set('pre');
    this.activeStepIndex.update((i) => Math.min(this.totalStepFrames() - 1, i + 1));
  }

  public seekStepFrame(index: number): void {
    this.viewMode.set('pre');
    this.activeStepIndex.set(Math.max(0, Math.min(this.totalStepFrames() - 1, index)));
  }

  public onStepScrub(event: Event): void {
    const input = event.target as HTMLInputElement;
    this.seekStepFrame(parseInt(input.value, 10));
  }

  public restartStep(): void {
    this.seekStepFrame(0);
    this.startStepPlay();
  }

  public setStepViewMode(mode: 'pre' | 'post', event?: Event): void {
    if (event) event.stopPropagation();
    this.viewMode.set(mode);
    setTimeout(() => this.drawStepOverlay(), 40);
  }

  public toggleStepViewMode(event?: Event): void {
    if (event) event.stopPropagation();
    this.setStepViewMode(this.viewMode() === 'pre' ? 'post' : 'pre');
  }

  public onViewportMouseEnter(): void {
    this.isCardHovered.set(true);
  }

  public onViewportMouseLeave(): void {
    this.isCardHovered.set(false);
  }

  public toggleCardPin(event?: Event): void {
    if (event) event.stopPropagation();
    this.isCardPinned.update((v) => !v);
  }

  public cycleSpeed(): void {
    const current = this.playbackRate();
    const idx = this.speedOptions.indexOf(current);
    const nextIdx = (idx + 1) % this.speedOptions.length;
    this.setSpeed(this.speedOptions[nextIdx]);
  }

  public onStepImageLoad(): void {
    this.isStepImageLoading.set(false);
    this.stepImageError.set(false);
    this.drawStepOverlay();
  }

  public onStepImageError(): void {
    this.isStepImageLoading.set(false);
    this.stepImageError.set(true);
  }

  public drawStepOverlay(): void {
    const img = this.stepImgRef?.nativeElement;
    const overlay = this.stepOverlayRef?.nativeElement;
    if (!img || !overlay) return;
    overlay.innerHTML = '';

    // Only draw coordinate overlays when viewing the pre-action screenshot
    if (this.viewMode() === 'post') {
      return;
    }

    const frame = this.currentStepFrame();
    if (!frame || !frame.action) return;

    try {
      drawActionCoordinatesOnOverlay(img, overlay, frame.action);
    } catch (err) {
      console.warn('Failed to draw step coordinates overlay:', err);
    }
  }

  public getStepActionIcon(action: any): string {
    return getActionIcon(action);
  }

  public openImageInNewTab(): void {
    const url = this.currentStepImageUrl() || this.currentStepFrame()?.imageUrl;
    if (url) {
      window.open(url, '_blank');
    }
  }

  public ngOnDestroy(): void {
    if (this.stepTimer) {
      clearTimeout(this.stepTimer);
      this.stepTimer = null;
    }
  }
}
