import { describe, expect, it } from 'vitest';

import { buildThreadTimeline } from './timelineModel';
import type { AgentChatMessage } from '../agent-detail/chatRuntime';

describe('X1 expanded block does not repeat the final answer', () => {
  it('drops the in-run prose copy when the same text is the answer below the block', () => {
    const answer = '我负责在你已授权的数据源内汇总周度销售数据，交付周度汇总表与中文周报草稿。';
    const messages: AgentChatMessage[] = [
      { id: 'input', role: 'user', content: '用一句话介绍你自己', timestamp: '2026-11-12T06:59:00Z' },
      {
        id: 'reasoning-1', role: 'assistant', thinking: '先想一下怎么介绍。', content: '', timestamp: '2026-11-12T06:59:10Z',
        sessionItem: { id: 'reasoning-1', kind: 'assistant_reasoning_summary', lifecycle: 'completed', terminal: true } as AgentChatMessage['sessionItem'],
      },
      {
        id: 'assistant-text-1', role: 'assistant', content: answer, timestamp: '2026-11-12T06:59:20Z',
        sessionItem: { id: 'assistant-text-1', kind: 'assistant_text', lifecycle: 'completed', terminal: true } as AgentChatMessage['sessionItem'],
      },
      {
        id: 'assistant-final-1', role: 'assistant', content: answer, timestamp: '2026-11-12T06:59:21Z',
        sessionItem: { id: 'assistant-final-1', kind: 'assistant_final', lifecycle: 'completed', terminal: true } as AgentChatMessage['sessionItem'],
      },
    ];
    const model = buildThreadTimeline({ messages, isWaiting: false, isStreaming: false, activeRunStatus: null });
    expect(model.cells.some((cell) => cell.kind === 'assistant_final')).toBe(true);
    const run = model.cells.find((cell) => cell.kind === 'active_run');
    if (!run || run.kind !== 'active_run') return;
    expect(run.timeline.answerMessageId).toBeTruthy();
    const repeated = run.timeline.steps.filter(
      (step) => (step.kind === 'prose' || step.kind === 'commentary') && step.details === answer,
    );
    expect(repeated).toHaveLength(0);
    // The block keeps its real process steps.
    expect(run.timeline.steps.some((step) => step.kind === 'reasoning')).toBe(true);
  });

  it('renders only the answer when the run held nothing but its copy', () => {
    const answer = '一句话回答。';
    const messages: AgentChatMessage[] = [
      { id: 'input', role: 'user', content: '问', timestamp: '2026-11-12T07:00:00Z' },
      {
        id: 'assistant-text-1', role: 'assistant', content: answer, timestamp: '2026-11-12T07:00:20Z',
        sessionItem: { id: 'assistant-text-1', kind: 'assistant_text', lifecycle: 'completed', terminal: true } as AgentChatMessage['sessionItem'],
      },
      {
        id: 'assistant-final-1', role: 'assistant', content: answer, timestamp: '2026-11-12T07:00:21Z',
        sessionItem: { id: 'assistant-final-1', kind: 'assistant_final', lifecycle: 'completed', terminal: true } as AgentChatMessage['sessionItem'],
      },
    ];
    const model = buildThreadTimeline({ messages, isWaiting: false, isStreaming: false, activeRunStatus: null });
    expect(model.cells.some((cell) => cell.kind === 'assistant_final')).toBe(true);
    const runs = model.cells.filter((cell) => cell.kind === 'active_run');
    for (const run of runs) {
      if (run.kind !== 'active_run') continue;
      expect(run.timeline.steps.some((step) => step.details === answer)).toBe(false);
    }
  });

  it('keeps a distinct in-run progress note that is not the final answer', () => {
    const note = '先去读取这份明细，再动手核对。';
    const answer = '核对完成，两处差异已列出。';
    const messages: AgentChatMessage[] = [
      { id: 'input', role: 'user', content: '核对这份明细', timestamp: '2026-11-12T07:00:00Z' },
      {
        id: 'assistant-text-1', role: 'assistant', content: note, timestamp: '2026-11-12T07:00:10Z',
        sessionItem: { id: 'assistant-text-1', kind: 'assistant_text', lifecycle: 'completed', terminal: true } as AgentChatMessage['sessionItem'],
      },
      {
        id: 'assistant-final-1', role: 'assistant', content: answer, timestamp: '2026-11-12T07:01:00Z',
        sessionItem: { id: 'assistant-final-1', kind: 'assistant_final', lifecycle: 'completed', terminal: true } as AgentChatMessage['sessionItem'],
      },
    ];
    const model = buildThreadTimeline({ messages, isWaiting: false, isStreaming: false, activeRunStatus: null });
    const run = model.cells.find((cell) => cell.kind === 'active_run');
    if (run?.kind !== 'active_run') throw new Error('run cell missing');
    expect(run.timeline.steps.some((step) => step.kind === 'prose' && step.details === note)).toBe(true);
  });
});
