import { drawActionCoordinatesOnOverlay, unwrapTraceAction } from './image-overlay.util';
import { getActionCoords, getActionTargetText } from './action-formatter.util';

// Trace records as the DataEngine publishes them in `generic_tools`.
const PRO_TAP_TRACE = {
  trace_id: 'pro-tap',
  type: 'action',
  name: 'tap',
  timestamp: 1788478482.05,
  status: 'success',
  payload: {
    action: {
      action: 'tap',
      coordinates: [922, 1710],
      coordinate_space: 'pixel',
      normalized_coordinates: [854, 705],
      target_text: 'YouTube'
    },
    success: true,
    post_screenshot: null
  }
};

const FLASH_CLICK_TRACE = {
  trace_id: 'flash-click',
  type: 'action',
  name: 'click',
  timestamp: 1788478305.12,
  status: 'success',
  payload: {
    args: { target: [618, 705], target_text: '相册' },
    result: '{"outcome": "Clicked"}'
  }
};

function fakeImage(): HTMLImageElement {
  return { naturalWidth: 1080, naturalHeight: 2424 } as unknown as HTMLImageElement;
}

describe('unwrapTraceAction', () => {
  it('flattens a Pro action trace (payload.action)', () => {
    const act = unwrapTraceAction(PRO_TAP_TRACE);
    expect(act.action).toBe('tap');
    expect(act.normalized_coordinates).toEqual([854, 705]);
    expect(act.target_text).toBe('YouTube');
    expect(act.trace_id).toBe('pro-tap');
    expect(act.timestamp).toBe(1788478482.05);
  });

  it('flattens a Flash action trace (payload.args)', () => {
    const act = unwrapTraceAction(FLASH_CLICK_TRACE);
    expect(act.action).toBe('click');
    expect(act.target).toEqual([618, 705]);
    expect(act.args.target).toEqual([618, 705]);
  });

  it('passes plain action objects through untouched', () => {
    const plain = { action: 'tap', normalized_coordinates: [10, 20] };
    expect(unwrapTraceAction(plain)).toBe(plain);
  });
});

describe('drawActionCoordinatesOnOverlay', () => {
  it('draws the tap marker for a Pro action trace', () => {
    const overlay = document.createElement('div');
    drawActionCoordinatesOnOverlay(fakeImage(), overlay, PRO_TAP_TRACE);
    const marker = overlay.querySelector('.action-point-marker') as HTMLElement;
    expect(marker).not.toBeNull();
    expect(marker.style.left).toBe('85.4%');
    expect(marker.style.top).toBe('70.5%');
  });

  it('draws the tap marker for a Flash action trace', () => {
    const overlay = document.createElement('div');
    drawActionCoordinatesOnOverlay(fakeImage(), overlay, FLASH_CLICK_TRACE);
    expect(overlay.querySelector('.action-point-marker')).not.toBeNull();
  });
});

describe('action card details from trace records', () => {
  it('reads coordinates and target text from a Pro action trace', () => {
    expect(getActionCoords(PRO_TAP_TRACE)).toContain('854');
    expect(getActionTargetText(PRO_TAP_TRACE)).toBe('YouTube');
  });
});
