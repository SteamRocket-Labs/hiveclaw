import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import AgentExtensionsSection from './AgentExtensionsSection';

vi.mock('./ToolsManager', () => ({ default: () => null }));
vi.mock('./AgentSkillsSection', () => ({ default: () => null }));
vi.mock('./AgentSubagentsSection', () => ({ default: () => null }));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key.split('.').pop() ?? key,
  }),
}));

describe('AgentExtensionsSection', () => {
  it('renders a single agent capability entry with MCP/plugin as the default subview', () => {
    const html = renderToStaticMarkup(<AgentExtensionsSection agentId="agent-1" canManage />);

    expect(html).toContain('data-testid="agent-extensions-section"');
    expect(html).toContain('MCP &amp; Plugins');
    expect(html).toContain('Skills');
    expect(html).toContain('Sub-agents');
    expect(html).toContain('data-testid="agent-extensions-mcp-view"');
  });
});
