import { describe, expect, it } from 'vitest';

import { composerShortcutText } from './sessionComposerShortcuts';

describe('sessionComposerShortcuts', () => {
  it('keeps non-plan composer actions as editable slash command drafts', () => {
    expect(composerShortcutText('goal')).toBe('/goal ');
    expect(composerShortcutText('schedule')).toBe('/schedule ');
  });
});
