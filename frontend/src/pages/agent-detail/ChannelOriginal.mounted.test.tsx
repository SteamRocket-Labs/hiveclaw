// @vitest-environment jsdom
import { expect, it } from 'vitest';

import { parseUserAttachment } from './chatAttachments';
import type { AgentChatMessage } from './chatRuntime';

it('retains the qualified legacy channel path as an ordinary attachment', () => {
  const msg = parseUserAttachment({ role: 'user', content: '[file:workspace/uploads/feishu/source/file.bin]' } as AgentChatMessage, 'employee');
  expect(msg.fileName).toBe('file.bin');
  expect(msg.originalWorkspacePath).toBe('workspace/uploads/feishu/source/file.bin');
});
