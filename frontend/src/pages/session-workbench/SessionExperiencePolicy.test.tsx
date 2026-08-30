import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { SessionWorkbenchHeader } from './SessionWorkbenchChrome';
import type { RuntimeSectionItemModel, SessionWorkbenchHeaderModel } from './timelineModel';
import { runtimeItemDisplayMeta, runtimeItemDisplayStatus } from '../agent-detail/SessionRuntimePanel';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback || _key,
  }),
}));

describe('session experience information policy', () => {
  it('keeps execution resume distinct from transport reconnection in both locales', () => {
    expect(en.agent.chat.phase.resuming).toBe('Resuming work');
    expect(zh.agent.chat.phase.resuming).toBe('恢复运行中');
    expect(en.agent.chat.phase.resuming).not.toBe(en.agent.chat.transport.reconnecting);
    expect(zh.agent.chat.phase.resuming).not.toBe(zh.agent.chat.transport.reconnecting);
  });

  it('keeps the ordinary session header semantic and hides runtime mechanics', () => {
    const model: SessionWorkbenchHeaderModel = {
      sessionId: '00000000-0000-4000-8000-000000000001',
      title: 'Quarterly research',
      status: 'waiting',
      modelLabel: 'GPT Test',
      providerLabel: 'openai',
      resumeHealth: 'healthy',
      permissionMode: 'default',
      governanceLabel: '12 tools · 2 roots',
      activeProjection: 'rewind',
      checkpointCount: 8,
      branchDepth: 3,
      compactionCount: 2,
      contextWindowStatusLabel: '14% used',
      contextWindowTitle: '18,400 / 128,000 tokens',
      activeRunStatus: 'waiting_user',
    };

    const markup = renderToStaticMarkup(<SessionWorkbenchHeader model={model} />);

    expect(markup).toContain('Quarterly research');
    expect(markup).toContain('waiting');
    expect(markup).toContain('GPT Test');
    expect(markup).toContain('Review the request below');
    expect(markup).not.toContain('00000000');
    expect(markup).not.toContain('openai');
    expect(markup).not.toContain('healthy');
    expect(markup).not.toContain('default');
    expect(markup).not.toContain('12 tools');
    expect(markup).not.toContain('rewind');
    expect(markup).not.toContain('checkpoints');
    expect(markup).not.toContain('compactions');
    expect(markup).not.toContain('14% used');
    expect(markup).not.toContain('waiting_user');
  });

  it('builds runtime row copy from semantic role, outcome, summary, and metrics only', () => {
    const item = {
      id: '00000000-0000-4000-8000-000000000002',
      label: 'Researcher',
      status: 'idle',
      state: 'idle',
      runtimeKind: 'team_member',
      summary: 'Compared the three vendors.',
      childSessionId: '00000000-0000-4000-8000-000000000003',
      enterable: true,
      metrics: {
        elapsedSeconds: 120,
        elapsedLabel: '2m',
        tokenCount: 1200,
        tokenLabel: '1.2K',
        toolUseCount: 3,
        toolUseLabel: '3',
        lastActivityLabel: null,
      },
      members: [],
      steps: [],
      leafCalls: [],
      userBlocker: null,
      raw: { member_role: 'Market analyst', last_turn_status: 'completed' },
    } satisfies RuntimeSectionItemModel;

    expect(runtimeItemDisplayMeta(item)).toBe('Market analyst · Compared the three vendors. · 2m · 1.2K tokens · 3 tools');
    expect(runtimeItemDisplayStatus(item)).toBe('Ready · Completed');
    expect(runtimeItemDisplayMeta(item)).not.toContain(item.id);
    expect(runtimeItemDisplayMeta(item)).not.toContain(item.childSessionId!);
    expect(runtimeItemDisplayMeta(item)).not.toContain('team_member');
  });

  it('presents an exact hash-pinned runtime receipt as a user-facing delivery state', () => {
    const receipt = 'Durable result committed: ref=runtime-result://3be99530-7c50-5adc-9aa6-bdd6e401ffba/22664ea63afefe92980bbc9293ea8c711fcec8226ad46ad1b62c0109534a9a6d sha256=22664ea63afefe92980bbc9293ea8c711fcec8226ad46ad1b62c0109534a9a6d bytes=795.';
    const item = {
      id: '83fd572f-fc55-44b8-9deb-98809a53a2d5',
      label: 'delegation',
      status: 'completed',
      state: 'completed',
      runtimeKind: 'peer_a2a',
      summary: receipt,
      childSessionId: '437323b5-44bb-4d4d-8fb9-231e1df42421',
      enterable: true,
      metrics: {
        elapsedSeconds: null,
        elapsedLabel: null,
        tokenCount: null,
        tokenLabel: null,
        toolUseCount: null,
        toolUseLabel: null,
        lastActivityLabel: null,
      },
      members: [],
      steps: [],
      leafCalls: [],
      userBlocker: null,
      raw: {},
    } satisfies RuntimeSectionItemModel;

    expect(runtimeItemDisplayMeta(item)).toBe('Result delivered');
    expect(runtimeItemDisplayMeta(item)).not.toContain('runtime-result://');
    expect(runtimeItemDisplayMeta(item)).not.toContain('sha256');
    expect(item.summary).toBe(receipt);
    expect(en.sessionWorkbench.rightPanel.resultDelivered).toBe('Result delivered');
    expect(zh.sessionWorkbench.rightPanel.resultDelivered).toBe('结果已交付');
  });

  it('shows a failed Team close as recoverable semantic state', () => {
    const item = {
      id: 'team-1',
      label: 'Research Team',
      status: 'active',
      state: 'active',
      runtimeKind: 'agent_team',
      summary: '',
      childSessionId: null,
      enterable: false,
      metrics: {
        elapsedSeconds: null,
        elapsedLabel: null,
        tokenCount: null,
        tokenLabel: null,
        toolUseCount: null,
        toolUseLabel: null,
        lastActivityLabel: null,
      },
      members: [],
      steps: [],
      leafCalls: [],
      raw: { close_status: 'failed', close_failure: 'Provider timeout' },
    } satisfies RuntimeSectionItemModel;

    expect(runtimeItemDisplayStatus(item)).toBe('Working · Needs attention');
  });
});
