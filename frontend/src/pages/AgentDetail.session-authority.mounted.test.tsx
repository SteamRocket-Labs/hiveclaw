// @vitest-environment jsdom

import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/core';
import { ccParityApi } from '../api/domains/ccParity';

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
  startSessionRun: vi.fn(),
  createSessionRun: vi.fn(),
  cancelSessionRun: vi.fn(),
  upload: vi.fn(),
  activeSessionRun: null as null | { run_id: string; status: string },
  chatProps: null as any,
  transport: null as any,
}));

vi.mock('../api/core/upload-progress', () => ({ uploadFileWithProgress: (...args: unknown[]) => ({ promise: mocks.upload(...args) }) }));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
    i18n: { language: 'en' },
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: (options: { queryKey: unknown[]; queryFn?: () => unknown; enabled?: unknown }) => {
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
      return { data: mocks.activeSessionRun, dataUpdatedAt: 1, isLoading: false, error: null };
    }
    return {
      data: undefined,
      dataUpdatedAt: 0,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    };
  },
  useQueryClient: () => ({
    invalidateQueries: vi.fn(),
    cancelQueries: vi.fn(),
    removeQueries: vi.fn(),
  }),
}));

vi.mock('react-router-dom', () => ({
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
  },
}));

vi.mock('../api/domains/activity', () => ({
  activityApi: {
    list: mocks.activityList,
    getToolFailureSummary: mocks.toolFailures,
  },
}));

vi.mock('./agent-detail/useSessionTransportController', () => ({
  useSessionTransportController: (options: any) => {
    mocks.transport = options;
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
}));

vi.mock('../components/ConfirmModal', () => ({ default: () => null }));
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
vi.mock('./agent-detail/AgentChatSection', () => ({
  default: ({ activeSession, chatMessages, historyMsgs, allSessions, chatMessagesSessionId, historyMessagesSessionId, onSendMessage,
    activeRunId, activeRunStatus, onAbortGeneration, ...composer }: {
    [key: string]: any;
    activeSession?: any;
    chatMessages?: Array<{ content?: string }>;
    historyMsgs?: Array<{ content?: string }>;
    allSessions?: Array<{ id?: string; title?: string }>;
    chatMessagesSessionId?: string | null;
    historyMessagesSessionId?: string | null;
    onSendMessage?: (text: string, options?: any) => unknown;
    activeRunId?: string | null;
    activeRunStatus?: string | null;
    onAbortGeneration?: () => unknown;
  }) => {
    mocks.chatProps = { activeSession, onSendMessage, chatMessages, activeRunId, activeRunStatus, onAbortGeneration, ...composer };
    return (
    <div data-testid="fabricated-session-shell">
      {activeSession?.operator_view ? <strong>Operator View</strong> : activeSession?.read_only ? 'Read-only · User' : null}
      {(!mocks.projectActiveTimeline || (!activeSession?.read_only && chatMessagesSessionId === activeSession?.id) ? chatMessages || [] : [])
        .map((message, index) => <span key={`chat-${index}`}>{message.content}</span>)}
      {(!mocks.projectActiveTimeline || (activeSession?.read_only && historyMessagesSessionId === activeSession?.id) ? historyMsgs || [] : [])
        .map((message, index) => <span key={index}>{message.content}</span>)}
      {(allSessions || []).map((session) => <span key={session.id}>{session.title}</span>)}
    </div>
    );
  },
}));

import AgentDetail from './AgentDetail';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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
  vi.clearAllMocks();
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
  mocks.activeSessionRun = null;
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

async function mountOwnedArtifactWork() {
  mocks.sessionId = 'owned-artifact-work';
  mocks.listSessions.mockResolvedValue([{ id: mocks.sessionId, agent_id: 'agent-1', user_id: 'platform-admin-1',
    title: 'Saved artifact work', session_kind: 'human_chat', read_only: false, operator_view: false }]);
  mocks.getSessionTranscript.mockResolvedValue([]);
  render(<AgentDetail />);
  await waitFor(() => expect(mocks.chatProps?.activeSession?.id).toBe(mocks.sessionId));
  return mocks.chatProps.onSendMessage as (text: string, options: { expectedSessionId: string; inputId: string; displayContent?: string }) => Promise<unknown>;
}

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
  const options = { expectedSessionId: 'owned-artifact-work', inputId: globalThis.crypto.randomUUID(), displayContent: 'report.md\nRevise saved file' };
  await act(async () => { await expect(send('Selected saved file reference', options)).rejects.toThrow('Acknowledgement unavailable'); });
  expect(mocks.chatProps.chatMessages.at(-1)).toMatchObject({ eventStatus: 'input_outcome_unknown' });
  expect(mocks.chatProps.chatMessages.at(-1).threadItem).toMatchObject({ item_type: 'warning', item_status: 'waiting_user' });
  await act(async () => { await send('Selected saved file reference', options); });
  expect(mocks.startSessionRun).toHaveBeenCalledTimes(2);
  expect(mocks.startSessionRun.mock.calls[1]).toEqual(mocks.startSessionRun.mock.calls[0]);
  expect(mocks.startSessionRun.mock.calls[0].slice(0, 2)).toEqual(['agent-1', 'owned-artifact-work']);
  expect(mocks.startSessionRun.mock.calls[0][2]).toMatchObject({ input_id: options.inputId,
    idempotency_key: `session-input:${options.inputId}`, display_content: options.displayContent });
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

it('reuses the same attempt after a network failure with no response', async () => {
  mocks.startSessionRun.mockRejectedValue(new TypeError('Failed to fetch'));
  await composeOrdinaryMessage();
  await act(async () => { await mocks.chatProps.onSendChatMsg(); });
  const firstId = mocks.startSessionRun.mock.calls[0][2].input_id;
  mocks.startSessionRun.mockResolvedValue({ run_id: 'accepted-run', status: 'running' });
  await act(async () => { mocks.chatProps.chatMessages.at(-1).retrySend(); });
  expect(mocks.startSessionRun.mock.calls[1][2].input_id).toBe(firstId);
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
