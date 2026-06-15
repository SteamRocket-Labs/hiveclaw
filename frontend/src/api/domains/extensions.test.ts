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

  it('routes plugin install, uninstall, backfill, and assignment through canonical plugin endpoints', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        get: vi.fn(),
        post: vi.fn(),
        put: vi.fn(),
      };
    });

    const { extensionsApi } = await import('./extensions');
    const { get, post, put } = await import('../core');
    vi.mocked(get).mockResolvedValue([]);
    vi.mocked(post).mockResolvedValue({ ok: true });
    vi.mocked(put).mockResolvedValue({ ok: true });

    await extensionsApi.listEnterprisePlugins();
    await extensionsApi.installEnterprisePlugin({ plugin_key: 'web_pack', agent_ids: ['agent-1'] });
    await extensionsApi.uninstallEnterprisePlugin({ plugin_key: 'web_pack' });
    await extensionsApi.backfillEnterprisePlugins();
    await extensionsApi.setAgentPluginAssignment('agent-1', 'web_pack', { enabled: true });

    expect(get).toHaveBeenCalledWith('/enterprise/plugins');
    expect(post).toHaveBeenCalledWith('/enterprise/plugins/install', {
      plugin_key: 'web_pack',
      agent_ids: ['agent-1'],
    });
    expect(post).toHaveBeenCalledWith('/enterprise/plugins/uninstall', { plugin_key: 'web_pack' });
    expect(post).toHaveBeenCalledWith('/enterprise/plugins/backfill');
    expect(put).toHaveBeenCalledWith('/agents/agent-1/plugins/web_pack', { enabled: true });
  });
});
