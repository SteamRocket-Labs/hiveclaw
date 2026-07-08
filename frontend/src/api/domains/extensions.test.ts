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
    await extensionsApi.activateExternalExtension('agent-1', 'snapshot-1', {
      component_qualified_names: ['docs-pack:skill:audit'],
      credential_handles: { docs_api_key: 'credential-handle-123' },
    });
    await extensionsApi.tryExternalExtensionInChat('agent-1', 'snapshot-1', {
      session_id: 'session-1',
      component_qualified_names: ['docs-pack:skill:audit'],
      credential_handles: { docs_api_key: 'credential-handle-123' },
      expires_in_minutes: 30,
    });

    expect(post).toHaveBeenCalledWith('/enterprise/external-capabilities/reviews/review-1/reject', {
      reason: 'unsafe hook',
    });
    expect(post).toHaveBeenCalledWith('/enterprise/external-capabilities/snapshots/snapshot-1/revoke');
    expect(post).toHaveBeenCalledWith('/agents/agent-1/external-extensions/snapshot-1/deactivate');
    expect(post).toHaveBeenCalledWith('/agents/agent-1/external-extensions/snapshot-1/activate', {
      component_qualified_names: ['docs-pack:skill:audit'],
      credential_handles: { docs_api_key: 'credential-handle-123' },
    });
    expect(post).toHaveBeenCalledWith('/agents/agent-1/external-extensions/snapshot-1/try', {
      session_id: 'session-1',
      component_qualified_names: ['docs-pack:skill:audit'],
      credential_handles: { docs_api_key: 'credential-handle-123' },
      expires_in_minutes: 30,
    });
  });

  it('routes marketplace source and entry operations through discovery-only endpoints', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        get: vi.fn(),
        post: vi.fn(),
      };
    });

    const { extensionsApi } = await import('./extensions');
    const { get, post } = await import('../core');
    vi.mocked(get).mockResolvedValue([]);
    vi.mocked(post).mockResolvedValue({ status: 'ok' });

    await extensionsApi.listMarketplaceSources();
    await extensionsApi.createMarketplaceSource({
      name: 'Workspace Marketplace',
      source_type: 'manual',
      source_uri: 'manual://workspace',
    });
    await extensionsApi.syncMarketplaceSource('source-1');
    await extensionsApi.listMarketplaceEntries();
    await extensionsApi.submitMarketplaceEntryForReview('entry-1');

    expect(get).toHaveBeenCalledWith('/enterprise/external-capabilities/marketplace-sources');
    expect(post).toHaveBeenCalledWith('/enterprise/external-capabilities/marketplace-sources', {
      name: 'Workspace Marketplace',
      source_type: 'manual',
      source_uri: 'manual://workspace',
    });
    expect(post).toHaveBeenCalledWith('/enterprise/external-capabilities/marketplace-sources/source-1/sync');
    expect(get).toHaveBeenCalledWith('/enterprise/external-capabilities/marketplace-entries');
    expect(post).toHaveBeenCalledWith('/enterprise/external-capabilities/marketplace-entries/entry-1/submit-review');
  });

  it('routes capability factor intake and promotion proposals without touching runtime activation', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        get: vi.fn(),
        post: vi.fn(),
      };
    });

    const { extensionsApi } = await import('./extensions');
    const { get, post } = await import('../core');
    vi.mocked(get).mockResolvedValue([]);
    vi.mocked(post).mockResolvedValue({ status: 'ok' });

    await extensionsApi.listAgentCapabilityFactors('agent-1');
    await extensionsApi.captureAgentCapabilityFactor('agent-1', {
      factor_kind: 'skill_candidate',
      display_name: 'Research Skill',
      summary: 'Agent generated a reusable skill.',
    });
    await extensionsApi.listEnterpriseCapabilityFactors();
    await extensionsApi.listCapabilityPromotionProposals();
    await extensionsApi.createCapabilityPromotionProposal('factor-1', {
      proposed_snapshot_kind: 'skill',
      proposed_catalog_scope: 'workspace',
      proposed_activation_policy: 'requestable',
    });
    await extensionsApi.approveCapabilityPromotionProposal('proposal-1', {
      reason: 'approved after review',
    });
    await extensionsApi.rejectCapabilityPromotionProposal('proposal-2', {
      reason: 'not reusable',
    });
    await extensionsApi.archiveCapabilityFactor('factor-1');

    expect(get).toHaveBeenCalledWith('/agents/agent-1/capability-factors');
    expect(post).toHaveBeenCalledWith('/agents/agent-1/capability-factors', {
      factor_kind: 'skill_candidate',
      display_name: 'Research Skill',
      summary: 'Agent generated a reusable skill.',
    });
    expect(get).toHaveBeenCalledWith('/enterprise/capability-factors');
    expect(get).toHaveBeenCalledWith('/enterprise/capability-promotion-proposals');
    expect(post).toHaveBeenCalledWith('/enterprise/capability-factors/factor-1/promotion-proposals', {
      proposed_snapshot_kind: 'skill',
      proposed_catalog_scope: 'workspace',
      proposed_activation_policy: 'requestable',
    });
    expect(post).toHaveBeenCalledWith('/enterprise/capability-promotion-proposals/proposal-1/approve', {
      reason: 'approved after review',
    });
    expect(post).toHaveBeenCalledWith('/enterprise/capability-promotion-proposals/proposal-2/reject', {
      reason: 'not reusable',
    });
    expect(post).toHaveBeenCalledWith('/enterprise/capability-factors/factor-1/archive');
  });

  it('routes legacy pack migration dry-run through a read-only enterprise endpoint', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        get: vi.fn(),
      };
    });

    const { extensionsApi } = await import('./extensions');
    const { get } = await import('../core');
    vi.mocked(get).mockResolvedValue({ migration_only: true });

    await extensionsApi.dryRunLegacyPackMigration();

    expect(get).toHaveBeenCalledWith('/enterprise/external-capabilities/legacy-pack-migration/dry-run');
  });
});
