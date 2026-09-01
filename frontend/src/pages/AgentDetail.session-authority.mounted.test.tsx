// @vitest-environment jsdom

import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/core';

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
}));

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
    user: { id: 'platform-admin-1', role: 'platform_admin' },
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
  },
}));

vi.mock('../api/domains/activity', () => ({
  activityApi: {
    list: mocks.activityList,
    getToolFailureSummary: mocks.toolFailures,
  },
}));

vi.mock('./agent-detail/useSessionTransportController', () => ({
  useSessionTransportController: () => ({
    wsConnected: false,
    transportPhase: 'initializing',
    transportReconnectAttempt: 0,
    closeSessionSocket: vi.fn(),
    getSessionSocket: vi.fn(() => null),
    reconnectActiveTransport: vi.fn(),
    resetActiveTransportState: vi.fn(),
    syncActiveSocketState: vi.fn(),
  }),
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
  default: ({ activeSession, historyMsgs, allSessions }: {
    activeSession?: any;
    historyMsgs?: Array<{ content?: string }>;
    allSessions?: Array<{ id?: string; title?: string }>;
  }) => (
    <div data-testid="fabricated-session-shell">
      {activeSession?.operator_view ? <strong>Operator View</strong> : 'Read-only · User'}
      {(historyMsgs || []).map((message, index) => <span key={index}>{message.content}</span>)}
      {(allSessions || []).map((session) => <span key={session.id}>{session.title}</span>)}
    </div>
  ),
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
  mocks.executedActivityQueries.clear();
  mocks.activityList.mockResolvedValue([]);
  mocks.toolFailures.mockResolvedValue(undefined);
  mocks.listSessions.mockResolvedValue([]);
  mocks.getSessionMessages.mockResolvedValue([]);
  mocks.getSessionLineage.mockResolvedValue([]);
});

afterEach(cleanup);

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
});
