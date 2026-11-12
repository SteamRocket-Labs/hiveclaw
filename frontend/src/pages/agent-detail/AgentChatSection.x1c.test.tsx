// @vitest-environment jsdom
// X1-r3 ruling 1: the colleague conversation link must reach the member
// through the production call site (AgentChatSection), not only when a test
// hand-passes the prop. It shows in the viewer's own private session for a
// colleague already inside the member's permission-filtered roster, and
// nowhere else.
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';

import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import AgentChatSection from './AgentChatSection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(zh, key, fallback, values), i18n: { language: 'zh' } }),
}));
vi.mock('../../api/domains/assistant', () => ({ assistantApi: { get: vi.fn() } }));
vi.mock('../../api/domains/ccParity', () => ({
  ccParityApi: {
    getSessionWorkbench: vi.fn().mockResolvedValue(null),
    getSessionContextUsage: vi.fn().mockResolvedValue(null),
  },
}));
vi.mock('../../api/domains/plans', () => ({ planApi: { list: vi.fn().mockResolvedValue([]) } }));

afterEach(() => { cleanup(); window.localStorage.clear(); });

const delegationEvent = {
  role: 'event' as const,
  content: '「周报助手」已完成委派任务。',
  eventType: 'child_session',
  eventStatus: 'completed',
  eventReason: 'delegation_completed',
  eventRuntimeTaskId: 'run-1',
  eventParentSessionId: 'session-1',
  threadItem: {
    metadata: { to_agent: 'agent-colleague', child_session_id: 'session-colleague', to_agent_name: '周报助手' },
  },
};

function renderChat({ audienceKind, operatorView = false, roster = [{ id: 'agent-colleague' }] }: {
  audienceKind?: string; operatorView?: boolean; roster?: Array<{ id: string }>;
}) {
  window.localStorage.setItem('current_tenant_id', 'tenant-1');
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(['agents', 'tenant-1'], roster);
  const { container } = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AgentChatSection
          agent={{ id: 'agent-1', name: 'Release Bot' }}
          currentUser={{ id: 'user-1', tenant_id: 'tenant-1' }}
          isAdmin={operatorView}
          operatorReason={operatorView ? 'review' : undefined}
          onLoadAllSessions={vi.fn()}
          onCreateNewSession={vi.fn()}
          sessionsLoading={false}
          sessions={[]}
          activeSession={{
            id: 'session-1',
            user_id: 'user-1',
            title: 'Parent session',
            created_at: '2026-06-25T09:00:00Z',
            audience_kind: audienceKind,
            operator_view: operatorView,
          }}
          wsConnected
          allSessions={[]}
          allSessionsLoading={false}
          allUserFilter=""
          onSetAllUserFilter={vi.fn()}
          onSelectSession={vi.fn()}
          onDeleteSession={vi.fn()}
          historyContainerRef={{ current: null }}
          onHistoryScroll={vi.fn()}
          // An operator view is read-only and hydrates from the history rail.
          historyMsgs={operatorView ? [delegationEvent as never] : []}
          historyMessagesSessionId={operatorView ? 'session-1' : null}
          showHistoryScrollBtn={false}
          onScrollHistoryToBottom={vi.fn()}
          chatContainerRef={{ current: null }}
          onChatScroll={vi.fn()}
          chatMessages={[delegationEvent as never]}
          chatMessagesSessionId="session-1"
          runtimeSummary={null}
          agentPermissions={{ scope_type: 'company', scope_ids: [], access_level: 'manage' }}
          transportNotice={null}
          isWaiting={false}
          activeRunStatus={null}
          chatEndRef={{ current: null }}
          showScrollBtn={false}
          onScrollToBottom={vi.fn()}
          agentExpired={false}
          attachedFiles={[]}
          onRemoveAttachedFile={vi.fn()}
          fileInputRef={{ current: null }}
          onHandleChatFile={vi.fn()}
          uploading={false}
          uploadProgress={-1}
          uploadAbortRef={{ current: null }}
          chatInputRef={{ current: null }}
          chatInput=""
          onSetChatInput={vi.fn()}
          onHandlePaste={vi.fn()}
          onSendChatMsg={vi.fn()}
          onResolveSessionPermission={vi.fn()}
          isStreaming={false}
          onAbortGeneration={vi.fn()}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  const block = container.querySelector('[data-testid="run-disclosure-block"]');
  expect(block).toBeTruthy();
  // The block folds a finished run; the step folds behind the block.
  fireEvent.click(block!.querySelector('.run-disclosure-header') as HTMLButtonElement);
  fireEvent.click(block!.querySelector('.run-step-toggle') as HTMLButtonElement);
  return container;
}

it('shows the colleague link in the viewer\'s own private session (X1-r3 ruling 1)', () => {
  const container = renderChat({ audienceKind: undefined });
  const link = container.querySelector(`a[href="/agents/agent-colleague/sessions/session-colleague"]`);
  expect(link).toBeTruthy();
  expect(link?.textContent).toContain('查看这位同事的对话');
});

it('shows no colleague link for a colleague outside the member\'s roster', () => {
  const container = renderChat({ audienceKind: undefined, roster: [{ id: 'agent-someone-else' }] });
  expect(container.querySelector('a[href^="/agents/agent-colleague"]')).toBeNull();
});

it('shows no colleague link in a group session', () => {
  // A group session's user_id cannot tell who requested this delegation.
  const container = renderChat({ audienceKind: 'group' });
  expect(container.querySelector('a[href^="/agents/agent-colleague"]')).toBeNull();
});

it('shows no colleague link in an operator view', () => {
  const container = renderChat({ audienceKind: undefined, operatorView: true });
  expect(container.querySelector('a[href^="/agents/agent-colleague"]')).toBeNull();
});

it('opens the approval card fold to the exact command JSON (X1-r3 ruling 4)', () => {
  // The baseline intent of AgentDetailSections' old 'Full parameters'
  // assertion, restored interactively here because that file stays in the
  // node environment for its source-reading tests: the plain-label fold
  // carries the complete arguments.
  window.localStorage.setItem('current_tenant_id', 'tenant-1');
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { container } = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AgentChatSection
          agent={{ id: 'agent-1', name: 'Release Bot' }}
          currentUser={{ id: 'user-1', tenant_id: 'tenant-1' }}
          isAdmin={false}
          onLoadAllSessions={vi.fn()}
          onCreateNewSession={vi.fn()}
          sessionsLoading={false}
          sessions={[]}
          activeSession={{ id: 'session-1', user_id: 'user-1', title: 'Permission request', created_at: '2026-06-25T09:00:00Z' }}
          wsConnected
          allSessions={[]}
          allSessionsLoading={false}
          allUserFilter=""
          onSetAllUserFilter={vi.fn()}
          onSelectSession={vi.fn()}
          onDeleteSession={vi.fn()}
          historyContainerRef={{ current: null }}
          onHistoryScroll={vi.fn()}
          historyMsgs={[]}
          historyMessagesSessionId={null}
          showHistoryScrollBtn={false}
          onScrollHistoryToBottom={vi.fn()}
          chatContainerRef={{ current: null }}
          onChatScroll={vi.fn()}
          chatMessages={[{
            role: 'event',
            content: "Tool 'run_command' requires session permission",
            eventType: 'permission',
            eventTitle: 'Permission Gate',
            eventStatus: 'session_permission_required',
            eventToolName: 'run_command',
            eventCapability: 'workspace.command.destructive_delete',
            sessionPermissionRequest: {
              permission_request_id: '11111111-1111-4111-8111-111111111111',
              session_id: 'session-1',
              tool_name: 'run_command',
              arguments: { command: 'rm workspace/report.md' },
              permission_mode: 'bypassPermissions',
              risk_class: 'destructive_delete',
              confirmation_kind: 'destructive_once',
              allow_session_allowed: false,
            },
          }] as never}
          chatMessagesSessionId="session-1"
          runtimeSummary={null}
          agentPermissions={{ scope_type: 'company', scope_ids: [], access_level: 'manage' }}
          transportNotice={null}
          isWaiting={false}
          activeRunStatus={null}
          chatEndRef={{ current: null }}
          showScrollBtn={false}
          onScrollToBottom={vi.fn()}
          agentExpired={false}
          attachedFiles={[]}
          onRemoveAttachedFile={vi.fn()}
          fileInputRef={{ current: null }}
          onHandleChatFile={vi.fn()}
          uploading={false}
          uploadProgress={-1}
          uploadAbortRef={{ current: null }}
          chatInputRef={{ current: null }}
          chatInput=""
          onSetChatInput={vi.fn()}
          onHandlePaste={vi.fn()}
          onSendChatMsg={vi.fn()}
          onResolveSessionPermission={vi.fn()}
          isStreaming={false}
          onAbortGeneration={vi.fn()}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByText('详情'));
  fireEvent.click(screen.getByRole('button', { name: '查看将要执行的完整内容' }));
  expect(container.querySelector('pre')?.textContent).toContain('"command": "rm workspace/report.md"');
});
