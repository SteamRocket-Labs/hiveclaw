import { expect, it } from 'vitest';
import { parseUserAttachment } from './chatAttachments';

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
