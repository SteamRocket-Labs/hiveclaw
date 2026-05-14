import { describe, expect, it } from 'vitest';

import {
  applyDeepResearchEvent,
  _INITIAL_DEEP_RESEARCH_STREAM_STATE,
  type DeepResearchStreamState,
} from './useDeepResearchStream';
import type { DeepResearchStreamEvent } from '../api/domains/deepResearch';

function _event(
  ev: DeepResearchStreamEvent['event'],
  payload: Record<string, unknown> = {},
): DeepResearchStreamEvent {
  return { event: ev, task_id: 'task-test', payload };
}

describe('applyDeepResearchEvent (Tier 3-4 hook reducer)', () => {
  it('routes typed events into their named buckets', () => {
    let state: DeepResearchStreamState = _INITIAL_DEEP_RESEARCH_STREAM_STATE;
    state = applyDeepResearchEvent(state, _event('step', { phase: 'plan' }));
    state = applyDeepResearchEvent(state, _event('claim', { claim_id: 'c1' }));
    state = applyDeepResearchEvent(state, _event('source_note', { source_id: 's1' }));
    state = applyDeepResearchEvent(state, _event('lane_summary', { lane_id: 'l1' }));
    state = applyDeepResearchEvent(state, _event('reflection', { stop_signal: false }));
    state = applyDeepResearchEvent(state, _event('controller_trace', { step_index: 1 }));

    expect(state.events).toHaveLength(6);
    expect(state.steps).toHaveLength(1);
    expect(state.claims).toHaveLength(1);
    expect(state.sourceNotes).toHaveLength(1);
    expect(state.laneSummaries).toHaveLength(1);
    expect(state.reflections).toHaveLength(1);
    expect(state.controllerTrace).toHaveLength(1);
  });

  it('captures the most recent report markdown delta', () => {
    let state: DeepResearchStreamState = _INITIAL_DEEP_RESEARCH_STREAM_STATE;
    state = applyDeepResearchEvent(state, _event('report', { markdown: '# v1', chars: 4 }));
    expect(state.reportMarkdown).toBe('# v1');
    state = applyDeepResearchEvent(state, _event('report', { markdown: '# v2 longer', chars: 11 }));
    expect(state.reportMarkdown).toBe('# v2 longer');
  });

  it('completes the stream when final.payload.status is completed', () => {
    let state: DeepResearchStreamState = { ..._INITIAL_DEEP_RESEARCH_STREAM_STATE, status: 'streaming' };
    state = applyDeepResearchEvent(state, _event('final', { status: 'completed', source_count: 4 }));
    expect(state.status).toBe('completed');
    expect(state.finalPayload).toEqual({ status: 'completed', source_count: 4 });
  });

  it('marks the stream failed when final.payload.status is failed', () => {
    let state: DeepResearchStreamState = { ..._INITIAL_DEEP_RESEARCH_STREAM_STATE, status: 'streaming' };
    state = applyDeepResearchEvent(state, _event('final', { status: 'failed', gaps: ['no sources'] }));
    expect(state.status).toBe('failed');
    expect(state.finalPayload?.gaps).toEqual(['no sources']);
  });

  it('counts heartbeats without changing status', () => {
    let state: DeepResearchStreamState = { ..._INITIAL_DEEP_RESEARCH_STREAM_STATE, status: 'streaming' };
    state = applyDeepResearchEvent(state, _event('heartbeat', {}));
    state = applyDeepResearchEvent(state, _event('heartbeat', {}));
    expect(state.heartbeats).toBe(2);
    expect(state.status).toBe('streaming');
  });

  it('captures error events and marks the stream failed', () => {
    let state: DeepResearchStreamState = { ..._INITIAL_DEEP_RESEARCH_STREAM_STATE, status: 'streaming' };
    state = applyDeepResearchEvent(state, _event('error', { message: 'upstream timeout' }));
    expect(state.status).toBe('failed');
    expect(state.error?.message).toBe('upstream timeout');
  });

  it('preserves prior buckets when applying further events', () => {
    let state: DeepResearchStreamState = _INITIAL_DEEP_RESEARCH_STREAM_STATE;
    state = applyDeepResearchEvent(state, _event('claim', { claim_id: 'c1' }));
    state = applyDeepResearchEvent(state, _event('claim', { claim_id: 'c2' }));
    state = applyDeepResearchEvent(state, _event('source_note', { source_id: 's1' }));
    expect(state.claims.map((e) => e.payload.claim_id)).toEqual(['c1', 'c2']);
    expect(state.sourceNotes).toHaveLength(1);
  });
});
