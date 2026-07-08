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

  it('routes legacy plugin assignment through agent-scoped compatibility endpoint', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        put: vi.fn(),
      };
    });

    const { extensionsApi } = await import('./extensions');
    const { put } = await import('../core');
    vi.mocked(put).mockResolvedValue({ ok: true });

    await extensionsApi.setAgentPluginAssignment('agent-1', 'web_pack', { enabled: true });

    expect(put).toHaveBeenCalledWith('/agents/agent-1/plugins/web_pack', { enabled: true });
  });

  it('treats enterprise MCP import as a Trust Gate review request, not direct server creation', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        post: vi.fn(),
      };
    });

    const { extensionsApi } = await import('./extensions');
    const { post } = await import('../core');
    vi.mocked(post).mockResolvedValue({
      id: 'review-1',
      status: 'review_required',
      source_format: 'mcp_server',
      source_uri: 'https://example.test/mcp',
      normalized_name: 'Example MCP',
    });

    const result = await extensionsApi.importEnterpriseMcpServer({
      mcp_url: 'https://example.test/mcp',
      server_name: 'Example MCP',
    });

    expect(post).toHaveBeenCalledWith('/enterprise/mcp-servers/import', {
      mcp_url: 'https://example.test/mcp',
      server_name: 'Example MCP',
    });
    expect(result).toMatchObject({
      status: 'review_required',
      source_format: 'mcp_server',
      normalized_name: 'Example MCP',
    });
  });

  it('routes external review rejection, snapshot revoke, and agent deactivation through Trust Gate endpoints', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        post: vi.fn(),
      };
    });

    const { extensionsApi } = await import('./extensions');
    const { post } = await import('../core');
    vi.mocked(post).mockResolvedValue({ status: 'ok' });

    await extensionsApi.rejectExternalCapabilityReview('review-1', { reason: 'unsafe hook' });
    await extensionsApi.revokeExternalCapabilitySnapshot('snapshot-1');
    await extensionsApi.deactivateExternalExtension('agent-1', 'snapshot-1');

    expect(post).toHaveBeenCalledWith('/enterprise/external-capabilities/reviews/review-1/reject', {
      reason: 'unsafe hook',
    });
    expect(post).toHaveBeenCalledWith('/enterprise/external-capabilities/snapshots/snapshot-1/revoke');
    expect(post).toHaveBeenCalledWith('/agents/agent-1/external-extensions/snapshot-1/deactivate');
  });
});
