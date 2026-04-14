import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

describe('AgentChatSection channel labels', () => {
  it('maps microsoft_teams to the teams label key', () => {
    const source = readFileSync(
      fileURLToPath(new URL('./AgentChatSection.tsx', import.meta.url)),
      'utf-8',
    );

    expect(source).toContain("microsoft_teams: t('common.channels.teams')");
  });
});
