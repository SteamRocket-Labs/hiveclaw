import { expect, it } from 'vitest';
import { parseUserAttachment } from './chatAttachments';
import type { AgentChatMessage } from './chatRuntime';

it('renders a selected shared goal without exposing its internal source envelope', () => {
  const parts = [{ type: 'text', text: 'Prepare a draft.' }, { type: 'text', text: 'Canonical machine source data',
    display_content: 'Reviewed goal\nPrepare the comparison', team_task_source: { disclosure_id: 'private-source-envelope' } }];
  const message: AgentChatMessage = { role: 'user', content: JSON.stringify(parts), sessionItem: {
    id: 'input', kind: 'human_input', scope: { level: 'session', session_id: 'work', thread_id: 'work' },
    lifecycle: 'accepted', terminal: false, revision: 0, content: JSON.stringify(parts), payload: { content_parts: parts },
    actor: { type: 'user', id: 'owner' }, visibility: { audience: 'private' }, occurredAt: '2026-10-20T00:00:00Z',
    first_sequence: 1, last_sequence: 1,
  } };
  const parsed = parseUserAttachment(message, 'employee');
  expect(parsed.content).toBe('Prepare a draft.\nReviewed goal\nPrepare the comparison');
  expect(parsed.sessionItem).toBe(message.sessionItem);
  expect(message.content).toContain('private-source-envelope');
});

it('displays canonical attachment refs without reinterpreting filename bytes as markers', () => {
  const parts = [{ type: 'text', text: 'Use both source files.' },
    { type: 'attachment_ref', attachment: { path: 'workspace/uploads/dingtalk/receipt/amount]report.csv', name: 'amount]report.csv', size: 12 } },
    { type: 'attachment_ref', attachment: { path: 'workspace/uploads/wecom/receipt/image.png', name: 'image.png', content_type: 'image/png' } }];
  const message: AgentChatMessage = { role: 'user', content: JSON.stringify(parts), sessionItem: {
    id: 'input', kind: 'human_input', scope: { level: 'session', session_id: 'work', thread_id: 'work' },
    lifecycle: 'accepted', terminal: false, revision: 0, content: JSON.stringify(parts), payload: { content_parts: parts },
    actor: { type: 'user', id: 'owner' }, visibility: { audience: 'private' }, occurredAt: '2026-10-19T00:00:00Z',
    first_sequence: 1, last_sequence: 1,
  } };
  const parsed = parseUserAttachment(message, 'employee');
  expect(parsed.content).toBe('Use both source files.');
  expect(parsed.inputAttachments).toEqual([
    { path: 'workspace/uploads/dingtalk/receipt/amount]report.csv', name: 'amount]report.csv', size: 12, source: 'human_input' },
    { path: 'workspace/uploads/wecom/receipt/image.png', name: 'image.png', mimeType: 'image/png', source: 'human_input' },
  ]);
  expect(parsed.sessionItem).toBe(message.sessionItem);
  expect(parsed.artifacts).toBeUndefined();
  const failedParts = [{ type: 'attachment_ref', attachment: { name: 'source.csv', state: 'unavailable',
    reason_code: 'company_file_expired', recovery_action: 'upload_new_message' } }];
  const unavailable = parseUserAttachment({ ...parsed, fileName: 'old.png', imageUrl: '/old-image',
    sessionItem: { ...message.sessionItem!, content: JSON.stringify(failedParts), payload: { content_parts: failedParts } } }, 'employee');
  expect(unavailable.content).toBe('');
  expect(unavailable.inputAttachments).toEqual([]);
  expect(unavailable.inputAttachmentFailures).toEqual([{ name: 'source.csv' }]);
  expect(unavailable.fileName).toBeUndefined();
  expect(unavailable.imageUrl).toBeUndefined();
  const restored = parseUserAttachment({ ...unavailable, sessionItem: message.sessionItem }, 'employee');
  expect(restored.inputAttachmentFailures).toBeUndefined();
  expect(restored.inputAttachments).toHaveLength(2);
});

it.each(['image.png', '图[2].png'])('keeps the exact qualified path for %s while displaying only its filename', (filename) => {
  const path = `workspace/uploads/feishu/source-id/${filename}`;
  const parsed = parseUserAttachment({ role: 'user', content: `[file:${path}]\nInspect this image.` }, 'employee');
  expect(parsed.fileName).toBe(filename);
  expect(parsed.content).toBe('Inspect this image.');
  const url = new URL(parsed.imageUrl!, 'https://hive.fixture.test');
  expect(url.pathname).toBe('/api/agents/employee/files/download');
  expect(url.searchParams.get('path')).toBe(path);
});

it.each(['[file:image.png]', '[文件已上传: workspace/uploads/image.png]'])('preserves legacy attachment previews for %s', (content) => {
  const parsed = parseUserAttachment({ role: 'user', content }, 'employee');
  expect(parsed.fileName).toBe('image.png');
  expect(new URL(parsed.imageUrl!, 'https://hive.fixture.test').searchParams.get('path')).toBe('workspace/uploads/image.png');
});

it('preserves old file questions and existing authorized previews', () => {
  expect(parseUserAttachment({ role: 'user', content: '[File: report.pdf]\nFile location: fixture\nQuestion: Edit it.' }, 'employee'))
    .toMatchObject({ fileName: 'report.pdf', content: 'Edit it.' });
  expect(parseUserAttachment({ role: 'user', content: '[file:image.png]', imageUrl: '/existing-scoped-preview' }, 'employee').imageUrl)
    .toBe('/existing-scoped-preview');
  expect(parseUserAttachment({ role: 'user', content: '[file:first.pdf] [file:second.pdf]' }, 'employee'))
    .toMatchObject({ fileName: 'first.pdf', content: '[file:second.pdf]' });
});
