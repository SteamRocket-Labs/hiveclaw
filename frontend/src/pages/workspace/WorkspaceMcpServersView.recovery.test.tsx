// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import WorkspaceMcpServersView from './WorkspaceMcpServersView';

const mocks = vi.hoisted(() => ({
  listEnterpriseMcpServers: vi.fn(),
  listEnterpriseMcpServerTools: vi.fn(),
  reviewEnterpriseMcpServerToolMetadata: vi.fn(),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => translateFromCatalog(zh, key, fallback),
    i18n: { language: 'zh' },
  }),
}));

vi.mock('../../api/domains/extensions', () => ({
  extensionsApi: {
    listEnterpriseMcpServers: mocks.listEnterpriseMcpServers,
    listEnterpriseMcpServerTools: mocks.listEnterpriseMcpServerTools,
    reviewEnterpriseMcpServerToolMetadata: mocks.reviewEnterpriseMcpServerToolMetadata,
  },
}));

const historical = 'b'.repeat(64);
const firstDigest = 'a'.repeat(64);
const reloadedDigest = 'c'.repeat(64);

function candidate(fingerprint: string, permissions: string[], runtimeApproved = false) {
  return {
    tool_id: 'tool-1',
    tool_name: 'issue_search',
    display_name: 'Issue Search',
    canonical_description: 'Search issues by query.',
    canonical_schema: { type: 'object' },
    raw_description: '<script>window.pwned=true</script>',
    raw_schema: { type: 'object' },
    metadata_fingerprint: fingerprint,
    reviewed_fingerprint: runtimeApproved ? fingerprint : historical,
    activation: {
      endpoint: 'https://mcp.example/review',
      package: 'acme.ledger',
      permissions,
      configuration: {
        transport: 'sse',
        headers: { Authorization: 'set' },
        credential: 'set',
      },
    },
    risk_flags: [],
    trust_status: runtimeApproved ? 'approved' : 'approved',
    trust_tier: 'admin_approved',
    reviewed_by: null,
    reviewed_at: null,
    runtime_approved: runtimeApproved,
  };
}

describe('MCP metadata review recovery', () => {
  afterEach(() => cleanup());

  it('reloads the API candidate after a stale rejection and approves that digest', async () => {
    expect(en.enterprise.tools.mcpActivationEndpoint).toBe('Endpoint');
    expect(zh.enterprise.tools.mcpActivationEndpoint).toBe('端点');
    expect(zh.enterprise.tools.mcpPreviouslyApproved).toBe('先前已批准');
    mocks.listEnterpriseMcpServers.mockResolvedValue([
      {
        id: 'server-1',
        name: '账本',
        server_key: 'ledger',
        status: 'connected',
        auth_status: 'configured',
        transport: 'sse',
        tool_count: 1,
        agent_count: 0,
        agents: [],
      },
    ]);
    mocks.listEnterpriseMcpServerTools
      .mockResolvedValueOnce([candidate(firstDigest, ['ledger:read'])])
      .mockResolvedValueOnce([candidate(reloadedDigest, ['ledger:admin'])])
      .mockResolvedValueOnce([candidate(reloadedDigest, ['ledger:admin'], true)]);
    mocks.reviewEnterpriseMcpServerToolMetadata
      .mockRejectedValueOnce(new Error('MCP metadata fingerprint changed; reload before reviewing'))
      .mockResolvedValueOnce({
        tool_name: 'issue_search',
        trust_status: 'approved',
        runtime_approved: true,
      });

    render(<WorkspaceMcpServersView selectedTenantId="tenant-1" />);
    fireEvent.click(await screen.findByRole('button', { name: '审查元数据' }));

    expect(await screen.findByText('https://mcp.example/review')).toBeTruthy();
    expect(screen.getByText('acme.ledger')).toBeTruthy();
    expect(screen.getByText('ledger:read')).toBeTruthy();
    expect(screen.getByText('已设置')).toBeTruthy();
    expect(screen.getByText('先前已批准')).toBeTruthy();
    expect(screen.getByText('运行时已阻断')).toBeTruthy();
    expect(screen.getByText(/<script>/)).toBeTruthy();
    expect(document.body.innerHTML).not.toContain('<script>window.pwned=true</script>');
    expect(document.body.innerHTML).not.toContain('synthetic-header-secret');

    fireEvent.click(screen.getByRole('button', { name: '批准指纹' }));
    expect((await screen.findByRole('alert')).textContent).toContain(
      'MCP metadata fingerprint changed; reload before reviewing',
    );
    expect(screen.getByText('运行时已阻断')).toBeTruthy();
    expect(mocks.reviewEnterpriseMcpServerToolMetadata).toHaveBeenCalledWith('server-1', 'issue_search', {
      decision: 'approve',
      expected_fingerprint: firstDigest,
      canonical_description: 'Search issues by query.',
    });

    fireEvent.click(screen.getByRole('button', { name: '关闭' }));
    fireEvent.click(screen.getByRole('button', { name: '审查元数据' }));
    expect(await screen.findByText('ledger:admin')).toBeTruthy();
    const description = screen.getByLabelText('运行时权威描述') as HTMLTextAreaElement;
    fireEvent.change(description, { target: { value: 'Search the reviewed ledger.' } });
    fireEvent.click(screen.getByRole('button', { name: '批准指纹' }));

    await waitFor(() => expect(screen.getByText('运行时已批准')).toBeTruthy());
    expect(screen.queryByText('先前已批准')).toBeNull();
    expect(mocks.reviewEnterpriseMcpServerToolMetadata).toHaveBeenLastCalledWith('server-1', 'issue_search', {
      decision: 'approve',
      expected_fingerprint: reloadedDigest,
      canonical_description: 'Search the reviewed ledger.',
    });
    expect(screen.getByRole('button', { name: '拒绝' })).toBeTruthy();
  });
});
