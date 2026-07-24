import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { readFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import WorkspaceGuardPolicySection, {
  buildGuardPolicyUpdate,
  readGuardrailControls,
} from './WorkspaceGuardPolicySection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
  }),
}));

const POLICY = {
  id: 'policy-1',
  tenant_id: 'tenant-1',
  version: 5,
  zone_guard: {
    tool_rules: [
      {
        rule_id: 'control_plane_default',
        tools: ['*'],
        decision: 'require_approval',
        reason: 'Company approval is required for every action',
      },
      {
        tools: ['internal_extension_action'],
        decision: 'deny',
        reason: 'Managed by a platform extension',
      },
    ],
  },
  egress_guard: {
    tool_rules: [
      {
        rule_id: 'control_plane_default',
        tools: ['*'],
        decision: 'deny',
        reason: 'Outbound actions are disabled',
      },
    ],
  },
};

describe('WorkspaceGuardPolicySection', () => {
  it('renders business guardrails without exposing raw tool or JSON policy internals', () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const markup = renderToStaticMarkup(
      <QueryClientProvider client={queryClient}>
        <WorkspaceGuardPolicySection initialPolicy={POLICY} />
      </QueryClientProvider>,
    );

    expect(markup).toContain('Action Guardrails');
    expect(markup).toContain('Every employee action');
    expect(markup).toContain('Actions that leave the company');
    expect(markup).toContain('Require approval');
    expect(markup).toContain('Block');
    expect(markup).toContain('1 additional managed rule');
    expect(markup).not.toContain('internal_extension_action');
    expect(markup).not.toContain('tool_rules');
    expect(markup).not.toContain('zone_guard');
  });

  it('preserves extension-owned rules while replacing only the control-plane defaults', () => {
    expect(readGuardrailControls(POLICY)).toEqual({
      allActions: 'require_approval',
      externalActions: 'deny',
      additionalRuleCount: 1,
    });

    const update = buildGuardPolicyUpdate(POLICY, {
      allActions: 'inherit',
      externalActions: 'require_approval',
    });

    expect(update.expected_version).toBe(5);
    expect(update.zone_guard.tool_rules).toEqual([
      {
        tools: ['internal_extension_action'],
        decision: 'deny',
        reason: 'Managed by a platform extension',
      },
    ]);
    expect(update.egress_guard.tool_rules).toEqual([
      {
        rule_id: 'control_plane_default',
        tools: ['*'],
        decision: 'require_approval',
        reason: 'Company approval is required for outbound actions',
      },
    ]);
  });

  it('is wired into the routed company control plane', () => {
    const settingsSource = readFileSync(new URL('../EnterpriseSettings.tsx', import.meta.url), 'utf8');
    const sectionSource = readFileSync(new URL('../../surfaces/workspace/sections.ts', import.meta.url), 'utf8');

    expect(settingsSource).toContain("activeTab === 'guard_policy'");
    expect(settingsSource).toContain('<WorkspaceGuardPolicySection');
    expect(sectionSource).toContain("tab: 'guard_policy'");
    expect(sectionSource).toContain("path: '/enterprise/action-guardrails'");
  });
});
