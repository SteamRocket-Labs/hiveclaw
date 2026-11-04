import { existsSync, readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

describe('session frontend surface hygiene', () => {
  it('keeps hidden gitline rail nodes from being hit targets and reveals them intact (B5 review)', () => {
    const css = readFileSync(new URL('../../index.css', import.meta.url), 'utf8');

    const ruleBlock = (selector: string): string => {
      const start = css.indexOf(selector);
      expect(start, `selector not found: ${selector}`).toBeGreaterThan(-1);
      return css.slice(start, css.indexOf('}', start));
    };

    // Checkpoints and branch nodes are invisible AND un-hittable until the
    // rail is pointed at or focused; reveal restores both.
    const hidden = ruleBlock('.session-gitline-node,\n.session-gitline-branch-node {');
    expect(hidden).toContain('opacity: 0');
    expect(hidden).toContain('pointer-events: none');
    const revealed = ruleBlock('.session-gitline:hover .session-gitline-node,');
    expect(revealed).toContain('opacity: 1');
    expect(revealed).toContain('pointer-events: auto');
    expect(revealed).toContain('.session-gitline-branch-cluster');

    // The +N branch cluster hides with the other nodes (it was the one node
    // left always-visible). Anchored to the line start so the touch-reveal
    // block's cluster selector does not shadow the cluster's own rule.
    const cluster = ruleBlock('\n.session-gitline-branch-cluster {');
    expect(cluster).toContain('opacity: 0');
    expect(cluster).toContain('pointer-events: none');

    // Hover-less devices: the first tap on the rail reveals (the tap cannot
    // hit a hidden node), a second tap acts.
    const mediaStart = css.indexOf('@media (hover: none)');
    expect(mediaStart).toBeGreaterThan(-1);
    const touchBlock = css.slice(
      mediaStart,
      css.indexOf('}', css.indexOf('is-touch-revealed', mediaStart)),
    );
    expect(touchBlock).toContain('.session-gitline.is-touch-revealed .session-gitline-node');
    expect(touchBlock).toContain('.session-gitline-branch-cluster');
    expect(touchBlock).toContain('pointer-events: auto');
  });

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
