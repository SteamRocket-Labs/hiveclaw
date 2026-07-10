import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { SessionWorkbenchHeader } from './SessionWorkbenchChrome';
import type { RuntimeSectionItemModel, SessionWorkbenchHeaderModel } from './timelineModel';
import { runtimeItemDisplayMeta, runtimeItemDisplayStatus } from '../agent-detail/AgentChatSection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback || _key,
  }),
}));

describe('session experience information policy', () => {
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
    expect(runtimeItemDisplayStatus(item)).toBe('idle · completed');
    expect(runtimeItemDisplayMeta(item)).not.toContain(item.id);
    expect(runtimeItemDisplayMeta(item)).not.toContain(item.childSessionId!);
    expect(runtimeItemDisplayMeta(item)).not.toContain('team_member');
  });
});
