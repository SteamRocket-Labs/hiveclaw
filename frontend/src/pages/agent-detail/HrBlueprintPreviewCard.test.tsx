import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => ({
      'agent.chat.toolResults.confirmAndCreate': 'Confirm & create',
      'agent.chat.toolResults.retryProvisioning': 'Retry provisioning',
    }[key] || fallback || key),
  }),
}));

import { hrCreationActionForStatus, HrBlueprintPreviewCard } from './HrBlueprintPreviewCard';

const preview = {
  kind: 'hr_preview' as const,
  blueprintId: '78b5d739-2c18-4a4a-aa65-42b858b8c188',
  blueprintVersion: 2,
  blueprintHash: 'sha256:canonical',
  status: 'awaiting_confirmation',
  name: 'Research Bot',
  mission: 'Research competitors.',
  firstMission: 'Prepare a landscape brief.',
  primaryUsers: ['Investment team'],
  coreOutputs: ['Landscape brief'],
  boundaries: 'Never fabricate sources.',
  permissionScope: 'company',
  sourceAttributions: [],
  riskClass: 'standard',
  missingGates: [],
  knowledgeDebt: [],
  confirmationRequirements: [],
  readyNow: ['Built-in tools'],
  willInstall: [],
  deferredCapabilities: [],
  warnings: [],
  manualSteps: [],
};

describe('HrBlueprintPreviewCard', () => {
  it('renders the user decision surface without raw hashes or JSON', () => {
    const markup = renderToStaticMarkup(
      <QueryClientProvider client={new QueryClient()}>
        <HrBlueprintPreviewCard agentId="hr-agent" preview={preview} />
      </QueryClientProvider>,
    );

    expect(markup).toContain('Research Bot');
    expect(markup).toContain('Investment team');
    expect(markup).toContain('Landscape brief');
    expect(markup).toContain('Prepare a landscape brief.');
    expect(markup).toContain('Confirm &amp; create');
    expect(markup).toContain('Request changes');
    expect(markup).toContain('Reject');
    expect(markup).not.toContain('sha256:canonical');
    expect(markup).not.toContain('78b5d739-2c18-4a4a-aa65-42b858b8c188');
    const primaryButton = markup.match(/<button[^>]*>Confirm &amp; create<\/button>/)?.[0] || '';
    expect(primaryButton).not.toContain('disabled');
  });

  it('maps UI actions directly to durable APIs without a model-message handoff', () => {
    expect(hrCreationActionForStatus('awaiting_confirmation')).toBe('confirm');
    expect(hrCreationActionForStatus('failed')).toBe('retry');
    expect(hrCreationActionForStatus('provisioning')).toBe('retry');
    expect(hrCreationActionForStatus('completed')).toBe('none');
  });

  it('shows source authority in user language without exposing machine codes or evidence ids', () => {
    const markup = renderToStaticMarkup(
      <QueryClientProvider client={new QueryClient()}>
        <HrBlueprintPreviewCard
          agentId="hr-agent"
          preview={{
            ...preview,
            sourceAttributions: [
              {
                field: 'boundaries',
                value_summary: 'Never publish without approval.',
                source_type: 'confirmed_by_user',
                source_refs: ['explicit:user-confirmed'],
              },
              {
                field: 'role_description',
                value_summary: 'Use authorized company documents and cite every claim.',
                source_type: 'supported_by_company_kb',
                source_refs: ['company-evidence://dfbb7f40-56eb-4dcb-bb46-f58c218f1429'],
              },
            ],
          }}
        />
      </QueryClientProvider>,
    );

    expect(markup).toContain('Sources');
    expect(markup).toContain('User confirmed');
    expect(markup).toContain('Company knowledge');
    expect(markup).toContain('Never publish without approval.');
    expect(markup).toContain('Use authorized company documents and cite every claim.');
    expect(markup).not.toContain('confirmed_by_user');
    expect(markup).not.toContain('supported_by_company_kb');
    expect(markup).not.toContain('company-evidence://');
    expect(markup).not.toContain('awaiting_confirmation');
    expect(markup).not.toContain('permission_scope');
  });

  it('shows persisted provisioning evidence and exposes a recovery action', () => {
    const client = new QueryClient();
    client.setQueryData(
      ['hr-creation-draft', 'hr-agent', preview.blueprintId],
      {
        blueprint_id: preview.blueprintId,
        blueprint_version: preview.blueprintVersion,
        blueprint_hash: preview.blueprintHash,
        draft_status: 'failed',
        blueprint: {},
        provisioning_steps: [
          {
            step_key: 'workspace',
            step_kind: 'workspace',
            required: true,
            status: 'completed',
            attempt_count: 1,
          },
          {
            step_key: 'capability:mcp:github',
            step_kind: 'mcp_server',
            required: true,
            status: 'failed',
            attempt_count: 2,
            error_message: 'Provider timed out',
          },
        ],
      },
    );

    const markup = renderToStaticMarkup(
      <QueryClientProvider client={client}>
        <HrBlueprintPreviewCard agentId="hr-agent" preview={preview} />
      </QueryClientProvider>,
    );

    expect(markup).toContain('Provisioning progress');
    expect(markup).toContain('Workspace');
    expect(markup).toContain('Completed');
    expect(markup).toContain('MCP connection');
    expect(markup).toContain('Provider timed out');
    expect(markup).toContain('Retry provisioning');
    expect(markup).not.toContain('claim_token');
    expect(markup).not.toContain('mcp_server');
    expect(markup).not.toContain('data-status');
  });

  it('renders the fetched canonical draft instead of a stale streamed preview', () => {
    const client = new QueryClient();
    client.setQueryData(
      ['hr-creation-draft', 'hr-agent', preview.blueprintId],
      {
        status: 'preview',
        blueprint_id: preview.blueprintId,
        blueprint_version: 3,
        blueprint_hash: 'sha256:new-canonical',
        draft_status: 'awaiting_confirmation',
        risk_class: 'standard',
        missing_gates: [],
        blueprint: {
          name: 'Canonical Research Bot',
          primary_users: ['Canonical user'],
          core_outputs: ['Canonical brief'],
          boundaries: 'Canonical boundary.',
          permission_scope: 'self',
          source_attributions: [],
        },
        summary: {
          mission: 'Canonical mission.',
          first_mission: 'Canonical first task.',
        },
      },
    );

    const markup = renderToStaticMarkup(
      <QueryClientProvider client={client}>
        <HrBlueprintPreviewCard agentId="hr-agent" preview={preview} />
      </QueryClientProvider>,
    );

    expect(markup).toContain('Canonical Research Bot');
    expect(markup).toContain('Canonical mission.');
    expect(markup).toContain('Canonical first task.');
    expect(markup).toContain('Canonical user');
    expect(markup).not.toContain('Research competitors.');
  });
});
