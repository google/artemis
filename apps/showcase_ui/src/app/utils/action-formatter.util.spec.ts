import {
  getStepPreImageUrl,
  getStepPostImageUrl
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

    // Action 2: "下一次的决策前是上一次的动作后" -> Pre MUST be Action 1's post (18bd2ac4...), Post should be Action 2's post (484361b1...)
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
});
