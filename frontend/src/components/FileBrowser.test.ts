import { describe, expect, it } from 'vitest';

import { buildNewSkillFilePath } from './fileBrowserPaths';
import { FILE_LIST_PAGE_SIZE, visibleFileWindow } from './fileBrowserWindow';

describe('FileBrowser skill path helpers', () => {
  it('creates folder-based SKILL.md paths instead of legacy flat md files', () => {
    expect(buildNewSkillFilePath('skills', 'deploy-checklist')).toBe('skills/deploy-checklist/SKILL.md');
    expect(buildNewSkillFilePath('', 'market research')).toBe('market-research/SKILL.md');
    expect(buildNewSkillFilePath('skills', 'legacy.md')).toBe('skills/legacy/SKILL.md');
    expect(buildNewSkillFilePath('skills', 'nested/SKILL.md')).toBe('skills/nested/SKILL.md');
  });
});

describe('FileBrowser large-list window', () => {
  it('bounds the initial DOM window and expands without dropping ordering', () => {
    const files = Array.from({ length: 1000 }, (_, index) => ({
      name: `artifact-${String(index).padStart(4, '0')}.md`,
      path: `workspace/artifact-${String(index).padStart(4, '0')}.md`,
      is_dir: false,
    }));

    expect(FILE_LIST_PAGE_SIZE).toBe(200);
    expect(visibleFileWindow(files, FILE_LIST_PAGE_SIZE)).toEqual(files.slice(0, 200));
    expect(visibleFileWindow(files, FILE_LIST_PAGE_SIZE * 2)).toEqual(files.slice(0, 400));
    expect(visibleFileWindow(files, Number.POSITIVE_INFINITY)).toEqual(files);
  });
});
