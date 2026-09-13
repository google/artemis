import {
  hasSessionOffsets,
  locateSessionTime,
  locateTimelineTime,
  sessionTimeToTimelineTime,
  TimelineSegment
} from './recording-timeline.util';

describe('recording-timeline.util', () => {
  // Recording started 1.2s after the session began. Segment 0 ran 10s, then
  // scrcpy restarted for rotation (1.5s gap), segment 1 ran 5s, another 0.8s
  // gap, and segment 2 ran 4s.
  const v2Segments: TimelineSegment[] = [
    { start: 0, duration: 10, offset_ms: 1200, duration_ms: 10000 },
    { start: 10, duration: 5, offset_ms: 12700, duration_ms: 5000 },
    { start: 15, duration: 4, offset_ms: 18500, duration_ms: 4000 }
  ];

  const legacySegments: TimelineSegment[] = [
    { start: 0, duration: 10 },
    { start: 10, duration: 5 }
  ];

  describe('hasSessionOffsets', () => {
    it('detects manifest v2 segments and rejects legacy or mixed lists', () => {
      expect(hasSessionOffsets(v2Segments)).toBeTrue();
      expect(hasSessionOffsets(legacySegments)).toBeFalse();
      expect(hasSessionOffsets([v2Segments[0], legacySegments[0]])).toBeFalse();
      expect(hasSessionOffsets([])).toBeFalse();
    });
  });

  describe('locateTimelineTime', () => {
    it('maps back-to-back playlist times into segments', () => {
      expect(locateTimelineTime(legacySegments, 3)).toEqual({ index: 0, localTime: 3, timelineTime: 3 });
      expect(locateTimelineTime(legacySegments, 10)).toEqual({ index: 1, localTime: 0, timelineTime: 10 });
      expect(locateTimelineTime(legacySegments, 12.5)).toEqual({ index: 1, localTime: 2.5, timelineTime: 12.5 });
    });

    it('clamps out-of-range times into the first or last segment', () => {
      expect(locateTimelineTime(legacySegments, -4)).toEqual({ index: 0, localTime: 0, timelineTime: 0 });
      expect(locateTimelineTime(legacySegments, 99)).toEqual({ index: 1, localTime: 5, timelineTime: 15 });
      expect(locateTimelineTime([], 3)).toBeNull();
    });
  });

  describe('locateSessionTime', () => {
    it('maps a session time inside a segment using its real offset', () => {
      // T+4.2s is 3.0s into segment 0 (which started at T+1.2s).
      const inFirst = locateSessionTime(v2Segments, 4.2)!;
      expect(inFirst.index).toBe(0);
      expect(inFirst.localTime).toBeCloseTo(3.0, 6);
      expect(inFirst.timelineTime).toBeCloseTo(3.0, 6);

      // T+14.7s is 2.0s into segment 1 (started at T+12.7s), which sits at
      // timeline 10s, so it must NOT drift to timeline 14.7s.
      const inSecond = locateSessionTime(v2Segments, 14.7)!;
      expect(inSecond.index).toBe(1);
      expect(inSecond.localTime).toBeCloseTo(2.0, 6);
      expect(inSecond.timelineTime).toBeCloseTo(12.0, 6);

      const inThird = locateSessionTime(v2Segments, 20.5)!;
      expect(inThird.index).toBe(2);
      expect(inThird.localTime).toBeCloseTo(2.0, 6);
      expect(inThird.timelineTime).toBeCloseTo(17.0, 6);
    });

    it('snaps a time inside a restart gap to the start of the next segment', () => {
      // Segment 0 ends at T+11.2s, segment 1 starts at T+12.7s.
      expect(locateSessionTime(v2Segments, 11.9)).toEqual({ index: 1, localTime: 0, timelineTime: 10 });
      // Segment 1 ends at T+17.7s, segment 2 starts at T+18.5s.
      expect(locateSessionTime(v2Segments, 18.0)).toEqual({ index: 2, localTime: 0, timelineTime: 15 });
    });

    it('treats segment boundaries as inclusive of the end frame', () => {
      const atEnd = locateSessionTime(v2Segments, 11.2)!;
      expect(atEnd.index).toBe(0);
      expect(atEnd.localTime).toBeCloseTo(10, 6);
      expect(locateSessionTime(v2Segments, 12.7)).toEqual({ index: 1, localTime: 0, timelineTime: 10 });
    });

    it('snaps times before the first frame and after the last frame', () => {
      expect(locateSessionTime(v2Segments, 0)).toEqual({ index: 0, localTime: 0, timelineTime: 0 });
      expect(locateSessionTime(v2Segments, 0.5)).toEqual({ index: 0, localTime: 0, timelineTime: 0 });
      expect(locateSessionTime(v2Segments, 60)).toEqual({ index: 2, localTime: 4, timelineTime: 19 });
      expect(locateSessionTime(v2Segments, Number.NaN)).toEqual({ index: 0, localTime: 0, timelineTime: 0 });
    });

    it('falls back to back-to-back timeline behaviour for legacy manifests', () => {
      expect(locateSessionTime(legacySegments, 12.5)).toEqual({ index: 1, localTime: 2.5, timelineTime: 12.5 });
      expect(locateSessionTime([], 5)).toBeNull();
    });
  });

  describe('sessionTimeToTimelineTime', () => {
    it('returns the playlist position for a session time', () => {
      expect(sessionTimeToTimelineTime(v2Segments, 14.7)).toBeCloseTo(12.0, 6);
      expect(sessionTimeToTimelineTime(v2Segments, 11.9)).toBe(10);
      expect(sessionTimeToTimelineTime(legacySegments, 7)).toBe(7);
    });

    it('passes the time through when no playlist is available', () => {
      expect(sessionTimeToTimelineTime([], 7)).toBe(7);
      expect(sessionTimeToTimelineTime([], -2)).toBe(0);
    });
  });
});
