// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import rows from '../../test/fixtures/x3bMemberRows.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { ArtifactCards } from './ArtifactSurface';
import RunDisclosureBlock from './RunDisclosureBlock';
import { buildRunTimelineFromMessages, type RunStepSnapshot } from './chatDisclosureReducer';
import { hydrateSessionTranscriptEvents } from './sessionEventConsumer';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierDeps } from './sessionTranscriptApplier';
import type { ChatTranscriptEventPayload } from './chatRuntime';

const locale = vi.hoisted(() => ({ language: 'zh' as 'zh' | 'en' }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(locale.language === 'zh' ? zh : en, key, fallback, values),
}) }));
vi.mock('./AgentWorkspaceSection', () => ({ default: () => null }));
afterEach(() => { cleanup(); locale.language = 'zh'; });

function expand(container: HTMLElement) {
  for (const button of container.querySelectorAll<HTMLButtonElement>('button[aria-expanded="false"]')) fireEvent.click(button);
  for (const button of container.querySelectorAll<HTMLButtonElement>('button[aria-expanded="false"]')) fireEvent.click(button);
}

function renderSteps(steps: RunStepSnapshot[]) {
  const view = render(<RunDisclosureBlock timeline={{ id: 'run', status: 'running', steps }} />);
  expand(view.container);
  return view;
}

function safe(text: string) {
  for (const leak of ['Provenance hint', 'folder(s)', rows.cancel, rows.placeholder, 'https://', 'workspace/']) expect(text).not.toContain(leak);
  expect(text).not.toMatch(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
}

function step(status: RunStepSnapshot['status'], presentation: RunStepSnapshot['presentation'] = 'tool_history'): RunStepSnapshot {
  return { id: `${status}-${presentation}`, kind: 'file', title: 'Read file', status, presentation,
    visibility: 'collapsed', details: { toolName: 'read_file', args: { path: 'workspace/uploads/sales-run18.csv' },
      result: status === 'failed' ? rows.refused_read : 'private raw CSV' } };
}

it.each(['list_files', 'future_tool'])('never renders unsummarized raw output for %s', toolName => {
  const { container } = renderSteps([{ ...step('done'), details: { toolName, result: rows.list_files } }]);
  safe(container.textContent || '');
  expect(container.querySelector('p')?.textContent).toBeTruthy();
});

it.each(['personal', 'company'])('states typed empty, found and failed %s KB results plainly', scope => {
  for (const [found, state, sentence] of [[false, 'done', '没有找到相关内容'], [true, 'done', '找到了相关内容'],
    [undefined, 'not_done', '这次没有成功']] as const) {
    const { container, unmount } = renderSteps([{ ...step(state === 'not_done' ? 'failed' : 'done'),
      details: { toolName: `search_${scope}_kb`, result: '', memberSummary: { kind: `${scope}_kb_search`, state, found } } }]);
    expect(container.textContent).toContain(`${scope === 'personal' ? '查找个人资料' : '查找公司资料'}，${sentence}`);
    safe(container.textContent || '');
    unmount();
  }
});

it.each(['zh', 'en'] as const)('marks unsuccessful history and surfaced rows accessibly in %s', language => {
  locale.language = language;
  for (const presentation of ['tool_history', 'surface'] as const) {
    for (const status of ['failed', 'cancelled', 'blocked'] as const) {
      const { container, unmount } = renderSteps([step(status, presentation)]);
      const row = container.querySelector(presentation === 'surface' ? '.run-step' : '.run-tool-history-item');
      expect(row?.getAttribute('data-status')).toBe(status);
      const label = status === 'cancelled' ? language === 'zh' ? '已停止' : 'Stopped'
        : status === 'blocked' ? language === 'zh' ? '等待处理' : 'Needs attention'
          : language === 'zh' ? '未完成' : 'Not completed';
      expect(row?.querySelector(`[role="img"][aria-label="${label}"]`)).toBeTruthy();
      expect(row?.querySelector('.tabler-icon-file-text')).toBeNull();
      unmount();
    }
  }
});

function native(sequence: number, kind: string, payload: Record<string, unknown>) {
  return { schema: 'hive.session_event', schema_version: 2, event_id: `event-${sequence}`, sequence, ordinal: sequence - 1,
    tenant_id: 'tenant', scope: { level: 'round', session_id: 'session', thread_id: 'session', turn_id: 'turn', run_id: 'run', round_id: 'round' },
    item_id: `${kind}-${sequence}`, item_kind: kind, kind: `${kind}.completed`, lifecycle: 'completed',
    payload_schema: `hive.session.payload.${kind}.completed.v2`, actor: { type: kind === 'tool_call' ? 'assistant' : 'tool' },
    visibility: { audience: 'direct_user' }, invocation_id: 'invocation', payload,
    occurred_at: '2026-11-13T09:51:21Z', persisted_at: '2026-11-13T09:51:21Z' };
}

it.each(rows.web_urls)('recovers only the typed host of a native fetch through reload: %s', url => {
  const events = [native(1, 'tool_call', { tool_name: 'web_fetch', args: { url } }),
    native(2, 'tool_result', { tool_name: 'web_fetch', outcome: 'success', content: '',
      member_summary: { kind: 'web_fetch', state: url.includes('.invalid') ? 'not_done' : 'done' } })];
  const messages = hydrateSessionTranscriptEvents(events as never, 0).messages;
  const { container } = renderSteps(buildRunTimelineFromMessages(messages).steps);
  expect(container.textContent).toContain(url.split('/')[2]);
  safe(container.textContent || '');
});

it('reads the basename from the paired typed call through reload', () => {
  const events = [native(1, 'tool_call', { tool_name: 'read_file', args: { path: 'workspace/uploads/sales-run18.csv' } }),
    native(2, 'tool_result', { tool_name: 'read_file', outcome: 'success', content: '', member_summary: { kind: 'file_read', state: 'done' } })];
  const messages = hydrateSessionTranscriptEvents(events as never, 0).messages;
  const { container } = renderSteps(buildRunTimelineFromMessages(messages).steps);
  expect(container.textContent).toContain('读取了文件「sales-run18.csv」');
  safe(container.textContent || '');
});

it('omits a UUID basename in the row summary and expanded sentence', () => {
  const messages = [{ role: 'tool_call', toolName: 'read_file', toolStatus: 'done', toolArgs: { path: 'workspace/120e98ce-d9e6-5e29-a707-7992e369a745.md' },
    toolMemberSummary: { kind: 'file_read', state: 'done' }, content: '' }];
  const { container } = renderSteps(buildRunTimelineFromMessages(messages as never).steps);
  safe(container.textContent || '');
});

it('counts only successful file reads, including reloaded content_source_changed refusals', () => {
  const events = ['done', 'failed', 'done', 'failed', 'failed'].map((status, index) => ({
    id: `read-${index}`, sequence: index + 1, event_type: 'tool_result', run_id: 'run',
    content: JSON.stringify({ name: 'read_file', args: { path: 'workspace/uploads/sales-run18.csv' }, status,
      result: status === 'failed' ? rows.refused_read : 'date,sales\n2026,35', tool_call_id: `call-${index}` }),
    metadata: { source: 'web_chat_runtime', status }, created_at: '2026-11-13T09:51:21Z',
  }));
  const timeline = buildRunTimelineFromMessages(hydrateSessionTranscriptEvents(events as never, 0).messages);
  expect(timeline.summary?.files).toBe(2);
  const { container } = render(<RunDisclosureBlock timeline={{ ...timeline, status: 'running' }} />);
  expand(container);
  expect(container.querySelector('.run-disclosure-header')?.textContent).toContain('读取 2 个文件');
  safe(container.textContent || '');
});

it.each(['completed', 'failed', 'killed'])('updates earlier delegation rows to %s live and after reload', status => {
  const task = '120e98ce-d9e6-5e29-a707-7992e369a745';
  const events = [
    { id: 'delegate', sequence: 1, event_type: 'tool_result', run_id: 'parent-run', created_at: '2026-11-13T09:51:21Z',
      content: JSON.stringify({ name: 'delegate_to_agent', args: { agent_name: '同事' }, status: 'done',
        result: JSON.stringify({ ok: true, operation: 'delegate', status: 'running', task_id: task }) }),
      metadata: { member_summary: { kind: 'delegation', state: 'in_progress', task_id: task } } },
    { id: 'started', sequence: 2, event_type: 'runtime_action_started', run_id: 'parent-run', content: '已委派给 同事，后台执行中。',
      metadata: { status: 'running', action_kind: 'a2a_delegation', runtime_task_id: task, target_agent_name: '同事' } },
    { id: 'check', sequence: 3, event_type: 'tool_result', run_id: 'parent-run', created_at: '2026-11-13T09:51:22Z',
      content: JSON.stringify({ name: 'check_async_task', args: { task_id: task }, status: 'done', result: '' }),
      metadata: { member_summary: { kind: 'colleague_result', state: 'in_progress', task_id: task } } },
    { id: 'terminal', sequence: 4, event_type: 'child_session', run_id: 'child-run', content: status === 'killed' ? rows.cancel : '同事结果',
      metadata: { status, reason: 'delegation_terminal', runtime_task_id: task, to_agent_name: '同事' } },
  ];
  const reload = hydrateSessionTranscriptEvents(events as never, 0).messages;
  let live: ReturnType<typeof hydrateSessionTranscriptEvents>['messages'] = [];
  const deps: SessionTranscriptApplierDeps = {
    refs: { transcriptEvents: {}, eventStores: {}, fullHydrationKeys: new Set(['agent:session']), replayStates: {},
      compatibilityTimelines: {}, visibilityBoundaries: {}, uiStates: {}, runtimeActivityAt: {}, pendingUserMessages: {} },
    getActiveRunId: () => 'parent-run', markActiveRunTerminal: () => true, isTerminalTranscriptToolMessage: () => false,
    mergePendingMessages: (_key, messages) => messages, setChatMessagesSessionId: vi.fn(),
    enqueueChatMessagesUpdate: (_session, update) => { live = update(live); },
    setChatMessagesAfterQueued: (_session, update) => { live = update(live); },
    setActivePhase: vi.fn(), setIsWaiting: vi.fn(), setIsStreaming: vi.fn(), parseChatMsg: message => message,
  };
  for (const event of events) {
    applyTranscriptToSessionRuntime(deps, 'agent', 'session', event as ChatTranscriptEventPayload, true);
  }
  for (const messages of [live, reload]) {
    const { container, unmount } = renderSteps(buildRunTimelineFromMessages(messages).steps);
    expect(container.textContent).not.toContain('后台执行中');
    expect(container.textContent).not.toContain('同事正在处理');
    expect(container.textContent).toContain(status === 'completed' ? '同事已完成' : status === 'killed' ? '已停止' : '没有完成');
    safe(container.textContent || '');
    unmount();
  }
});

it('does not claim a cancellation note is a kept result', () => {
  const row: RunStepSnapshot = { id: 'notification', kind: 'event', status: 'done', title: 'Task notification', visibility: 'visible',
    details: '1 runtime result(s) ready in integration epoch 1 (terminal=1/expected=1).',
    systemEvent: { type: 'agent_task_notification', metadata: { runtime_result_integration_page: true, continuation_status: 'denied', item_count: 1,
      result_manifest: { items: [{ terminal_status: 'killed', result_size_bytes: 30, artifact_count: 0 }] } } } };
  const { container } = renderSteps([row]);
  expect(container.textContent).not.toContain('结果已保留');
  expect(container.textContent).toContain('没有可供查看的结果');
});

it.each(['zh', 'en'] as const)('localizes file card types in %s for uploads and results', language => {
  locale.language = language;
  const { container } = render(<ArtifactCards artifacts={[
    { name: 'report.md', path: 'workspace/report.md', previewKind: 'markdown', size: 5120 },
    { name: 'sales.csv', path: 'workspace/uploads/sales.csv', source: 'human_input', size: 100 },
    { name: 'data.custom', path: 'workspace/uploads/data.custom', source: 'human_input', size: 200 },
  ]} agentId="agent" onOpenArtifact={vi.fn()} />);
  const types = [...container.querySelectorAll('.chat-artifact-meta')].map(node => node.textContent);
  expect(types).toEqual(language === 'zh' ? ['Markdown 文档 · 5 KB', 'CSV 表格 · 100 B', '文件 · 200 B']
    : ['markdown · 5 KB', 'csv · 100 B', 'File · 200 B']);
});
