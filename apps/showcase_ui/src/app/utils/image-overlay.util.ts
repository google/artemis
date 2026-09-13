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
 * Unwrap Flash (`payload.args`) and Pro (`payload.action`) traces.
 * Preserve trace metadata and leave plain action objects unchanged.
 */
export function unwrapTraceAction(record: any): any {
  if (!record || typeof record !== 'object' || !record.payload) return record;

  let payload = record.payload;
  if (typeof payload === 'string') {
    try {
      payload = JSON.parse(payload);
    } catch {
      return record;
    }
  }
  if (!payload || typeof payload !== 'object') return record;

  const asObject = (v: any) => (Array.isArray(v) ? v[0] : v);
  const candidates = [asObject(payload.args?.action), asObject(payload.action), asObject(payload.args)];
  const inner = candidates.find(c => c && typeof c === 'object');
  if (!inner) return record;

  return {
    ...record,
    ...inner,
    args: (record.args && typeof record.args === 'object') ? record.args : inner,
    name: record.name || inner.action || inner.name,
    action: inner.action || record.name,
    trace_id: record.trace_id,
    timestamp: record.timestamp ?? inner.timestamp
  };
}

/**
 * Helper to parse any coordinate representation into an array of numbers.
 * Handles:
 * - [x, y] or [x1, y1, x2, y2]
 * - "[[x1, y1], [x2, y2]]" or [[x1, y1], [x2, y2]]
 * - "[920, 290, 920, 180]" or "920 290 920 180" or "920, 290, 920, 180"
 * - "(920, 290, 920, 180)"
 */
export function extractNumbersFromCoordinateValue(val: any): number[] | null {
  if (val === null || val === undefined) return null;

  if (Array.isArray(val)) {
    if (val.length === 4 && val.every(v => typeof v === 'number' && !isNaN(v))) {
      return val;
    }
    if (val.length === 2 && val.every(v => typeof v === 'number' && !isNaN(v))) {
      return val;
    }
    if (val.length === 2 && Array.isArray(val[0]) && Array.isArray(val[1])) {
      const p1 = extractNumbersFromCoordinateValue(val[0]);
      const p2 = extractNumbersFromCoordinateValue(val[1]);
      if (p1 && p2 && p1.length === 2 && p2.length === 2) {
        return [p1[0], p1[1], p2[0], p2[1]];
      }
    }
    const flattened: number[] = [];
    for (const item of val) {
      if (typeof item === 'number' && !isNaN(item)) {
        flattened.push(item);
      } else if (typeof item === 'string') {
        const matches = item.match(/-?\d+(?:\.\d+)?/g);
        if (matches) {
          matches.forEach(m => flattened.push(Number(m)));
        }
      } else if (Array.isArray(item)) {
        const sub = extractNumbersFromCoordinateValue(item);
        if (sub) flattened.push(...sub);
      }
    }
    if (flattened.length === 4 || flattened.length === 2) {
      return flattened;
    }
  }

  if (typeof val === 'string') {
    const trimmed = val.trim();
    if (!trimmed) return null;

    try {
      const parsed = JSON.parse(trimmed);
      const res = extractNumbersFromCoordinateValue(parsed);
      if (res) return res;
    } catch {}

    const matches = trimmed.match(/-?\d+(?:\.\d+)?/g);
    if (matches && (matches.length === 4 || matches.length === 2)) {
      return matches.map(m => Number(m));
    }
  }

  return null;
}

/**
 * Parse sequence of coordinates (e.g. [[x1, y1], [x2, y2]] or stringified format)
 */
export function parseSequenceCoordinates(seq: any): number[][] | null {
  if (!seq) return null;
  if (typeof seq === 'string') {
    const trimmed = seq.trim();
    try {
      const parsed = JSON.parse(trimmed);
      const res = parseSequenceCoordinates(parsed);
      if (res && res.length > 0) return res;
    } catch {}

    const pairs: number[][] = [];
    const pairRegex = /\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]/g;
    let m: RegExpExecArray | null;
    while ((m = pairRegex.exec(trimmed)) !== null) {
      pairs.push([Number(m[1]), Number(m[2])]);
    }
    if (pairs.length > 0) return pairs;
  }

  if (Array.isArray(seq)) {
    const points: number[][] = [];
    for (const item of seq) {
      const p = extractNumbersFromCoordinateValue(item);
      if (p && p.length === 2) {
        points.push([p[0], p[1]]);
      } else if (Array.isArray(item) && item.length >= 2 && typeof item[0] === 'number' && typeof item[1] === 'number') {
        points.push([item[0], item[1]]);
      }
    }
    if (points.length > 0) return points;
  }
  return null;
}

export function isPureDirectionString(str: any): boolean {
  if (typeof str !== 'string') return false;
  const s = str.trim().toLowerCase();
  if (/\d/.test(s)) return false;
  return [
    'up', 'down', 'left', 'right',
    'swipe up', 'swipe down', 'swipe left', 'swipe right',
    'scroll up', 'scroll down', 'scroll left', 'scroll right',
    'top', 'bottom'
  ].includes(s);
}

/**
 * Draw action coordinates (points, click sequence, swipe lines) onto the image overlay element
 */
export function drawActionCoordinatesOnOverlay(
  img: HTMLImageElement,
  overlay: HTMLElement,
  actionObj: any
): void {
  if (!img || !overlay || !actionObj) return;
  overlay.innerHTML = '';

  const actions = Array.isArray(actionObj) ? actionObj : [actionObj];
  const naturalWidth = img.naturalWidth || 1080;
  const naturalHeight = img.naturalHeight || 2400;

  actions.forEach(record => {
    const act = unwrapTraceAction(record);
    if (!act || typeof act !== 'object') return;
    const actionName = (act.action || act.name || 'tap').toLowerCase();
    const args = (act.args && typeof act.args === 'object') ? act.args : {};

    // Filter out non-touch / system actions that do not operate via touch coordinates
    const nonDrawingActions = [
      'manage_app', 'launch_app', 'open_app', 'stop_app', 'close_app',
      'press_key', 'press_home', 'press_back', 'wait_for_delay', 'wait', 'delay',
      'report_task_status', 'report_failure_analysis'
    ];
    if (nonDrawingActions.includes(actionName)) {
      return;
    }

    // 1. Multi-point click sequence (High priority for click_sequence / tap_sequence)
    const isSequenceAction = actionName.includes('sequence') || Boolean(act.sequence || args.sequence || act.normalized_sequence || args.normalized_sequence);
    const rawSeq = act.normalized_sequence || args.normalized_sequence || act.sequence || args.sequence || act.targets || args.targets || (Array.isArray(act.coordinates) && Array.isArray(act.coordinates[0]) ? act.coordinates : null) || (Array.isArray(args.coordinates) && Array.isArray(args.coordinates[0]) ? args.coordinates : null);
    const seqPoints = parseSequenceCoordinates(rawSeq);

    if (seqPoints && seqPoints.length > 0 && (isSequenceAction || seqPoints.length > 1)) {
      const isNormSeq = Boolean(act.normalized_sequence || args.normalized_sequence) || seqPoints.every(p => p[0] <= 1000 && p[1] <= 1000);
      // Draw connecting lines between consecutive points
      for (let i = 0; i < seqPoints.length - 1; i++) {
        createSequenceConnector(
          seqPoints[i][0], seqPoints[i][1],
          seqPoints[i + 1][0], seqPoints[i + 1][1],
          naturalWidth, naturalHeight,
          overlay,
          isNormSeq
        );
      }
      // Draw numbered markers for each point in the sequence
      seqPoints.forEach((pt, idx) => {
        createPointMarker(
          pt[0], pt[1],
          actionName,
          naturalWidth, naturalHeight,
          overlay,
          isNormSeq,
          idx + 1,
          seqPoints.length
        );
      });
      return;
    }

    // Determine if this is a swipe/scroll/drag gesture
    const isSwipeLike = actionName.includes('swipe') || actionName.includes('scroll') || actionName.includes('drag') || actionName.includes('slide');

    // 2. Check separate start and end coordinate objects
    const normStart = extractNumbersFromCoordinateValue(act.normalized_start_coordinates || args.normalized_start_coordinates);
    const normEnd = extractNumbersFromCoordinateValue(act.normalized_end_coordinates || args.normalized_end_coordinates);
    const startCoords = normStart || extractNumbersFromCoordinateValue(act.start_coordinates || act.start_point || act.start || act.from || 
                        args.start_coordinates || args.start_point || args.start || args.from);
    const endCoords = normEnd || extractNumbersFromCoordinateValue(act.end_coordinates || act.end_point || act.end || act.to || 
                      args.end_coordinates || args.end_point || args.end || args.to);

    if (startCoords && endCoords && startCoords.length >= 2 && endCoords.length >= 2) {
      createLineMarker(
        startCoords[0], startCoords[1],
        endCoords[0], endCoords[1],
        naturalWidth, naturalHeight,
        overlay,
        Boolean(normStart && normEnd),
        actionName
      );
      return;
    }

    // 3. Check 4-element coordinate array in coordinates, target, action, gesture, etc.
    const normCoords = extractNumbersFromCoordinateValue(act.normalized_coordinates || args.normalized_coordinates);
    const isNorm = Boolean(normCoords);
    const rawCoords = normCoords ||
                      extractNumbersFromCoordinateValue(act.coordinates) ||
                      extractNumbersFromCoordinateValue(args.coordinates) ||
                      extractNumbersFromCoordinateValue(act.coords) ||
                      extractNumbersFromCoordinateValue(args.coords) ||
                      extractNumbersFromCoordinateValue(act.target) ||
                      extractNumbersFromCoordinateValue(args.target) ||
                      (isSwipeLike ? extractNumbersFromCoordinateValue(args.action) : null) ||
                      (isSwipeLike ? extractNumbersFromCoordinateValue(act.action) : null) ||
                      (isSwipeLike ? extractNumbersFromCoordinateValue(args.gesture) : null) ||
                      (isSwipeLike ? extractNumbersFromCoordinateValue(act.gesture) : null);

    if (rawCoords && Array.isArray(rawCoords)) {
      if (rawCoords.length === 4) {
        createLineMarker(
          rawCoords[0], rawCoords[1], rawCoords[2], rawCoords[3],
          naturalWidth, naturalHeight,
          overlay,
          isNorm,
          actionName
        );
        return;
      } else if (rawCoords.length === 2 && !isSwipeLike) {
        createPointMarker(rawCoords[0], rawCoords[1], actionName, naturalWidth, naturalHeight, overlay, isNorm);
        return;
      }
    }

    // 4. Check for directional swipe/scroll/drag gestures ONLY when action is swipe-like
    if (isSwipeLike) {
      const rawDir = (typeof args.action === 'string' && isPureDirectionString(args.action) ? args.action : null) ||
                     (typeof act.action === 'string' && act.action !== actionName && isPureDirectionString(act.action) ? act.action : null) ||
                     (isPureDirectionString(args.direction) ? args.direction : null) ||
                     (isPureDirectionString(act.direction) ? act.direction : null) ||
                     (isPureDirectionString(args.gesture) ? args.gesture : null) ||
                     (isPureDirectionString(act.gesture) ? act.gesture : null) || '';
      const dirStr = String(rawDir).toLowerCase().trim();

      if (dirStr) {
        let sx = 500, sy = 750, ex = 500, ey = 250;
        let label = 'SWIPE';

        if (dirStr.includes('up') || dirStr.includes('top') || actionName.includes('up')) {
          sx = 500; sy = 750; ex = 500; ey = 250;
          label = 'SWIPE UP';
        } else if (dirStr.includes('down') || dirStr.includes('bottom') || actionName.includes('down')) {
          sx = 500; sy = 250; ex = 500; ey = 750;
          label = 'SWIPE DOWN';
        } else if (dirStr.includes('left') || actionName.includes('left')) {
          sx = 800; sy = 500; ex = 200; ey = 500;
          label = 'SWIPE LEFT';
        } else if (dirStr.includes('right') || actionName.includes('right')) {
          sx = 200; sy = 500; ex = 800; ey = 500;
          label = 'SWIPE RIGHT';
        }

        createLineMarker(sx, sy, ex, ey, naturalWidth, naturalHeight, overlay, true, label);
        return;
      }
    }

    // 5. Default tap/click coordinate fallback
    if (rawCoords && Array.isArray(rawCoords) && rawCoords.length === 2) {
      createPointMarker(rawCoords[0], rawCoords[1], actionName, naturalWidth, naturalHeight, overlay, isNorm);
    }
  });
}

/**
 * Helper to create and position an animated red dot point marker with edge-aware label
 */
export function createPointMarker(
  x: number, 
  y: number, 
  actionType: string, 
  naturalWidth: number, 
  naturalHeight: number, 
  overlay: HTMLElement, 
  isNormalized: boolean = false,
  sequenceIndex?: number,
  totalSequence?: number
): void {
  let pctX: number;
  let pctY: number;

  // Accurate 0-1000 scale resolution
  if (isNormalized || (!isNormalized && x <= 1000 && y <= 1000)) {
    pctX = x / 10;
    pctY = y / 10;
  } else {
    pctX = (x / (naturalWidth || 1080)) * 100;
    pctY = (y / (naturalHeight || 2400)) * 100;
  }

  // Safety clamp within container boundaries
  const clampedPctX = Math.max(1.5, Math.min(98.5, pctX));
  const clampedPctY = Math.max(1.5, Math.min(98.5, pctY));

  const marker = document.createElement('div');
  marker.className = 'action-point-marker';
  marker.style.position = 'absolute';
  marker.style.left = `${clampedPctX}%`;
  marker.style.top = `${clampedPctY}%`;
  marker.style.transform = 'translate(-50%, -50%)';
  marker.style.pointerEvents = 'none';
  marker.style.zIndex = '10';

  const dotSize = sequenceIndex !== undefined ? 20 : 14;
  const dot = document.createElement('div');
  dot.style.width = `${dotSize}px`;
  dot.style.height = `${dotSize}px`;
  dot.style.borderRadius = '50%';
  dot.style.backgroundColor = '#ef4444';
  dot.style.border = '2px solid white';
  dot.style.boxShadow = '0 0 6px rgba(0,0,0,0.5)';
  if (sequenceIndex !== undefined) {
    dot.style.display = 'flex';
    dot.style.alignItems = 'center';
    dot.style.justifyContent = 'center';
    dot.style.fontSize = '11px';
    dot.style.fontWeight = 'bold';
    dot.style.color = 'white';
    dot.innerText = String(sequenceIndex);
  }

  const pulseSize = dotSize * 2;
  const pulse = document.createElement('div');
  pulse.style.position = 'absolute';
  pulse.style.top = `-${dotSize / 2 + 2}px`;
  pulse.style.left = `-${dotSize / 2 + 2}px`;
  pulse.style.width = `${pulseSize}px`;
  pulse.style.height = `${pulseSize}px`;
  pulse.style.borderRadius = '50%';
  pulse.style.border = '2px solid #ef4444';
  pulse.style.animation = 'pulse-animation 1.5s infinite ease-out';
  pulse.style.pointerEvents = 'none';

  const label = document.createElement('div');
  if (sequenceIndex !== undefined) {
    label.innerText = totalSequence && totalSequence > 1 ? `#${sequenceIndex} TAP` : actionType.toUpperCase();
  } else {
    label.innerText = actionType.toUpperCase();
  }
  label.style.position = 'absolute';
  label.style.backgroundColor = 'rgba(15, 23, 42, 0.9)';
  label.style.color = 'white';
  label.style.padding = '1.5px 5px';
  label.style.borderRadius = '3px';
  label.style.fontSize = '9px';
  label.style.fontWeight = 'bold';
  label.style.letterSpacing = '0.5px';
  label.style.whiteSpace = 'nowrap';
  label.style.boxShadow = '0 1px 4px rgba(0,0,0,0.4)';
  label.style.border = '1px solid rgba(255,255,255,0.2)';

  // Smart edge-aware vertical positioning
  if (clampedPctY > 85) {
    label.style.bottom = `${dotSize + 2}px`;
    label.style.top = 'auto';
  } else {
    label.style.top = `${dotSize + 2}px`;
    label.style.bottom = 'auto';
  }

  // Smart edge-aware horizontal positioning
  if (clampedPctX > 82) {
    label.style.right = '0px';
    label.style.left = 'auto';
    label.style.transform = 'none';
  } else if (clampedPctX < 18) {
    label.style.left = '0px';
    label.style.right = 'auto';
    label.style.transform = 'none';
  } else {
    label.style.left = '50%';
    label.style.right = 'auto';
    label.style.transform = 'translateX(-50%)';
  }

  marker.appendChild(pulse);
  marker.appendChild(dot);
  marker.appendChild(label);
  overlay.appendChild(marker);
}

/**
 * Helper to draw connecting line/arrow between points in a click sequence
 */
export function createSequenceConnector(
  x1: number,
  y1: number,
  x2: number,
  y2: number,
  naturalWidth: number,
  naturalHeight: number,
  overlay: HTMLElement,
  isNormalized: boolean = false
): void {
  const isNormCoord = isNormalized || (x1 <= 1000 && y1 <= 1000 && x2 <= 1000 && y2 <= 1000);
  const pctX1 = isNormCoord ? x1 / 10 : (x1 / (naturalWidth || 1080)) * 100;
  const pctY1 = isNormCoord ? y1 / 10 : (y1 / (naturalHeight || 2400)) * 100;
  const pctX2 = isNormCoord ? x2 / 10 : (x2 / (naturalWidth || 1080)) * 100;
  const pctY2 = isNormCoord ? y2 / 10 : (y2 / (naturalHeight || 2400)) * 100;

  const clampedX1 = Math.max(1.5, Math.min(98.5, pctX1));
  const clampedY1 = Math.max(1.5, Math.min(98.5, pctY1));
  const clampedX2 = Math.max(1.5, Math.min(98.5, pctX2));
  const clampedY2 = Math.max(1.5, Math.min(98.5, pctY2));

  let svg = overlay.querySelector('svg');
  if (!svg) {
    svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '100%');
    svg.setAttribute('height', '100%');
    svg.style.position = 'absolute';
    svg.style.top = '0';
    svg.style.left = '0';
    svg.style.pointerEvents = 'none';
    svg.style.overflow = 'visible';
    svg.style.zIndex = '5';
    overlay.appendChild(svg);
  }

  let defs = svg.querySelector('defs');
  if (!defs) {
    defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
    svg.insertBefore(defs, svg.firstChild);
  }

  const markerId = 'seq-arrow-' + Math.random().toString(36).substring(2, 9);
  const marker = document.createElementNS('http://www.w3.org/2000/svg', 'marker');
  marker.setAttribute('id', markerId);
  marker.setAttribute('viewBox', '0 0 12 12');
  marker.setAttribute('refX', '8');
  marker.setAttribute('refY', '6');
  marker.setAttribute('markerWidth', '6');
  marker.setAttribute('markerHeight', '6');
  marker.setAttribute('orient', 'auto');

  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', 'M 1 2 L 10 6 L 1 10 z');
  path.setAttribute('fill', '#ef4444');
  path.setAttribute('stroke', '#ffffff');
  path.setAttribute('stroke-width', '1');

  marker.appendChild(path);
  defs.appendChild(marker);

  // Background white contrast line
  const bgLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  bgLine.setAttribute('x1', `${clampedX1}%`);
  bgLine.setAttribute('y1', `${clampedY1}%`);
  bgLine.setAttribute('x2', `${clampedX2}%`);
  bgLine.setAttribute('y2', `${clampedY2}%`);
  bgLine.setAttribute('stroke', '#ffffff');
  bgLine.setAttribute('stroke-width', '4');
  bgLine.setAttribute('stroke-linecap', 'round');
  bgLine.setAttribute('opacity', '0.8');

  // Main red dashed sequence line
  const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  line.setAttribute('x1', `${clampedX1}%`);
  line.setAttribute('y1', `${clampedY1}%`);
  line.setAttribute('x2', `${clampedX2}%`);
  line.setAttribute('y2', `${clampedY2}%`);
  line.setAttribute('stroke', '#ef4444');
  line.setAttribute('stroke-width', '2.5');
  line.setAttribute('stroke-linecap', 'round');
  line.setAttribute('marker-end', `url(#${markerId})`);
  line.setAttribute('stroke-dasharray', '4,4');

  svg.appendChild(bgLine);
  svg.appendChild(line);
}

/**
 * Helper to create and position an SVG arrow, high-contrast dashed line, and badge for swipes/drags
 */
export function createLineMarker(
  x1: number, 
  y1: number, 
  x2: number, 
  y2: number, 
  naturalWidth: number, 
  naturalHeight: number, 
  overlay: HTMLElement, 
  isNormalized: boolean = false,
  actionType: string = 'swipe'
): void {
  const isNormCoord = isNormalized || (x1 <= 1000 && y1 <= 1000 && x2 <= 1000 && y2 <= 1000);
  const pctX1 = isNormCoord ? x1 / 10 : (x1 / (naturalWidth || 1080)) * 100;
  const pctY1 = isNormCoord ? y1 / 10 : (y1 / (naturalHeight || 2400)) * 100;
  const pctX2 = isNormCoord ? x2 / 10 : (x2 / (naturalWidth || 1080)) * 100;
  const pctY2 = isNormCoord ? y2 / 10 : (y2 / (naturalHeight || 2400)) * 100;

  const clampedX1 = Math.max(1.5, Math.min(98.5, pctX1));
  const clampedY1 = Math.max(1.5, Math.min(98.5, pctY1));
  const clampedX2 = Math.max(1.5, Math.min(98.5, pctX2));
  const clampedY2 = Math.max(1.5, Math.min(98.5, pctY2));

  let svg = overlay.querySelector('svg');
  if (!svg) {
    svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '100%');
    svg.setAttribute('height', '100%');
    svg.style.position = 'absolute';
    svg.style.top = '0';
    svg.style.left = '0';
    svg.style.pointerEvents = 'none';
    svg.style.overflow = 'visible';
    svg.style.zIndex = '5';
    overlay.appendChild(svg);
  }

  let defs = svg.querySelector('defs');
  if (!defs) {
    defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
    svg.insertBefore(defs, svg.firstChild);
  }

  const markerId = 'action-arrow-' + Math.random().toString(36).substring(2, 9);
  const marker = document.createElementNS('http://www.w3.org/2000/svg', 'marker');
  marker.setAttribute('id', markerId);
  marker.setAttribute('viewBox', '0 0 12 12');
  marker.setAttribute('refX', '8');
  marker.setAttribute('refY', '6');
  marker.setAttribute('markerWidth', '8');
  marker.setAttribute('markerHeight', '8');
  marker.setAttribute('orient', 'auto');

  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', 'M 1 2 L 10 6 L 1 10 z');
  path.setAttribute('fill', '#ef4444');
  path.setAttribute('stroke', '#ffffff');
  path.setAttribute('stroke-width', '1.5');
  path.setAttribute('stroke-linejoin', 'round');

  marker.appendChild(path);
  defs.appendChild(marker);

  // Background white contrast line
  const bgLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  bgLine.setAttribute('x1', `${clampedX1}%`);
  bgLine.setAttribute('y1', `${clampedY1}%`);
  bgLine.setAttribute('x2', `${clampedX2}%`);
  bgLine.setAttribute('y2', `${clampedY2}%`);
  bgLine.setAttribute('stroke', '#ffffff');
  bgLine.setAttribute('stroke-width', '6');
  bgLine.setAttribute('stroke-linecap', 'round');
  bgLine.setAttribute('opacity', '0.9');

  // Main red dashed line
  const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  line.setAttribute('x1', `${clampedX1}%`);
  line.setAttribute('y1', `${clampedY1}%`);
  line.setAttribute('x2', `${clampedX2}%`);
  line.setAttribute('y2', `${clampedY2}%`);
  line.setAttribute('stroke', '#ef4444');
  line.setAttribute('stroke-width', '3.5');
  line.setAttribute('stroke-linecap', 'round');
  line.setAttribute('marker-end', `url(#${markerId})`);
  line.setAttribute('stroke-dasharray', '6,4');

  // Start touch point circle with white border
  const startCircleOuter = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  startCircleOuter.setAttribute('cx', `${clampedX1}%`);
  startCircleOuter.setAttribute('cy', `${clampedY1}%`);
  startCircleOuter.setAttribute('r', '6.5');
  startCircleOuter.setAttribute('fill', '#ef4444');
  startCircleOuter.setAttribute('stroke', 'white');
  startCircleOuter.setAttribute('stroke-width', '2');

  const startCircleInner = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  startCircleInner.setAttribute('cx', `${clampedX1}%`);
  startCircleInner.setAttribute('cy', `${clampedY1}%`);
  startCircleInner.setAttribute('r', '2.5');
  startCircleInner.setAttribute('fill', 'white');

  svg.appendChild(bgLine);
  svg.appendChild(line);
  svg.appendChild(startCircleOuter);
  svg.appendChild(startCircleInner);

  // Floating Action badge at midpoint of swipe
  const midX = (clampedX1 + clampedX2) / 2;
  const midY = (clampedY1 + clampedY2) / 2;

  const label = document.createElement('div');
  label.className = 'action-point-marker action-line-label';
  label.style.position = 'absolute';
  label.style.left = `${midX}%`;
  label.style.top = `${midY}%`;
  label.style.transform = 'translate(-50%, -50%)';
  label.style.pointerEvents = 'none';
  label.style.zIndex = '10';

  const badge = document.createElement('div');
  badge.innerText = actionType.toUpperCase();
  badge.style.backgroundColor = 'rgba(15, 23, 42, 0.92)';
  badge.style.color = 'white';
  badge.style.padding = '2px 6px';
  badge.style.borderRadius = '4px';
  badge.style.fontSize = '9px';
  badge.style.fontWeight = 'bold';
  badge.style.letterSpacing = '0.5px';
  badge.style.whiteSpace = 'nowrap';
  badge.style.boxShadow = '0 2px 6px rgba(0,0,0,0.5)';
  badge.style.border = '1px solid rgba(255,255,255,0.3)';

  label.appendChild(badge);
  overlay.appendChild(label);
}
