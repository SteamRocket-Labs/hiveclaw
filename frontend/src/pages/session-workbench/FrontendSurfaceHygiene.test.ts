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

  it('does not retain sidebar pin or search state after their product surfaces are removed', () => {
    const layout = readFileSync(new URL('../Layout.tsx', import.meta.url), 'utf8');
    const sidebar = readFileSync(new URL('../layout/AppSidebar.tsx', import.meta.url), 'utf8');
    const css = readFileSync(new URL('../../index.css', import.meta.url), 'utf8');
    const english = readFileSync(new URL('../../i18n/en.json', import.meta.url), 'utf8');
    const chinese = readFileSync(new URL('../../i18n/zh.json', import.meta.url), 'utf8');

    for (const retiredSymbol of [
      'pinnedAgents',
      'onTogglePin',
      'sidebarSearch',
      'onSetSidebarSearch',
      'pinned_agents',
    ]) {
      expect(layout, retiredSymbol).not.toContain(retiredSymbol);
      expect(sidebar, retiredSymbol).not.toContain(retiredSymbol);
    }
    for (const retiredSurface of [
      'sidebar-pin-btn',
      'sidebar-search-wrap',
      'sidebar-search-icon',
      'sidebar-search-input',
      'sidebar-search-clear',
      'sidebar-quick-open',
      'workspaceSearch',
      'quickOpen',
    ]) {
      expect(css, retiredSurface).not.toContain(retiredSurface);
      expect(english, retiredSurface).not.toContain(retiredSurface);
      expect(chinese, retiredSurface).not.toContain(retiredSurface);
    }
  });
});
