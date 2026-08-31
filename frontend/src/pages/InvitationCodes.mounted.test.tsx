// @vitest-environment jsdom

import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import InvitationCodes from './InvitationCodes';

const mocks = vi.hoisted(() => ({
  createInvitationCode: vi.fn(),
  deleteInvitationCode: vi.fn(),
  exportInvitationCodesCsv: vi.fn(),
  listInvitationCodes: vi.fn(),
  t: (key: string, fallback?: string) => fallback || ({
    'common.loading': 'Loading...',
    'common.noData': 'No data',
    'enterprise.invites.exportCsv': 'Export CSV',
  } as Record<string, string>)[key] || '',
}));

vi.mock('../api/domains/enterprise', () => ({
  enterpriseApi: {
    createInvitationCode: mocks.createInvitationCode,
    deleteInvitationCode: mocks.deleteInvitationCode,
    exportInvitationCodesCsv: mocks.exportInvitationCodesCsv,
    listInvitationCodes: mocks.listInvitationCodes,
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: mocks.t }),
}));

describe('InvitationCodes recovery', () => {
  beforeEach(() => {
    mocks.createInvitationCode.mockReset();
    mocks.deleteInvitationCode.mockReset();
    mocks.exportInvitationCodesCsv.mockReset();
    mocks.listInvitationCodes.mockReset();
    mocks.listInvitationCodes.mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      page_size: 20,
    });
  });

  afterEach(cleanup);

  it('shows a retryable state when the invitation list cannot load', async () => {
    mocks.listInvitationCodes.mockRejectedValueOnce(undefined);
    render(<InvitationCodes />);

    expect((await screen.findByRole('alert')).textContent).toContain('Could not load invitation codes.');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    await waitFor(() => expect(mocks.listInvitationCodes).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  });

  it('restores the generate action after a failed request', async () => {
    mocks.createInvitationCode.mockRejectedValueOnce(undefined);
    render(<InvitationCodes />);
    await waitFor(() => expect(mocks.listInvitationCodes).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: 'Generate' }));

    expect((await screen.findByRole('alert')).textContent).toContain('Could not create invitation codes.');
    expect((screen.getByRole('button', { name: 'Generate' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('announces a successful member invitation-code batch', async () => {
    mocks.createInvitationCode.mockResolvedValue({ created: 5, codes: ['MEMBER001'] });
    render(<InvitationCodes />);
    await waitFor(() => expect(mocks.listInvitationCodes).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: 'Generate' }));

    expect((await screen.findByRole('status')).textContent).toContain('Invitation codes created.');
    expect(mocks.createInvitationCode).toHaveBeenCalledWith({ count: 5, max_uses: 5 });
  });

  it('recovers from disable and export failures without an unhandled request', async () => {
    mocks.listInvitationCodes.mockResolvedValue({
      items: [{
        id: 'invite-1',
        code: 'INVITE-1',
        max_uses: 2,
        used_count: 0,
        is_active: true,
        created_at: '2026-08-31T00:00:00Z',
      }],
      total: 1,
      page: 1,
      page_size: 20,
    });
    mocks.deleteInvitationCode.mockRejectedValueOnce(undefined);
    mocks.exportInvitationCodesCsv.mockRejectedValueOnce(undefined);
    render(<InvitationCodes />);
    await screen.findByText('INVITE-1');

    fireEvent.click(screen.getByRole('button', { name: 'Disable' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Could not disable the invitation code.');
    expect((screen.getByRole('button', { name: 'Disable' }) as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }));
    await waitFor(() => {
      expect(screen.getByRole('alert').textContent).toContain('Could not export invitation codes.');
    });
    expect((screen.getByRole('button', { name: 'Export CSV' }) as HTMLButtonElement).disabled).toBe(false);
  });
});
