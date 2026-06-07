import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('extensions API adapter', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('routes MCP server tool policy reads and writes through server-first endpoints', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        get: vi.fn(),
        put: vi.fn(),
      };
    });

    const { extensionsApi } = await import('./extensions');
    const { get, put } = await import('../core');
    vi.mocked(get).mockResolvedValue([]);
    vi.mocked(put).mockResolvedValue({
      tool_name: 'github_search',
      display_name: 'GitHub Search',
      mode: 'approval',
      effective_mode: 'approval',
    });

    await extensionsApi.getAgentMcpServerTools('agent-1', 'server-1');
    await extensionsApi.setAgentMcpToolPolicy('agent-1', 'server-1', 'github_search', { mode: 'approval' });
    await extensionsApi.setAgentMcpAssignment('agent-1', 'server-1', {
      enabled: true,
      default_tool_mode: 'auto',
      always_load: true,
    });

    expect(get).toHaveBeenCalledWith('/agents/agent-1/mcp-servers/server-1/tools');
    expect(put).toHaveBeenCalledWith('/agents/agent-1/mcp-servers/server-1/tools/github_search/policy', {
      mode: 'approval',
    });
    expect(put).toHaveBeenCalledWith('/agents/agent-1/mcp-servers/server-1', {
      enabled: true,
      default_tool_mode: 'auto',
      always_load: true,
    });
  });
});
