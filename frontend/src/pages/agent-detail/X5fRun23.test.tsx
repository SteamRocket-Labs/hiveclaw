// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import rows from '../../test/fixtures/x5fRun23.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { fileApi } from '../../api/domains/files';
import { ArtifactCards, ArtifactPreviewPanel } from './ArtifactSurface';
import { AssistantMessageBody, assistantReplyForDisplay } from './CanonicalCardAssistantSupplement';
import type { AgentChatMessage, ChatArtifactPart, ChatTranscriptEventPayload } from './chatRuntime';
import type { ArtifactContinuationContext } from './ArtifactContinuation';
import { useArtifactPreview } from './useArtifactPreview';
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

const projections = (['zh', 'en'] as const).flatMap(language =>
  (['live', 'reload'] as const).map((mode, index) => ({ language, mode, index })));
const passRead = <T,>(request: Promise<T>) => request;
function PreviewHarness({ messages, onClose, continuation, onDownload }: {
  messages: AgentChatMessage[];
  onClose: () => void;
  continuation?: ArtifactContinuationContext;
  onDownload?: (artifact: ChatArtifactPart) => void;
}) {
  const { artifactPreview, closeArtifactPreview, openArtifact } = useArtifactPreview({
    authorityIdentity: 'employee:session', effectiveAgentId: 'employee', guardOperatorRead: passRead, operatorView: false,
  });
  const final = messages.find(message => message.sessionItem?.kind === 'assistant_final');
  return <>
    <ArtifactCards agentId="employee" artifacts={[artifact, ...(final?.artifacts || [])]}
      onOpenArtifact={openArtifact} onDownloadArtifact={onDownload} />
    {artifactPreview && <ArtifactPreviewPanel preview={artifactPreview} agentId="employee" messages={messages}
      onClose={() => { onClose(); closeArtifactPreview(); }} continuation={continuation} t={t as never} />}
  </>;
}

it.each(projections)('closes after continuation and reopens the workspace through the existing read ($language/$mode)', async ({ language, index }) => {
  locale.language = language;
  const read = vi.spyOn(fileApi, 'read').mockResolvedValueOnce({ path: artifact.path, content: before, content_hash: 'a'.repeat(64) })
    .mockResolvedValue({ path: artifact.path, content: after, content_hash: 'b'.repeat(64) });
  const readArtifact = vi.spyOn(fileApi, 'readArtifact').mockResolvedValue({ path: artifact.path, content: preview.content, uses_snapshot: true });
  const send = vi.fn().mockResolvedValue(undefined);
  const props = { onClose: vi.fn(), onDownload: vi.fn(), continuation: { sessionId: 'session', send } };
  const view = render(<PreviewHarness {...props} messages={[]} />);
  fireEvent.click(screen.getByTestId('chat-artifact-row-open'));
  await screen.findByText(preview.content);
  fireEvent.click(screen.getByRole('button', { name: t('agent.chat.artifacts.openCurrentFile') }));
  await screen.findByText((_text, element) => element?.tagName === 'PRE' && element.textContent === before);
  const form = screen.getByRole('textbox', { name: t('agent.chat.artifacts.continueLabel') });
  fireEvent.change(form, { target: { value: '把下一步建议放到末尾' } });
  fireEvent.click(screen.getByRole('button', { name: t('agent.chat.artifacts.continueHere') }));
  await waitFor(() => expect(send).toHaveBeenCalledWith('把下一步建议放到末尾', expect.objectContaining({
    expectedSessionId: 'session', parts: [{ type: 'workspace_file_ref', agent_id: 'employee', path: artifact.path }],
  })));
  const beforeTerminal = rows.continuation.filter(event => event.item_kind !== 'run' || event.lifecycle !== 'completed');
  view.rerender(<PreviewHarness {...props} messages={both(beforeTerminal)[index]} />);
  expect(read).toHaveBeenCalledTimes(1);
  expect(props.onClose).not.toHaveBeenCalled();
  const messages = both(rows.continuation)[index];
  view.rerender(<PreviewHarness {...props} messages={messages} />);
  await waitFor(() => expect(props.onClose).toHaveBeenCalledTimes(1));
  expect(screen.queryByTestId('session-artifact-preview-inspector')).toBeNull();
  expect(screen.getAllByTestId('chat-artifact-row-open')).toHaveLength(2);
  fireEvent.click(screen.getAllByRole('button', { name: t('agent.chat.artifacts.download') }).at(-1)!);
  expect(props.onDownload).toHaveBeenCalledWith(messages.find(message => message.sessionItem?.kind === 'assistant_final')!.artifacts![0]);
  fireEvent.click(screen.getAllByTestId('chat-artifact-row-open').at(-1)!);
  await screen.findByText(preview.content);
  const snapshotReads = readArtifact.mock.calls.length;
  fireEvent.click(screen.getByRole('button', { name: t('agent.chat.artifacts.openCurrentFile') }));
  await screen.findByText((_text, element) => element?.tagName === 'PRE' && element.textContent === after);
  expect(read).toHaveBeenCalledTimes(2);
  expect(read).toHaveBeenLastCalledWith('employee', artifact.path, undefined);
  expect(readArtifact).toHaveBeenCalledTimes(snapshotReads);
  view.rerender(<PreviewHarness {...props} messages={[...messages]} />);
  expect(props.onClose).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('complementary')).toBeTruthy();
  expect(read).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole('button', { name: t('agent.chat.artifacts.backToDelivery') }));
  expect(screen.getByText('Original delivery')).toBeTruthy();
});

it.each(projections)('closes the snapshot layer without opening the current file ($language/$mode)', async ({ language, index }) => {
  locale.language = language;
  const read = vi.spyOn(fileApi, 'read');
  vi.spyOn(fileApi, 'readArtifact').mockResolvedValue({ path: artifact.path, content: preview.content, uses_snapshot: true });
  const props = { onClose: vi.fn() };
  const view = render(<PreviewHarness {...props} messages={[]} />);
  fireEvent.click(screen.getByTestId('chat-artifact-row-open'));
  await screen.findByText(preview.content);
  view.rerender(<PreviewHarness {...props} messages={both(rows.continuation)[index]} />);
  await waitFor(() => expect(props.onClose).toHaveBeenCalledTimes(1));
  expect(screen.queryByTestId('session-artifact-preview-inspector')).toBeNull();
  expect(read).not.toHaveBeenCalled();
});

it('calls onClose once per new revision, including when its callback changes', () => {
  const first = vi.fn();
  const next = vi.fn();
  const props = { preview, agentId: 'employee', t: t as never };
  const view = render(<ArtifactPreviewPanel {...props} onClose={first} messages={[]} />);
  const messages = both(rows.continuation)[0];
  view.rerender(<ArtifactPreviewPanel {...props} onClose={first} messages={messages} />);
  expect(first).toHaveBeenCalledTimes(1);
  view.rerender(<ArtifactPreviewPanel {...props} onClose={next} messages={[...messages]} />);
  expect(next).not.toHaveBeenCalled();
  const write = messages.find(message => message.artifacts?.some(artifact => artifact.source === 'workspace_write'))!;
  view.rerender(<ArtifactPreviewPanel {...props} onClose={next} messages={[...messages, { ...write, id: `${write.id}-next` }]} />);
  expect(next).toHaveBeenCalledTimes(1);
});

it.each(projections)('preserves an unsaved draft and closes after it clears ($language/$mode)', async ({ language, index }) => {
  locale.language = language;
  const read = vi.spyOn(fileApi, 'read').mockResolvedValue({ path: artifact.path, content: before, content_hash: 'a'.repeat(64) });
  const props = { preview, agentId: 'employee', onClose: vi.fn(), t: t as never, messages: [] as AgentChatMessage[] };
  const view = render(<ArtifactPreviewPanel {...props} />);
  fireEvent.click(screen.getByRole('button', { name: t('agent.chat.artifacts.openCurrentFile') }));
  await screen.findByText((_text, element) => element?.tagName === 'PRE' && element.textContent === before);
  fireEvent.click(screen.getByRole('button', { name: t('agent.soul.editButton') }));
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Unsaved human draft' } });
  view.rerender(<ArtifactPreviewPanel {...props} messages={both(rows.continuation)[index]} />);
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Unsaved human draft');
  expect(screen.getByRole('complementary')).toBeTruthy();
  expect(props.onClose).not.toHaveBeenCalled();
  expect(read).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: t('common.cancel') }));
  await waitFor(() => expect(props.onClose).toHaveBeenCalledTimes(1));
});

it.each(projections)('preserves a busy save and any newer draft ($language/$mode)', async ({ language, index }) => {
  locale.language = language;
  vi.spyOn(fileApi, 'read').mockResolvedValue({ path: artifact.path, content: before, content_hash: 'a'.repeat(64) });
  let finishSave!: (value: { content_hash: string }) => void;
  const save = vi.spyOn(fileApi, 'writeIfUnchanged').mockReturnValue(new Promise(resolve => { finishSave = resolve; }));
  const props = { preview, agentId: 'employee', onClose: vi.fn(), t: t as never, messages: [] as AgentChatMessage[] };
  const view = render(<ArtifactPreviewPanel {...props} />);
  fireEvent.click(screen.getByRole('button', { name: t('agent.chat.artifacts.openCurrentFile') }));
  await screen.findByText((_text, element) => element?.tagName === 'PRE' && element.textContent === before);
  fireEvent.click(screen.getByRole('button', { name: t('agent.soul.editButton') }));
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Submitted draft' } });
  fireEvent.click(screen.getByRole('button', { name: t('agent.soul.saveButton') }));
  expect(save).toHaveBeenCalledWith('employee', artifact.path, 'Submitted draft', 'a'.repeat(64));
  // Returning to the saved text makes dirty=false while the save is busy.
  fireEvent.change(screen.getByRole('textbox'), { target: { value: before } });
  view.rerender(<ArtifactPreviewPanel {...props} messages={both(rows.continuation)[index]} />);
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe(before);
  expect(screen.getByRole('complementary')).toBeTruthy();
  expect(props.onClose).not.toHaveBeenCalled();
  await act(async () => { finishSave({ content_hash: 'b'.repeat(64) }); });
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe(before);
  expect(props.onClose).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: t('common.cancel') }));
  await waitFor(() => expect(props.onClose).toHaveBeenCalledTimes(1));
});

it.each(projections.flatMap(projection => ['folder', 'agent', 'failed', 'denied', 'running'].map(reason => ({ ...projection, reason }))))(
  'does not close for $reason ($language/$mode)', ({ language, index, reason }) => {
    locale.language = language;
    const events: Array<Record<string, any>> = structuredClone(rows.continuation);
    const write = events.find(event => event.payload.member_summary?.kind === 'file_edit')!;
    for (const event of events) {
      if (['failed', 'denied'].includes(reason) && event.invocation_id === write.invocation_id) {
        event.payload.outcome = reason;
        if (event.payload.member_summary) event.payload.member_summary.state = 'not_done';
      }
      for (const part of event.payload.parts || []) {
        if (part.source !== 'workspace_write') continue;
        if (reason === 'folder') part.path = `workspace/other/${artifact.name}`;
        if (reason === 'agent') part.download_agent_id = 'another-employee';
      }
    }
    const messages = both(reason === 'running' ? events.filter(event => event.item_kind !== 'run' || event.lifecycle !== 'completed') : events)[index];
    const props = { preview, agentId: 'employee', onClose: vi.fn(), t: t as never };
    const view = render(<ArtifactPreviewPanel {...props} messages={[]} />);
    view.rerender(<ArtifactPreviewPanel {...props} messages={messages} />);
    expect(props.onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('complementary')).toBeTruthy();
  },
);

it.each(projections)('keeps the panel open for the member own save ($language/$mode)', async ({ language, index }) => {
  locale.language = language;
  vi.spyOn(fileApi, 'read').mockResolvedValue({ path: artifact.path, content: before, content_hash: 'a'.repeat(64) });
  const save = vi.spyOn(fileApi, 'writeIfUnchanged').mockResolvedValue({ content_hash: 'b'.repeat(64) });
  const onClose = vi.fn();
  render(<ArtifactPreviewPanel preview={preview} agentId="employee" messages={both(rows.delivery)[index]} onClose={onClose} t={t as never} />);
  fireEvent.click(screen.getByRole('button', { name: t('agent.chat.artifacts.openCurrentFile') }));
  await screen.findByText((_text, element) => element?.tagName === 'PRE' && element.textContent === before);
  fireEvent.click(screen.getByRole('button', { name: t('agent.soul.editButton') }));
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Member saved content' } });
  fireEvent.click(screen.getByRole('button', { name: t('agent.soul.saveButton') }));
  await screen.findByText('Member saved content');
  expect(save).toHaveBeenCalledTimes(1);
  expect(onClose).not.toHaveBeenCalled();
  expect(screen.getByRole('complementary')).toBeTruthy();
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

it.each(['下载地址：', '**下载链接**：', '报告下载：', '- 下载链接：', '### 下载链接：', '请看这里：',
  'Download link:', 'Download the report here:'].flatMap(label => ['', '\n'].map(blank => ({ label, blank }))))(
  'drops a short dangling lead-in $label (extra blank: "$blank")', ({ label, blank }) => {
    const content = `报告已完成。\n\n${label}\n${blank}DELIVERABLE: workspace/report.md\n\n文件已按既有结构组织。`;
    expect(assistantReplyForDisplay(content)).toBe('报告已完成。\n\n\n文件已按既有结构组织。');
    expect(content).toContain(label);
  },
);

it.each([
  '我已经按你的要求写好报告，并补充了三个新章节，请看：\n\nDELIVERABLE: workspace/report.md\n文件已按既有结构组织。',
  '下载地址：\n[report](https://example.test/report)\n\nDELIVERABLE: workspace/report.md\n文件已按既有结构组织。',
  '请看这里：\n这行是正文。\n\nDELIVERABLE: workspace/report.md\n文件已按既有结构组织。',
  `${'x'.repeat(30)}:\nDELIVERABLE: workspace/report.md\n文件已按既有结构组织。`,
  ...['。', '，', ',', '；', ';', '！', '!', '？', '?'].map(punctuation => `Done${punctuation} here:\nDELIVERABLE: workspace/report.md\n文件已按既有结构组织。`),
])('preserves non-label text around a removed marker: %s', content => {
  expect(assistantReplyForDisplay(content)).toBe(content.replace(/^DELIVERABLE:[^\r\n]*\n/gm, ''));
});

it.each(['__下载链接__：', '*下载链接*：', '_下载链接_：', '* 下载链接：', '1. 下载链接：',
  `${'x'.repeat(29)}:`, `${'文'.repeat(29)}：`])('strips label formatting before the inclusive 30-character limit: %s', label => {
  expect(assistantReplyForDisplay(`${label}\nDELIVERABLE: workspace/report.md\n文件已按既有结构组织。`)).toBe('文件已按既有结构组织。');
});

it('retains the known same-line marker limit and removes adjacent standalone markers', () => {
  const sameLine = '下载链接：DELIVERABLE: workspace/report.md';
  expect(assistantReplyForDisplay(sameLine)).toBe(sameLine);
  expect(assistantReplyForDisplay('DELIVERABLE: workspace/a.md\nDELIVERABLE: workspace/b.md\n文件已按既有结构组织。')).toBe('文件已按既有结构组织。');
});
