import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import type { AgentChatMessage } from '../agent-detail/chatRuntime';
import { SessionWorkbenchHeader } from './SessionWorkbenchChrome';
import { buildThreadTimeline } from './timelineModel';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (_key: string, fallback?: string) => fallback || _key }),
}));

const prompt: AgentChatMessage = {
  id: 'request-1', role: 'user', content: 'Prepare the report.', timestamp: '2026-10-30T00:00:00Z',
};

function runEvent(lifecycle: string, sequence = 1, runId = 'run-1'): AgentChatMessage {
  return {
    id: `run-event-${sequence}`, role: 'event', content: '',
    timestamp: `2026-10-30T00:00:0${sequence}Z`, eventType: 'run', eventStatus: lifecycle,
    sessionItem: {
      id: `run-event-${sequence}`, kind: 'run', lifecycle,
      scope: { level: 'run', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: runId },
      terminal: ['completed', 'failed', 'cancelled'].includes(lifecycle),
    },
  } as unknown as AgentChatMessage;
}

function model(messages: AgentChatMessage[], runtimePhase = 'failed', activeRunStatus: string | null = 'failed', activeRunId?: string) {
  return buildThreadTimeline({ messages, activeSession: { id: 'session-1', title: 'Report' },
    isWaiting: false, isStreaming: false, runtimePhase, activeRunStatus, activeRunId });
}

describe('workbench unconfirmed execution', () => {
  it.each(['failed', 'thinking'])('preserves current canonical reconciliation over the %s presentation phase', (phase) => {
    const messages = [prompt, runEvent('needs_reconciliation')];
    const before = JSON.stringify(messages);
    const header = model(messages, phase).header;
    expect(header.status).toBe('failed');
    expect(header.requiresReconciliation).toBe(true);
    const markup = renderToStaticMarkup(<SessionWorkbenchHeader model={header} />);
    expect(markup).toContain('Execution outcome is unconfirmed');
    expect(markup).not.toContain('Review the failed step');
    expect(markup).not.toContain('<button');
    expect(JSON.stringify(messages)).toBe(before);
  });

  it('preserves an explicit current runtime reconciliation status without a transcript boundary', () => {
    const header = model([prompt], 'failed', 'needs_reconciliation').header;
    expect(header.status).toBe('failed');
    expect(header.requiresReconciliation).toBe(true);
  });

  it('does not carry an earlier unknown outcome into a new accepted user turn', () => {
    const next = { ...prompt, id: 'request-2', timestamp: '2026-10-30T00:00:03Z' };
    expect(model([prompt, runEvent('needs_reconciliation'), next], 'thinking', 'running').header.status).toBe('running');
  });

  it('does not let an earlier unknown run take over a distinct current run without a new user turn', () => {
    expect(model([prompt, runEvent('needs_reconciliation')], 'thinking', 'running', 'run-2').header.status).toBe('running');
  });

  it('does not let an earlier completed run hide the current run requiring reconciliation', () => {
    const header = model([prompt, runEvent('completed')], 'failed', 'needs_reconciliation', 'run-2').header;
    expect(header.status).toBe('failed');
    expect(header.requiresReconciliation).toBe(true);
  });

  it('recognizes compact and canonical UUIDs as the same current run', () => {
    expect(model([prompt, runEvent('needs_reconciliation', 1, '123e4567-e89b-12d3-a456-426614174000')],
      'failed', 'failed', '123e4567e89b12d3a456426614174000').header.requiresReconciliation).toBe(true);
  });

  it('keeps a later canonical resolution and a known failure distinct', () => {
    const final = { id: 'answer-1', role: 'assistant', content: 'The report is ready.',
      timestamp: '2026-10-30T00:00:02Z',
      sessionItem: { id: 'answer-1', kind: 'assistant_final', terminal: true,
        scope: { level: 'run', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: 'run-1' } },
    } as unknown as AgentChatMessage;
    expect(model([prompt, runEvent('needs_reconciliation'), final, runEvent('completed', 3)], 'done', null).header.status).toBe('complete');
    expect(model([prompt, runEvent('failed')]).header.status).toBe('failed');
  });

  it('provides explicit unconfirmed-outcome copy in both shipped locales', () => {
    expect((en.sessionWorkbench.status as Record<string, string>).needs_reconciliation).toBe('Needs reconciliation');
    expect((zh.sessionWorkbench.status as Record<string, string>).needs_reconciliation).toBe('待核对');
  });
});
