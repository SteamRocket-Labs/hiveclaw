// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import rows from '../../test/fixtures/x5fRun23.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { fileApi } from '../../api/domains/files';
import { ArtifactCards, ArtifactPreviewPanel } from './ArtifactSurface';
import { AssistantMessageBody, assistantReplyForDisplay } from './CanonicalCardAssistantSupplement';
import type { AgentChatMessage, ChatTranscriptEventPayload } from './chatRuntime';
import { hydrateSessionTranscriptEvents } from './sessionEventConsumer';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierDeps } from './sessionTranscriptApplier';

const locale = vi.hoisted(() => ({ language: 'zh' as 'zh' | 'en' }));
const t = (key: string, fallback?: string, values?: Record<string, unknown>) =>
  translateFromCatalog(locale.language === 'zh' ? zh : en, key, fallback, values);
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t, i18n: { language: locale.language } }) }));
vi.mock('../../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <pre>{content}</pre> }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); locale.language = 'zh'; });

// Selected run23 api-evidence responses: step2 line124, step2b line209.
// Keep file facts, event shapes and source-block linkage;
// anonymize identities, renumber the selected stream contiguously, and omit
// unrelated bookkeeping and file snapshots. Event ids retain source sequences.
function both(events: unknown[]): AgentChatMessage[][] {
  let live: AgentChatMessage[] = [];
  const deps: SessionTranscriptApplierDeps = {
    refs: { transcriptEvents: {}, eventStores: {}, fullHydrationKeys: new Set(['agent:session']), replayStates: {},
      compatibilityTimelines: {}, visibilityBoundaries: {}, uiStates: {}, runtimeActivityAt: {}, pendingUserMessages: {} },
    getActiveRunId: () => null, markActiveRunTerminal: () => true, isTerminalTranscriptToolMessage: () => false,
    mergePendingMessages: (_key, messages) => messages, setChatMessagesSessionId: vi.fn(),
    enqueueChatMessagesUpdate: (_session, update) => { live = update(live); },
    setChatMessagesAfterQueued: (_session, update) => { live = update(live); },
    setActivePhase: vi.fn(), setIsWaiting: vi.fn(), setIsStreaming: vi.fn(), parseChatMsg: message => message,
  };
  for (const frame of events) applyTranscriptToSessionRuntime(deps, 'agent', 'session', frame as ChatTranscriptEventPayload, true);
  return [live, hydrateSessionTranscriptEvents(events as ChatTranscriptEventPayload[], 0).messages];
}

const artifact = { id: 'old-delivery', name: 'sales-report-sales-run23.md', path: 'workspace/sales-report-sales-run23.md',
  downloadAgentId: 'employee', previewKind: 'markdown' as const };
const preview = { artifact, content: 'Original delivery', usingSnapshot: true };
// Excerpts from run23's original and newest report downloads plus the member
// note appended by step2b. Omit paragraphs unrelated to the changed order.
const before = '## 下一步建议\n\n- 拉取长周期订单复核区域与产品结构\n\n人工补充：华东区数据待核对';
const after = '人工补充：华东区数据待核对\n\n## 下一步建议\n\n- 拉取长周期订单复核区域结构';

it.each(['zh', 'en'] as const)('refreshes the current file at run completion and folds the panel after continuation (%s)', async language => {
  locale.language = language;
  const read = vi.spyOn(fileApi, 'read').mockResolvedValueOnce({ path: artifact.path, content: before, content_hash: 'a'.repeat(64) })
    .mockResolvedValue({ path: artifact.path, content: after, content_hash: 'b'.repeat(64) });
  const send = vi.fn().mockResolvedValue(undefined);
  const props = { preview, agentId: 'employee', onClose: vi.fn(), t: t as never,
    continuation: { sessionId: 'session', send }, messages: [] as AgentChatMessage[] };
  const view = render(<ArtifactPreviewPanel {...props} />);
  fireEvent.click(screen.getByRole('button', { name: language === 'zh' ? '打开当前文件' : 'Open current file' }));
  await screen.findByText((_text, element) => element?.tagName === 'PRE' && element.textContent === before);
  const form = screen.getByRole('textbox', { name: t('agent.chat.artifacts.continueLabel') });
  fireEvent.change(form, { target: { value: '把下一步建议放到末尾' } });
  fireEvent.click(screen.getByRole('button', { name: t('agent.chat.artifacts.continueHere') }));
  await waitFor(() => expect(send).toHaveBeenCalledWith('把下一步建议放到末尾', expect.objectContaining({
    expectedSessionId: 'session', parts: [{ type: 'workspace_file_ref', agent_id: 'employee', path: artifact.path }],
  })));
  const reopen = language === 'zh' ? '查看当前文件' : 'View current file';
  const beforeTerminal = rows.continuation.filter(event => event.item_kind !== 'run' || event.lifecycle !== 'completed');
  view.rerender(<ArtifactPreviewPanel {...props} messages={both(beforeTerminal)[0]} />);
  expect(read).toHaveBeenCalledTimes(1);
  view.rerender(<ArtifactPreviewPanel {...props} messages={both(rows.continuation)[0]} />);
  await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByRole('complementary')).toBeNull());
  expect(screen.getByRole('button', { name: reopen })).toBeTruthy();
  expect(read).toHaveBeenLastCalledWith('employee', artifact.path, undefined);
  fireEvent.click(screen.getByRole('button', { name: reopen }));
  await screen.findByText((_text, element) => element?.tagName === 'PRE' && element.textContent === after);
  view.rerender(<ArtifactPreviewPanel {...props} messages={both(rows.continuation)[1]} />);
  expect(read).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole('button', { name: t('agent.chat.artifacts.backToDelivery') }));
  expect(screen.getByText('Original delivery')).toBeTruthy();
  // Opening a delivery card again must reopen it, including the same path.
  view.rerender(<ArtifactPreviewPanel {...props} preview={{ ...preview }} messages={both(rows.continuation)[1]} />);
  expect(screen.getByRole('complementary')).toBeTruthy();
});

it('refreshes an open file after a run from the conversation composer finishes, using the existing read', async () => {
  const read = vi.spyOn(fileApi, 'read').mockResolvedValueOnce({ path: artifact.path, content: before, content_hash: 'a'.repeat(64) })
    .mockResolvedValue({ path: artifact.path, content: after, content_hash: 'b'.repeat(64) });
  const props = { preview, agentId: 'employee', onClose: vi.fn(), t: t as never, messages: [] as AgentChatMessage[] };
  const view = render(<ArtifactPreviewPanel {...props} />);
  fireEvent.click(screen.getByRole('button', { name: '打开当前文件' }));
  await screen.findByText((_text, element) => element?.tagName === 'PRE' && element.textContent === before);
  view.rerender(<ArtifactPreviewPanel {...props} messages={both(rows.continuation)[0]} />);
  await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  expect(screen.queryByRole('complementary')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '查看当前文件' }));
  await screen.findByText((_text, element) => element?.tagName === 'PRE' && element.textContent === after);
});

it('does not refresh an unrelated file or discard unsaved current-file edits', async () => {
  const read = vi.spyOn(fileApi, 'read').mockResolvedValue({ path: artifact.path, content: before, content_hash: 'a'.repeat(64) });
  const props = { preview, agentId: 'employee', onClose: vi.fn(), t: t as never, messages: [] as AgentChatMessage[] };
  const view = render(<ArtifactPreviewPanel {...props} />);
  fireEvent.click(screen.getByRole('button', { name: '打开当前文件' }));
  await screen.findByText((_text, element) => element?.tagName === 'PRE' && element.textContent === before);
  const unrelated = both(rows.continuation)[1].map(message => ({ ...message,
    artifacts: message.artifacts?.map(artifact => ({ ...artifact, path: 'workspace/another-report.md' })),
  }));
  view.rerender(<ArtifactPreviewPanel {...props} messages={unrelated} />);
  expect(read).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: '编辑' }));
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Unsaved human draft' } });
  view.rerender(<ArtifactPreviewPanel {...props} messages={both(rows.continuation)[1]} />);
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Unsaved human draft');
  expect(read).toHaveBeenCalledTimes(1);
});

it.each(['zh', 'en'] as const)('keeps the delivery card without its empty download label (%s)', language => {
  locale.language = language;
  for (const messages of both(rows.delivery)) {
    const reply = messages.find(message => message.role === 'assistant' && message.sessionItem?.kind === 'assistant_final')!;
    const content = language === 'zh' ? reply.content : reply.content.replace('下载链接：', 'Download link:');
    const view = render(<><AssistantMessageBody content={content} streaming={false} supplemental={false} supplementalLabel="" />
      <ArtifactCards agentId="employee" artifacts={reply.artifacts} /></>);
    expect(view.container.textContent).not.toContain(language === 'zh' ? '下载链接：' : 'Download link:');
    expect(view.container.textContent).not.toContain('DELIVERABLE');
    expect(within(view.getByTestId('chat-artifacts-card')).getByText(artifact.name)).toBeTruthy();
    const prefix = content.slice(0, content.indexOf(language === 'zh' ? '下载链接：' : 'Download link:'));
    expect(assistantReplyForDisplay(content)).toContain(prefix);
    view.unmount();
  }
});

it('keeps populated download links and labels unrelated to a deliverable marker', () => {
  const content = '下载链接：\n[report](https://example.test/report)\n\nDELIVERABLE: workspace/report.md';
  expect(assistantReplyForDisplay(content)).toContain('下载链接：\n[report](https://example.test/report)');
  expect(assistantReplyForDisplay('下载链接：')).toBe('下载链接：');
});
