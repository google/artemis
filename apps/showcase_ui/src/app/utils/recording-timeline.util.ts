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

/**
 * Pure helpers that map times onto the multi-segment screen recording.
 *
 * A recording is split into segments whenever scrcpy restarts (device
 * rotation, crash recovery). The player plays them back to back on a
 * "timeline" axis (`start`/`duration`, no gaps), while step timestamps live
 * on the "session" axis (seconds since the DataEngine session start). Manifest
 * v2 segments carry `offset_ms`/`duration_ms` on the session axis, which lets
 * a session time land in the right segment even after each restart gap.
 */

export interface TimelineSegment {
  /** Back-to-back playlist start in seconds. */
  start: number;
  /** Segment length in seconds. */
  duration: number;
  /** Session-relative first-frame offset in milliseconds (manifest v2). */
  offset_ms?: number;
  /** Segment length in milliseconds (manifest v2). */
  duration_ms?: number;
}

export interface SegmentLocation {
  /** Index into the segment list. */
  index: number;
  /** Seconds into that segment's own media. */
  localTime: number;
  /** Equivalent position on the back-to-back playlist axis, in seconds. */
  timelineTime: number;
}

const clamp = (value: number, min: number, max: number): number => Math.max(min, Math.min(max, value));

/** True when every segment carries the session-relative v2 fields. */
export function hasSessionOffsets(segments: readonly TimelineSegment[]): boolean {
  return segments.length > 0 && segments.every(
    (segment) => Number.isFinite(segment.offset_ms) && Number.isFinite(segment.duration_ms)
  );
}

/**
 * Locate a position on the back-to-back playlist axis (what the scrubber uses).
 * Times past the end clamp into the last segment.
 */
export function locateTimelineTime(
  segments: readonly TimelineSegment[],
  timelineSeconds: number
): SegmentLocation | null {
  if (!segments.length) return null;
  const total = segments.reduce((sum, segment) => sum + Math.max(0, segment.duration), 0);
  const target = clamp(Number.isFinite(timelineSeconds) ? timelineSeconds : 0, 0, total);
  let index = segments.findIndex((segment) => target < segment.start + segment.duration);
  if (index < 0) index = segments.length - 1;
  const segment = segments[index];
  const localTime = clamp(target - segment.start, 0, Math.max(0, segment.duration));
  return { index, localTime, timelineTime: segment.start + localTime };
}

/**
 * Locate a session-relative time (seconds since session start, the axis step
 * timestamps use). A time inside `[offset, offset + duration]` of a segment maps
 * into that segment; a time in the gap between two segments (scrcpy restart)
 * snaps to the start of the next segment; a time before the first frame snaps
 * to the first frame; a time after the last frame clamps to the last frame.
 *
 * Segments without session offsets (legacy manifests) are treated as if the
 * session axis were the back-to-back timeline axis.
 */
export function locateSessionTime(
  segments: readonly TimelineSegment[],
  sessionSeconds: number
): SegmentLocation | null {
  if (!segments.length) return null;
  if (!hasSessionOffsets(segments)) {
    return locateTimelineTime(segments, sessionSeconds);
  }
  const targetMs = Number.isFinite(sessionSeconds) ? sessionSeconds * 1000 : 0;
  for (let index = 0; index < segments.length; index++) {
    const segment = segments[index];
    const offsetMs = segment.offset_ms as number;
    const endMs = offsetMs + (segment.duration_ms as number);
    if (targetMs < offsetMs) {
      // Before this segment's first frame: either before the recording began
      // or inside the restart gap after the previous segment. Snap forward.
      return { index, localTime: 0, timelineTime: segment.start };
    }
    if (targetMs <= endMs) {
      const localTime = clamp((targetMs - offsetMs) / 1000, 0, Math.max(0, segment.duration));
      return { index, localTime, timelineTime: segment.start + localTime };
    }
  }
  const lastIndex = segments.length - 1;
  const last = segments[lastIndex];
  const localTime = Math.max(0, last.duration);
  return { index: lastIndex, localTime, timelineTime: last.start + localTime };
}

/**
 * Convert a session-relative time into the back-to-back playlist time the
 * player scrubber understands. Returns 0 when there are no segments.
 */
export function sessionTimeToTimelineTime(
  segments: readonly TimelineSegment[],
  sessionSeconds: number
): number {
  return locateSessionTime(segments, sessionSeconds)?.timelineTime ?? Math.max(0, sessionSeconds || 0);
}
