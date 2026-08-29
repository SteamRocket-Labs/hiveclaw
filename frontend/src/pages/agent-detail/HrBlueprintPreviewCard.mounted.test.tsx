/** @vitest-environment jsdom */

import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const hrMocks = vi.hoisted(() => ({
  get: vi.fn(),
  confirm: vi.fn(),
  reject: vi.fn(),
  retry: vi.fn(),
  cancel: vi.fn(),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallbackOrOptions?: string | Record<string, unknown>) => {
      if (typeof fallbackOrOptions === 'string') return fallbackOrOptions;
      const options = fallbackOrOptions || {};
      const template = typeof options.defaultValue === 'string' ? options.defaultValue : key;
      return template.replace('{{changes}}', String(options.changes || ''));
    },
  }),
}));

vi.mock('../../api/domains/hrCreation', () => ({
  hrCreationApi: hrMocks,
}));

import { HrBlueprintPreviewCard } from './HrBlueprintPreviewCard';
import type { HrPreviewToolResult } from './toolResultEnvelope';

const preview: HrPreviewToolResult = {
  kind: 'hr_preview' as const,
  blueprintId: 'draft-1',
  blueprintVersion: 1,
  blueprintHash: 'sha256:canonical',
  status: 'awaiting_confirmation',
  name: 'Release coordinator',
  mission: 'Prepare release checks.',
  firstMission: 'Prepare three checks.',
  primaryUsers: ['Owner'],
  coreOutputs: ['Checklist'],
  boundaries: 'Read-only.',
  permissionScope: 'company',
  sourceAttributions: [],
  riskClass: 'standard',
  missingGates: [],
  knowledgeDebt: [],
  confirmationRequirements: [],
  readyNow: [],
  willInstall: [],
  deferredCapabilities: [],
  warnings: [],
  manualSteps: [],
};

function renderCard(
  onSendMessage?: (message: string) => Promise<unknown>,
  cardPreview: HrPreviewToolResult = preview,
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  hrMocks.get.mockResolvedValue({
    blueprint_id: cardPreview.blueprintId,
    blueprint_version: cardPreview.blueprintVersion,
    blueprint_hash: cardPreview.blueprintHash,
    draft_status: cardPreview.status,
    blueprint: {},
  });
  return render(
    <QueryClientProvider client={client}>
      <HrBlueprintPreviewCard agentId="hr-agent" preview={cardPreview} onSendMessage={onSendMessage} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('HrBlueprintPreviewCard revision UX', () => {
  it('collects the requested change locally instead of sending an agent prompt on click', () => {
    const onSendMessage = vi.fn().mockResolvedValue(undefined);
    renderCard(onSendMessage);

    fireEvent.click(screen.getByRole('button', { name: 'Request changes' }));

    expect(onSendMessage).not.toHaveBeenCalled();
    expect(screen.getByRole('textbox', { name: 'What would you like to change?' })).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Preview changes' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('submits the user change exactly once and closes the editor after success', async () => {
    const onSendMessage = vi.fn().mockResolvedValue(undefined);
    renderCard(onSendMessage);
    fireEvent.click(screen.getByRole('button', { name: 'Request changes' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'What would you like to change?' }), {
      target: { value: 'Rename it to Weekend Release Coordinator V2.' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Preview changes' }));

    await waitFor(() => expect(onSendMessage).toHaveBeenCalledTimes(1));
    expect(onSendMessage).toHaveBeenCalledWith(
      'Please revise this Agent blueprint: Rename it to Weekend Release Coordinator V2.\nKeep the existing draft and return an updated preview. Do not create the employee yet.',
    );
    await waitFor(() => {
      expect(screen.queryByRole('textbox', { name: 'What would you like to change?' })).toBeNull();
    });
  });

  it('cancels a local edit without sending anything', () => {
    const onSendMessage = vi.fn().mockResolvedValue(undefined);
    renderCard(onSendMessage);
    fireEvent.click(screen.getByRole('button', { name: 'Request changes' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'What would you like to change?' }), {
      target: { value: 'Discard me' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Cancel changes' }));

    expect(onSendMessage).not.toHaveBeenCalled();
    expect(screen.queryByRole('textbox', { name: 'What would you like to change?' })).toBeNull();
  });

  it('keeps the edit recoverable when submission fails', async () => {
    const onSendMessage = vi.fn().mockRejectedValue(new Error('network offline'));
    renderCard(onSendMessage);
    fireEvent.click(screen.getByRole('button', { name: 'Request changes' }));
    const input = screen.getByRole('textbox', { name: 'What would you like to change?' });
    fireEvent.change(input, { target: { value: 'Keep this change for retry.' } });

    fireEvent.click(screen.getByRole('button', { name: 'Preview changes' }));

    expect((await screen.findByRole('alert')).textContent).toContain('network offline');
    expect((screen.getByRole('textbox', { name: 'What would you like to change?' }) as HTMLTextAreaElement).value)
      .toBe('Keep this change for retry.');
    expect(onSendMessage).toHaveBeenCalledTimes(1);
  });
});

describe('HrBlueprintPreviewCard decision hierarchy', () => {
  it('keeps typed configuration evidence in a closed disclosure while actionable decisions stay visible', () => {
    const { container } = renderCard(undefined, {
      ...preview,
      readyNow: ['builtin tools + 9 default skills', 'workspace, memory, heartbeat, and self-evolution scaffolding'],
      willInstall: ['mcp: company-search'],
      deferredCapabilities: ['web research until separately approved'],
      sourceAttributions: [{
        field: 'mission',
        value_summary: 'Company release coordinator',
        source_type: 'confirmed_by_user',
        source_refs: ['explicit:user-confirmed'],
      }],
      knowledgeDebt: ['No historical release retrospective found'],
      missingGates: ['Choose the final owner'],
      warnings: ['External publishing remains disabled'],
      manualSteps: ['Review access before creation'],
    });

    const details = container.querySelector<HTMLDetailsElement>('details.hr-blueprint-technical-details');
    expect(details).not.toBeNull();
    expect(details?.open).toBe(false);
    expect(details?.querySelector('summary')?.textContent).toBe('Configuration & sources');
    expect(details?.textContent).toContain('builtin tools + 9 default skills');
    expect(details?.textContent).toContain('workspace, memory, heartbeat, and self-evolution scaffolding');
    expect(details?.textContent).toContain('mcp: company-search');
    expect(details?.textContent).toContain('web research until separately approved');
    expect(details?.textContent).toContain('Company release coordinator');
    expect(details?.textContent).toContain('No historical release retrospective found');

    for (const decisionText of [
      'Prepare release checks.',
      'Choose the final owner',
      'External publishing remains disabled',
      'Review access before creation',
    ]) {
      const decisionNode = screen.getByText(decisionText);
      expect(details?.contains(decisionNode)).toBe(false);
    }
  });

  it('does not render an empty configuration disclosure', () => {
    const { container } = renderCard(undefined, {
      ...preview,
      readyNow: [],
      willInstall: [],
      deferredCapabilities: [],
      sourceAttributions: [],
      knowledgeDebt: [],
    });

    expect(container.querySelector('details.hr-blueprint-technical-details')).toBeNull();
  });
});
