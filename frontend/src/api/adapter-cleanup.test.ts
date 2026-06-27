import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('request cleanup adapters', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubGlobal(
      'localStorage',
      {
        getItem: vi.fn(() => 'token'),
        removeItem: vi.fn(),
      } as unknown as Storage,
    );
    vi.stubGlobal(
      'window',
      {
        location: { href: '' },
      } as unknown as Window & typeof globalThis,
    );
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('passes RequestInit options such as AbortSignal through get()', async () => {
    const { get } = await import('./core/request');
    const signal = new AbortController().signal;
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify([{ id: 'm1' }]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await get('/agents/a1/sessions/s1/messages', { signal });

    expect(fetch).toHaveBeenCalledWith(
      '/api/agents/a1/sessions/s1/messages',
      expect.objectContaining({ method: 'GET', signal }),
    );
  });

  it('supports blob downloads through a dedicated helper', async () => {
    const { getBlob } = await import('./core/request');
    const blob = new Blob(['code,uses\nABC,1\n'], { type: 'text/csv' });
    vi.mocked(fetch).mockResolvedValue(new Response(blob, { status: 200 }));

    const result = await getBlob('/enterprise/invitation-codes/export');

    expect(await result.text()).toContain('ABC');
  });

  it('routes invitation export through enterpriseApi instead of page-level fetch', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        getBlob: vi.fn(),
      };
    });
    const { enterpriseApi } = await import('./domains/enterprise');
    const { getBlob } = await import('./core/request');
    const blob = new Blob(['csv'], { type: 'text/csv' });
    vi.mocked(getBlob).mockResolvedValue(blob);

    const result = await enterpriseApi.exportInvitationCodesCsv();

    expect(getBlob).toHaveBeenCalledWith('/enterprise/invitation-codes/export');
    expect(result).toBe(blob);
  });

  it('routes enterprise memory config through enterpriseApi', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        get: vi.fn(),
        put: vi.fn(),
      };
    });
    const { enterpriseApi } = await import('./domains/enterprise');
    const { get, put } = await import('./core/request');
    vi.mocked(get).mockResolvedValue({
      summary_model_id: 'summary-model-1',
      rerank_model_id: 'rerank-model-1',
      compress_threshold: 82,
      keep_recent: 12,
      extract_to_viking: false,
    });
    vi.mocked(put).mockResolvedValue({
      summary_model_id: 'summary-model-1',
      rerank_model_id: 'rerank-model-1',
      compress_threshold: 82,
      keep_recent: 12,
      extract_to_viking: false,
    });

    await enterpriseApi.getMemoryConfig('tenant-1');
    await enterpriseApi.updateMemoryConfig(
      {
        summary_model_id: 'summary-model-1',
        rerank_model_id: 'rerank-model-1',
        compress_threshold: 82,
        keep_recent: 12,
        extract_to_viking: false,
      },
      'tenant-1',
    );

    expect(get).toHaveBeenCalledWith('/enterprise/memory/config?tenant_id=tenant-1');
    expect(put).toHaveBeenCalledWith('/enterprise/memory/config?tenant_id=tenant-1', {
      summary_model_id: 'summary-model-1',
      rerank_model_id: 'rerank-model-1',
      compress_threshold: 82,
      keep_recent: 12,
      extract_to_viking: false,
    });
  });

  it('routes capability policies through enterpriseApi', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        get: vi.fn(),
        put: vi.fn(),
        del: vi.fn(),
      };
    });
    const { enterpriseApi } = await import('./domains/enterprise');
    const { get, put, del } = await import('./core/request');
    vi.mocked(get).mockResolvedValue([]);
    vi.mocked(put).mockResolvedValue({
      id: 'policy-1',
      capability: 'workspace.file.delete',
      agent_id: 'agent-1',
      allowed: false,
      requires_approval: false,
      conditions: {},
    });
    vi.mocked(del).mockResolvedValue({ status: 'deleted' });

    await enterpriseApi.listCapabilityDefinitions();
    await enterpriseApi.listCapabilityPolicies('agent-1');
    await enterpriseApi.upsertCapabilityPolicy({
      capability: 'workspace.file.delete',
      agent_id: 'agent-1',
      allowed: false,
      requires_approval: false,
      conditions: {},
    });
    await enterpriseApi.deleteCapabilityPolicy('policy-1');

    expect(get).toHaveBeenCalledWith('/enterprise/capabilities/definitions');
    expect(get).toHaveBeenCalledWith('/enterprise/capabilities?agent_id=agent-1');
    expect(put).toHaveBeenCalledWith('/enterprise/capabilities', {
      capability: 'workspace.file.delete',
      agent_id: 'agent-1',
      allowed: false,
      requires_approval: false,
      conditions: {},
    });
    expect(del).toHaveBeenCalledWith('/enterprise/capabilities/policy-1');
  });

  it('routes session message loading through chatApi with abort support', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        get: vi.fn(),
      };
    });
    const { chatApi } = await import('./domains/chat');
    const { get } = await import('./core/request');
    const signal = new AbortController().signal;
    vi.mocked(get).mockResolvedValue([{ id: 'm1', role: 'assistant', content: 'ok', created_at: '2026-03-27' }]);

    await chatApi.getSessionMessages('agent-1', 'session-1', { signal });

    expect(get).toHaveBeenCalledWith('/agents/agent-1/sessions/session-1/messages', { signal });
  });

  it('routes replayable session transcript loading through chatApi', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        get: vi.fn(),
      };
    });
    const { chatApi } = await import('./domains/chat');
    const { get } = await import('./core/request');
    const signal = new AbortController().signal;
    vi.mocked(get).mockResolvedValue([{ id: 'evt-1', sequence: 1, type: 'assistant_message', content: 'ok' }]);

    await chatApi.getSessionTranscript('agent-1', 'session-1', { signal });

    expect(get).toHaveBeenCalledWith('/agents/agent-1/sessions/session-1/transcript', { signal });
  });

  it('supports scoped session listing through chatApi', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        get: vi.fn(),
      };
    });
    const { chatApi } = await import('./domains/chat');
    const { get } = await import('./core/request');
    vi.mocked(get).mockResolvedValue([]);

    await chatApi.listSessions('agent-1', 'all');

    expect(get).toHaveBeenCalledWith('/agents/agent-1/sessions?scope=all');
  });

  it('routes durable chat run APIs through chatApi', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        get: vi.fn(),
        post: vi.fn(),
      };
    });
    const { chatApi } = await import('./domains/chat');
    const { get, post } = await import('./core/request');
    vi.mocked(get).mockResolvedValue({ run_id: 'run-1', status: 'running' });
    vi.mocked(post).mockResolvedValue({ run_id: 'run-1', status: 'running' });

    await chatApi.startSessionRun('agent-1', 'session-1', {
      content: 'Plan this',
      display_content: 'Plan this',
      file_name: '',
      plan_mode_requested: true,
    });
    await chatApi.getActiveSessionRun('agent-1', 'session-1');
    await chatApi.cancelSessionRun('agent-1', 'session-1', 'run-1');

    expect(post).toHaveBeenCalledWith('/agents/agent-1/sessions/session-1/runs', {
      content: 'Plan this',
      display_content: 'Plan this',
      file_name: '',
      plan_mode_requested: true,
    });
    expect(get).toHaveBeenCalledWith('/agents/agent-1/sessions/session-1/runs/active');
    expect(post).toHaveBeenCalledWith('/agents/agent-1/sessions/session-1/runs/run-1/cancel', {});
  });

  it('routes notification center queries through notificationsApi with query params', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        get: vi.fn(),
        post: vi.fn(),
      };
    });
    const { notificationsApi } = await import('./domains/notifications');
    const { get, post } = await import('./core/request');
    vi.mocked(get).mockResolvedValue([]);
    vi.mocked(post).mockResolvedValue({ users_notified: 3, agents_notified: 2 });

    await notificationsApi.list({ limit: 50, category: 'system' });
    await notificationsApi.broadcast({ title: 'Heads up', body: 'Deploying now' });

    expect(get).toHaveBeenCalledWith('/notifications?limit=50&category=system');
    expect(post).toHaveBeenCalledWith('/notifications/broadcast', {
      title: 'Heads up',
      body: 'Deploying now',
    });
  });

  it('routes agent tool removal through toolsApi', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        del: vi.fn(),
      };
    });
    const { toolsApi } = await import('./domains/tools');
    const { del } = await import('./core/request');
    vi.mocked(del).mockResolvedValue(undefined);

    await toolsApi.removeAgentTool('tool-123');

    expect(del).toHaveBeenCalledWith('/tools/agent-tool/tool-123');
  });

  it('routes A2A collaborator projection through a2aApi', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        get: vi.fn(),
      };
    });
    const { a2aApi } = await import('./domains/a2a');
    const { get } = await import('./core/request');
    vi.mocked(get).mockResolvedValue({ same_owner_agents: [], public_agents: [], collaboration_groups: [] });

    await a2aApi.listCollaborators('agent-1');

    expect(get).toHaveBeenCalledWith('/agents/agent-1/a2a/collaborators');
  });

  it('routes agent permission updates through agentApi with backend payload shape', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        put: vi.fn(),
      };
    });
    const { agentApi } = await import('./domains/agents');
    const { put } = await import('./core/request');
    vi.mocked(put).mockResolvedValue(undefined);

    await agentApi.updatePermissions('agent-1', {
      scope_type: 'company',
      scope_ids: [],
      access_level: 'use',
    });

    expect(put).toHaveBeenCalledWith('/agents/agent-1/permissions', {
      scope_type: 'company',
      scope_ids: [],
      access_level: 'use',
    });
  });

  it('routes agent timezone updates through agentApi patch()', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        patch: vi.fn(),
      };
    });
    const { agentApi } = await import('./domains/agents');
    const { patch } = await import('./core/request');
    vi.mocked(patch).mockResolvedValue({ id: 'agent-1', timezone: 'Asia/Shanghai' } as any);

    await agentApi.update('agent-1', { timezone: 'Asia/Shanghai' });

    expect(patch).toHaveBeenCalledWith('/agents/agent-1', { timezone: 'Asia/Shanghai' });
  });

  it('routes capability install reads through agentApi', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        get: vi.fn(),
      };
    });
    const { agentApi } = await import('./domains/agents');
    const { get } = await import('./core/request');
    vi.mocked(get).mockResolvedValue([]);

    await agentApi.getCapabilityInstalls('agent-1');

    expect(get).toHaveBeenCalledWith('/agents/agent-1/capability-installs');
  });

  it('wraps skill adapter string arguments in backend payload shapes', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        post: vi.fn(),
        put: vi.fn(),
      };
    });
    const { skillApi } = await import('./domains/skills');
    const { post, put } = await import('./core/request');
    vi.mocked(post).mockResolvedValue({});
    vi.mocked(put).mockResolvedValue({});

    await skillApi.clawhub.install('market-research');
    await skillApi.importFromUrl('https://github.com/acme/skills/tree/main/research');
    await skillApi.previewUrl('https://github.com/acme/skills/tree/main/research');
    await skillApi.agentImport.fromSkill('agent-1', 'skill-1');
    await skillApi.agentImport.fromClawhub('agent-1', 'market-research');
    await skillApi.agentImport.fromUrl('agent-1', 'https://github.com/acme/skills/tree/main/research');
    await skillApi.settings.setToken('ghp_test');
    await skillApi.settings.setClawhubKey('ch_test');

    expect(post).toHaveBeenCalledWith('/skills/clawhub/install', { slug: 'market-research' });
    expect(post).toHaveBeenCalledWith('/skills/import-from-url', {
      url: 'https://github.com/acme/skills/tree/main/research',
    });
    expect(post).toHaveBeenCalledWith('/skills/import-from-url/preview', {
      url: 'https://github.com/acme/skills/tree/main/research',
    });
    expect(post).toHaveBeenCalledWith('/agents/agent-1/files/import-skill', { skill_id: 'skill-1' });
    expect(post).toHaveBeenCalledWith('/agents/agent-1/files/import-from-clawhub', { slug: 'market-research' });
    expect(post).toHaveBeenCalledWith('/agents/agent-1/files/import-from-url', {
      url: 'https://github.com/acme/skills/tree/main/research',
    });
    expect(put).toHaveBeenCalledWith('/skills/settings/token', { github_token: 'ghp_test' });
    expect(put).toHaveBeenCalledWith('/skills/settings/token', { clawhub_key: 'ch_test' });
  });

  it('routes user management through usersApi', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        get: vi.fn(),
        patch: vi.fn(),
      };
    });
    const { usersApi } = await import('./domains/users');
    const { get, patch } = await import('./core/request');
    vi.mocked(get).mockResolvedValue([]);
    vi.mocked(patch).mockResolvedValue({});

    await usersApi.list('tenant-1');
    await usersApi.updateQuota('user-1', { quota_message_limit: 10 });
    await usersApi.updateRole('user-1', 'org_admin');

    expect(get).toHaveBeenCalledWith('/users/?tenant_id=tenant-1');
    expect(patch).toHaveBeenCalledWith('/users/user-1/quota', { quota_message_limit: 10 });
    expect(patch).toHaveBeenCalledWith('/org/users/user-1', { role: 'org_admin' });
  });

  it('routes plaza data access through plazaApi', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        get: vi.fn(),
        post: vi.fn(),
        del: vi.fn(),
      };
    });
    const { plazaApi } = await import('./domains/plaza');
    const { get, post, del } = await import('./core/request');
    vi.mocked(get).mockResolvedValue([]);
    vi.mocked(post).mockResolvedValue({});
    vi.mocked(del).mockResolvedValue(undefined);

    await plazaApi.listPosts({ limit: 50, tenantId: 'tenant-1' });
    await plazaApi.listUsers('tenant-1');
    await plazaApi.createComment('post-1', { content: 'hi' });
    await plazaApi.toggleLike('post-1');
    await plazaApi.removePost('post-1');

    expect(get).toHaveBeenCalledWith('/plaza/posts?limit=50&tenant_id=tenant-1');
    expect(get).toHaveBeenCalledWith('/org/users?tenant_id=tenant-1');
    expect(post).toHaveBeenCalledWith('/plaza/posts/post-1/comments', { content: 'hi' });
    expect(post).toHaveBeenCalledWith('/plaza/posts/post-1/like', {});
    expect(del).toHaveBeenCalledWith('/plaza/posts/post-1');
  });

  it('routes per-channel configuration through channelApi generic helpers', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        get: vi.fn(),
        post: vi.fn(),
        del: vi.fn(),
      };
    });
    const { channelApi } = await import('./domains/channels');
    const { get, post, del } = await import('./core/request');
    vi.mocked(get).mockResolvedValue({});
    vi.mocked(post).mockResolvedValue({});
    vi.mocked(del).mockResolvedValue(undefined);

    await channelApi.getChannelConfig('agent-1', 'slack-channel');
    await channelApi.getChannelWebhook('agent-1', 'slack-channel');
    await channelApi.createChannelConfig('agent-1', 'slack-channel', { bot_token: 'x' });
    await channelApi.testChannelConfig('agent-1', 'atlassian-channel');
    await channelApi.deleteChannelConfig('agent-1', 'slack-channel');

    expect(get).toHaveBeenCalledWith('/agents/agent-1/slack-channel');
    expect(get).toHaveBeenCalledWith('/agents/agent-1/slack-channel/webhook-url');
    expect(post).toHaveBeenCalledWith('/agents/agent-1/slack-channel', { bot_token: 'x' });
    expect(post).toHaveBeenCalledWith('/agents/agent-1/atlassian-channel/test', undefined);
    expect(del).toHaveBeenCalledWith('/agents/agent-1/slack-channel');
  });

  it('routes tenant reads and updates through systemApi', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        get: vi.fn(),
        put: vi.fn(),
        del: vi.fn(),
      };
    });
    const { systemApi } = await import('./domains/system');
    const { get, put, del } = await import('./core/request');
    vi.mocked(get).mockResolvedValue({ id: 'tenant-1', name: 'ACME', slug: 'acme', is_active: true });
    vi.mocked(put).mockResolvedValue({ id: 'tenant-1', name: 'ACME 2', slug: 'acme', is_active: true });
    vi.mocked(del).mockResolvedValue({ fallback_tenant_id: 'tenant-2', needs_company_setup: false });

    await systemApi.getTenant('tenant-1');
    await systemApi.updateTenant('tenant-1', { name: 'ACME 2' });
    const deleted = await systemApi.deleteTenant('tenant-1');

    expect(get).toHaveBeenCalledWith('/tenants/tenant-1');
    expect(put).toHaveBeenCalledWith('/tenants/tenant-1', { name: 'ACME 2' });
    expect(del).toHaveBeenCalledWith('/tenants/tenant-1');
    expect(deleted).toEqual({ fallback_tenant_id: 'tenant-2', needs_company_setup: false });
  });

  it('routes global tools management through toolsApi', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        get: vi.fn(),
        post: vi.fn(),
        put: vi.fn(),
        del: vi.fn(),
      };
    });
    const { toolsApi } = await import('./domains/tools');
    const { get, post, put, del } = await import('./core/request');
    vi.mocked(get).mockResolvedValue([]);
    vi.mocked(post).mockResolvedValue({ ok: true });
    vi.mocked(put).mockResolvedValue({ ok: true });
    vi.mocked(del).mockResolvedValue(undefined);

    await toolsApi.listCatalog('tenant-1');
    await toolsApi.listAgentInstalled('tenant-1');
    await toolsApi.testMcp({ server_url: 'https://example.com/sse' });
    await toolsApi.createTool({ name: 'mcp_demo' });
    await toolsApi.updateGlobalTool('tool-1', { enabled: false });
    await toolsApi.deleteGlobalTool('tool-1');

    expect(get).toHaveBeenCalledWith('/tools?tenant_id=tenant-1');
    expect(get).toHaveBeenCalledWith('/tools/agent-installed?tenant_id=tenant-1');
    expect(post).toHaveBeenCalledWith('/tools/test-mcp', { server_url: 'https://example.com/sse' });
    expect(post).toHaveBeenCalledWith('/tools', { name: 'mcp_demo' });
    expect(put).toHaveBeenCalledWith('/tools/tool-1', { enabled: false });
    expect(del).toHaveBeenCalledWith('/tools/tool-1');
  });

  it('routes enterprise audit logs through the real audit-logs endpoint', async () => {
    vi.doMock('./core/request', async () => {
      const actual = await vi.importActual<typeof import('./core/request')>('./core/request');
      return {
        ...actual,
        get: vi.fn(),
        post: vi.fn(),
      };
    });
    const { enterpriseApi } = await import('./domains/enterprise');
    const { get, post } = await import('./core/request');
    vi.mocked(get).mockResolvedValue([]);
    vi.mocked(post).mockResolvedValue({ success: true, latency_ms: 123 });

    await enterpriseApi.getAuditLogs('limit=200&tenant_id=tenant-1');
    await enterpriseApi.testLLM({ provider: 'openai', model: 'gpt-test' });
    await enterpriseApi.getEvalRuntimeStatus();
    await enterpriseApi.syncEvalRuntimeModel('model-1', 'tenant-1');

    expect(get).toHaveBeenCalledWith('/enterprise/audit-logs?limit=200&tenant_id=tenant-1');
    expect(post).toHaveBeenCalledWith('/enterprise/llm-test', {
      provider: 'openai',
      model: 'gpt-test',
    });
    expect(get).toHaveBeenCalledWith('/enterprise/eval-ci/runtime');
    expect(post).toHaveBeenCalledWith('/enterprise/eval-ci/runtime/model?tenant_id=tenant-1', {
      model_id: 'model-1',
    });
  });
});
