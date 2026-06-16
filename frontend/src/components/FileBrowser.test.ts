import { describe, expect, it } from 'vitest';

import { buildNewSkillFilePath } from './FileBrowser';

describe('FileBrowser skill path helpers', () => {
  it('creates folder-based SKILL.md paths instead of legacy flat md files', () => {
    expect(buildNewSkillFilePath('skills', 'deploy-checklist')).toBe('skills/deploy-checklist/SKILL.md');
    expect(buildNewSkillFilePath('', 'market research')).toBe('market-research/SKILL.md');
    expect(buildNewSkillFilePath('skills', 'legacy.md')).toBe('skills/legacy/SKILL.md');
    expect(buildNewSkillFilePath('skills', 'nested/SKILL.md')).toBe('skills/nested/SKILL.md');
  });
});
