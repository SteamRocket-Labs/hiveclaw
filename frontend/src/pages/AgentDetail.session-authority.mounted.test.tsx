// @vitest-environment jsdom

import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { ApiError } from '../api/core';
import { ccParityApi } from '../api/domains/ccParity';
import ArtifactContinuation from './agent-detail/ArtifactContinuation';
import { StructuredToolResultBody } from './agent-detail/StructuredToolResult';
import zh from '../i18n/zh.json';

const mocks = vi.hoisted(() => ({
  queryCalls: [] as Array<{ key: unknown[]; enabled: unknown }>,
  listSessions: vi.fn(),
  getSessionTranscript: vi.fn(),
  getSessionMessages: vi.fn(),
  getSessionLineage: vi.fn(),
  navigate: vi.fn(),
  activityList: vi.fn(),
  toolFailures: vi.fn(),
  executedActivityQueries: new Set<string>(),
  sessionId: 'foreign-session' as string | undefined,
  search: '',
  hash: '',
  accessLevel: 'manage',
  agentType: 'cloud',
  operatorCap: false,
  agentError: null as unknown,
  userRole: 'platform_admin' as string,
  projectActiveTimeline: false,
  realChat: false,
  realTransport: false,
  startSessionRun: vi.fn(),
  createSessionRun: vi.fn(),
  cancelSessionRun: vi.fn(),
  deleteSession: vi.fn(),
  upload: vi.fn(),
  activeSessionRun: null as null | { run_id: string; status: string },
  activeRunObservedAt: 1,
  getActiveSessionRun: vi.fn(),
  activeRunQueryClient: null as QueryClient | null,
  chatProps: null as any,
  answerQuestion: vi.fn(),
  questionControl: false,
  transport: null as any,
  language: 'en',
  messageStore: null as ReturnType<typeof import('./agent-detail/sessionMessageStore').createSessionMessageStore> | null,
}));

vi.mock('./agent-detail/sessionMessageStore', async importOriginal => {
  const actual = await importOriginal<typeof import('./agent-detail/sessionMessageStore')>();
  return { ...actual, createSessionMessageStore: () => (mocks.messageStore = actual.createSessionMessageStore()) };
});

vi.mock('../api/core/upload-progress', () => ({ uploadFileWithProgress: (...args: unknown[]) => ({ promise: mocks.upload(...args) }) }));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string | Record<string, unknown>) => {
      const translated = mocks.language === 'zh' ? key.split('.').reduce<any>((value, part) => value?.[part], zh) : undefined;
      return typeof translated === 'string' ? translated : typeof fallback === 'string' ? fallback : key;
    },
    i18n: { language: mocks.language },
  }),
}));

vi.mock('@tanstack/react-query', async importOriginal => {
  const actual = await importOriginal<typeof import('@tanstack/react-query')>();
  return ({
  ...actual,
  useMutation: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useQuery: (options: { queryKey: unknown[]; queryFn?: () => unknown; enabled?: unknown }) => {
    if (mocks.activeRunQueryClient && options.queryKey[0] === 'chat-active-run') {
      return actual.useQuery(options as any);
    }
    mocks.queryCalls.push({ key: options.queryKey, enabled: options.enabled });
    if (options.enabled && options.queryKey[0] === 'activity') {
      const cacheKey = JSON.stringify(options.queryKey);
      if (!mocks.executedActivityQueries.has(cacheKey)) {
        mocks.executedActivityQueries.add(cacheKey);
        void options.queryFn?.();
      }
    }
    if (String(options.queryKey[0]) === 'agent') {
      return {
        data: {
          id: 'agent-1',
          name: 'EventPilot',
          status: 'running',
          agent_type: mocks.agentType,
          access_level: mocks.accessLevel,
          action_capabilities: {
            can_manage: mocks.accessLevel === 'manage',
            can_manage_permissions: mocks.accessLevel === 'manage',
            can_operator_inspect: mocks.operatorCap,
          },
        },
        isLoading: false,
        error: mocks.agentError,
      };
    }
    if (options.queryKey[0] === 'chat-active-run' && options.enabled) {
      return { data: mocks.activeSessionRun, dataUpdatedAt: mocks.activeRunObservedAt, isLoading: false, error: null };
    }
    return {
      data: undefined,
      dataUpdatedAt: 0,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    };
  },
  useQueryClient: () => mocks.activeRunQueryClient || ({
    invalidateQueries: vi.fn(),
    cancelQueries: vi.fn(),
    removeQueries: vi.fn(),
  }),
});
});

vi.mock('react-router-dom', () => ({
  useInRouterContext: () => false,
  Link: ({ to, children, ...props }: any) => <a href={to} {...props}>{children}</a>,
  useParams: () => ({ id: 'agent-1', sessionId: mocks.sessionId }),
  useLocation: () => ({
    hash: mocks.hash,
    search: mocks.search,
    pathname: mocks.sessionId
      ? `/agents/agent-1/sessions/${mocks.sessionId}`
      : '/agents/agent-1',
    state: null,
  }),
  useNavigate: () => mocks.navigate,
}));

vi.mock('../stores', () => {
  const state = {
    token: 'signed-in-token',
    get user() { return { id: 'platform-admin-1', role: mocks.userRole }; },
  };
  const useAuthStore = ((selector: (input: typeof state) => unknown) => selector(state)) as unknown as typeof import('../stores').useAuthStore;
  Object.assign(useAuthStore, { getState: () => state });
  return { useAuthStore };
});

vi.mock('../api/domains/chat', () => ({
  chatApi: {
    listSessions: mocks.listSessions,
    getSessionTranscript: mocks.getSessionTranscript,
    getSessionMessages: mocks.getSessionMessages,
    getSessionLineage: mocks.getSessionLineage,
    startSessionRun: mocks.startSessionRun,
    createSessionRun: mocks.createSessionRun,
    cancelSessionRun: mocks.cancelSessionRun,
    getActiveSessionRun: mocks.getActiveSessionRun,
    deleteSession: mocks.deleteSession,
    answerQuestion: mocks.answerQuestion,
  },
}));

vi.mock('../api/domains/activity', () => ({
  activityApi: {
    list: mocks.activityList,
    getToolFailureSummary: mocks.toolFailures,
  },
}));

vi.mock('./agent-detail/useSessionTransportController', async importOriginal => {
  const actual = await importOriginal<typeof import('./agent-detail/useSessionTransportController')>();
  return ({
  useSessionTransportController: (options: any) => {
    mocks.transport = options;
    if (mocks.realTransport) return actual.useSessionTransportController(options);
    return ({
    wsConnected: false,
    transportPhase: 'initializing',
    transportReconnectAttempt: 0,
    closeSessionSocket: vi.fn(),
    getSessionSocket: vi.fn(() => null),
    reconnectActiveTransport: vi.fn(),
    resetActiveTransportState: vi.fn(),
    syncActiveSocketState: vi.fn(),
    });
  },
});
});

vi.mock('../components/ConfirmModal', () => ({ default: ({ open, confirmLabel, message, onConfirm }: any) => open
  ? <div role="dialog">{message}<button onClick={onConfirm}>{confirmLabel}</button></div> : null }));
vi.mock('./agent-detail/AgentApprovalsSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentActivityLogSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentAwareSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentEvolutionSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentExtensionsSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentKnowledgeSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentBusinessTasksSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentSettingsSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentWorkspaceSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentWorkflowsSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentA2ASection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentStatusSection', () => ({ default: () => null }));
vi.mock('./LocalAgents', () => ({ default: () => null }));
vi.mock('./agent-detail/LocalAgentChatSection', () => ({
  default: () => <div data-testid="local-agent-chat">Local mutable chat</div>,
}));
vi.mock('./agent-detail/AgentChatSection', async (importOriginal) => {
  const { default: ActualChat } = await importOriginal<typeof import('./agent-detail/AgentChatSection')>();
  return ({
  default: (props: any) => {
    const { activeSession, chatMessages, historyMsgs, allSessions, chatMessagesSessionId, historyMessagesSessionId, onSendMessage,
    activeRunId, activeRunStatus, onAbortGeneration, ...composer } = props;
    mocks.chatProps = props;
    if (mocks.realChat) return <ActualChat {...props} />;
    return (
    <div data-testid="fabricated-session-shell">
      {mocks.questionControl && <StructuredToolResultBody toolName="ask_user_question" questionItemId="child-question"
        toolMeta={{ kind: 'user_clarification', blocking: true, nextAction: null, questions: [{ header: 'Audience',
          question: 'Which audience?', multiSelect: false, options: [{ label: 'Team', description: '' }] }] }}
        onAnswerQuestion={composer.onAnswerQuestion} />}
      {activeSession?.operator_view ? <strong>Operator View</strong> : activeSession?.read_only ? 'Read-only · User' : null}
      {(!mocks.projectActiveTimeline || (!activeSession?.read_only && chatMessagesSessionId === activeSession?.id) ? chatMessages || [] : [])
        .map((message: any, index: number) => <span key={`chat-${index}`}>{message.content}</span>)}
      {(!mocks.projectActiveTimeline || (activeSession?.read_only && historyMessagesSessionId === activeSession?.id) ? historyMsgs || [] : [])
        .map((message: any, index: number) => <span key={index}>{message.content}</span>)}
      {(allSessions || []).map((session: any) => <span key={session.id}>{session.title}</span>)}
    </div>
    );
  },
});
});

import AgentDetail from './AgentDetail';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

beforeAll(async () => {
  // Resolve the real lazy chat module before the first mounted waitFor starts.
  await import('./agent-detail/AgentChatSection');
}, 30_000);

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  vi.clearAllMocks();
  mocks.navigate.mockReset();
  mocks.queryCalls.length = 0;
  mocks.sessionId = 'foreign-session';
  mocks.search = '';
  mocks.hash = '';
  mocks.accessLevel = 'manage';
  mocks.agentType = 'cloud';
  mocks.operatorCap = false;
  mocks.agentError = null;
  mocks.userRole = 'platform_admin';
  mocks.projectActiveTimeline = false;
  mocks.realChat = false;
  mocks.realTransport = false;
  mocks.executedActivityQueries.clear();
  mocks.activityList.mockResolvedValue([]);
  mocks.toolFailures.mockResolvedValue(undefined);
  mocks.listSessions.mockResolvedValue([]);
  mocks.getSessionMessages.mockResolvedValue([]);
  mocks.getSessionLineage.mockResolvedValue([]);
  mocks.chatProps = null;
  mocks.startSessionRun.mockReset();
  mocks.createSessionRun.mockReset();
  mocks.cancelSessionRun.mockReset();
  mocks.deleteSession.mockReset();
  mocks.activeSessionRun = null;
  mocks.activeRunObservedAt = 1;
  mocks.activeRunQueryClient = null;
  mocks.getActiveSessionRun.mockImplementation(async () => mocks.activeSessionRun);
  mocks.questionControl = false;
  mocks.language = 'en';
  mocks.messageStore = null;
});

afterEach(() => { cleanup(); mocks.activeRunQueryClient?.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

it('reuses one input id for three failed retries on the child conversation answer control', async () => {
  mocks.sessionId = 'child-session';
  mocks.questionControl = true;
  mocks.listSessions.mockResolvedValue([{ id: 'child-session', agent_id: 'agent-1', user_id: 'platform-admin-1',
    title: 'First task', session_kind: 'delegation_run', read_only: true }]);
  mocks.getSessionTranscript.mockResolvedValue([]);
  mocks.answerQuestion.mockRejectedValue(new Error('Connection lost'));
  render(<AgentDetail />);
  await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('child-session'));
  fireEvent.click(await screen.findByRole('radio', { name: 'Team' }));
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    fireEvent.click(screen.getByRole('button', { name: 'Submit answer' }));
    await waitFor(() => expect(mocks.answerQuestion).toHaveBeenCalledTimes(attempt));
    await screen.findByText('Connection lost');
  }
  const ids = mocks.answerQuestion.mock.calls.map(call => call[4]);
  expect(ids[0]).toBeTruthy();
  expect(new Set(ids).size).toBe(1);
  expect(mocks.answerQuestion.mock.calls.every(call => call[0] === 'agent-1' && call[1] === 'child-session'
    && call[2] === 'child-question' && call[3] === 'Audience: Team')).toBe(true);
});

async function mountOwnedArtifactWork() {
  mocks.sessionId = 'owned-artifact-work';
  mocks.listSessions.mockResolvedValue([{ id: mocks.sessionId, agent_id: 'agent-1', user_id: 'platform-admin-1',
    title: 'Saved artifact work', session_kind: 'human_chat', read_only: false, operator_view: false }]);
  mocks.getSessionTranscript.mockResolvedValue([]);
  render(mocks.activeRunQueryClient
    ? <QueryClientProvider client={mocks.activeRunQueryClient}><AgentDetail /></QueryClientProvider>
    : <AgentDetail />);
  await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe(mocks.sessionId));
  return mocks.chatProps.onSendMessage as (text: string, options: { expectedSessionId: string; inputId: string; displayContent?: string }) => Promise<unknown>;
}

it('confirms member removal through the existing API and refreshes their conversation list', async () => {
  mocks.userRole = 'member';
  mocks.sessionId = 'owned-removal';
  let sessions = [{ id: mocks.sessionId, agent_id: 'agent-1', user_id: 'platform-admin-1',
    title: 'Member report', session_kind: 'human_chat', read_only: false, operator_view: false }];
  mocks.listSessions.mockImplementation(async (_agentId, scope) => scope === 'mine' ? sessions : []);
  mocks.getSessionTranscript.mockResolvedValue([]);
  mocks.deleteSession.mockImplementation(async () => { sessions = []; });
  mocks.navigate.mockImplementation(() => { mocks.sessionId = undefined; });
  render(<AgentDetail />);
  await waitFor(() => expect(mocks.chatProps?.sessions).toHaveLength(1));
  await act(async () => { await mocks.chatProps.onDeleteSession('owned-removal'); });
  expect(screen.getByRole('dialog').textContent).toContain('Its records are kept');
  expect(mocks.deleteSession).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Remove from list' }));
  await waitFor(() => expect(mocks.deleteSession).toHaveBeenCalledExactlyOnceWith('agent-1', 'owned-removal'));
  await waitFor(() => expect(mocks.chatProps.sessions.filter((session: any) => !session.is_draft)).toHaveLength(0));
  expect(mocks.navigate).toHaveBeenCalledWith('/agents/agent-1#chat', { replace: true });
  expect(mocks.chatProps.activeSession?.id).not.toBe('owned-removal');
});

it.each(['suspended', 'resumable'])('restores the exact %s run as the cancellation target after a reload', async (status) => {
  mocks.activeSessionRun = { run_id: 'paused-original-run', status };
  mocks.cancelSessionRun.mockResolvedValue({ accepted: true });
  await mountOwnedArtifactWork();
  await waitFor(() => expect(mocks.chatProps?.activeRunId).toBe('paused-original-run'));
  expect(mocks.chatProps.activeRunStatus).toBe(status);
  await act(async () => { await mocks.chatProps.onAbortGeneration(); });
  expect(mocks.cancelSessionRun).toHaveBeenCalledExactlyOnceWith('agent-1', 'owned-artifact-work', 'paused-original-run');
  expect(mocks.startSessionRun).not.toHaveBeenCalled();
  expect(mocks.createSessionRun).not.toHaveBeenCalled();
});

it('retries an artifact input in the exact original work with the same durable identity', async () => {
  const send = await mountOwnedArtifactWork();
  mocks.startSessionRun.mockRejectedValueOnce(new ApiError(503, 'Acknowledgement unavailable'))
    .mockResolvedValue({ schema: 'hive.human_input_receipt', schema_version: 2, status: 'accepted' });
  const options = { expectedSessionId: 'owned-artifact-work', inputId: globalThis.crypto.randomUUID(), displayContent: 'report.md\nRevise saved file',
    parts: [{ type: 'workspace_file_ref', agent_id: 'agent-1', path: 'workspace/report.md', human_saved_hash: 'a'.repeat(64) }] };
  await act(async () => { await expect(send('Selected saved file reference', options)).rejects.toThrow('Acknowledgement unavailable'); });
  expect(mocks.chatProps.chatMessages.at(-1)).toMatchObject({ eventStatus: 'input_outcome_unknown' });
  expect(mocks.chatProps.chatMessages.at(-1).threadItem).toMatchObject({ item_type: 'warning', item_status: 'waiting_user' });
  await act(async () => { await send('Selected saved file reference', options); });
  expect(mocks.startSessionRun).toHaveBeenCalledTimes(2);
  expect(mocks.startSessionRun.mock.calls[1]).toEqual(mocks.startSessionRun.mock.calls[0]);
  expect(mocks.startSessionRun.mock.calls[0].slice(0, 2)).toEqual(['agent-1', 'owned-artifact-work']);
  expect(mocks.startSessionRun.mock.calls[0][2]).toMatchObject({ input_id: options.inputId,
    idempotency_key: `session-input:${options.inputId}`, display_content: options.displayContent, parts: options.parts });
  expect(mocks.createSessionRun).not.toHaveBeenCalled();
});

it('refuses an artifact callback after its original work or account changes', async () => {
  const send = await mountOwnedArtifactWork();
  await expect(send('Private artifact follow-up', { expectedSessionId: 'different-work', inputId: globalThis.crypto.randomUUID() })).rejects.toThrow('original work');
  mocks.userRole = 'member';
  await expect(send('Private artifact follow-up', { expectedSessionId: 'owned-artifact-work', inputId: globalThis.crypto.randomUUID() })).rejects.toThrow('original work');
  expect(mocks.startSessionRun).not.toHaveBeenCalled();
  expect(mocks.createSessionRun).not.toHaveBeenCalled();
});

describe('AgentDetail direct-session authority presentation', () => {
  it.each([
    [403, 'Session access denied'],
    [404, 'Session not found'],
  ] as const)('keeps an unknown route resolving, then renders the %s terminal without a Session shell', async (status, title) => {
    const transcript = deferred<never[]>();
    mocks.getSessionTranscript.mockReturnValue(transcript.promise);

    const view = render(<AgentDetail />);
    await waitFor(() => expect(mocks.getSessionTranscript).toHaveBeenCalledTimes(1));

    const shellWhilePending = screen.queryByTestId('fabricated-session-shell');
    const resolvingWhilePending = screen.queryByText('Resolving session…');

    await act(async () => {
      transcript.reject(new ApiError(status, title));
    });

    const alert = await screen.findByRole('alert');
    expect(resolvingWhilePending).toBeTruthy();
    expect(shellWhilePending).toBeNull();
    expect(alert.textContent).toContain(title);
    expect(screen.queryByTestId('fabricated-session-shell')).toBeNull();
    expect(mocks.getSessionMessages).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Back to digital employees' }));
    expect(mocks.navigate).toHaveBeenCalledWith('/agents', { replace: true });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(mocks.getSessionTranscript).toHaveBeenCalledTimes(1);
    mocks.sessionId = undefined;
    view.rerender(<AgentDetail />);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('replaces the resolving state with the verified read-only Session after a successful lookup', async () => {
    mocks.getSessionTranscript.mockResolvedValue([]);

    render(<AgentDetail />);

    expect(await screen.findByTestId('fabricated-session-shell')).toBeTruthy();
    expect(screen.queryByText('Resolving session…')).toBeNull();
    expect(mocks.getSessionMessages).toHaveBeenCalledTimes(1);
  });

  it('purges operator Session text and banner when the reason changes or capability is lost', async () => {
    mocks.search = '?manage=true';
    mocks.accessLevel = 'operator';
    mocks.operatorCap = true;
    const reasonBSessions = deferred<any[]>();
    const operatorSession = {
      id: 'foreign-session',
      agent_id: 'agent-1',
      user_id: 'another-user',
      title: 'Private customer thread',
      source_channel: 'web',
      read_only: true,
      is_current_user_session: false,
      operator_view: true,
    };
    mocks.listSessions.mockImplementation((_agentId, scope, options) => {
      if (scope !== 'all') return Promise.resolve([]);
      if (options?.operatorReason === 'Reason B') return reasonBSessions.promise;
      return Promise.resolve([operatorSession]);
    });
    mocks.getSessionTranscript.mockResolvedValue([]);
    mocks.getSessionMessages.mockImplementation((_agentId, _sessionId, options) => Promise.resolve([
      { id: 'private-message', role: 'assistant', content: `PRIVATE:${options?.operatorReason}` },
    ]));

    const view = render(<AgentDetail />);
    fireEvent.change(screen.getByLabelText('Operator inspection reason'), { target: { value: 'Reason A' } });
    expect(mocks.listSessions.mock.calls.filter((call) => call[1] === 'all')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Begin inspection' }));
    expect(await screen.findByText('PRIVATE:Reason A')).toBeTruthy();
    expect(screen.getByText('Operator View')).toBeTruthy();
    expect(mocks.listSessions.mock.calls.filter((call) => call[1] === 'all')).toHaveLength(1);

    fireEvent.change(screen.getByLabelText('Operator inspection reason'), { target: { value: 'Reason B' } });
    expect(screen.getByText('PRIVATE:Reason A')).toBeTruthy();
    expect(mocks.listSessions.mock.calls.filter((call) => call[1] === 'all')).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Apply inspection reason' }));
    await waitFor(() => {
      expect(screen.queryByText('PRIVATE:Reason A')).toBeNull();
      expect(screen.queryByText('Operator View')).toBeNull();
    });
    expect(mocks.listSessions.mock.calls.filter((call) => call[1] === 'all')).toHaveLength(2);

    await act(async () => {
      reasonBSessions.resolve([operatorSession]);
    });
    expect(await screen.findByText('PRIVATE:Reason B')).toBeTruthy();
    expect(screen.getByText('Operator View')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'End inspection' }));
    await waitFor(() => {
      expect(screen.queryByText('PRIVATE:Reason B')).toBeNull();
      expect(screen.queryByText('Operator View')).toBeNull();
    });

    fireEvent.change(screen.getByLabelText('Operator inspection reason'), { target: { value: 'Reason B' } });
    fireEvent.click(screen.getByRole('button', { name: 'Begin inspection' }));
    expect(await screen.findByText('PRIVATE:Reason B')).toBeTruthy();

    mocks.operatorCap = false;
    view.rerender(<AgentDetail />);
    await waitFor(() => {
      expect(screen.queryByText('PRIVATE:Reason B')).toBeNull();
      expect(screen.queryByText('Operator View')).toBeNull();
      expect(screen.queryByTestId('agent-operator-reason')).toBeNull();
    });
  });

  it.each([403, 410] as const)(
    'withdraws a retained operator shell immediately when agent revalidation returns %s',
    async (status) => {
      mocks.sessionId = undefined;
      mocks.hash = '#chat';
      mocks.search = '?manage=true';
      mocks.accessLevel = 'operator';
      mocks.operatorCap = true;
      const operatorSession = {
        id: 'foreign-session',
        agent_id: 'agent-1',
        user_id: 'another-user',
        title: 'Private customer thread',
        source_channel: 'web',
        read_only: true,
        is_current_user_session: false,
        operator_view: true,
      };
      mocks.listSessions.mockImplementation((_agentId, scope) => (
        scope === 'all' ? Promise.resolve([operatorSession]) : Promise.resolve([])
      ));
      mocks.getSessionTranscript.mockResolvedValue([]);
      mocks.getSessionMessages.mockResolvedValue([
        { id: 'private-message', role: 'assistant', content: 'PRIVATE:CACHED' },
      ]);

      const view = render(<AgentDetail />);
      fireEvent.change(screen.getByLabelText('Operator inspection reason'), {
        target: { value: 'Incident review' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Begin inspection' }));
      expect(await screen.findByText('Private customer thread')).toBeTruthy();
      expect(screen.getByText('EventPilot')).toBeTruthy();
      expect(screen.getByTestId('fabricated-session-shell')).toBeTruthy();
      expect(screen.getByTestId('agent-workbench-nav')).toBeTruthy();

      mocks.agentError = new ApiError(status, status === 403 ? 'Forbidden' : 'Expired');
      view.rerender(<AgentDetail />);

      const denial = await screen.findByRole('alert');
      expect(denial.textContent).toContain('You do not have access to this employee.');
      expect(screen.queryByText('Private customer thread')).toBeNull();
      expect(screen.queryByText('EventPilot')).toBeNull();
      expect(screen.queryByTestId('fabricated-session-shell')).toBeNull();
      expect(screen.queryByTestId('agent-workbench-nav')).toBeNull();
      expect(screen.queryByTestId('agent-operator-reason')).toBeNull();
    },
  );

  it('does not issue owner activity reads before a reason and auto-starts operator reads on Apply', async () => {
    mocks.sessionId = undefined;
    mocks.hash = '#activityLog';
    mocks.search = '?manage=true';
    mocks.accessLevel = 'operator';
    mocks.operatorCap = true;

    render(<AgentDetail />);

    expect(mocks.activityList).not.toHaveBeenCalled();
    expect(mocks.toolFailures).not.toHaveBeenCalled();
    expect(screen.getByText('Enter and apply an inspection reason before viewing private activity.')).toBeTruthy();

    fireEvent.change(screen.getByLabelText('Operator inspection reason'), {
      target: { value: 'Incident review' },
    });
    expect(mocks.activityList).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Begin inspection' }));

    await waitFor(() => expect(mocks.activityList).toHaveBeenCalledTimes(1));
    expect(mocks.activityList).toHaveBeenCalledWith(
      'agent-1',
      100,
      { operatorView: true, reason: 'Incident review' },
    );
    expect(mocks.toolFailures).toHaveBeenCalledWith(
      'agent-1',
      24,
      200,
      { operatorView: true, reason: 'Incident review' },
    );
  });

  it('keeps an operator-only local agent off the local mutable transport and enters unified audited reads after Apply', async () => {
    mocks.sessionId = undefined;
    mocks.hash = '#chat';
    mocks.search = '?manage=true';
    mocks.accessLevel = 'operator';
    mocks.agentType = 'local_agent';
    mocks.operatorCap = true;
    mocks.listSessions.mockResolvedValue([]);

    render(<AgentDetail />);

    expect(screen.getByTestId('fabricated-session-shell')).toBeTruthy();
    expect(screen.queryByTestId('local-agent-chat')).toBeNull();
    expect(mocks.listSessions).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText('Operator inspection reason'), {
      target: { value: 'Local incident review' },
    });
    expect(mocks.listSessions).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Begin inspection' }));

    await waitFor(() => expect(mocks.listSessions).toHaveBeenCalledTimes(1));
    expect(mocks.listSessions).toHaveBeenCalledWith(
      'agent-1',
      'all',
      { operatorView: true, operatorReason: 'Local incident review' },
    );
    expect(screen.queryByTestId('local-agent-chat')).toBeNull();
    expect(screen.getByTestId('fabricated-session-shell')).toBeTruthy();
  });

  it('withdraws the whole operator shell when an operator-scoped session read returns 403', async () => {
    mocks.sessionId = undefined;
    mocks.hash = '#chat';
    mocks.search = '?manage=true';
    mocks.accessLevel = 'operator';
    mocks.operatorCap = true;
    mocks.listSessions.mockRejectedValue(new ApiError(403, 'Inspection grant revoked'));

    render(<AgentDetail />);
    fireEvent.change(screen.getByLabelText('Operator inspection reason'), {
      target: { value: 'Incident review' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Begin inspection' }));

    const denial = await screen.findByRole('alert');
    expect(denial.textContent).toContain('You do not have access to this employee.');
    expect(screen.queryByTestId('fabricated-session-shell')).toBeNull();
    expect(screen.queryByTestId('agent-operator-reason')).toBeNull();
    expect(screen.queryByTestId('agent-workbench-nav')).toBeNull();
  });

  it('opens a managed employee session for a scoped administrator without an operator reason', async () => {
    // Privacy cases expose all buffers; this presentation case follows the real active timeline.
    mocks.projectActiveTimeline = true;
    // PDEC-013: platform_admin + server manage projection = normal audited
    // business lane (authority_source=scoped_business_admin, operator_view=false).
    // The requested Session id arrives by direct URL / reload.
    mocks.sessionId = 'foreign-session';
    mocks.hash = '#chat';
    const managedSession = {
      id: 'foreign-session',
      agent_id: 'agent-1',
      user_id: 'employee-9',
      title: 'Employee private thread',
      source_channel: 'web',
      read_only: false,
      is_current_user_session: false,
      authority_source: 'scoped_business_admin',
      operator_view: false,
    };
    mocks.listSessions.mockImplementation((_agentId, scope) => (
      scope === 'all' ? Promise.resolve([managedSession]) : Promise.resolve([])
    ));
    mocks.getSessionTranscript.mockResolvedValue([]);
    mocks.getSessionMessages.mockResolvedValue([
      { id: 'employee-message', role: 'user', content: 'EMPLOYEE-AUTHORED-TEXT' },
    ]);

    render(<AgentDetail />);

    // The managed inventory loads through the scoped business lane with no
    // fabricated operator authority or reason on any call.
    await waitFor(() => {
      expect(mocks.listSessions.mock.calls.filter((call) => call[1] === 'all').length).toBeGreaterThan(0);
    });
    const allScopeCalls = mocks.listSessions.mock.calls.filter((call) => call[1] === 'all');
    expect(allScopeCalls.every((call) => call[2] === undefined)).toBe(true);
    expect(screen.queryByTestId('agent-operator-reason')).toBeNull();

    // The employee session opens as a writable business view: no
    // operator params on any read, no Operator View banner, the real sender's
    // content is shown.
    await waitFor(() => {
      expect(screen.getByText('EMPLOYEE-AUTHORED-TEXT')).toBeTruthy();
      expect(screen.queryByText('Read-only · User')).toBeNull();
    });
    expect(screen.queryByText('Operator View')).toBeNull();
    const transcriptCalls = mocks.getSessionTranscript.mock.calls;
    expect(transcriptCalls.length).toBeGreaterThan(0);
    expect(transcriptCalls.every((call) => !call[2]?.operatorView && !call[2]?.operatorReason)).toBe(true);
    expect(mocks.getSessionMessages.mock.calls.every((call) => !call[2]?.operatorView && !call[2]?.operatorReason)).toBe(true);
  });

  it('keeps a member-owner off the all-users inventory lane entirely', async () => {
    mocks.sessionId = undefined;
    mocks.hash = '#chat';
    mocks.userRole = 'member';

    render(<AgentDetail />);

    await waitFor(() => expect(mocks.listSessions).toHaveBeenCalledWith('agent-1', 'mine'));
    expect(mocks.listSessions.mock.calls.filter((call) => call[1] === 'all')).toHaveLength(0);
    expect(screen.queryByTestId('agent-operator-reason')).toBeNull();
  });
});

async function composeOrdinaryMessage(text = 'Keep this message') {
  await mountOwnedArtifactWork();
  act(() => mocks.chatProps.onSetChatInput(text));
}

function receiveLiveMessage(data: any) {
  mocks.transport.callbacks.onMessage({ data, session: mocks.chatProps.activeSession,
    agentId: 'agent-1', sessionId: mocks.sessionId, key: `agent-1:${mocks.sessionId}`, isActiveRuntime: true,
    closeSessionSocket: vi.fn(), failAuthentication: vi.fn() });
}

function activeRunEvent(sequence: number, kind: string, lifecycle: string, runId: string | null,
  payload: Record<string, unknown> = {}, itemId = `${kind}-${sequence}`) {
  const sessionId = 'owned-artifact-work';
  const round = kind.startsWith('assistant_');
  const timestamp = new Date(Date.UTC(2026, 10, 10, 0, 0, sequence)).toISOString();
  return {
    schema: 'hive.session_event', schema_version: 2, event_id: `active-run-event-${sequence}`, sequence,
    tenant_id: 'tenant-1', item_id: kind === 'run' ? runId : itemId, item_kind: kind,
    kind: `${kind}.${lifecycle}`, lifecycle, payload_schema: `hive.session.payload.${kind}.${lifecycle}.v2`,
    scope: runId ? { level: round ? 'round' : 'run', session_id: sessionId, thread_id: sessionId,
      turn_id: `turn-${runId}`, run_id: runId, ...(round ? { round_id: `round-${runId}` } : {}) }
      : { level: 'session', session_id: sessionId, thread_id: sessionId },
    actor: { type: 'runtime' }, visibility: { audience: 'direct_user' }, payload,
    occurred_at: timestamp, persisted_at: timestamp,
  };
}

function completedRunEvents(sequence: number, runId: string) {
  return [
    activeRunEvent(sequence, 'human_input', 'accepted', null,
      { intent: 'start_turn', content_parts: [{ type: 'text', text: `Request ${runId}` }] }),
    activeRunEvent(sequence + 1, 'run', 'running', runId),
    activeRunEvent(sequence + 2, 'assistant_commentary', 'completed', runId,
      { phase: 'commentary', content: `Preparing ${runId}.` }),
    activeRunEvent(sequence + 3, 'assistant_final', 'completed', runId, { content: `Result ${runId}.` }),
    activeRunEvent(sequence + 4, 'run', 'completed', runId),
  ];
}

async function receiveRunEvents(events: ReturnType<typeof activeRunEvent>[], transcript: unknown = events) {
  mocks.getSessionTranscript.mockResolvedValue(transcript);
  await act(async () => { events.forEach(receiveLiveMessage); });
  await waitFor(() => expect(mocks.chatProps.chatMessages.some((message: any) =>
    message.sessionItem?.last_sequence === events.at(-1)?.sequence)).toBe(true));
}

function expectMemberRunState(state: string) {
  if (screen.getByTestId('session-runtime-panel').classList.contains('is-collapsed')) {
    fireEvent.click(screen.getByTestId('session-runtime-collapse-toggle'));
  }
  expect(screen.getByTestId('session-workbench-header').querySelector('.swb-header-status')?.textContent).toBe(state);
  expect(screen.getByTestId('session-runtime-run-status').querySelector('.session-runtime-status')?.textContent).toBe(state);
}

function disclosureStates() {
  return screen.queryAllByTestId('run-disclosure-block').map(block => block.getAttribute('data-status'));
}

it('R13a adopts a platform continuation in one existing active-run poll and stops that exact run', async () => {
  // Fake only intervals so the 3 s poll is deterministic and async UI waits keep their real timers.
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
  mocks.realChat = true;
  mocks.language = 'zh';
  mocks.userRole = 'member';
  mocks.activeRunQueryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  mocks.activeSessionRun = { run_id: 'earlier-run', status: 'running' };
  mocks.cancelSessionRun.mockResolvedValue({ accepted: true });
  await mountOwnedArtifactWork();
  await waitFor(() => expect(mocks.chatProps.activeRunId).toBe('earlier-run'));
  await receiveRunEvents(completedRunEvents(1, 'earlier-run'));
  expectMemberRunState('完成');
  expect(screen.queryByTestId('session-composer-stop')).toBeNull();
  const reads = mocks.getActiveSessionRun.mock.calls.length;
  mocks.activeSessionRun = { run_id: 'platform-continuation', status: 'running' };
  // No send, refetch, rerender, navigation or reload: the existing 3 s query owns discovery.
  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  const stop = await screen.findByTestId('session-composer-stop');
  expect(mocks.getActiveSessionRun).toHaveBeenCalledTimes(reads + 1);
  expectMemberRunState('运行中');
  expect(disclosureStates()).toEqual(['done', 'running']);
  fireEvent.click(stop);
  await waitFor(() => expect(mocks.cancelSessionRun).toHaveBeenCalledExactlyOnceWith(
    'agent-1', 'owned-artifact-work', 'platform-continuation'));
  expect(mocks.startSessionRun).not.toHaveBeenCalled();
  expect(mocks.createSessionRun).not.toHaveBeenCalled();
});

it('R13a adopts a platform continuation from run.running after an empty active-run read stops polling', async () => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
  mocks.realChat = true;
  mocks.language = 'zh';
  mocks.userRole = 'member';
  mocks.activeRunQueryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  mocks.activeSessionRun = { run_id: 'earlier-run', status: 'running' };
  mocks.cancelSessionRun.mockResolvedValue({ accepted: true });
  await mountOwnedArtifactWork();
  await waitFor(() => expect(mocks.chatProps.activeRunId).toBe('earlier-run'));
  const history = completedRunEvents(1, 'earlier-run');
  const readsBeforeTerminal = mocks.getActiveSessionRun.mock.calls.length;
  mocks.activeSessionRun = null;
  await receiveRunEvents(history);
  await waitFor(() => expect(mocks.activeRunQueryClient!.getQueryState(
    ['chat-active-run', 'agent-1', 'owned-artifact-work', 'owner'])).toMatchObject({ data: null, fetchStatus: 'idle' }));
  expect(mocks.getActiveSessionRun.mock.calls.length).toBeGreaterThan(readsBeforeTerminal);
  expectMemberRunState('完成');
  expect(screen.queryByTestId('session-composer-stop')).toBeNull();
  const reads = mocks.getActiveSessionRun.mock.calls.length;
  await act(async () => { await vi.advanceTimersByTimeAsync(6_000); });
  expect(mocks.getActiveSessionRun).toHaveBeenCalledTimes(reads);
  mocks.activeSessionRun = { run_id: 'platform-continuation', status: 'running' };
  const running = activeRunEvent(6, 'run', 'running', 'platform-continuation');
  // The real event projector invalidates the stopped query; no manual refetch or page change.
  mocks.getSessionTranscript.mockResolvedValue([...history, running]);
  await act(async () => { receiveLiveMessage(running); });
  await waitFor(() => expect(mocks.chatProps.activeRunId).toBe('platform-continuation'));
  expect(mocks.getActiveSessionRun).toHaveBeenCalledTimes(reads + 1);
  expectMemberRunState('运行中');
  expect(disclosureStates()).toEqual(['done', 'running']);
  fireEvent.click(screen.getByTestId('session-composer-stop'));
  await waitFor(() => expect(mocks.cancelSessionRun).toHaveBeenCalledExactlyOnceWith(
    'agent-1', 'owned-artifact-work', 'platform-continuation'));
  expect(mocks.startSessionRun).not.toHaveBeenCalled();
  expect(mocks.createSessionRun).not.toHaveBeenCalled();
});

it.each(['deferred', 'successor'])('R13a keeps finished blocks finished and a %s queued turn visible for 88 seconds', async kind => {
  mocks.realChat = true;
  mocks.language = 'zh';
  mocks.userRole = 'member';
  mocks.activeSessionRun = { run_id: 'second-run', status: 'running' };
  await mountOwnedArtifactWork();
  await waitFor(() => expect(mocks.chatProps.activeRunId).toBe('second-run'));
  const history = [...completedRunEvents(1, 'first-run'), ...completedRunEvents(6, 'second-run')];
  const terminalRead = deferred<typeof history>();
  await receiveRunEvents(history, terminalRead.promise);
  expect(disclosureStates()).toEqual(['done', 'done']);
  const request = deferred<any>();
  mocks.startSessionRun.mockReturnValue(request.promise);
  fireEvent.change(screen.getByTestId('session-composer-input'), { target: { value: 'Continue the report.' } });
  fireEvent.click(screen.getByTestId('session-composer-send'));
  await waitFor(() => expect(mocks.startSessionRun).toHaveBeenCalledTimes(1));
  expect.soft(disclosureStates()).toEqual(['done', 'done', 'running']);
  await act(async () => request.resolve({ schema: 'hive.human_input_receipt', schema_version: 2,
    intent: kind === 'successor' ? 'queue_next_turn' : 'start_turn', status: 'accepted', admission_state: 'admitted',
    run: null, dispatch: { kind, code: kind === 'deferred' ? 'terminal_boundary_ack_pending' : undefined } }));
  await waitFor(() => expect(screen.getByTestId('message-send-status').textContent).toBe('排队中'));
  expect.soft(disclosureStates()).toEqual(['done', 'done', 'running']);
  const inputId = mocks.startSessionRun.mock.calls[0][2].input_id;
  history.push(activeRunEvent(11, 'human_input', 'accepted', null,
    { intent: 'start_turn', content_parts: [{ type: 'text', text: 'Continue the report.' }] }, inputId));
  await receiveRunEvents(history, terminalRead.promise);
  expect.soft(disclosureStates()).toEqual(['done', 'done', 'running']);
  const now = Date.now();
  vi.spyOn(Date, 'now').mockReturnValue(now + 88_000);
  mocks.activeSessionRun = null;
  mocks.activeRunObservedAt += 1;
  await act(async () => { mocks.chatProps.onSetChatInput('Next draft'); });
  expect.soft(disclosureStates()).toEqual(['done', 'done', 'running']);
  expectMemberRunState('运行中');
  expect(screen.getByTestId('message-send-status').textContent).toBe('排队中');
  history.push(activeRunEvent(12, 'run', 'queued', 'queued-run', { input_id: inputId }));
  await receiveRunEvents(history, terminalRead.promise);
  mocks.activeSessionRun = { run_id: 'queued-run', status: 'pending' };
  mocks.activeRunObservedAt += 1;
  await act(async () => { mocks.chatProps.onSetChatInput('Preserve this draft'); });
  expect(disclosureStates()).toEqual(['done', 'done', 'running']);
  expect(screen.queryByTestId('message-send-status')).toBeNull();
  await act(async () => terminalRead.resolve(history));
});

it('R13a clears the old stop notice and state before the next run POST settles', async () => {
  mocks.realChat = true;
  mocks.language = 'zh';
  mocks.userRole = 'member';
  mocks.activeSessionRun = { run_id: 'stopped-run', status: 'running' };
  mocks.cancelSessionRun.mockResolvedValue({ accepted: true });
  await mountOwnedArtifactWork();
  await screen.findByTestId('session-composer-stop');
  fireEvent.click(screen.getByTestId('session-composer-stop'));
  await screen.findByText(zh.agent.chat.stopRecorded);
  const history = completedRunEvents(1, 'stopped-run').slice(0, 3);
  history.push(activeRunEvent(4, 'run', 'cancelled', 'stopped-run'));
  await receiveRunEvents(history);
  expectMemberRunState('已停止');
  expect(screen.queryByText(zh.agent.chat.stopRecorded)).toBeNull();
  const now = Date.now();
  vi.spyOn(Date, 'now').mockReturnValue(now + 87_000);
  mocks.activeSessionRun = null;
  mocks.activeRunObservedAt += 1;
  const request = deferred<any>();
  mocks.startSessionRun.mockReturnValue(request.promise);
  fireEvent.change(screen.getByTestId('session-composer-input'), { target: { value: 'Start a fresh report.' } });
  fireEvent.click(screen.getByTestId('session-composer-send'));
  await waitFor(() => expect(mocks.startSessionRun).toHaveBeenCalledTimes(1));
  expect.soft(screen.queryByText(zh.agent.chat.stopRecorded)).toBeNull();
  expectMemberRunState('运行中');
  expect(disclosureStates()).toEqual(['cancelled', 'running']);
  await act(async () => request.resolve({ run_id: 'next-run', status: 'running' }));
  await waitFor(() => expect(mocks.chatProps.activeRunId).toBe('next-run'));
  expectMemberRunState('运行中');
  expect(disclosureStates()).toEqual(['cancelled', 'running']);
  expect(screen.queryByText(zh.agent.chat.stopRecorded)).toBeNull();
});

it('R13a keeps a queued resend after stop running when its deferred receipt has no run id', async () => {
  mocks.realChat = true;
  mocks.language = 'zh';
  mocks.userRole = 'member';
  mocks.activeSessionRun = { run_id: 'stopped-run', status: 'running' };
  mocks.cancelSessionRun.mockResolvedValue({ accepted: true });
  await mountOwnedArtifactWork();
  fireEvent.click(await screen.findByTestId('session-composer-stop'));
  await screen.findByText(zh.agent.chat.stopRecorded);
  const history = completedRunEvents(1, 'stopped-run').slice(0, 3);
  history.push(activeRunEvent(4, 'run', 'cancelled', 'stopped-run'));
  await receiveRunEvents(history);
  expectMemberRunState('已停止');
  vi.spyOn(Date, 'now').mockReturnValue(Date.now() + 87_000);
  mocks.activeSessionRun = null;
  mocks.activeRunObservedAt += 1;
  const request = deferred<any>();
  mocks.startSessionRun.mockReturnValue(request.promise);
  fireEvent.change(screen.getByTestId('session-composer-input'), { target: { value: 'Start a fresh queued report.' } });
  fireEvent.click(screen.getByTestId('session-composer-send'));
  await waitFor(() => expect(mocks.startSessionRun).toHaveBeenCalledTimes(1));
  expectMemberRunState('运行中');
  expect(disclosureStates()).toEqual(['cancelled', 'running']);
  await act(async () => request.resolve({ schema: 'hive.human_input_receipt', schema_version: 2,
    intent: 'start_turn', status: 'accepted', admission_state: 'admitted', run: null,
    dispatch: { kind: 'deferred', code: 'terminal_boundary_ack_pending' } }));
  await waitFor(() => expect(screen.getByTestId('message-send-status').textContent).toBe('排队中'));
  expect(mocks.chatProps.activeRunId).toBeNull();
  expect.soft(disclosureStates()).toEqual(['cancelled', 'running']);
  expectMemberRunState('运行中');
  expect(screen.queryByText(zh.agent.chat.stopRecorded)).toBeNull();
});

it.each(['accepted', 'failed'])('R13a preserves an accepted stop notice during a steer POST that is %s', async outcome => {
  mocks.realChat = true;
  mocks.language = 'zh';
  mocks.userRole = 'member';
  mocks.activeSessionRun = { run_id: 'stopping-run', status: 'running' };
  mocks.cancelSessionRun.mockResolvedValue({ accepted: true });
  await mountOwnedArtifactWork();
  fireEvent.click(await screen.findByTestId('session-composer-stop'));
  await screen.findByText(zh.agent.chat.stopRecorded);
  const request = deferred<any>();
  mocks.startSessionRun.mockReturnValue(request.promise);
  fireEvent.change(screen.getByTestId('session-composer-input'), { target: { value: 'Keep the current findings.' } });
  fireEvent.click(screen.getByTestId('session-composer-send'));
  await waitFor(() => expect(mocks.startSessionRun).toHaveBeenCalledTimes(1));
  expect(mocks.chatProps.activeRunId).toBe('stopping-run');
  expect.soft(screen.queryByText(zh.agent.chat.stopRecorded)).not.toBeNull();
  await act(async () => {
    if (outcome === 'accepted') request.resolve({ run_id: 'stopping-run', status: 'running' });
    else request.reject(new Error('Connection lost'));
  });
  if (outcome === 'accepted') expect(screen.queryByText(zh.agent.chat.stopRecorded)).toBeNull();
  else {
    await screen.findByText(zh.agent.chat.sendFailed);
    expect(screen.queryByText(zh.agent.chat.stopRecorded)).not.toBeNull();
  }
  expect(mocks.chatProps.activeRunId).toBe('stopping-run');
  expect(mocks.cancelSessionRun).toHaveBeenCalledExactlyOnceWith('agent-1', 'owned-artifact-work', 'stopping-run');
});

it.each([
  ['terminal-first', 'stream'], ['reply-first', 'stream'], ['terminal-first', 'rest'], ['reply-first', 'rest'],
] as const)('reconciles the final reply and file after an older read across %s %s channels', async (order, channel) => {
  mocks.userRole = 'member';
  mocks.activeSessionRun = { run_id: 'run-1', status: 'running' };
  await mountOwnedArtifactWork();
  const scope = { level: 'round', session_id: mocks.sessionId, thread_id: mocks.sessionId,
    turn_id: 'turn-1', run_id: 'run-1', round_id: 'round-1' };
  const envelope = (sequence: number, item: string, kind: string, payload: Record<string, unknown>) => ({
    schema: 'hive.session_event', schema_version: 2, event_id: `event-${sequence}`, tenant_id: 'tenant-1',
    sequence, item_id: item, item_kind: kind, kind: `${kind}.completed`, lifecycle: 'completed',
    payload_schema: `hive.session.payload.${kind}.completed.v2`, scope, actor: { type: 'runtime' },
    visibility: { audience: 'direct_user' }, payload,
    occurred_at: '2026-11-08T00:00:01Z', persisted_at: '2026-11-08T00:00:01Z',
  });
  const text = envelope(1, 'text', 'assistant_text', { content: 'The saved continuation is ready.' });
  const final = envelope(2, 'final', 'assistant_final', { zero_copy: true,
    source_blocks: [{ item_id: 'text', block_index: 0, content_hash: 'hash' }],
    parts: [{ type: 'artifact', artifact_id: 'new-report', name: 'report.md', path: 'workspace/report.md' }] });
  const olderRead = deferred<any[]>();
  const initialReads = mocks.getSessionTranscript.mock.calls.length;
  mocks.getSessionTranscript.mockReturnValueOnce(olderRead.promise).mockResolvedValue([text, final]);
  let read!: unknown;
  act(() => { read = mocks.transport.callbacks.onBackfill(mocks.chatProps.activeSession, 'agent-1'); });
  await waitFor(() => expect(mocks.getSessionTranscript.mock.calls.length).toBe(initialReads + 1));
  if (order === 'reply-first') await act(async () => { receiveLiveMessage(text); receiveLiveMessage(final); });
  if (channel === 'stream') {
    await act(async () => { receiveLiveMessage({ type: 'done', run_id: 'run-1', content: '' }); });
  } else {
    mocks.activeSessionRun = null;
    act(() => mocks.chatProps.onSetChatInput('Trigger the authoritative active-run read'));
  }
  await waitFor(() => expect(mocks.chatProps.terminalTranscriptPending).toBe(true));
  await act(async () => { olderRead.resolve([]); await read; });
  await screen.findByText('The saved continuation is ready.');
  await waitFor(() => expect(mocks.chatProps.chatMessages.find((message: any) => message.sessionItem?.kind === 'assistant_final')?.artifacts)
    .toEqual(expect.arrayContaining([expect.objectContaining({ id: 'new-report', path: 'workspace/report.md' })])));
  expect(mocks.getSessionTranscript.mock.calls.length).toBe(initialReads + 2);
  await waitFor(() => expect(mocks.chatProps.terminalTranscriptPending).toBe(false));
});

it.each(['stream', 'rest'] as const)('releases terminal delivery after a fresh %s read with no reply', async channel => {
  mocks.userRole = 'member';
  mocks.activeSessionRun = { run_id: 'run-1', status: 'running' };
  await mountOwnedArtifactWork();
  await waitFor(() => expect(mocks.chatProps.terminalTranscriptPending).toBe(true));
  const fresh = deferred<any[]>();
  mocks.getSessionTranscript.mockReturnValueOnce(fresh.promise);
  if (channel === 'stream') {
    await act(async () => { receiveLiveMessage({ type: 'done', run_id: 'run-1', content: '' }); });
  } else {
    mocks.activeSessionRun = null;
    act(() => mocks.chatProps.onSetChatInput('Observe terminal'));
  }
  await waitFor(() => expect(mocks.chatProps.terminalTranscriptPending).toBe(true));
  await act(async () => { fresh.resolve([]); });
  await waitFor(() => expect(mocks.chatProps.terminalTranscriptPending).toBe(false));
});

it('replaces the optimistic pasted text from the live server input before the send response', async () => {
  const request = deferred<any>();
  mocks.startSessionRun.mockReturnValue(request.promise);
  await composeOrdinaryMessage('Use sk-proj-abcdefghijklmnopqrstuvwxyz1234567890ABCD');
  let sending!: Promise<unknown>;
  act(() => { sending = mocks.chatProps.onSendChatMsg(); });
  const inputId = mocks.startSessionRun.mock.calls[0][2].input_id;
  await act(async () => { receiveLiveMessage({ schema: 'hive.session_event', schema_version: 2,
    event_id: 'paste-live', tenant_id: 'tenant-1', sequence: 1, item_id: inputId, item_kind: 'human_input',
    kind: 'human_input.accepted', lifecycle: 'accepted', payload_schema: 'hive.session.payload.human_input.accepted.v2',
    scope: { level: 'session', session_id: mocks.sessionId, thread_id: mocks.sessionId },
    actor: { type: 'user', id: 'platform-admin-1' }, visibility: { audience: 'direct_user' },
    payload: { content_parts: [{ type: 'text', text: 'Use [REDACTED_SECRET]', metadata: { pasted_credential_notice: true } }] },
    occurred_at: '2026-11-05T00:00:00Z', persisted_at: '2026-11-05T00:00:00Z' }); });
  await waitFor(() => expect(mocks.chatProps.chatMessages.filter((m: any) => m.role === 'user')).toEqual([
    expect.objectContaining({ id: inputId, content: 'Use [REDACTED_SECRET]', pastedCredentialNotice: true }),
  ]));
  await act(async () => { request.resolve({ run_id: 'accepted-run', status: 'pending' }); await sending; });
  expect(mocks.chatProps.chatMessages[0].content).toBe('Use [REDACTED_SECRET]');
});

it('replaces the optimistic /goal command with the existing live goal projection before the response', async () => {
  const request = deferred<any>();
  vi.spyOn(ccParityApi, 'executeCommand').mockReturnValue(request.promise);
  await composeOrdinaryMessage('/goal Use sk-proj-abcdefghijklmnopqrstuvwxyz1234567890ABCD');
  let sending!: Promise<unknown>;
  await act(async () => { sending = mocks.chatProps.onSendChatMsg(); });
  expect(mocks.chatProps.chatMessages.some((m: any) => m.content.startsWith('/goal'))).toBe(true);
  await act(async () => { receiveLiveMessage({ event_id: 'goal-live', id: 'goal-live', sequence: 1,
    event_type: 'goal', role: 'system', content: 'Goal started: Use [REDACTED_SECRET]',
    metadata: { goal_id: 'goal-1', command: 'goal_start', objective: 'Use [REDACTED_SECRET]', pasted_credential_notice: true } }); });
  await waitFor(() => expect(mocks.chatProps.chatMessages.some((m: any) => m.content.includes('sk-proj-'))).toBe(false));
  expect(mocks.chatProps.chatMessages).toContainEqual(expect.objectContaining({
    eventType: 'goal', eventGoalId: 'goal-1', pastedCredentialNotice: true,
  }));
  await act(async () => { request.resolve({ ok: true, command: 'goal_start', result: { id: 'goal-1', status: 'active' } }); await sending; });
  expect(mocks.chatProps.chatMessages.some((m: any) => m.content.includes('sk-proj-'))).toBe(false);
});

it.each([
  [false, true], [false, false], [true, true], [true, false],
])('reconciles a goal-mode paste by receipt input identity, replay: %s, live before HTTP: %s', async (replayed, liveBeforeResponse) => {
  const request = deferred<any>();
  vi.spyOn(ccParityApi, 'startGoal').mockReturnValue(request.promise);
  await composeOrdinaryMessage('Use sk-proj-abcdefghijklmnopqrstuvwxyz1234567890ABCD');
  act(() => mocks.chatProps.onToggleGoalMode());
  let sending!: Promise<unknown>;
  act(() => { sending = mocks.chatProps.onSendChatMsg(); });
  await waitFor(() => expect(ccParityApi.startGoal).toHaveBeenCalledTimes(1));
  const receive = () => receiveLiveMessage({ schema: 'hive.session_event', schema_version: 2,
    event_id: 'goal-mode-live', tenant_id: 'tenant-1', sequence: 1, item_id: 'goal-human-input', item_kind: 'human_input',
    kind: 'human_input.accepted', lifecycle: 'accepted', payload_schema: 'hive.session.payload.human_input.accepted.v2',
    scope: { level: 'session', session_id: mocks.sessionId, thread_id: mocks.sessionId },
    actor: { type: 'user', id: 'platform-admin-1' }, visibility: { audience: 'direct_user' },
    payload: { content_parts: [{ type: 'text', text: 'Use [REDACTED_SECRET]', metadata: { pasted_credential_notice: true } }] },
    occurred_at: '2026-11-05T00:00:00Z', persisted_at: '2026-11-05T00:00:00Z' });
  if (liveBeforeResponse) await act(async () => { receive(); });
  await act(async () => {
    request.resolve({ id: 'goal-1', input: { input_id: 'goal-human-input' }, run: { run_id: 'goal-run', status: 'pending', replayed } });
    await sending;
  });
  if (!liveBeforeResponse) await act(async () => { receive(); });
  await waitFor(() => expect(mocks.chatProps.chatMessages.filter((m: any) => m.role === 'user')).toEqual([
    expect.objectContaining({ id: 'goal-human-input', content: 'Use [REDACTED_SECRET]', pastedCredentialNotice: true }),
  ]));
});

it('shows the ordinary message before the response and sends only once on a double press', async () => {
  const request = deferred<any>();
  mocks.startSessionRun.mockReturnValue(request.promise);
  await composeOrdinaryMessage();
  let first!: Promise<unknown>;
  act(() => {
    first = mocks.chatProps.onSendChatMsg();
    void mocks.chatProps.onSendChatMsg();
  });
  expect(mocks.startSessionRun).toHaveBeenCalledTimes(1);
  expect(mocks.chatProps.chatMessages.filter((m: any) => m.role === 'user')).toHaveLength(1);
  expect(mocks.chatProps.chatMessages.at(-1)).toMatchObject({ content: 'Keep this message', sendState: 'sending' });
  expect(mocks.chatProps.chatInput).toBe('Keep this message');
  await act(async () => { request.resolve({ schema: 'hive.human_input_receipt', admission_state: 'admitted', run: null }); await first; });
  expect(mocks.chatProps.chatInput).toBe('');
  expect(mocks.chatProps.chatMessages.at(-1).sendState).toBeUndefined();
});

it.each(['rejected', 'cancelled', 'needs_reconciliation'])('keeps text and offers a new attempt after HTTP 202 with %s', async admission_state => {
  mocks.startSessionRun.mockResolvedValue({ schema: 'hive.human_input_receipt', admission_state, run: null });
  await composeOrdinaryMessage();
  await act(async () => { expect(await mocks.chatProps.onSendChatMsg()).toBe(false); });
  expect(mocks.chatProps.chatInput).toBe('Keep this message');
  const failed = mocks.chatProps.chatMessages.at(-1);
  expect(failed).toMatchObject({ content: 'Keep this message', sendState: 'failed', retrySend: expect.any(Function) });
  expect(failed.sendError).not.toMatch(/admission|reconciliation/);
  const firstId = mocks.startSessionRun.mock.calls[0][2].input_id;
  mocks.startSessionRun.mockResolvedValue({ run_id: 'accepted-run', status: 'running' });
  await act(async () => { failed.retrySend(); });
  await waitFor(() => expect(mocks.chatProps.chatInput).toBe(''));
  expect(mocks.startSessionRun).toHaveBeenCalledTimes(2);
  const retry = mocks.startSessionRun.mock.calls[1][2];
  expect(retry.input_id).not.toBe(firstId);
  expect(retry.idempotency_key).toBe(`session-input:${retry.input_id}`);
  expect(mocks.chatProps.chatMessages.filter((m: any) => m.role === 'user')).toHaveLength(1);
});

// Round 2 F2 replaces the round-1 expectation: terminal-boundary holds and
// successors are accepted queues; mailbox authority belongs to the backend.
it.each(['deferred', 'successor'])('shows an accepted %s message as queued until its own run arrives', async kind => {
  mocks.realChat = true;
  mocks.startSessionRun.mockImplementation(async () => ({ schema: 'hive.human_input_receipt', schema_version: 2,
    intent: kind === 'successor' ? 'queue_next_turn' : 'start_turn', status: 'accepted', admission_state: 'admitted',
    run: null, dispatch: { kind, code: kind === 'deferred' ? 'terminal_boundary_ack_pending' : undefined } }));
  await composeOrdinaryMessage('What were the three tasks before compaction?');
  await act(async () => { expect(await mocks.chatProps.onSendChatMsg()).toBe(true); });
  expect(mocks.chatProps.chatInput).toBe('');
  expect(mocks.chatProps.chatMessages.at(-1).sendState).not.toBe('failed');
  expect(screen.getByTestId('message-send-status').textContent).toBe('Queued');
  expect(mocks.chatProps.transportNotice || '').not.toContain('Your input is recorded');
  const inputId = mocks.startSessionRun.mock.calls[0][2].input_id;
  const envelope = (sequence: number, kind: string, lifecycle: string, item_id: string, payload: any) => ({
    schema: 'hive.session_event', schema_version: 2, event_id: `queued-${sequence}`, tenant_id: 'tenant-1',
    sequence, item_id, item_kind: kind, kind: `${kind}.${lifecycle}`, lifecycle,
    payload_schema: `hive.session.payload.${kind}.${lifecycle}.v2`,
    scope: kind === 'run' ? { level: 'run', session_id: mocks.sessionId, thread_id: mocks.sessionId,
      turn_id: `turn-${item_id}`, run_id: item_id } : { level: 'session', session_id: mocks.sessionId, thread_id: mocks.sessionId },
    actor: { type: 'runtime' }, visibility: { audience: 'direct_user' }, payload,
    occurred_at: '2026-11-09T00:00:00Z', persisted_at: '2026-11-09T00:00:00Z',
  });
  await act(async () => receiveLiveMessage(envelope(1, 'human_input', 'accepted', inputId,
    { content_parts: [{ type: 'text', text: 'What were the three tasks before compaction?' }] })));
  await waitFor(() => expect(mocks.chatProps.chatMessages.some((m: any) => m.sessionItem?.id === inputId)).toBe(true));
  expect(screen.getByTestId('message-send-status').textContent).toBe('Queued');
  await act(async () => receiveLiveMessage(envelope(2, 'run', 'queued', 'another-run', { input_id: 'another-input' })));
  await waitFor(() => expect(mocks.chatProps.chatMessages.some((m: any) => m.sessionItem?.id === 'another-run')).toBe(true));
  expect(screen.getByTestId('message-send-status').textContent).toBe('Queued');
  await act(async () => receiveLiveMessage(envelope(3, 'run', 'queued', 'new-run', { input_id: inputId })));
  await waitFor(() => expect(screen.queryByTestId('message-send-status')).toBeNull());
  await act(async () => {
    receiveLiveMessage(envelope(4, 'run', 'running', 'new-run', { input_id: inputId }));
    receiveLiveMessage({ ...envelope(5, 'assistant_commentary', 'completed', 'new-step',
      { phase: 'commentary', content: 'Checking the previous three tasks.' }),
      scope: { level: 'round', session_id: mocks.sessionId, thread_id: mocks.sessionId,
        turn_id: 'turn-new-run', run_id: 'new-run', round_id: 'round-new-run' } });
  });
  expect(mocks.startSessionRun).toHaveBeenCalledTimes(1);
  expect(mocks.chatProps.chatMessages.some((m: any) => m.sessionItem?.id === 'new-run')).toBe(true);
  await waitFor(() => expect(screen.getByTestId('run-disclosure-block').getAttribute('data-status')).toBe('running'));
});

it('presents a nonretryable dead-letter rejection without Retry', async () => {
  const message = '上一轮任务没有正常结束，这段对话暂时无法继续。请新建对话后再试。';
  mocks.realChat = true;
  mocks.language = 'zh';
  mocks.startSessionRun.mockResolvedValue({ schema: 'hive.human_input_receipt', admission_state: 'admitted',
    status: 'rejected', run: null, dispatch: { kind: 'rejected', retryable: false,
      reason_code: 'terminal_boundary_dead_letter', message } });
  await composeOrdinaryMessage();
  await act(async () => { expect(await mocks.chatProps.onSendChatMsg()).toBe(false); });
  expect(mocks.chatProps.chatInput).toBe('Keep this message');
  expect(screen.getByText(message)).toBeTruthy();
  // Round 3 replaces the round-2 Retry expectation: retryable:false forbids another attempt here.
  expect(screen.queryByRole('button', { name: '重试' })).toBeNull();
  expect(mocks.chatProps.chatMessages.at(-1).retrySend).toBeUndefined();
  expect(mocks.startSessionRun).toHaveBeenCalledTimes(1);
});

function heldInputEvent(sequence: number, inputId: string, lifecycle: 'accepted' | 'rejected', reason_code?: string) {
  return { schema: 'hive.session_event', schema_version: 2, event_id: `held-${sequence}`, tenant_id: 'tenant-1',
    sequence, item_id: inputId, item_kind: 'human_input', kind: `human_input.${lifecycle}`, lifecycle,
    payload_schema: `hive.session.payload.human_input.${lifecycle}.v2`,
    scope: { level: 'session', session_id: mocks.sessionId, thread_id: mocks.sessionId },
    actor: { type: 'runtime' }, visibility: { audience: 'direct_user' },
    payload: lifecycle === 'accepted' ? { input_id: inputId, intent: 'start_turn',
      content_parts: [{ type: 'text', text: '压缩前的三件事是什么？' }] }
      : { input_id: inputId, intent: 'start_turn', reason_code },
    occurred_at: '2026-11-09T00:00:00Z', persisted_at: '2026-11-09T00:00:00Z' };
}

it('replaces a deferred queued bubble with the localized dead-letter refusal on the open page', async () => {
  mocks.realChat = true;
  mocks.language = 'zh';
  mocks.startSessionRun.mockResolvedValue({ schema: 'hive.human_input_receipt', admission_state: 'admitted',
    status: 'accepted', run: null, dispatch: { kind: 'deferred', code: 'terminal_boundary_ack_pending' } });
  await composeOrdinaryMessage('压缩前的三件事是什么？');
  await act(async () => { expect(await mocks.chatProps.onSendChatMsg()).toBe(true); });
  expect(screen.getByTestId('message-send-status').textContent).toBe('排队中');
  const inputId = mocks.startSessionRun.mock.calls[0][2].input_id;
  await act(async () => receiveLiveMessage(heldInputEvent(1, inputId, 'accepted')));
  expect(screen.getByTestId('message-send-status').textContent).toBe('排队中');
  await act(async () => receiveLiveMessage(heldInputEvent(2, inputId, 'rejected', 'terminal_boundary_dead_letter')));
  await waitFor(() => expect(screen.queryByText('排队中')).toBeNull());
  expect(screen.getByTestId('message-send-status').textContent).toBe('上一轮任务没有正常结束，这段对话暂时无法继续。请新建对话后再试。');
  expect(screen.queryByRole('button', { name: '重试' })).toBeNull();
  expect(mocks.chatProps.chatMessages.filter((m: any) => m.role === 'user')).toHaveLength(1);
  expect(mocks.chatProps.chatMessages.find((m: any) => m.id === inputId).sendState).toBe('failed');
});

it('reloads a rejected human input as refused without an HTTP receipt or Retry', async () => {
  mocks.realChat = true;
  mocks.language = 'zh';
  mocks.sessionId = 'held-reload';
  mocks.listSessions.mockResolvedValue([{ id: mocks.sessionId, agent_id: 'agent-1', user_id: 'platform-admin-1',
    title: 'Held message', session_kind: 'human_chat', read_only: false }]);
  mocks.getSessionTranscript.mockResolvedValue([heldInputEvent(1, 'held-input', 'accepted'),
    heldInputEvent(2, 'held-input', 'rejected', 'terminal_boundary_dead_letter')]);
  render(<AgentDetail />);
  await waitFor(() => expect(screen.getByTestId('message-send-status').textContent)
    .toBe('上一轮任务没有正常结束，这段对话暂时无法继续。请新建对话后再试。'));
  expect(screen.getByText('压缩前的三件事是什么？')).toBeTruthy();
  expect(screen.queryByText('排队中')).toBeNull();
  expect(screen.queryByRole('button', { name: '重试' })).toBeNull();
  expect(mocks.startSessionRun).not.toHaveBeenCalled();
});

it('trusts a mailbox receipt even without a local active run id', async () => {
  mocks.startSessionRun.mockResolvedValue({ schema: 'hive.human_input_receipt', admission_state: 'admitted',
    status: 'queued', target_run_id: 'active-on-server', run: null, dispatch: { kind: 'mailbox' } });
  await composeOrdinaryMessage();
  expect(mocks.chatProps.activeRunId).toBeFalsy();
  await act(async () => { expect(await mocks.chatProps.onSendChatMsg()).toBe(true); });
  expect(mocks.chatProps.chatInput).toBe('');
  expect(mocks.chatProps.chatMessages.at(-1).sendState).toBeUndefined();
  expect(mocks.chatProps.transportNotice).toBe('Your input is recorded. Follow its application status in this work.');
});

it('preserves base acceptance for other deferred codes', async () => {
  mocks.startSessionRun.mockResolvedValue({ schema: 'hive.human_input_receipt', admission_state: 'admitted',
    status: 'accepted', run: null, dispatch: { kind: 'deferred', code: 'execution_denied' } });
  await composeOrdinaryMessage();
  await act(async () => { expect(await mocks.chatProps.onSendChatMsg()).toBe(true); });
  expect(mocks.chatProps.chatInput).toBe('');
  expect(mocks.chatProps.chatMessages.at(-1).sendState).toBeUndefined();
});

it('opens a writable conversation when the newest history entry is a delegated read-only session', async () => {
  mocks.sessionId = undefined;
  mocks.hash = '#chat';
  mocks.getSessionTranscript.mockResolvedValue([]);
  mocks.listSessions.mockResolvedValue([
    { id: 'delegated', agent_id: 'agent-1', user_id: 'platform-admin-1', session_kind: 'delegation_run',
      read_only: true, created_at: '2026-11-09T12:00:00Z' },
    { id: 'editable', agent_id: 'agent-1', user_id: 'platform-admin-1', session_kind: 'human_chat',
      read_only: false, source_channel: 'web', created_at: '2026-11-09T11:00:00Z' },
  ]);
  render(<AgentDetail />);
  await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('editable'));
});

it('reuses the same attempt after a network failure with no response', async () => {
  mocks.startSessionRun.mockRejectedValue(new TypeError('Failed to fetch'));
  await composeOrdinaryMessage();
  await act(async () => { await mocks.chatProps.onSendChatMsg(); });
  const firstId = mocks.startSessionRun.mock.calls[0][2].input_id;
  mocks.startSessionRun.mockResolvedValue({ run_id: 'accepted-run', status: 'running' });
  await act(async () => { mocks.chatProps.chatMessages.at(-1).retrySend(); });
  expect(mocks.startSessionRun.mock.calls[1][2].input_id).toBe(firstId);
});

it.each(['thinking', 'phase', 'tool_call'].flatMap(type => [[type, undefined], [type, 'accepted-run']]))(
  'keeps an accepted %s frame when its POST later returns 504, run id: %s', async (type, run_id) => {
    const request = deferred<any>();
    mocks.startSessionRun.mockReturnValue(request.promise);
    await composeOrdinaryMessage();
    let sending!: Promise<unknown>;
    act(() => { sending = mocks.chatProps.onSendChatMsg(); });
    const frame = type === 'thinking' ? { type, content: 'Checking the report.', run_id }
      : type === 'phase' ? { type, phase: 'thinking', run_id }
        : { type, name: 'run_command', id: 'accepted-tool', status: 'running', args: {}, run_id };
    await act(async () => { receiveLiveMessage(frame); });
    const phase = mocks.chatProps.runtimePhase;
    expect(['thinking', 'tool_running']).toContain(phase);
    await act(async () => { request.reject(new ApiError(504, 'Gateway timeout')); await sending; });
    expect(mocks.chatProps.runtimePhase).toBe(phase);
    expect(mocks.chatProps.isWaiting || mocks.chatProps.isStreaming).toBe(true);
    expect(mocks.chatProps.chatMessages.find((message: any) => message.role === 'user')).toMatchObject({
      sendState: 'failed', retrySend: expect.any(Function),
    });
  },
);

it.each([undefined, 'accepted-run'])('shows a run that arrives after the 504, run id: %s', async run_id => {
  mocks.startSessionRun.mockRejectedValue(new ApiError(504, 'Gateway timeout'));
  await composeOrdinaryMessage();
  await act(async () => { await mocks.chatProps.onSendChatMsg(); });
  expect(mocks.chatProps.chatMessages.at(-1).sendState).toBe('failed');
  await act(async () => { receiveLiveMessage({ type: 'thinking', content: 'The server accepted this run.', run_id }); });
  expect(mocks.chatProps.runtimePhase).toBe('thinking');
  expect(mocks.chatProps.isWaiting || mocks.chatProps.isStreaming).toBe(true);
});


it('keeps attachments on a refusal and resends their original bytes', async () => {
  mocks.startSessionRun.mockResolvedValue({ schema: 'hive.human_input_receipt', admission_state: 'rejected', run: null });
  mocks.upload.mockResolvedValue({ filename: 'notes.txt', extracted_text: 'Private notes', workspace_path: 'workspace/uploads/notes.txt' });
  await composeOrdinaryMessage();
  await act(async () => { await mocks.chatProps.onHandleChatFile({ target: { files: [new File(['Private notes'], 'notes.txt')] } }); });
  await act(async () => { await mocks.chatProps.onSendChatMsg(); });
  expect(mocks.chatProps.attachedFiles).toMatchObject([{ name: 'notes.txt', text: 'Private notes' }]);
  const originalInput = mocks.startSessionRun.mock.calls[0][2];
  mocks.startSessionRun.mockResolvedValue({ run_id: 'accepted', status: 'pending' });
  await act(async () => { mocks.chatProps.chatMessages.at(-1).retrySend(); });
  const nextInput = mocks.startSessionRun.mock.calls[1][2];
  expect(nextInput.content).toBe(originalInput.content);
  expect(nextInput.attachments).toEqual(originalInput.attachments);
  expect(mocks.chatProps.attachedFiles).toEqual([]);
});

it('R14a sends a delayed upload with its own message and bubble, then leaves the next payload empty', async () => {
  mocks.realChat = true;
  mocks.userRole = 'member';
  const upload = deferred<any>();
  mocks.upload.mockReturnValueOnce(upload.promise);
  mocks.startSessionRun.mockResolvedValue({ run_id: 'accepted', status: 'pending' });
  await composeOrdinaryMessage('Analyze sales');
  fireEvent.change(document.querySelector('input[type="file"]')!, { target: { files: [new File(['sales'], 'sales.csv')] } });
  await act(async () => {
    fireEvent.click(screen.getByTestId('session-composer-send'));
    fireEvent.keyDown(screen.getByTestId('session-composer-input'), { key: 'Enter' });
  });
  await act(async () => { upload.resolve({ filename: 'sales.csv', extracted_text: 'sales', workspace_path: 'workspace/uploads/sales.csv' }); });
  fireEvent.click(screen.getByTestId('session-composer-send'));
  await waitFor(() => expect(mocks.startSessionRun).toHaveBeenCalledTimes(1));
  expect(mocks.startSessionRun.mock.calls[0][2].attachments).toEqual([
    expect.objectContaining({ name: 'sales.csv', path: 'workspace/uploads/sales.csv' }),
  ]);
  await waitFor(() => expect(screen.queryByTestId('session-composer-attachments')).toBeNull());
  expect(screen.getByText('sales.csv')).toBeTruthy();
  expect(mocks.chatProps.chatMessages.find((message: any) => message.role === 'user').inputAttachments)
    .toEqual([expect.objectContaining({ name: 'sales.csv' })]);
  fireEvent.change(screen.getByTestId('session-composer-input'), { target: { value: 'A different request' } });
  fireEvent.click(screen.getByTestId('session-composer-send'));
  await waitFor(() => expect(mocks.startSessionRun).toHaveBeenCalledTimes(2));
  expect(mocks.startSessionRun.mock.calls[1][2].attachments).toEqual([]);
  expect(mocks.startSessionRun.mock.calls[1][2].file_name).toBe('');
  expect(mocks.startSessionRun.mock.calls[1][2].content).toBe('A different request');
});

it('keeps synchronous compact visible when polling finds no run after the absence grace', async () => {
  mocks.realChat = true;
  mocks.userRole = 'member';
  const request = deferred<any>();
  vi.spyOn(ccParityApi, 'executeCommand').mockReturnValue(request.promise);
  await composeOrdinaryMessage('/compact');
  let sending!: Promise<unknown>;
  act(() => { sending = mocks.chatProps.onSendChatMsg(); });
  await waitFor(() => expect(ccParityApi.executeCommand).toHaveBeenCalledTimes(1));
  expect(screen.getByTestId('run-disclosure-block').getAttribute('data-status')).toBe('running');
  const now = Date.now();
  vi.spyOn(Date, 'now').mockReturnValue(now + 60_000);
  mocks.activeRunObservedAt += 1;
  await act(async () => { mocks.chatProps.onSetChatInput('Next request'); });
  expect(screen.getByTestId('run-disclosure-block').getAttribute('data-status')).toBe('running');
  await act(async () => {
    request.resolve({ ok: true, command: 'compact', result: { message: '上下文已压缩。' } });
    await sending;
  });
  expect(mocks.chatProps.chatInput).toBe('Next request');
  expect(mocks.chatProps.isWaiting || mocks.chatProps.isStreaming).toBe(false);
});

const compactRefusalResults = {
  'unsettled-history': { ok: false, action: 'unavailable', ui_action: { type: 'toast', level: 'error',
    message: 'Compaction is unavailable: canonical session history could not be verified. Context was not changed.' } },
  'no-messages': { ok: false, action: 'not_supported', ui_action: { type: 'toast', level: 'warning',
    message: 'No session messages are available to compact.' } },
  'empty-summary': { ok: false, action: 'not_supported', ui_action: { type: 'toast', level: 'warning',
    message: 'Compaction summary model is unavailable; context was not changed.' } },
};

it.each(['success', 'http', 'network', 'unsettled-history', 'no-messages', 'empty-summary'] as const)('clears compact on send and keeps progress through the %s response', async outcome => {
  mocks.realChat = true;
  mocks.language = 'zh';
  mocks.userRole = 'member';
  const request = deferred<any>();
  vi.spyOn(ccParityApi, 'executeCommand').mockReturnValue(request.promise);
  await composeOrdinaryMessage('/compact');
  let sending!: Promise<unknown>;
  act(() => { sending = mocks.chatProps.onSendChatMsg(); });
  await waitFor(() => expect(ccParityApi.executeCommand).toHaveBeenCalledTimes(1));
  expect(mocks.chatProps.chatInput).toBe('');
  expect(screen.getByTestId('run-disclosure-block').getAttribute('data-status')).toBe('running');
  await act(async () => { mocks.chatProps.onSetChatInput('继续处理下一件事'); });
  expect(screen.getByTestId('run-disclosure-block').getAttribute('data-status')).toBe('running');
  await act(async () => {
    if (outcome === 'http') request.reject(new ApiError(504, 'HTTP 504 Gateway Timeout'));
    else if (outcome === 'network') request.reject(new ApiError(0, 'Failed to fetch'));
    else request.resolve({ ok: true, command: 'compact', result: outcome === 'success'
      ? { ok: true, action: 'compacted_context_installed', message: '上下文已压缩。' }
      : compactRefusalResults[outcome] });
    await sending;
  });
  if (outcome !== 'success') {
    const failure = mocks.chatProps.chatMessages.find((message: any) => message.eventType === 'runtime_action_failed');
    expect(failure.content).toBe(outcome === 'network' ? zh.common.networkFailed : outcome === 'http' ? zh.common.serviceUnavailable : zh.common.requestFailed);
    expect(mocks.messageStore!.getSnapshot(mocks.chatProps.activeSession.id).filter(message => message.content === failure.content)).toHaveLength(1);
    expect(mocks.chatProps.chatMessages.some((message: any) => message.content === '上下文已压缩。')).toBe(false);
    expect(screen.queryByText('上下文已压缩。')).toBeNull();
    expect(document.querySelector('.agent-detail-upload-toast')).toBeNull();
    expect(mocks.chatProps.runtimePhase).toBe('failed');
    expect(screen.getByTestId('run-disclosure-block').getAttribute('data-status')).toBe('failed');
    expect(document.body.textContent).not.toMatch(/504|Gateway|Failed to fetch|Summary model|Compaction|No session messages/);
  } else {
    expect(mocks.chatProps.chatMessages.filter((message: any) => message.role === 'assistant'
      && message.content === '上下文已压缩。')).toHaveLength(1);
    expect(screen.getByText('上下文已压缩。')).toBeTruthy();
  }
  expect(mocks.chatProps.chatInput).toBe('继续处理下一件事');
  expect(mocks.chatProps.isWaiting).toBe(false);
  expect((screen.getByTestId('session-composer-send') as HTMLButtonElement).disabled).toBe(false);
});

it('keeps the compacting composer editable without Stop or another send until the response', async () => {
  mocks.realChat = true;
  mocks.userRole = 'member';
  const request = deferred<any>();
  vi.spyOn(ccParityApi, 'executeCommand').mockReturnValue(request.promise);
  await composeOrdinaryMessage('/compact');
  let first!: Promise<unknown>;
  let second!: Promise<unknown>;
  act(() => {
    first = mocks.chatProps.onSendChatMsg();
    second = mocks.chatProps.onSendChatMsg();
  });
  await waitFor(() => expect(ccParityApi.executeCommand).toHaveBeenCalledTimes(1));
  expect(screen.queryByTestId('session-composer-stop')).toBeNull();
  const input = screen.getByTestId('session-composer-input') as HTMLTextAreaElement;
  const send = screen.getByTestId('session-composer-send') as HTMLButtonElement;
  expect(input.disabled).toBe(false);
  fireEvent.change(input, { target: { value: 'Keep the next draft' } });
  expect(send.disabled).toBe(true);
  fireEvent.keyDown(input, { key: 'Enter' });
  await act(async () => { await mocks.chatProps.onSendChatMsg(); });
  expect(mocks.startSessionRun).not.toHaveBeenCalled();
  fireEvent.change(input, { target: { value: '/compact' } });
  await act(async () => { await mocks.chatProps.onSendChatMsg(); });
  expect(ccParityApi.executeCommand).toHaveBeenCalledTimes(1);
  fireEvent.change(input, { target: { value: 'Keep the next draft' } });
  await act(async () => {
    request.resolve({ ok: true, command: 'compact', result: { action: 'compacted_context_installed',
      message: '上下文已压缩。', ui_action: { type: 'install_compacted_context', message: '上下文已压缩。' } } });
    await Promise.all([first, second]);
  });
  expect(input.value).toBe('Keep the next draft');
  expect(send.disabled).toBe(false);
  expect(screen.queryByTestId('session-composer-stop')).toBeNull();
});

it.each(['success', 'http', 'unsettled-history', 'no-messages', 'empty-summary'] as const)('writes the %s compact outcome to its session until transcript reload', async outcome => {
  mocks.realChat = true;
  mocks.language = 'zh';
  mocks.userRole = 'member';
  const request = deferred<any>();
  vi.spyOn(ccParityApi, 'executeCommand').mockReturnValue(request.promise);
  await composeOrdinaryMessage('/compact');
  const compactingSession = mocks.chatProps.activeSession;
  const otherSession = { ...compactingSession, id: 'other-session', title: 'Other conversation' };
  mocks.listSessions.mockResolvedValue([compactingSession, otherSession]);
  let sending!: Promise<unknown>;
  act(() => { sending = mocks.chatProps.onSendChatMsg(); });
  await waitFor(() => expect(ccParityApi.executeCommand).toHaveBeenCalledTimes(1));
  mocks.sessionId = otherSession.id;
  await act(async () => { await mocks.chatProps.onSelectSession(otherSession); });
  await waitFor(() => expect(mocks.chatProps.activeSession.id).toBe(otherSession.id));
  const input = screen.getByTestId('session-composer-input') as HTMLTextAreaElement;
  fireEvent.change(input, { target: { value: 'Work in the other conversation' } });
  expect((screen.getByTestId('session-composer-send') as HTMLButtonElement).disabled).toBe(false);
  mocks.startSessionRun.mockResolvedValue({ run_id: 'other-run', status: 'running' });
  mocks.activeSessionRun = { run_id: 'other-run', status: 'running' };
  await act(async () => { await mocks.chatProps.onSendChatMsg(); });
  expect(mocks.startSessionRun).toHaveBeenCalledExactlyOnceWith('agent-1', otherSession.id,
    expect.objectContaining({ content: 'Work in the other conversation' }));
  const otherPhase = mocks.chatProps.runtimePhase;
  expect(otherPhase).not.toBe('compacting');
  fireEvent.change(input, { target: { value: 'Keep the other draft' } });
  await act(async () => {
    if (outcome === 'http') request.reject(new ApiError(504, 'HTTP 504 Gateway Timeout'));
    else if (outcome === 'success') request.resolve({ ok: true, command: 'compact', result: {
      action: 'compacted_context_installed',
      ui_action: { type: 'install_compacted_context', message: '上下文已压缩。' },
    } });
    else request.resolve({ ok: true, command: 'compact', result: compactRefusalResults[outcome] });
    await sending;
  });
  expect(mocks.chatProps.activeSession.id).toBe(otherSession.id);
  expect(mocks.chatProps.runtimePhase).toBe(otherPhase);
  expect(mocks.chatProps.chatMessages.some((m: any) => m.eventType === 'runtime_action_failed')).toBe(false);
  expect(screen.queryByText('上下文已压缩。')).toBeNull();
  expect(input.value).toBe('Keep the other draft');
  const expected = outcome === 'success' ? '上下文已压缩。'
    : outcome === 'http' ? zh.common.serviceUnavailable : zh.common.requestFailed;
  expect(mocks.messageStore!.getSnapshot(compactingSession.id).filter(m => m.content === expected)).toHaveLength(1);
  expect(mocks.messageStore!.getSnapshot(otherSession.id).some(m => m.content === expected)).toBe(false);
  mocks.activeSessionRun = null;
  mocks.sessionId = compactingSession.id;
  const transcript = deferred<any[]>();
  mocks.getSessionTranscript.mockReturnValue(transcript.promise);
  let selecting!: Promise<unknown>;
  act(() => { selecting = mocks.chatProps.onSelectSession(compactingSession); });
  await waitFor(() => expect(mocks.chatProps.activeSession.id).toBe(compactingSession.id));
  const storedTranscript = outcome === 'http' ? [] : [{
    event_id: 'prior-reply', id: 'prior-reply', session_id: compactingSession.id, sequence: 1,
    event_type: 'assistant_message', role: 'assistant', content: 'Earlier completed work',
  }];
  await act(async () => { transcript.resolve(storedTranscript); await selecting; });
  expect(mocks.chatProps.chatMessages.some((m: any) => m.content === expected)).toBe(false);
  expect(mocks.messageStore!.getSnapshot(compactingSession.id).some(m => m.content === expected)).toBe(false);
  expect(screen.queryByText(expected)).toBeNull();
  expect(mocks.chatProps.chatMessages.some((m: any) => m.eventType === 'runtime_action_failed')).toBe(false);
  if (storedTranscript.length) expect(screen.getByText('Earlier completed work')).toBeTruthy();
  fireEvent.change(screen.getByTestId('session-composer-input'), { target: { value: 'Retry later' } });
  expect((screen.getByTestId('session-composer-send') as HTMLButtonElement).disabled).toBe(false);
});

it.each(['artifact', 'command-panel'])('blocks %s continuation only for the compacting session', async source => {
  mocks.realChat = true;
  mocks.language = 'zh';
  mocks.userRole = 'member';
  const request = deferred<any>();
  vi.spyOn(ccParityApi, 'executeCommand').mockReturnValue(request.promise);
  await composeOrdinaryMessage('/compact');
  const compactingSession = mocks.chatProps.activeSession;
  let sending!: Promise<unknown>;
  act(() => { sending = mocks.chatProps.onSendChatMsg(); });
  await waitFor(() => expect(ccParityApi.executeCommand).toHaveBeenCalledTimes(1));
  const continuation = source === 'artifact' ? render(<ArtifactContinuation
    artifact={{ name: 'report.md', path: 'workspace/report.md' }} workspaceAgentId="agent-1" blocked={false}
    context={{ sessionId: compactingSession.id, send: mocks.chatProps.onSendMessage }} />) : null;
  if (continuation) {
    const draft = screen.getByLabelText(zh.agent.chat.artifacts.continueLabel) as HTMLTextAreaElement;
    fireEvent.change(draft, { target: { value: 'Continue the work' } });
    fireEvent.click(screen.getByRole('button', { name: zh.agent.chat.artifacts.continueHere }));
    await waitFor(() => expect(screen.queryByText(zh.agent.chat.artifacts.continuing)).toBeNull());
    expect(draft.value).toBe('Continue the work');
    expect(screen.getByRole('alert').textContent).toBe('正在压缩上下文，完成后再继续。');
    expect(screen.queryByText(zh.agent.chat.artifacts.continueRecorded)).toBeNull();
  } else {
    // Command-panel Continue uses the same callback without original-work options.
    await act(async () => { await expect(mocks.chatProps.onSendMessage('Continue the work'))
      .rejects.toThrow('正在压缩上下文，完成后再继续。'); });
  }
  expect(mocks.startSessionRun).not.toHaveBeenCalled();

  const otherSession = { ...compactingSession, id: 'other-session', title: 'Other conversation' };
  mocks.listSessions.mockResolvedValue([compactingSession, otherSession]);
  mocks.sessionId = otherSession.id;
  await act(async () => { await mocks.chatProps.onSelectSession(otherSession); });
  mocks.startSessionRun.mockResolvedValue({ run_id: 'other-run', status: 'running' });
  if (continuation) {
    continuation.rerender(<ArtifactContinuation key={otherSession.id}
      artifact={{ name: 'report.md', path: 'workspace/report.md' }} workspaceAgentId="agent-1" blocked={false}
      context={{ sessionId: otherSession.id, send: mocks.chatProps.onSendMessage }} />);
    const draft = screen.getByLabelText(zh.agent.chat.artifacts.continueLabel) as HTMLTextAreaElement;
    fireEvent.change(draft, { target: { value: 'Continue the other work' } });
    fireEvent.click(screen.getByRole('button', { name: zh.agent.chat.artifacts.continueHere }));
    await screen.findByText(zh.agent.chat.artifacts.continueRecorded);
    expect(draft.value).toBe('');
  } else {
    await act(async () => { await mocks.chatProps.onSendMessage('Continue the other work'); });
  }
  expect(mocks.startSessionRun).toHaveBeenCalledExactlyOnceWith('agent-1', otherSession.id,
    expect.objectContaining({ content: 'Continue the other work' }));
  await act(async () => {
    request.resolve({ ok: true, command: 'compact', result: { message: '上下文已压缩。' } });
    await sending;
  });
});

it.each([true, false])('clears slash input only after its existing success result: %s', async success => {
  const request = deferred<any>();
  vi.spyOn(ccParityApi, 'executeCommand').mockReturnValue(request.promise);
  await composeOrdinaryMessage('/help');
  let send!: Promise<unknown>;
  act(() => { send = mocks.chatProps.onSendChatMsg(); });
  await waitFor(() => expect(ccParityApi.executeCommand).toHaveBeenCalledTimes(1));
  expect(mocks.chatProps.chatInput).toBe('/help');
  await act(async () => { if (success) request.resolve({ result: 'Help available' }); else request.reject(new Error('Command unavailable')); await send; });
  expect(mocks.chatProps.chatInput).toBe(success ? '' : '/help');
});

it.each([true, false])('preserves the goal run-id success criterion and input on failure: %s', async success => {
  const request = deferred<any>();
  vi.spyOn(ccParityApi, 'startGoal').mockReturnValue(request.promise);
  await composeOrdinaryMessage('Goal objective');
  act(() => mocks.chatProps.onToggleGoalMode());
  let send!: Promise<unknown>;
  act(() => { send = mocks.chatProps.onSendChatMsg(); });
  await waitFor(() => expect(ccParityApi.startGoal).toHaveBeenCalledTimes(1));
  expect(mocks.chatProps.chatInput).toBe('Goal objective');
  await act(async () => { request.resolve({ run: success ? { run_id: 'goal-run', status: 'pending' } : null }); await send; });
  expect(mocks.chatProps.chatInput).toBe(success ? '' : 'Goal objective');
});

it('keeps the optimistic message visible when a draft becomes a durable session before transcript hydration', async () => {
  await mountOwnedArtifactWork();
  mocks.sessionId = undefined;
  await act(async () => { await mocks.chatProps.onCreateNewSession(); });
  expect(mocks.chatProps.activeSession.id).toMatch(/^draft:/);
  expect(mocks.transport.writableSession).toBe(false);
  act(() => mocks.chatProps.onSetChatInput('Keep the first draft message'));
  mocks.createSessionRun.mockResolvedValue({
    session: { id: 'new-durable-session', agent_id: 'agent-1', user_id: 'platform-admin-1',
      session_kind: 'human_chat', read_only: false },
    run: { schema: 'hive.human_input_receipt', admission_state: 'admitted', run: null },
  });
  await act(async () => { await mocks.chatProps.onSendChatMsg(); });
  expect(mocks.chatProps.activeSession.id).toBe('new-durable-session');
  expect(mocks.chatProps.chatMessages).toEqual(expect.arrayContaining([
    expect.objectContaining({ role: 'user', content: 'Keep the first draft message', sendState: undefined }),
  ]));
});

it('keeps the read-only conversation the member opened after leaving and returning to chat', async () => {
  mocks.sessionId = undefined;
  mocks.hash = '#chat';
  mocks.getSessionTranscript.mockResolvedValue([]);
  const delegated = { id: 'delegated', agent_id: 'agent-1', user_id: 'platform-admin-1', session_kind: 'delegation_run', read_only: true };
  mocks.listSessions.mockResolvedValue([delegated, { id: 'editable', agent_id: 'agent-1', user_id: 'platform-admin-1',
    session_kind: 'human_chat', source_channel: 'web', read_only: false }]);
  const view = render(<AgentDetail />);
  await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe('editable'));
  await act(async () => mocks.chatProps.onSelectSession(delegated));
  await waitFor(() => expect(mocks.chatProps.activeSession.id).toBe('delegated'));
  mocks.hash = '#settings';
  view.rerender(<AgentDetail />);
  await waitFor(() => expect(screen.queryByTestId('fabricated-session-shell')).toBeNull());
  mocks.hash = '#chat';
  view.rerender(<AgentDetail />);
  await waitFor(() => expect(mocks.listSessions.mock.calls.length).toBeGreaterThan(1));
  expect(mocks.chatProps.activeSession.id).toBe('delegated');
});

it.each(['failed', 'completed'])('R15b updates a read-only colleague live to %s with its actual duration', async status => {
  mocks.sessionId = 'owned-artifact-work';
  mocks.hash = '#chat';
  mocks.language = 'zh';
  mocks.realChat = true;
  const child = { id: mocks.sessionId, agent_id: 'agent-1', user_id: 'platform-admin-1',
    title: '周报助手', session_kind: 'delegation_run', runtime_source: 'delegation', read_only: true, runtime_task_id: 'child-run' };
  mocks.listSessions.mockResolvedValue([child]);
  const started = activeRunEvent(1, 'run', 'running', 'child-run');
  const progress = activeRunEvent(2, 'assistant_commentary', 'completed', 'child-run', { content: '正在核对工作记录。' });
  mocks.getSessionTranscript.mockResolvedValue([started, progress]);
  render(<AgentDetail />);
  await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe(child.id));
  expect(mocks.transport.readableSession).toBe(true);
  expect(mocks.transport.writableSession).toBe(false);
  expect(await screen.findByText('正在核对工作记录。')).toBeTruthy();
  expect(screen.getAllByTestId('run-disclosure-block')[0].getAttribute('data-status')).toBe('running');
  const terminal = activeRunEvent(3, 'run', status, 'child-run', {
    task_type: 'delegation',
    started_at: started.occurred_at, completed_at: new Date(Date.parse(started.occurred_at) + 591_000).toISOString(),
    ...(status === 'failed' ? { reason_code: 'tool_budget', message: '这次用完了用量预算，没有完成。' } : {}),
  });
  terminal.occurred_at = terminal.payload.completed_at as string;
  mocks.getSessionTranscript.mockResolvedValue([started, progress, terminal]);
  await act(async () => { receiveLiveMessage(terminal); });
  await waitFor(() => expect(screen.getAllByTestId('run-disclosure-block')[0].getAttribute('data-status'))
    .toBe(status === 'failed' ? 'failed' : 'done'));
  expect(screen.getAllByTestId('run-disclosure-block')[0].querySelector('.run-disclosure-duration')?.textContent).toBe('9m 51s');
  if (status === 'failed') {
    expect(screen.getAllByText('这次用完了用量预算，没有完成。')).toHaveLength(1);
    expect(screen.getAllByTestId('run-disclosure-block')[0].textContent).not.toContain('完成');
  }
  expect(mocks.startSessionRun).not.toHaveBeenCalled();
});

it.each([
  ['parent page', { agent_id: 'colleague-agent', peer_agent_id: 'agent-1' }],
  ['remote A2A', { runtime_source: 'remote_a2a' }],
  ['operator view', { operator_view: true, user_id: 'another-user' }],
])('R15b retains colleague history without subscribing from %s', async (_entry, overrides) => {
  mocks.sessionId = 'colleague-history';
  mocks.hash = '#chat';
  mocks.realTransport = true;
  const opened = vi.fn();
  vi.stubGlobal('WebSocket', class {
    static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
    readyState = 0;
    constructor() { opened(); }
    close() { this.readyState = 3; }
  });
  const child = { id: mocks.sessionId, agent_id: 'agent-1', user_id: 'platform-admin-1',
    source_channel: 'agent', session_kind: 'delegation_run', runtime_source: 'delegation',
    title: 'Colleague history', read_only: true, ...overrides };
  if ('operator_view' in child && child.operator_view) {
    mocks.search = '?manage=true';
    mocks.accessLevel = 'operator';
    mocks.operatorCap = true;
  }
  mocks.listSessions.mockResolvedValue([child]);
  mocks.getSessionTranscript.mockResolvedValue([activeRunEvent(1, 'assistant_commentary', 'completed', 'child-run',
    { content: 'The already loaded colleague transcript.' })]);
  render(<AgentDetail />);
  if ('operator_view' in child && child.operator_view) {
    fireEvent.change(screen.getByLabelText('Operator inspection reason'), { target: { value: 'Review this conversation' } });
    fireEvent.click(screen.getByRole('button', { name: 'Begin inspection' }));
  }
  await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe(child.id));
  expect(mocks.transport.readableSession).toBe(false);
  expect(mocks.transport.writableSession).toBe(false);
  expect(await screen.findByText('The already loaded colleague transcript.')).toBeTruthy();
  expect(opened).not.toHaveBeenCalled();
  expect(mocks.chatProps.historyMessagesSessionId).toBe(child.id);
  expect(mocks.chatProps.historyMsgs.some((message: any) => message.content === 'The already loaded colleague transcript.')).toBe(true);
  expect(mocks.startSessionRun).not.toHaveBeenCalled();
});
