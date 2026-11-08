/** @vitest-environment jsdom */

import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render as renderWithoutRouter, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
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
      const label = {
        'hrChat.goToAgent': 'Go to Detail Page',
        'agent.chat.toolResults.provisioned': 'Provisioned',
      }[key];
      if (label) return label;
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
import { parsePreviewAgentBlueprintResult, type HrPreviewToolResult } from './toolResultEnvelope';

const render = (ui: React.ReactNode) => renderWithoutRouter(ui, { wrapper: MemoryRouter });
function NavigationState() {
  return <output data-testid="creation-navigation">{JSON.stringify(useLocation().state)}</output>;
}

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
  firstTaskAutostart: null,
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

it('confirms a private creation proposal with one click and shows its exact first task', async () => {
  const proposal = parsePreviewAgentBlueprintResult({ status: 'preview', blueprint_id: 'draft-1',
    draft_status: 'awaiting_confirmation', blueprint: { name: 'Researcher', permission_scope: 'self' },
    creation_authorization: { mode: 'policy_direct', first_task: 'Draft the authorized report' } })!;
  hrMocks.confirm.mockResolvedValue({ blueprint_id: 'draft-1', draft_status: 'completed',
    blueprint: { name: 'Researcher', permission_scope: 'self' }, created_agent_id: 'confirmed-employee',
    creation_authorization: { mode: 'policy_direct', first_task: 'Draft the authorized report' } });
  renderCard(undefined, proposal);
  expect(screen.getByText('Draft the authorized report')).toBeTruthy();
  expect(screen.getByText('Access: Only me')).toBeTruthy();
  expect(hrMocks.confirm).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'agent.chat.toolResults.confirmAndCreate' }));
  await waitFor(() => expect(hrMocks.confirm).toHaveBeenCalledExactlyOnceWith('hr-agent', 'draft-1', { blueprint_version: 1 }));
  expect((await screen.findByRole('link', { name: 'Talk to it' })).getAttribute('href')).toBe('/agents/confirmed-employee#chat');
});

it('recovers a requested first task on the existing employee without confirming or navigating', async () => {
  const directPreview = { ...preview, status: 'completed', authorizationMode: 'policy_direct',
    explicitFirstTask: 'Draft a comparison', firstTaskAutostart: false };
  const draft = { blueprint_id: preview.blueprintId, blueprint_version: 1, blueprint_hash: preview.blueprintHash,
    draft_status: 'completed', blueprint: {}, created_agent_id: 'existing-employee',
    provisioning: { first_task: { status: 'blocked' } }, recovery: { can_retry: true } };
  hrMocks.get.mockResolvedValue(draft);
  hrMocks.retry.mockResolvedValue({ ...draft, recovery: { can_retry: false } });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><HrBlueprintPreviewCard agentId="source" preview={directPreview} /></QueryClientProvider>);
  await waitFor(() => expect((screen.getByRole('button', { name: 'Retry first task' }) as HTMLButtonElement).disabled).toBe(false));
  expect(screen.getByRole('link', { name: 'Talk to it' }).getAttribute('href')).toBe('/agents/existing-employee#chat');
  expect(screen.queryByText('Authorized from your request using approved private defaults.')).toBeNull();
  expect(screen.queryByText('Prepare three checks.')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Retry first task' }));
  await waitFor(() => expect(hrMocks.retry).toHaveBeenCalledWith('source', 'draft-1'));
  expect(hrMocks.confirm).not.toHaveBeenCalled();
});

it.each(['rejected', 'completed'])('reloads %s from the draft instead of the pending transcript', async (status) => {
  hrMocks.get.mockResolvedValue({ blueprint_id: 'draft-1', blueprint_version: 1, draft_status: status,
    blueprint: { name: '周报助手', permission_scope: 'self' },
    summary: { mission: '整理周报' },
    created_agent_id: status === 'completed' ? 'employee' : null,
    creation_authorization: { mode: 'policy_direct', first_task: '列出三条要点' } });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><HrBlueprintPreviewCard agentId="source" preview={preview} /></QueryClientProvider>);
  await screen.findByRole('heading', { name: '周报助手' });
  expect(screen.queryByRole('button', { name: 'agent.chat.toolResults.confirmAndCreate' })).toBeNull();
  if (status === 'rejected') {
    expect(screen.getAllByText('No digital employee was created. What would you like to change?')).toHaveLength(1);
    expect(screen.queryByRole('link', { name: 'Talk to it' })).toBeNull();
  } else {
    expect(screen.getByRole('link', { name: 'Talk to it' }).getAttribute('href')).toBe('/agents/employee#chat');
    expect(screen.getByRole('button', { name: 'Undo' })).toBeTruthy();
  }
  expect(hrMocks.confirm).not.toHaveBeenCalled();
});

it('keeps internal confirmation errors out of the member card', async () => {
  hrMocks.confirm.mockRejectedValue(new Error('focus_content is empty; creation gates incomplete: activation bp_private'));
  renderCard();
  fireEvent.click(screen.getByRole('button', { name: 'agent.chat.toolResults.confirmAndCreate' }));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Could not update creation. Please reload the card and retry.');
  expect(document.body.textContent).not.toMatch(/focus_content|creation gates|activation|bp_private/);
});

describe('HrBlueprintPreviewCard revision UX', () => {
  it('shows the preserved company reason when the first task is blocked', async () => {
    const cardPreview = { ...preview, status: 'completed', authorizationMode: 'policy_direct',
      explicitFirstTask: 'Draft a comparison' };
    hrMocks.get.mockResolvedValue({ blueprint_id: 'draft-1', draft_status: 'completed', blueprint: {},
      created_agent_id: 'employee', provisioning: { first_task: { status: 'blocked', error: 'capability_denied',
        message: '公司暂停首任务，请联系管理员。' } } });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><HrBlueprintPreviewCard agentId="source" preview={cardPreview} /></QueryClientProvider>);
    expect(await screen.findByText('公司暂停首任务，请联系管理员。')).toBeTruthy();
  });
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

    expect((await screen.findByRole('alert')).textContent).toBe('Could not update creation. Please reload the card and retry.');
    expect(document.body.textContent).not.toContain('network offline');
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
    expect(details?.querySelector('summary')?.textContent).toBe('Configuration');
    expect(details?.textContent).toContain('builtin tools + 9 default skills');
    expect(details?.textContent).toContain('workspace, memory, heartbeat, and self-evolution scaffolding');
    expect(details?.textContent).toContain('mcp: company-search');
    expect(details?.textContent).toContain('web research until separately approved');
    expect(details?.textContent).not.toContain('Company release coordinator');
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

  it('opens the completed employee from the canonical draft instead of leaving a disabled status button', () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const completedDraft = {
      blueprint_id: preview.blueprintId,
      blueprint_version: preview.blueprintVersion,
      blueprint_hash: preview.blueprintHash,
      draft_status: 'completed',
      created_agent_id: '00000240-0000-4000-8000-000000000000',
      blueprint: {},
    };
    client.setQueryData(['hr-creation-draft', 'hr-agent', preview.blueprintId, undefined], completedDraft);
    hrMocks.get.mockResolvedValue(completedDraft);

    render(
      <QueryClientProvider client={client}>
        <HrBlueprintPreviewCard agentId="hr-agent" preview={preview} />
      </QueryClientProvider>,
    );

    expect(screen.getByRole('button', { name: 'Go to Detail Page' }).getAttribute('disabled')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Provisioned' })).toBeNull();
  });
});

it('shows a compact created bot card with conversation and undo actions', async () => {
  const direct = { ...preview, status: 'completed', name: '周报助手', mission: '整理每周工作要点',
    authorizationMode: 'policy_direct', explicitFirstTask: '先列三条本周要点' };
  hrMocks.get.mockResolvedValue({ blueprint_id: direct.blueprintId, blueprint_version: 1,
    blueprint_hash: direct.blueprintHash, draft_status: 'completed', blueprint: {},
    created_agent_id: 'weekly-assistant', provisioning: { first_task: { status: 'dispatched', session_id: 'first-work' } } });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<><QueryClientProvider client={client}><HrBlueprintPreviewCard agentId="source" preview={direct} /></QueryClientProvider><NavigationState /></>);
  const open = await screen.findByRole('link', { name: 'Talk to it' });
  expect(open.getAttribute('href')).toBe('/agents/weekly-assistant#chat');
  expect(screen.getByRole('button', { name: 'Undo' })).toBeTruthy();
  expect(screen.getAllByRole('button')).toHaveLength(1);
  expect(screen.getByText('周报助手')).toBeTruthy();
  expect(screen.getByText('整理每周工作要点')).toBeTruthy();
  expect(screen.queryByText('Primary users')).toBeNull();
  expect(screen.queryByText(/Authorized from your request/)).toBeNull();
  fireEvent.click(open);
  expect(JSON.parse(screen.getByTestId('creation-navigation').textContent!)).toMatchObject({
    newSessionDraft: { agent_id: 'weekly-assistant', request_id: expect.any(String) },
  });
});

it.each(['capability_denied', 'approval_required'])('shows the stored company reason for a %s click job', async (code) => {
  // Round 4 nit: job-time refusals must retain the company-written sentence.
  const direct = { ...preview, status: 'failed', authorizationMode: 'policy_direct' };
  hrMocks.get.mockResolvedValue({ blueprint_id: direct.blueprintId, blueprint_version: 1,
    blueprint_hash: direct.blueprintHash, draft_status: 'failed', blueprint: {},
    failure: { code, message: '公司暂停创建，请联系管理员。' } });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><HrBlueprintPreviewCard agentId="source" preview={direct} /></QueryClientProvider>);
  await waitFor(() => expect(screen.getByRole('alert')).toHaveProperty('textContent', '公司暂停创建，请联系管理员。'));
});

it('shows a Chinese sentence for a disabled first-task tool', async () => {
  const direct = { ...preview, status: 'completed', authorizationMode: 'policy_direct' };
  hrMocks.get.mockResolvedValue({ blueprint_id: direct.blueprintId, blueprint_version: 1,
    blueprint_hash: direct.blueprintHash, draft_status: 'completed', blueprint: {}, created_agent_id: 'employee',
    provisioning: { first_task: { status: 'blocked', error: 'capability_denied', message: '公司不允许派发首任务。' } } });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><HrBlueprintPreviewCard agentId="source" preview={direct} /></QueryClientProvider>);
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', '公司不允许派发首任务。');
});

it('keeps a reloaded undone employee stopped without conversation or undo actions', async () => {
  // Round 4 nit: the server's bot status survives a new query client/page load.
  const direct = { ...preview, status: 'completed', authorizationMode: 'policy_direct' };
  hrMocks.get.mockResolvedValue({ blueprint_id: direct.blueprintId, blueprint_version: 1,
    blueprint_hash: direct.blueprintHash, draft_status: 'completed', blueprint: {},
    created_agent_id: 'employee', creation_state: 'stopped' });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><HrBlueprintPreviewCard agentId="source" preview={direct} /></QueryClientProvider>);
  expect(await screen.findByText('Stopped')).toBeTruthy();
  expect(screen.queryByRole('link', { name: 'Talk to it' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Undo' })).toBeNull();
});
