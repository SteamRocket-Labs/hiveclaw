import { existsSync, readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

describe('session frontend surface hygiene', () => {
  it('removes unmounted control and schedule-client duplicates', () => {
    expect(existsSync(new URL('./SessionNativeControls.tsx', import.meta.url))).toBe(false);
    expect(existsSync(new URL('./SessionNativeControls.css', import.meta.url))).toBe(false);
    expect(existsSync(new URL('../../api/domains/schedules.ts', import.meta.url))).toBe(false);

    const domainIndex = readFileSync(new URL('../../api/domains/index.ts', import.meta.url), 'utf8');
    expect(domainIndex).not.toContain('scheduleApi');
  });

  it('keeps Local Agent transport-specific but reuses the canonical session composer', () => {
    const source = readFileSync(new URL('../agent-detail/LocalAgentChatSection.tsx', import.meta.url), 'utf8');

    expect(source).toContain("import { SessionComposer } from '../session-workbench/SessionComposer'");
    expect(source).toContain('<SessionComposer');
    expect(source).not.toContain('local-chat-plus-menu');
    expect(source).not.toContain('local-chat-textarea');
    expect(source).not.toContain('composerMenuOpen');
  });
});
