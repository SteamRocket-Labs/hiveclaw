import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { SessionWorkbenchHeader } from './SessionWorkbenchChrome';
import type { RuntimeSectionItemModel, SessionWorkbenchHeaderModel } from './timelineModel';
import { runtimeItemDisplayMeta, runtimeItemDisplayStatus } from '../agent-detail/SessionRuntimePanel';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback || _key,
    i18n: { language: 'en' },
  }),
}));

describe('session experience information policy', () => {
  it('keeps exactly five runtime labels and removes recovery phase labels in both locales', () => {
    expect(Object.values(en.agent.runtimeStatus)).toEqual(['Running', 'Waiting for you', 'Done', 'Failed', 'Stopped']);
    expect(Object.values(zh.agent.runtimeStatus)).toEqual(['运行中', '等你决定', '完成', '失败', '已停止']);
    expect(en.agent.chat.phase).not.toHaveProperty('resuming');
    expect(zh.agent.chat.phase).not.toHaveProperty('resuming');
  });

  it('offers the single management entry only when this person can manage the bot (B5)', () => {
    const model = { title: 'Quarterly research', status: 'idle' as const, modelLabel: null };
    const withManage = renderToStaticMarkup(
      <MemoryRouter>
        <SessionWorkbenchHeader model={model} history={{
          agentId: 'agent-1', sessions: [], activeSessionId: null,
          manageHref: '/agents/agent-1?manage=true#status',
        }} />
      </MemoryRouter>,
    );
    expect(withManage).toContain('href="/agents/agent-1?manage=true#status"');
    expect(withManage).toContain('Settings');
    const withoutManage = renderToStaticMarkup(
      <MemoryRouter>
        <SessionWorkbenchHeader model={model} history={{ agentId: 'agent-1', sessions: [], activeSessionId: null }} />
      </MemoryRouter>,
    );
    expect(withoutManage).not.toContain('manage=true');
  });

  it('keeps the ordinary session header semantic and hides runtime mechanics', () => {
    // B5: the header model carries only rendered fields; the unread inspector
    // metadata was deleted with the header chrome it used to feed.
    const model: SessionWorkbenchHeaderModel = {
      title: 'Quarterly research',
      status: 'waiting',
      modelLabel: 'GPT Test',
    };

    const markup = renderToStaticMarkup(<SessionWorkbenchHeader model={model} />);

    expect(markup).toContain('Quarterly research');
    expect(markup).toContain('Waiting for you');
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

  it('shows no run status for a conversation that has not run yet (B5 review)', () => {
    // An untouched draft maps to idle; idle is not one of the five user-visible
    // run states, so the header shows the localized placeholder title and no
    // status chip (it used to read ● Done/完成). The five-state mapping for
    // conversations with runs is unchanged — see the control below.
    const fresh: SessionWorkbenchHeaderModel = { title: '', status: 'idle', modelLabel: null };
    const markup = renderToStaticMarkup(
      <MemoryRouter><SessionWorkbenchHeader model={fresh} /></MemoryRouter>,
    );
    expect(markup).toContain('New conversation');
    expect(markup).not.toContain('swb-header-status');
    expect(markup).not.toContain('>Done<');

    // A local-runtime conversation that is not sending shows its Local badge
    // without a run status.
    const localIdle: SessionWorkbenchHeaderModel = { title: 'Desk assistant', status: 'idle', modelLabel: 'Local' };
    const localMarkup = renderToStaticMarkup(
      <MemoryRouter><SessionWorkbenchHeader model={localIdle} /></MemoryRouter>,
    );
    expect(localMarkup).toContain('Local');
    expect(localMarkup).not.toContain('>Done<');

    // Control: a conversation with a completed run keeps its five-state label.
    const complete: SessionWorkbenchHeaderModel = { title: 'Report', status: 'complete', modelLabel: null };
    expect(renderToStaticMarkup(
      <MemoryRouter><SessionWorkbenchHeader model={complete} /></MemoryRouter>,
    )).toContain('>Done<');
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
    expect(runtimeItemDisplayStatus(item)).toBe('Done');
    expect(runtimeItemDisplayMeta(item)).not.toContain(item.id);
    expect(runtimeItemDisplayMeta(item)).not.toContain(item.childSessionId!);
    expect(runtimeItemDisplayMeta(item)).not.toContain('team_member');
  });

  it('presents an exact hash-pinned runtime receipt as a user-facing delivery state', () => {
    const receipt = 'Durable result committed: ref=runtime-result://00000132-0000-4000-8000-000000000000/22664ea63afefe92980bbc9293ea8c711fcec8226ad46ad1b62c0109534a9a6d sha256=22664ea63afefe92980bbc9293ea8c711fcec8226ad46ad1b62c0109534a9a6d bytes=795.';
    const item = {
      id: '0000027f-0000-4000-8000-000000000000',
      label: 'delegation',
      status: 'completed',
      state: 'completed',
      runtimeKind: 'peer_a2a',
      summary: receipt,
      childSessionId: '0000015b-0000-4000-8000-000000000000',
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

  it('keeps the collapsed rail and empty console copy semantic in both locales', () => {
    expect(en.sessionWorkbench.rightPanel.noRuntimeActivity).toBe(
      'No background agents, teams, or workflows in this session.',
    );
    expect(zh.sessionWorkbench.rightPanel.noRuntimeActivity).toBe('此会话没有后台任务、团队或工作流。');
    expect(en.sessionWorkbench.rightPanel.showDeliverables).toBe('Show deliverables ({{count}})');
    expect(zh.sessionWorkbench.rightPanel.showDeliverables).toBe('查看 {{count}} 个交付物');
    expect(en.sessionWorkbench.rightPanel.showAttentionItems).toBe('Show items waiting for you ({{count}})');
    expect(zh.sessionWorkbench.rightPanel.showAttentionItems).toBe('查看 {{count}} 个待你处理的事项');
    expect(en.sessionWorkbench.rightPanel.showRunningItems).toBe('Show running items ({{count}})');
    expect(zh.sessionWorkbench.rightPanel.showRunningItems).toBe('查看 {{count}} 个正在运行的任务');
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

    expect(runtimeItemDisplayStatus(item)).toBe('Failed');
  });
});
