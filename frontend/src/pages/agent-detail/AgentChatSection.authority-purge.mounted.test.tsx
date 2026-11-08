// @vitest-environment jsdom

import React from 'react';
import { useAuthStore } from '../../stores';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import type { RuntimeWorkLedgerView } from '../../api/domains/autonomy';

const noticeLocale = vi.hoisted(() => ({ language: 'en' }));

const harness = vi.hoisted(() => ({
  ledger: undefined as RuntimeWorkLedgerView | undefined,
  activeRun: undefined as { id: string; status: string } | undefined,
  ledgerQuery: undefined as { refetchInterval?: number | false } | undefined,
  readArtifact: vi.fn(),
  downloadArtifact: vi.fn(),
  createObjectURL: vi.fn(),
  revokeObjectURL: vi.fn(),
  saveBlob: vi.fn(),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string | Record<string, unknown>, values?: Record<string, unknown>) => {
      if (_key === 'agent.chat.pastedCredentialNotice' || _key === 'agent.chat.hiddenCredential'
        || _key.startsWith('agent.runtimeStatus.')) {
        const catalog = noticeLocale.language === 'zh' ? zh : en;
        return _key.split('.').reduce<any>((value, key) => value?.[key], catalog) ?? _key;
      }
      const template = typeof fallback === 'string'
        ? fallback
        : (typeof fallback?.defaultValue === 'string' ? fallback.defaultValue : _key);
      const interpolation = typeof fallback === 'object' ? fallback : values;
      return Object.entries(interpolation || {}).reduce(
        (text, [key, value]) => text.replace(`{{${key}}}`, String(value)),
        template,
      );
    },
    i18n: { language: 'en' },
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: (options: { queryKey: unknown[]; enabled?: boolean; refetchInterval?: number | false }) => {
    const { queryKey, enabled } = options;
    if (enabled === false) return { data: undefined, isLoading: false, refetch: vi.fn() };
    if (queryKey[0] === 'chat-session-work-ledger') {
      harness.ledgerQuery = options;
      return { data: harness.ledger, isLoading: false, refetch: vi.fn() };
    }
    if (queryKey[0] === 'chat-session-workbench') {
      return {
        data: {
          schema: 'session_workbench.v1',
          agent_id: 'agent-1',
          session: { id: String(queryKey[2]), title: 'Private session' },
          active_run: harness.activeRun,
          runtime_sections: {
            workflows: [{
              id: 'private-workflow',
              runtime_kind: 'workflow',
              label: 'Private workflow run',
              status: 'running',
              steps: [],
              leaf_calls: [],
            }],
          },
        },
        isLoading: false,
        refetch: vi.fn(),
      };
    }
    if (queryKey[0] === 'chat-session-index') {
      return {
        data: {
          checkpoints: [{
            checkpoint_event_id: 'private-checkpoint',
            content: 'Private checkpoint marker',
            created_at: '2026-09-01T10:00:00Z',
          }],
        },
        isLoading: false,
        refetch: vi.fn(),
      };
    }
    if (queryKey[0] === 'remote-a2a-work') {
      return {
        data: { agent_id: 'agent-1', source_session_id: 'retirement-session',
          connection: { ready: false, version: null }, items: [] },
        isLoading: false,
        refetch: vi.fn(),
      };
    }
    return { data: [], isLoading: false, refetch: vi.fn() };
  },
  useMutation: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false }),
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

vi.mock('../../api/domains/files', () => ({
  fileApi: {
    readArtifact: harness.readArtifact,
    read: vi.fn(),
    downloadArtifact: harness.downloadArtifact,
    download: vi.fn(),
  },
}));

vi.mock('../../utils/authenticatedResource', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../utils/authenticatedResource')>(),
  saveBlob: harness.saveBlob,
}));

vi.mock('../../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}));

import AgentChatSection from './AgentChatSection';
import { projectsApi } from '../../api/domains/projects';

it('shows the waiting state once for one approval in the real conversation timeline', () => {
  const base = props('', false, 'waiting-work');
  render(<AgentChatSection {...base} agent={{ ...base.agent, access_level: 'use' }}
    currentUser={{ id: 'member', tenant_id: 'company' }}
    activeSession={{ ...base.activeSession, user_id: 'member', read_only: false, operator_view: false }}
    historyMsgs={[]} chatMessagesSessionId="waiting-work"
    chatMessages={[{ id: 'input', role: 'user', content: 'Please check first' },
      { id: 'question', role: 'tool_call', content: '', toolName: 'ask_user_question', toolStatus: 'done',
        toolMeta: { kind: 'user_clarification', blocking: true, questions: [], nextAction: null } }]}
    activeRunId="waiting-run" activeRunStatus="suspended" runtimePhase="awaiting_approval"
    isWaiting={false} isStreaming={false} />);
  expect(screen.getByTestId('active-run-cell').textContent?.match(/Waiting for you/g)).toHaveLength(1);
});

it.each(['awaiting_approval', 'awaiting_budget', 'idle'] as const)(
  'keeps Stop available for the owned suspended run after stream flags clear: %s', (phase) => {
    const base = props('', false, 'original-work');
    const stop = vi.fn();
    render(<AgentChatSection {...base} agent={{ ...base.agent, access_level: 'use' }}
      currentUser={{ id: 'operator-user', tenant_id: 'company' }}
      activeSession={{ ...base.activeSession, user_id: 'operator-user', read_only: false,
        is_current_user_session: true, operator_view: false }}
      historyMsgs={[]} activeRunId="owned-run" activeRunStatus="suspended" runtimePhase={phase}
      isStreaming={false} isWaiting={false} onAbortGeneration={stop} />);
    const control = screen.getByTestId('session-composer-stop');
    expect(control).toHaveProperty('disabled', false);
    fireEvent.click(control);
    expect(stop).toHaveBeenCalledTimes(1);
  },
);

it.each(['read_only', 'non_cancellable', 'missing_run', 'terminal'])(
  'does not offer an executable Stop outside the current cancellable run: %s', (state) => {
    const base = props('', false, 'original-work');
    const stop = vi.fn();
    render(<AgentChatSection {...base} agent={{ ...base.agent, access_level: state === 'read_only' ? 'read' : 'use' }}
      currentUser={{ id: 'operator-user', tenant_id: 'company' }}
      activeSession={{ ...base.activeSession, user_id: 'operator-user', read_only: state === 'read_only',
        is_current_user_session: true, operator_view: false }}
      historyMsgs={[]} activeRunId={state === 'missing_run' ? null : 'owned-run'}
      activeRunStatus={state === 'terminal' ? 'killed' : 'suspended'}
      runtimePhase={state === 'terminal' ? 'cancelled' : 'awaiting_approval'}
      canAbortGeneration={state !== 'non_cancellable'} isStreaming={false} isWaiting={false} onAbortGeneration={stop} />);
    const control = screen.queryByTestId('session-composer-stop');
    if (state === 'non_cancellable') {
      expect(control).toHaveProperty('disabled', true);
      fireEvent.click(control!);
    } else {
      expect(control).toBeNull();
    }
    expect(stop).not.toHaveBeenCalled();
  },
);

it('keeps selected project inputs and the same input identifier after an unconfirmed original-work send', async () => {
  const base = props('', false, 'original-work');
  vi.spyOn(projectsApi, 'list').mockResolvedValue({ projects: [{ id: 'project', title: 'Research', revision: 1,
    owner_user_id: 'operator-user', archived: false, can_manage: true, can_write: true }], next_offset: null });
  vi.spyOn(projectsApi, 'read').mockResolvedValue({ id: 'project', title: 'Research', revision: 1,
    owner_user_id: 'operator-user', archived: false, can_manage: true, can_write: true, next_offset: null,
    entries: [{ id: 'source', target_kind: 'workspace_file', section: '', removed: false, state: 'linked', title: 'source.md' }] });
  const send = vi.fn().mockResolvedValueOnce(undefined).mockResolvedValueOnce(true);
  useAuthStore.setState({ user: { id: 'operator-user', tenant_id: 'company' } as any });
  render(<AgentChatSection {...base} agent={{ ...base.agent, access_level: 'use' }} currentUser={{ id: 'operator-user', tenant_id: 'company' }}
    activeSession={{ ...base.activeSession, user_id: 'operator-user', read_only: false, is_current_user_session: true, operator_view: false }}
    historyMsgs={[]} chatInput="Use the selected sources" onSendChatMsg={send} planModeRequested={true} goalModeRequested={true} />);
  // Project references now open from the composer "+" menu (P3). Remote A2A
  // task operations live behind the same menu, on demand (7.3 L2): opening and
  // collapsing the panel must not disturb the composer's pending input.
  fireEvent.click(screen.getByRole('button', { name: 'Open composer actions' }));
  fireEvent.click(screen.getByRole('menuitem', { name: /Remote tasks/ }));
  expect(screen.getByText('Remote A2A requests in this work')).toBeTruthy();
  // jsdom does not toggle <details> from <summary> clicks; simulate a collapse.
  const remotePanel = screen.getByText('Remote A2A requests in this work').closest('details')!;
  await act(async () => { remotePanel.open = false; remotePanel.dispatchEvent(new Event('toggle')); });
  // Closing only hides the panel; the same mounted instance reopens from the
  // menu, so an unconfirmed request keeps its exact identity.
  await waitFor(() => expect(remotePanel.hidden).toBe(true));
  fireEvent.click(screen.getByRole('button', { name: 'Open composer actions' }));
  fireEvent.click(screen.getByRole('menuitem', { name: /Remote tasks/ }));
  await waitFor(() => expect(remotePanel.hidden).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'Open composer actions' }));
  fireEvent.click(screen.getByRole('menuitem', { name: /Project files/ }));
  await waitFor(() => expect(screen.getByRole('option', { name: 'Research' })).toBeTruthy());
  fireEvent.change(screen.getByRole('combobox', { name: 'projects.contextProject' }), { target: { value: 'project' } });
  fireEvent.click(await screen.findByRole('checkbox', { name: 'source.md' }));
  fireEvent.click(screen.getByRole('button', { name: 'chat.send' }));
  await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(screen.getByText('projects.contextSendUnconfirmed')).toBeTruthy());
  expect((screen.getByRole('checkbox', { name: 'source.md' }) as HTMLInputElement).checked).toBe(true);
  const original = send.mock.calls[0][0];
  expect(original).toMatchObject({ expectedSessionId: 'original-work', parts: [{ type: 'project_ref', project_id: 'project', entry_ids: ['source'] }] });
  fireEvent.click(screen.getByRole('button', { name: 'chat.send' }));
  await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
  expect(send.mock.calls[1][0].inputId).toBe(original.inputId);
  await waitFor(() => expect((screen.getByRole('checkbox', { name: 'source.md' }) as HTMLInputElement).checked).toBe(false));
});

it.each(['owner', 'other_user', 'operator', 'read_grant'])('answers a delegated question only as its executing owner: %s', async (mode) => {
  const base = props('Audit', mode === 'operator', 'delegated-session');
  const answer = vi.fn().mockResolvedValue({ status: 'queued' });
  const message = { id: 'question-event', transcriptEventId: 'question-item', role: 'tool_call' as const, content: '',
    toolName: 'ask_user_question', toolMeta: { kind: 'user_clarification' as const, blocking: true, nextAction: null,
      questions: [{ header: 'Audience', question: 'Which audience?', multiSelect: false,
        options: [{ label: 'Team', description: 'Prepare the team report' }] }] } };
  render(<AgentChatSection {...base} agent={{ ...base.agent, access_level: mode === 'read_grant' ? 'read' : 'use' }}
    activeSession={{ ...base.activeSession, session_kind: 'delegation_run', user_id: 'operator-user',
      operator_view: mode === 'operator' }} currentUser={{ id: mode === 'other_user' ? 'other-user' : 'operator-user' }}
    historyMsgs={[message]} onAnswerQuestion={answer} />);
  expect(screen.getByText('Which audience?')).toBeTruthy();
  const submit = screen.queryByRole('button', { name: 'Submit answer' });
  if (mode !== 'owner') {
    expect(submit).toBeNull();
    expect(answer).not.toHaveBeenCalled();
    return;
  }
  expect(submit).not.toBeNull();
  fireEvent.click(screen.getByRole('radio', { name: /Team/ }));
  fireEvent.click(submit!);
  await waitFor(() => expect(answer).toHaveBeenCalledWith('question-item', 'Audience: Team'));
  expect(screen.queryByRole('textbox', { name: /message/i })).toBeNull();
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

const operatorThreadItem = {
  schema: 'hive.thread_item.v1',
  schema_version: 1,
  id: 'private-thread-item',
  sequence: 7,
  item_type: 'error',
  item_status: 'failed',
  actor_type: 'system',
  event_type: 'runtime_failure',
  type: 'runtime_failure',
  role: 'system',
  visibility_scope: 'operator',
  listed_surface: 'chat',
  content: 'Private runtime failure',
  parts: [],
  metadata: {},
  evidence_refs: [{ kind: 'transcript_event', id: 'private-thread-item' }],
  item_data: { code: 'private_failure', retryable: false },
  audience: 'operator',
  user_summary: 'Private runtime failure',
  user_action: null,
  operator_details: {
    item_data: { code: 'private_failure' },
    metadata: { private_marker: 'operator-only' },
    evidence_refs: [{ kind: 'transcript_event', id: 'private-thread-item' }],
    links: { session_id: 'operator-session' },
  },
};

function props(operatorReason: string, isAdmin = true, sessionId = 'operator-session') {
  return {
    agentId: 'agent-1',
    agent: { id: 'agent-1', name: 'Audited Agent', access_level: 'operator' },
    currentUser: { id: 'operator-user' },
    isAdmin,
    operatorReason,
    chatScope: 'all' as const,
    onSetChatScope: vi.fn(),
    onLoadAllSessions: vi.fn(),
    onCreateNewSession: vi.fn(),
    sessionsLoading: false,
    sessions: [],
    activeSession: {
      id: sessionId,
      agent_id: 'agent-1',
      user_id: 'private-owner',
      username: 'Private owner',
      title: 'Private session',
      read_only: true,
      is_current_user_session: false,
      operator_view: true,
    },
    branchLineage: [],
    branchLineageLoading: false,
    onSelectBranchSession: vi.fn(),
    wsConnected: false,
    allSessions: [],
    allSessionsLoading: false,
    allUserFilter: '',
    onSetAllUserFilter: vi.fn(),
    onSelectSession: vi.fn(),
    onDeleteSession: vi.fn(),
    historyContainerRef: React.createRef<HTMLDivElement>(),
    onHistoryScroll: vi.fn(),
    historyMsgs: [
      {
        id: 'private-artifact-message',
        role: 'assistant',
        content: 'Private delivered file',
        artifacts: [{
          id: 'private-artifact',
          name: 'private-report.md',
          path: 'workspace/private-report.md',
          previewKind: 'markdown',
          source: 'workspace_write',
        }],
      },
      {
        id: 'private-event-message',
        role: 'event',
        content: 'Private runtime failure',
        eventType: 'runtime_failure',
        eventStatus: 'failed',
        threadItem: operatorThreadItem,
      },
    ],
    historyMessagesSessionId: sessionId,
    showHistoryScrollBtn: false,
    onScrollHistoryToBottom: vi.fn(),
    chatContainerRef: React.createRef<HTMLDivElement>(),
    onChatScroll: vi.fn(),
    chatMessages: [],
    chatMessagesSessionId: null,
    runtimeSummary: null,
    transportNotice: null,
    isWaiting: false,
    chatEndRef: React.createRef<HTMLDivElement>(),
    showScrollBtn: false,
    onScrollToBottom: vi.fn(),
    agentExpired: false,
    attachedFiles: [],
    onRemoveAttachedFile: vi.fn(),
    fileInputRef: React.createRef<HTMLInputElement>(),
    onHandleChatFile: vi.fn(),
    uploading: false,
    uploadProgress: -1,
    uploadAbortRef: { current: null },
    chatInputRef: React.createRef<HTMLTextAreaElement>(),
    chatInput: '',
    onSetChatInput: vi.fn(),
    onHandlePaste: vi.fn(),
    onSendChatMsg: vi.fn(),
    isStreaming: false,
    onAbortGeneration: vi.fn(),
  };
}

function imageProps(operatorReason: string) {
  const base = props(operatorReason);
  return {
    ...base,
    historyMsgs: [
      {
        ...base.historyMsgs[0],
        artifacts: [{
          id: 'private-image-artifact',
          name: `private-${operatorReason}.png`,
          path: `workspace/private-${operatorReason}.png`,
          previewKind: 'image',
          source: 'workspace_write',
        }],
      },
      base.historyMsgs[1],
    ],
  };
}

function downloadProps(operatorReason: string) {
  const base = props(operatorReason);
  return {
    ...base,
    historyMsgs: [
      {
        ...base.historyMsgs[0],
        artifacts: [{
          id: 'private-download-artifact',
          name: `private-${operatorReason}.zip`,
          path: `workspace/private-${operatorReason}.zip`,
          previewKind: 'download',
          source: 'workspace_write',
        }],
      },
      base.historyMsgs[1],
    ],
  };
}

beforeEach(() => {
  harness.ledger = undefined;
  harness.activeRun = undefined;
  harness.ledgerQuery = undefined;
  noticeLocale.language = 'en';
  harness.readArtifact.mockReset();
  harness.readArtifact.mockResolvedValue({ content: 'PRIVATE ARTIFACT CONTENT' });
  harness.downloadArtifact.mockReset();
  harness.createObjectURL.mockReset();
  harness.createObjectURL.mockReturnValue('blob:preview');
  harness.revokeObjectURL.mockReset();
  harness.saveBlob.mockReset();
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true,
    value: harness.createObjectURL,
  });
  Object.defineProperty(URL, 'revokeObjectURL', {
    configurable: true,
    value: harness.revokeObjectURL,
  });
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockImplementation(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    value: vi.fn(),
  });
});

afterEach(cleanup);

it.each([
  { item: 'ordinary employee with the reserved name', name: '__system_hr__', agent_class: 'internal_tenant', label: '__system_hr__' },
  { item: 'system HR employee', name: '__system_hr__', agent_class: 'internal_system', label: 'HR Agent' },
  { item: 'system employee without the HR name', name: 'System helper', agent_class: 'internal_system', label: 'System helper' },
])('G3f round 3 item 3: labels $item by its actual system identity', ({ name, agent_class, label }) => {
  const base = props('Audit employee conversations');
  const { container } = render(<AgentChatSection {...base} agent={{ ...base.agent, name, agent_class }} historyMsgs={[]} />);
  expect(container.querySelector('.detail-session-browser-subtitle')?.textContent).toBe(label);
});

function noticeProps() {
  const base = props('', false, 'original-work');
  return { ...base, agent: { ...base.agent, access_level: 'use' },
    activeSession: { ...base.activeSession, user_id: 'operator-user', read_only: false,
      is_current_user_session: true, operator_view: false },
    historyMsgs: [], chatMessagesSessionId: 'original-work' };
}

it('uses the settled transcript status for the todo after the active run clears', () => {
  harness.ledger = {
    schema: 'agent_work_ledger_view.v1', runtime_task_id: 'stopped-run', status: 'in_progress',
    todo_items: [{ id: '1', title: 'Write the report', activeForm: 'Writing the report', status: 'in_progress', required: true }],
    counts: { todos_total: 1, todos_complete: 0, todos_open: 1 },
  };
  render(<AgentChatSection {...noticeProps()} activeRunId={null} activeRunStatus={null} chatMessages={[
    { id: 'input', role: 'user', content: 'Write the report', timestamp: '2026-11-07T00:00:00Z' },
    { id: 'stopped', role: 'event', content: '', timestamp: '2026-11-07T00:00:01Z', sessionItem: {
      id: 'stopped-run', terminal: true, revision: 1, content: '', payload: {}, actor: { type: 'runtime' },
      visibility: { audience: 'direct_user' }, occurredAt: '2026-11-07T00:00:01Z', first_sequence: 1, last_sequence: 1,
      kind: 'run', lifecycle: 'cancelled', scope: { level: 'run', session_id: 'original-work', thread_id: 'original-work',
        turn_id: 'stopped-turn', run_id: 'stopped-run' },
    } },
  ]} />);
  expect(screen.getByTestId('chat-work-ledger-summary').textContent).toContain('Stopped');
  expect(screen.queryByText('Writing the report')).toBeNull();
});

it('stops the todo with the header while the cached workbench run is still running', () => {
  noticeLocale.language = 'zh';
  harness.activeRun = { id: 'stopped-run', status: 'running' };
  harness.ledger = {
    schema: 'agent_work_ledger_view.v1', runtime_task_id: 'stopped-run', status: 'in_progress',
    todo_items: [{ id: '1', title: '委派行业综述初稿撰写', activeForm: '正在委派行业综述初稿撰写',
      status: 'in_progress', required: true }],
    counts: { todos_total: 1, todos_complete: 0, todos_open: 1 },
  };
  render(<AgentChatSection {...noticeProps()} activeRunId={null} activeRunStatus={null} chatMessages={[
    { id: 'input', role: 'user', content: 'Write the report', timestamp: '2026-11-07T00:00:00Z' },
    { id: 'stopped', role: 'event', content: '', timestamp: '2026-11-07T00:00:01Z', sessionItem: {
      id: 'stopped-run', terminal: true, revision: 1, content: '', payload: {}, actor: { type: 'runtime' },
      visibility: { audience: 'direct_user' }, occurredAt: '2026-11-07T00:00:01Z', first_sequence: 1, last_sequence: 1,
      kind: 'run', lifecycle: 'cancelled', scope: { level: 'run', session_id: 'original-work', thread_id: 'original-work',
        turn_id: 'stopped-turn', run_id: 'stopped-run' },
    } },
  ]} />);
  expect(screen.getByTestId('session-workbench-header').textContent).toContain('已停止');
  expect(screen.getByTestId('chat-work-ledger-summary').textContent).toContain('已停止');
  expect(screen.getByTestId('chat-work-ledger-dock').textContent).not.toContain('正在');
});

it('keeps the todo live when an empty transcript leaves the header idle', () => {
  harness.activeRun = { id: 'current-run', status: 'running' };
  harness.ledger = {
    schema: 'agent_work_ledger_view.v1', runtime_task_id: 'current-run', status: 'in_progress',
    todo_items: [{ id: '1', title: 'Write the report', activeForm: 'Writing the report',
      status: 'in_progress', required: true }],
    counts: { todos_total: 1, todos_complete: 0, todos_open: 1 },
  };
  render(<AgentChatSection {...noticeProps()} activeRunId={null} activeRunStatus={null} chatMessages={[]} />);
  expect(screen.getByTestId('chat-work-ledger-dock').classList.contains('is-live')).toBe(true);
  expect(screen.getByText('Writing the report...')).toBeTruthy();
  expect(harness.ledgerQuery?.refetchInterval).toBe(5000);
});

it.each(['cancelled', 'failed', 'completed'] as const)(
  'keeps a different unfinished run live after a previous %s run', (lifecycle) => {
    harness.activeRun = { id: 'current-run', status: 'running' };
    harness.ledger = {
      schema: 'agent_work_ledger_view.v1', runtime_task_id: 'current-run', status: 'in_progress',
      todo_items: [{ id: '1', title: 'Write the report', activeForm: 'Writing the report',
        status: 'in_progress', required: true }],
      counts: { todos_total: 1, todos_complete: 0, todos_open: 1 },
    };
    render(<AgentChatSection {...noticeProps()} activeRunId={null} activeRunStatus={null} chatMessages={[
      { id: 'previous-input', role: 'user', content: 'Previous task', timestamp: '2026-11-07T00:00:00Z' },
      { id: 'previous-terminal', role: 'event', content: '', timestamp: '2026-11-07T00:00:01Z', sessionItem: {
        id: 'previous-run', terminal: true, revision: 1, content: '', payload: {}, actor: { type: 'runtime' },
        visibility: { audience: 'direct_user' }, occurredAt: '2026-11-07T00:00:01Z', first_sequence: 1, last_sequence: 1,
        kind: 'run', lifecycle, scope: { level: 'run', session_id: 'original-work', thread_id: 'original-work',
          turn_id: 'previous-turn', run_id: 'previous-run' },
      } },
    ]} />);
    expect(screen.getByTestId('session-workbench-header').textContent).toContain('Running');
    expect(screen.getByTestId('chat-work-ledger-dock').classList.contains('is-live')).toBe(true);
    expect(screen.getByText('Writing the report...')).toBeTruthy();
    expect(harness.ledgerQuery?.refetchInterval).toBe(5000);
  },
);

it.each([
  ['en', 'Hidden key', 'The key you pasted was hidden. It will not be saved or sent to the model.'],
  ['zh', '已隐藏的密钥', '已隐藏你粘贴的密钥，不会保存，也不会发给模型。'],
])('shows the localized notice and inline marks in a member bubble, preserving copy: %s', async (language, mark, notice) => {
  noticeLocale.language = language;
  const text = 'Before [REDACTED_SECRET]\nAfter [REDACTED_SECRET]';
  const message = { id: 'pasted', role: 'user' as const, content: text, pastedCredentialNotice: true };
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
  const { container } = render(<AgentChatSection {...noticeProps()} chatMessages={[message]} />);
  expect(screen.getByText(notice)).toBeTruthy();
  expect(screen.getAllByText(mark)).toHaveLength(2);
  expect(container.querySelector('.session-tui-message-bubble')?.textContent).not.toContain('[REDACTED_SECRET]');
  await act(async () => { fireEvent.click(screen.getByTitle('Copy')); });
  expect(writeText).toHaveBeenCalledWith(text);
  expect(message.content).toBe(text);
});

it('marks a placeholder in an unflagged member bubble without adding the sentence', () => {
  const text = 'Use [REDACTED_SECRET] safely';
  const { container } = render(<AgentChatSection {...noticeProps()}
    chatMessages={[{ id: 'echoed', role: 'user', content: text }]} />);
  expect(container.querySelector('.session-tui-message-bubble')?.textContent).toBe('Use Hidden key safely');
  expect(container.querySelector('.hidden-credential-mark')).toBeTruthy();
  expect(container.querySelector('.pasted-credential-notice')).toBeNull();
});

it.each(['user', 'assistant'] as const)('leaves unflagged %s content unchanged without detecting keys', role => {
  const text = 'Ordinary sk-proj-abcdefghijklmnopqrstuvwxyz1234567890ABCD';
  const { container } = render(<AgentChatSection {...noticeProps()}
    chatMessages={[{ id: 'ordinary', role, content: text }]} />);
  expect(container.querySelector('.session-tui-message-bubble')?.textContent).toContain(text);
  expect(container.querySelector('.pasted-credential-notice')).toBeNull();
  expect(container.querySelector('.hidden-credential-mark')).toBeNull();
});

describe('AgentChatSection operator child-state lifetime', () => {
  it('removes artifact, checkpoint, workflow, and technical inspectors when authority identity changes', async () => {
    useAuthStore.setState({ user: { id: 'operator-user', tenant_id: 'company', role: 'org_admin' } as any });
    const view = render(React.createElement(AgentChatSection as any, props('Reason A')));

    fireEvent.click(screen.getByTestId('chat-artifact-row-open'));
    expect(await screen.findByTestId('session-artifact-inspector')).toBeTruthy();
    expect(await screen.findByText('PRIVATE ARTIFACT CONTENT')).toBeTruthy();

    fireEvent.click(screen.getByTestId('session-gitline-checkpoint'));
    expect(document.querySelector('.session-gitline-node.is-focused')).toBeTruthy();

    fireEvent.click(screen.getByTestId('thread-item-technical-details'));
    expect(await screen.findByTestId('thread-item-inspector')).toBeTruthy();

    fireEvent.click(screen.getByTestId('session-runtime-workflow-open'));
    expect(screen.getByTestId('session-workflow-run-window')).toBeTruthy();

    view.rerender(React.createElement(AgentChatSection as any, props('Reason B')));
    expect(screen.queryByTestId('session-artifact-inspector')).toBeNull();
    await waitFor(() => {
      expect(screen.queryByTestId('session-artifact-inspector')).toBeNull();
      expect(document.querySelector('.session-gitline-node.is-focused')).toBeNull();
      expect(screen.queryByTestId('thread-item-inspector')).toBeNull();
      expect(screen.queryByTestId('session-workflow-run-window')).toBeNull();
    });

    fireEvent.click(screen.getByTestId('chat-artifact-row-open'));
    expect(await screen.findByTestId('session-artifact-inspector')).toBeTruthy();
    view.rerender(React.createElement(AgentChatSection as any, props('Reason B', true, 'operator-session-2')));
    await waitFor(() => expect(screen.queryByTestId('session-artifact-inspector')).toBeNull());

    fireEvent.click(screen.getByTestId('chat-artifact-row-open'));
    expect(await screen.findByTestId('session-artifact-inspector')).toBeTruthy();
    view.rerender(React.createElement(AgentChatSection as any, props('Reason B', false, 'operator-session-2')));
    await waitFor(() => {
      expect(screen.queryByTestId('session-artifact-inspector')).toBeNull();
      expect(screen.queryByTestId('session-operator-view')).toBeNull();
      expect(screen.queryByText('Private delivered file')).toBeNull();
    });
  });

  it('drops a stale artifact response after authority changes and only renders the new authority response', async () => {
    const stale = deferred<Blob>();
    const fresh = deferred<Blob>();
    harness.downloadArtifact
      .mockReturnValueOnce(stale.promise)
      .mockReturnValueOnce(fresh.promise);
    harness.createObjectURL
      .mockReturnValueOnce('blob:stale-private')
      .mockReturnValueOnce('blob:fresh-private');

    const view = render(React.createElement(AgentChatSection as any, imageProps('Reason A')));
    fireEvent.click(screen.getByTestId('chat-artifact-row-open'));
    expect(harness.downloadArtifact).toHaveBeenCalledTimes(1);

    view.rerender(React.createElement(AgentChatSection as any, imageProps('Reason B')));
    await waitFor(() => expect(screen.queryByTestId('session-artifact-inspector')).toBeNull());

    await act(async () => {
      stale.resolve(new Blob(['PRIVATE REASON A BYTES'], { type: 'image/png' }));
      await stale.promise;
    });
    expect(screen.queryByTestId('session-artifact-inspector')).toBeNull();
    expect(document.querySelector('img[src="blob:stale-private"]')).toBeNull();
    expect(harness.revokeObjectURL).toHaveBeenCalledWith('blob:stale-private');

    fireEvent.click(screen.getByTestId('chat-artifact-row-open'));
    await act(async () => {
      fresh.resolve(new Blob(['PRIVATE REASON B BYTES'], { type: 'image/png' }));
      await fresh.promise;
    });

    expect(await screen.findByTestId('session-artifact-inspector')).toBeTruthy();
    const freshImage = screen.getByAltText('private-Reason B.png') as HTMLImageElement;
    expect(freshImage.getAttribute('src')).toBe('blob:fresh-private');
    expect(harness.downloadArtifact).toHaveBeenNthCalledWith(
      1,
      'agent-1',
      'private-image-artifact',
      { operatorView: true, reason: 'Reason A' },
    );
    expect(harness.downloadArtifact).toHaveBeenNthCalledWith(
      2,
      'agent-1',
      'private-image-artifact',
      { operatorView: true, reason: 'Reason B' },
    );
  });

  it('drops a stale operator download after authority changes and only saves the current response', async () => {
    const stale = deferred<Blob>();
    const fresh = deferred<Blob>();
    const staleBlob = new Blob(['PRIVATE REASON A DOWNLOAD']);
    const freshBlob = new Blob(['PRIVATE REASON B DOWNLOAD']);
    harness.downloadArtifact
      .mockReturnValueOnce(stale.promise)
      .mockReturnValueOnce(fresh.promise);

    const view = render(React.createElement(AgentChatSection as any, downloadProps('Reason A')));
    fireEvent.click(screen.getByTestId('chat-artifact-row-open'));
    expect(harness.downloadArtifact).toHaveBeenCalledTimes(1);

    view.rerender(React.createElement(AgentChatSection as any, downloadProps('Reason B')));
    expect(screen.queryByTestId('session-artifact-inspector')).toBeNull();
    await act(async () => {
      stale.resolve(staleBlob);
      await stale.promise;
    });
    expect(harness.saveBlob).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId('chat-artifact-row-open'));
    await act(async () => {
      fresh.resolve(freshBlob);
      await fresh.promise;
    });
    expect(harness.saveBlob).toHaveBeenCalledTimes(1);
    expect(harness.saveBlob).toHaveBeenCalledWith(freshBlob, 'private-Reason B.zip');
    expect(harness.downloadArtifact).toHaveBeenNthCalledWith(
      1,
      'agent-1',
      'private-download-artifact',
      { operatorView: true, reason: 'Reason A' },
    );
    expect(harness.downloadArtifact).toHaveBeenNthCalledWith(
      2,
      'agent-1',
      'private-download-artifact',
      { operatorView: true, reason: 'Reason B' },
    );
  });
});

it('keeps original history visible but disables a cached writable composer during retirement', async () => {
  const base = props('Audit', false, 'retirement-session');
  const send = vi.fn();
  useAuthStore.setState({ user: { id: 'operator-user', tenant_id: 'company' } as any });
  render(<AgentChatSection {...base} agent={{ ...base.agent, access_level: 'read', deactivation_reason: 'retirement_pending', action_capabilities: { can_use: false } }} activeSession={{ ...base.activeSession, user_id: 'operator-user', read_only: false, operator_view: false, is_current_user_session: true }}
    currentUser={{ id: 'operator-user' }} historyMsgs={[{ id: 'retained-result', role: 'assistant', content: 'Retained original result' }]}
    chatMessagesSessionId="retirement-session" chatMessages={[{ id: 'retained-result', role: 'assistant', content: 'Retained original result' }]}
    onSendMessage={send} />);
  expect(screen.getByText('Retained original result')).toBeTruthy();
  const composer = screen.queryByTestId('session-composer-shell');
  expect(!composer || composer.querySelector('textarea')?.disabled).toBe(true);
  // Sending is paused, but the retiring employee's remote tasks stay operable
  // on demand from the composer "+" menu (7.3 L2), as before the trim.
  fireEvent.click(screen.getByRole('button', { name: 'Open composer actions' }));
  fireEvent.click(screen.getByRole('menuitem', { name: /Remote tasks/ }));
  expect(screen.getByText('Remote A2A requests in this work')).toBeTruthy();
  expect(send).not.toHaveBeenCalled();
  useAuthStore.setState({ user: null });
});

it.each(['reason', 'unmount'] as const)(
  'drops an obsolete direct artifact-card download after an operator %s change', async (change) => {
    const stale = deferred<Blob>();
    const fresh = deferred<Blob>();
    const staleBlob = new Blob(['PRIVATE REASON A DIRECT DOWNLOAD']);
    const freshBlob = new Blob(['PRIVATE REASON B DIRECT DOWNLOAD']);
    harness.downloadArtifact
      .mockReturnValueOnce(stale.promise)
      .mockReturnValueOnce(fresh.promise);

    const view = render(React.createElement(AgentChatSection as any, downloadProps('Reason A')));
    fireEvent.click(screen.getByRole('button', { name: /^Download$/ }));
    expect(harness.downloadArtifact).toHaveBeenCalledTimes(1);
    expect(harness.downloadArtifact).toHaveBeenNthCalledWith(
      1, 'agent-1', 'private-download-artifact', { operatorView: true, reason: 'Reason A' },
    );

    if (change === 'unmount') {
      view.unmount();
      render(React.createElement(AgentChatSection as any, downloadProps('Reason B')));
    } else {
      view.rerender(React.createElement(AgentChatSection as any, downloadProps('Reason B')));
    }
    await act(async () => {
      stale.resolve(staleBlob);
      await stale.promise;
    });
    expect(harness.saveBlob).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /^Download$/ }));
    await act(async () => {
      fresh.resolve(freshBlob);
      await fresh.promise;
    });
    expect(harness.saveBlob).toHaveBeenCalledTimes(1);
    expect(harness.saveBlob).toHaveBeenCalledWith(freshBlob, 'private-Reason B.zip');
    expect(harness.downloadArtifact).toHaveBeenNthCalledWith(
      2, 'agent-1', 'private-download-artifact', { operatorView: true, reason: 'Reason B' },
    );
  },
);
