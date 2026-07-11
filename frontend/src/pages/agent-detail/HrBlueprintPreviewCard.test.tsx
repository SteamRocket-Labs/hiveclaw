import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (_key: string, fallback?: string) => fallback || _key }),
}));

import { buildHrCreationInstruction, HrBlueprintPreviewCard } from './HrBlueprintPreviewCard';

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
        <HrBlueprintPreviewCard agentId="hr-agent" preview={preview} onSendMessage={vi.fn()} />
      </QueryClientProvider>,
    );

    expect(markup).toContain('Research Bot');
    expect(markup).toContain('Investment team');
    expect(markup).toContain('Landscape brief');
    expect(markup).toContain('Confirm &amp; create');
    expect(markup).toContain('Request changes');
    expect(markup).toContain('Reject');
    expect(markup).not.toContain('sha256:canonical');
    expect(markup).not.toContain('78b5d739-2c18-4a4a-aa65-42b858b8c188');
  });

  it('builds a stable reference-only creation instruction', () => {
    const instruction = buildHrCreationInstruction(preview);
    expect(instruction).toContain('blueprint_id');
    expect(instruction).not.toContain('idempotency_key');
    expect(instruction).not.toContain(preview.blueprintId || '');
    expect(instruction).not.toContain(preview.mission || '');
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
        <HrBlueprintPreviewCard agentId="hr-agent" preview={preview} onSendMessage={vi.fn()} />
      </QueryClientProvider>,
    );

    expect(markup).toContain('Provisioning progress');
    expect(markup).toContain('workspace');
    expect(markup).toContain('completed');
    expect(markup).toContain('Provider timed out');
    expect(markup).toContain('Resume provisioning');
    expect(markup).not.toContain('claim_token');
  });
});
