import { existsSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

import { enterpriseApi } from './domains/enterprise';
import { officeApi } from './domains/office';

describe('retired frontend compatibility surfaces', () => {
  it('does not ship the zero-consumer Local Agent link wrapper', () => {
    expect(
      existsSync(new URL('../pages/agent-detail/LocalAgentLinkCard.tsx', import.meta.url)),
    ).toBe(false);
  });

  it('keeps unused role-template and Office creation adapters out of the public API', () => {
    expect('templates' in enterpriseApi).toBe(false);
    expect('createDocument' in officeApi).toBe(false);
  });
});
