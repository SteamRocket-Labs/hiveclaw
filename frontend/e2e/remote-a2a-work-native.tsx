/** Actual original-work UI, owner HTTP, registered worker and governed remote HTTP. */
import { createRef } from 'react';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider } from 'react-i18next';
import i18next from 'i18next';
import { expect, it } from 'vitest';
import AgentChatSection from '../src/pages/agent-detail/AgentChatSection';
import en from '../src/i18n/en.json';
import { useAuthStore } from '../src/stores';
import type { User } from '../src/types';

it('sends and reads a complete remote result in the actual original work', async () => {
  const base = process.env.HIVE_G90_NATIVE_URL || '';
  expect(base.startsWith('http://127.0.0.1:')).toBe(true);
  const original = globalThis.fetch;
  globalThis.fetch = (url, options) => {
    if (typeof url !== 'string' || !url.startsWith('/api/')) throw new Error('Only owned loopback HTTP is permitted');
    return original(new URL(url, base), options);
  };
  Object.defineProperty(window, 'matchMedia', { configurable: true, value: () => ({ matches: false,
    addEventListener() {}, removeEventListener() {} }) });
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: () => {} });
  const language = i18next.createInstance();
  await language.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
  const user = { id: process.env.HIVE_G90_USER_ID!, tenant_id: process.env.HIVE_G90_TENANT_ID!,
    role: 'org_admin', username: 'Remote work owner' } as User;
  const agentId = process.env.HIVE_G90_AGENT_ID!;
  const sessionId = process.env.HIVE_M3O_SESSION_ID!;
  useAuthStore.getState().setAuth(user, process.env.HIVE_M3N_TOKEN || '');
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const noop = () => {};
  const props: any = { agentId, agent: { id: agentId, name: 'Remote work employee', access_level: 'manage' }, currentUser: user,
    isAdmin: true, operatorReason: '', chatScope: 'mine', onSetChatScope: noop, onLoadAllSessions: noop,
    onCreateNewSession: noop, sessionsLoading: false, sessions: [], activeSession: { id: sessionId, agent_id: agentId,
      user_id: user.id, title: 'Original remote work', read_only: false, operator_view: false, is_current_user_session: true },
    branchLineage: [], branchLineageLoading: false, onSelectBranchSession: noop, wsConnected: false, allSessions: [],
    allSessionsLoading: false, allUserFilter: '', onSetAllUserFilter: noop, onSelectSession: noop, onDeleteSession: noop,
    historyContainerRef: createRef(), onHistoryScroll: noop, historyMsgs: [], historyMessagesSessionId: sessionId,
    showHistoryScrollBtn: false, onScrollHistoryToBottom: noop, chatContainerRef: createRef(), onChatScroll: noop,
    chatMessages: [], chatMessagesSessionId: sessionId, runtimeSummary: null, transportNotice: null, isWaiting: false,
    chatEndRef: createRef(), showScrollBtn: false, onScrollToBottom: noop, agentExpired: false, attachedFiles: [],
    onRemoveAttachedFile: noop, fileInputRef: createRef(), onHandleChatFile: noop, uploading: false, uploadProgress: -1,
    uploadAbortRef: { current: null }, chatInputRef: createRef(), chatInput: '', onSetChatInput: noop, onHandlePaste: noop,
    onSendChatMsg: () => { throw new Error('Remote requests must not invoke the native model composer'); },
    isStreaming: false, onAbortGeneration: noop };
  try {
    render(<I18nextProvider i18n={language}><QueryClientProvider client={client}>
      <AgentChatSection {...props} />
    </QueryClientProvider></I18nextProvider>);
    const summary = await screen.findByText('Remote A2A requests in this work');
    const details = summary.closest('details')!;
    fireEvent.click(summary);
    const controls = within(details);
    fireEvent.change(await controls.findByLabelText('Remote request or reply'), { target: { value: 'Prepare the complete remote report.' } });
    fireEvent.click(controls.getByLabelText('I authorize this exact remote request with unverified internal effect controls.'));
    fireEvent.click(controls.getByLabelText('I accept unknown external charges for this request; no hard spend limit is claimed.'));
    fireEvent.click(controls.getByText('Send new remote request'));
    await controls.findByLabelText('Saved remote receipt');
    const barrier = await original(new URL('/__remote-test/wait', base));
    expect(barrier.status).toBe(200);
    fireEvent.click(controls.getByText('Read saved state'));
    const saved = await controls.findByText(/NATIVE_REMOTE_FULL_RESULT/);
    const observation = JSON.parse(saved.textContent || '{}');
    expect(observation.task.artifacts[0].parts[0].text).toBe('NATIVE_REMOTE_FULL_RESULT\n'.repeat(4000));
    expect(controls.getByText('External cost: unknown. Internal action control: unverified.')).toBeTruthy();
  } finally {
    cleanup();client.clear();useAuthStore.getState().logout();globalThis.fetch = original;
  }
});
