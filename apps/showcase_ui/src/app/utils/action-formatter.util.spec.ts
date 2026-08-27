import {
  getStepPreImageUrl,
  getStepPostImageUrl,
  extractStepReplayFrames
} from './action-formatter.util';

describe('action-formatter.util screenshot chaining', () => {
  it('should chain pre/post images correctly in FailureAnalyzer multi-step recovery', () => {
    const stepData = {
      step_id: 'step-06',
      pre_image_name: '01a0e8d8cddbeb6a352e3bef1afbee0f0476a05b0fc62abb78e59c7b6aa16079',
      post_image_name: '484361b1ab4b340415e01042e3496f0edc0549c837c897ece242835d12d7d71d',
      action_taken: {
        action: 'tap',
        coordinates: [540, 720],
        timestamp: 1787077538.0
      },
      last_execution_result: {
        status: 'success',
        repair_status: 'fixed',
        execution: [
          {
            attempts: ['Pre-execution validation failed']
          }
        ]
      },
      generic_tools: [
        {
          trace_id: 'tool-save-note',
          name: 'save_note',
          type: 'tool',
          timestamp: 1787077537.0,
          payload: { args: { title: 'Note 1' } }
        },
        {
          trace_id: 'agent-failure-analyzer',
          name: 'failure_analyzer',
          agent_name: 'failure_analyzer',
          type: 'agent',
          timestamp: 1787077540.0,
          payload: {
            args: {
              pre_screenshot_name: '01a0e8d8cddbeb6a352e3bef1afbee0f0476a05b0fc62abb78e59c7b6aa16079',
              post_screenshot_name: 'fbdf17fa7a970d2c37aa52ae4336119e76a7d13ea457811cf8aaab19b3f53843'
            }
          }
        },
        {
          trace_id: 'action-click-1',
          name: 'click',
          type: 'action',
          agent_name: 'failure_analyzer',
          timestamp: 1787077607.0,
          payload: {
            args: { target: '[500, 300]' },
            result: {
              outcome: 'Clicked successfully',
              post_image_name: '18bd2ac49e64976f1e74e828ddae76e54da1587044b1d3fb01317dd202ff7c67'
            }
          }
        },
        {
          trace_id: 'action-click-seq-2',
          name: 'click_sequence',
          type: 'action',
          agent_name: 'failure_analyzer',
          timestamp: 1787077631.0,
          payload: {
            args: { sequence: '[[500, 300], [876, 360]]' },
            result: {
              outcome: 'Sequence clicked successfully',
              post_image_name: '484361b1ab4b340415e01042e3496f0edc0549c837c897ece242835d12d7d71d'
            }
          }
        }
      ]
    };

    const action1 = stepData.generic_tools[2]; // click
    const action2 = stepData.generic_tools[3]; // click_sequence

    // Action 1: Pre should be the failed state screenshot (fbdf17fa...), Post should be Action 1's post (18bd2ac4...)
    const act1Pre = getStepPreImageUrl(stepData, action1);
    const act1Post = getStepPostImageUrl(stepData, action1);
    expect(act1Pre).toBe('/images/fbdf17fa7a970d2c37aa52ae4336119e76a7d13ea457811cf8aaab19b3f53843');
    expect(act1Post).toBe('/images/18bd2ac49e64976f1e74e828ddae76e54da1587044b1d3fb01317dd202ff7c67');

    // Action 2: "The next pre-action state is the previous post-action state" -> Pre MUST be Action 1's post (18bd2ac4...), Post should be Action 2's post (484361b1...)
    const act2Pre = getStepPreImageUrl(stepData, action2);
    const act2Post = getStepPostImageUrl(stepData, action2);
    expect(act2Pre).toBe('/images/18bd2ac49e64976f1e74e828ddae76e54da1587044b1d3fb01317dd202ff7c67');
    expect(act2Post).toBe('/images/484361b1ab4b340415e01042e3496f0edc0549c837c897ece242835d12d7d71d');
  });

  it('should handle standard single-action step correctly', () => {
    const stepData = {
      step_id: 'step-01',
      pre_image_name: 'pre123',
      post_image_name: 'post456',
      action_taken: {
        action: 'tap',
        coordinates: [100, 200],
        timestamp: 1000
      }
    };

    const preUrl = getStepPreImageUrl(stepData, stepData.action_taken);
    const postUrl = getStepPostImageUrl(stepData, stepData.action_taken);

    expect(preUrl).toBe('/images/pre123');
    expect(postUrl).toBe('/images/post456');
  });

  it('should resolve post screenshot for primary action when generic_tools contains action traces', () => {
    const stepData = {
      step_id: 'step-02',
      pre_image_name: 'pre_screen_hash',
      post_image_name: 'post_screen_hash',
      action_taken: {
        action: 'click',
        coordinates: [500, 928],
        args: { target: [500, 928] },
        timestamp: 1000
      },
      generic_tools: [
        {
          trace_id: 'trace-click-internal',
          type: 'action',
          name: 'click',
          timestamp: 1001,
          payload: {
            args: { target: '[500, 928]', times: '1', delay_ms: '100' },
            result: {
              outcome: 'Clicked successfully.',
              post_image_name: 'post_screen_hash',
              status: 'success'
            }
          }
        }
      ]
    };

    const preUrl = getStepPreImageUrl(stepData, stepData.action_taken);
    const postUrl = getStepPostImageUrl(stepData, stepData.action_taken);

    expect(preUrl).toBe('/images/pre_screen_hash');
    expect(postUrl).toBe('/images/post_screen_hash');
  });
});

describe('extractStepReplayFrames', () => {
  it('should return an empty array for empty or invalid logs', () => {
    expect(extractStepReplayFrames([])).toEqual([]);
    expect(extractStepReplayFrames(null as any)).toEqual([]);
  });

  it('should extract 1:1 ordered StepReplayFrames with action objects and pre/post images', () => {
    const logs = [
      {
        type: 'step_updated',
        data: {
          step_id: 's-1',
          step_number: 1,
          pre_image_name: 'img_screen_1',
          post_image_name: 'img_screen_2',
          action_taken: { action: 'tap', coordinates: [500, 1000] },
          timestamp: 100
        }
      },
      {
        type: 'step_updated',
        data: {
          step_id: 's-2',
          step_number: 2,
          pre_image_name: 'img_screen_2',
          post_image_name: 'img_screen_3',
          action_taken: { action: 'input_text', text: 'hello' },
          timestamp: 200
        }
      }
    ];

    const frames = extractStepReplayFrames(logs);
    expect(frames.length).toBe(2);
    expect(frames[0].stepNumber).toBe(1);
    expect(frames[0].imageUrl).toBe('/images/img_screen_1');
    expect(frames[0].preImageUrl).toBe('/images/img_screen_1');
    expect(frames[0].postImageUrl).toBe('/images/img_screen_2');
    expect(frames[0].action).toEqual({ action: 'tap', coordinates: [500, 1000] });

    expect(frames[1].stepNumber).toBe(2);
    expect(frames[1].imageUrl).toBe('/images/img_screen_2');
    expect(frames[1].preImageUrl).toBe('/images/img_screen_2');
    expect(frames[1].postImageUrl).toBe('/images/img_screen_3');
    expect(frames[1].action).toEqual({ action: 'input_text', text: 'hello' });
  });

  it('should handle terminal step with only pre_image (e.g. cancelled / failed task)', () => {
    const logs = [
      {
        step_number: 1,
        pre_image_name: 'img_start',
        post_image_name: null,
        action_taken: null,
        timestamp: 100
      }
    ];

    const frames = extractStepReplayFrames(logs);
    expect(frames.length).toBe(1);
    expect(frames[0].imageUrl).toBe('/images/img_start');
    expect(frames[0].stepNumber).toBe(1);
  });

  it('should deduplicate steps with identical step_number and prefer real physical actions', () => {
    const logs = [
      {
        step_id: 'step-click',
        step_number: 1,
        pre_image_name: 'img_screen_1',
        post_image_name: 'img_screen_2',
        action_taken: { action: 'click', coordinates: [106, 101] },
        summary: 'In Step 1, I tapped the back arrow',
        timestamp: 100
      },
      {
        step_id: 'step-failed-report',
        step_number: 1,
        pre_image_name: 'img_screen_2',
        post_image_name: null,
        action_taken: { action: 'report_task_status', args: { status: 'failed' } },
        timestamp: 200
      },
      {
        step_id: 'step-completed-report',
        step_number: 2,
        pre_image_name: 'img_screen_2',
        post_image_name: null,
        action_taken: { action: 'report_task_status', args: { status: 'completed' } },
        timestamp: 150
      }
    ];

    const frames = extractStepReplayFrames(logs);
    // Duplicate step_number: 1 merges into Step 1, while Step 2 report is faithfully displayed
    expect(frames.length).toBe(2);
    expect(frames[0].stepNumber).toBe(1);
    expect(frames[0].action?.action).toBe('click');
    expect(frames[0].actionText).toContain('Tapping Element');
    expect(frames[1].stepNumber).toBe(2);
    expect(frames[1].title).toContain('Report Task Status');
  });

  it('should enforce strictly sequential stepNumber without duplicates', () => {
    const logs = [
      {
        step_id: 's-1',
        step_number: 1,
        pre_image_name: 'img_1',
        action_taken: { action: 'click', coordinates: [100, 100] },
        timestamp: 100
      },
      {
        step_id: 's-2',
        step_number: 2,
        pre_image_name: 'img_2',
        action_taken: { action: 'swipe', direction: 'up' },
        timestamp: 200
      }
    ];

    const frames = extractStepReplayFrames(logs);
    expect(frames.length).toBe(2);
    expect(frames[0].stepNumber).toBe(1);
    expect(frames[1].stepNumber).toBe(2);
  });

  it('should start from Step 1 even when step_updated events lack step_number (preventing the Step 4 bug)', () => {
    const logs = [
      {
        type: 'step_recorded',
        data: {
          step_id: 'step1-uuid',
          step_number: 1,
          pre_image_name: 'img1',
          post_image_name: 'img2',
          action_taken: { action: 'manage_app', app_name: 'Maps' },
          timestamp: 100
        }
      },
      {
        type: 'step_recorded',
        data: {
          step_id: 'step2-uuid',
          step_number: 2,
          pre_image_name: 'img2',
          post_image_name: 'img3',
          action_taken: { action: 'input_text', text: 'Coffee' },
          timestamp: 200
        }
      },
      {
        type: 'step_recorded',
        data: {
          step_id: 'step3-uuid',
          step_number: 3,
          pre_image_name: 'img3',
          post_image_name: 'img4',
          action_taken: { action: 'press_key', key: 'ENTER' },
          timestamp: 300
        }
      },
      // VisualStepSummarizer sends step_updated with only step_id and summary (no step_number)
      {
        type: 'step_updated',
        data: {
          step_id: 'step1-uuid',
          summary: 'In Step 1, I launched Google Maps'
        }
      },
      {
        type: 'step_updated',
        data: {
          step_id: 'step2-uuid',
          summary: 'In Step 2, I typed Coffee'
        }
      },
      {
        type: 'step_updated',
        data: {
          step_id: 'step3-uuid',
          summary: 'In Step 3, I pressed ENTER'
        }
      }
    ];

    const frames = extractStepReplayFrames(logs);
    expect(frames.length).toBe(3);

    // Frame 1 MUST start at Step 1, NOT Step 4!
    expect(frames[0].stepNumber).toBe(1);
    expect(frames[0].index).toBe(0);
    expect(frames[0].title).toContain('Step 1');
    expect(frames[0].summary).toBe('In Step 1, I launched Google Maps');
    expect(frames[0].preImageUrl).toBe('/images/img1');

    // Frame 2 MUST be Step 2
    expect(frames[1].stepNumber).toBe(2);
    expect(frames[1].index).toBe(1);
    expect(frames[1].title).toContain('Step 2');
    expect(frames[1].summary).toBe('In Step 2, I typed Coffee');

    // Frame 3 MUST be Step 3
    expect(frames[2].stepNumber).toBe(3);
    expect(frames[2].index).toBe(2);
    expect(frames[2].title).toContain('Step 3');
    expect(frames[2].summary).toBe('In Step 3, I pressed ENTER');
  });

  it('should filter non-visual steps (like pre-planning step 0) without shifting 1-based replay numbering', () => {
    const logs = [
      // Virtual step 0 (pre-planning) with no screenshots
      {
        step_id: 'pre-planning',
        step_number: 0,
        summary: 'Planning the mission',
        timestamp: 50,
        pre_image_name: null,
        post_image_name: null,
        generic_tools: [{ name: 'save_note', type: 'tool' }]
      },
      // Real step 1 with screenshots
      {
        step_id: 'real-step-1',
        step_number: 1,
        summary: 'Tapping search bar',
        action_taken: { action: 'click', coordinates: [100, 200] },
        pre_image_name: 'shot1',
        timestamp: 100
      },
      // Real step 2 with screenshots
      {
        step_id: 'real-step-2',
        step_number: 2,
        summary: 'Entering search keywords',
        action_taken: { action: 'input_text', text: 'latte' },
        pre_image_name: 'shot2',
        timestamp: 200
      }
    ];

    const frames = extractStepReplayFrames(logs);
    expect(frames.length).toBe(2);

    // Frame 0 must be labeled Step 1 (not Step 2)
    expect(frames[0].stepNumber).toBe(1);
    expect(frames[0].rawStepNumber).toBe(1);
    expect(frames[0].stepId).toBe('real-step-1');

    // Frame 1 must be labeled Step 2
    expect(frames[1].stepNumber).toBe(2);
    expect(frames[1].rawStepNumber).toBe(2);
    expect(frames[1].stepId).toBe('real-step-2');
  });
});
