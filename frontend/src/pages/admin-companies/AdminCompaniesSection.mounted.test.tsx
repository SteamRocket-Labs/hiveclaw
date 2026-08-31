// @vitest-environment jsdom

import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import AdminCompaniesSection from './AdminCompaniesSection';

const mocks = vi.hoisted(() => ({
  assignUserToTenant: vi.fn(),
  createCompany: vi.fn(),
  listCompanies: vi.fn(),
  requestAppConfirm: vi.fn(),
  toggleCompany: vi.fn(),
}));

vi.mock('../../api/domains/admin', () => ({
  adminApi: {
    assignUserToTenant: mocks.assignUserToTenant,
    createCompany: mocks.createCompany,
    listCompanies: mocks.listCompanies,
    toggleCompany: mocks.toggleCompany,
  },
}));

vi.mock('../../components/AppDialogs', () => ({
  requestAppConfirm: mocks.requestAppConfirm,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string, values?: Record<string, string>) =>
      (fallback || '').replace(/{{(\w+)}}/g, (_match, key) => values?.[key] || ''),
  }),
}));

vi.mock('@tabler/icons-react', () => ({ IconFilter: () => <span>Filter Icon</span> }));

const company = {
  id: 'company-1',
  name: 'Acme',
  slug: 'acme',
  org_admin_email: null,
  user_count: 1,
  agent_count: 1,
  total_tokens: 0,
  created_at: '2026-08-31T00:00:00Z',
  is_active: true,
};

function openAssignmentForm() {
  render(<AdminCompaniesSection initialCompanies={[company]} />);
  fireEvent.click(screen.getByRole('button', { name: 'Assign admin' }));
  const input = screen.getByPlaceholderText('Registered account email');
  fireEvent.change(input, { target: { value: 'admin@example.com' } });
  return input;
}

describe('AdminCompaniesSection assignment authority flow', () => {
  beforeEach(() => {
    mocks.assignUserToTenant.mockReset();
    mocks.createCompany.mockReset();
    mocks.listCompanies.mockReset();
    mocks.requestAppConfirm.mockReset();
    mocks.toggleCompany.mockReset();
    mocks.listCompanies.mockResolvedValue([{ ...company, org_admin_email: 'admin@example.com' }]);
  });

  afterEach(cleanup);

  it('shows the one-time administrator invitation returned by company creation', async () => {
    mocks.createCompany.mockResolvedValue({
      company: { ...company, id: 'company-2', name: 'Northwind', slug: 'northwind' },
      admin_invitation_code: 'ADMIN-CODE-1',
    });
    render(<AdminCompaniesSection initialCompanies={[company]} />);

    fireEvent.click(screen.getByRole('button', { name: '+ Create Company' }));
    fireEvent.change(screen.getByPlaceholderText('Company name'), { target: { value: 'Northwind' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));

    expect(await screen.findByText('ADMIN-CODE-1')).toBeTruthy();
    expect(screen.getByRole('dialog', { name: 'Company Created' })).toBeTruthy();
    expect(screen.getByText('Northwind')).toBeTruthy();
    expect(screen.getByText(/This code grants company administrator access and is single-use\./)).toBeTruthy();
    expect(screen.getByText(/use Assign admin with their registered email/)).toBeTruthy();
    expect(mocks.createCompany).toHaveBeenCalledWith({ name: 'Northwind' });

    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByText('ADMIN-CODE-1')).toBeNull();
  });

  it('reports clipboard failure without closing the administrator invitation dialog', async () => {
    mocks.createCompany.mockResolvedValue({
      company: { ...company, id: 'company-2', name: 'Northwind', slug: 'northwind' },
      admin_invitation_code: 'ADMIN-CODE-1',
    });
    const writeText = vi.fn().mockRejectedValue(new Error('clipboard denied'));
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    render(<AdminCompaniesSection initialCompanies={[company]} />);

    fireEvent.click(screen.getByRole('button', { name: '+ Create Company' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Company name' }), { target: { value: 'Northwind' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));
    await screen.findByRole('dialog', { name: 'Company Created' });
    fireEvent.click(screen.getByRole('button', { name: 'Copy Code' }));

    expect((await screen.findByRole('alert')).textContent).toContain('Could not copy the invitation code.');
    expect(writeText).toHaveBeenCalledWith('ADMIN-CODE-1');
    expect(screen.getByRole('dialog', { name: 'Company Created' })).toBeTruthy();
  });

  it('explains that disabling stops running employees and re-enable does not restart them', async () => {
    mocks.requestAppConfirm.mockResolvedValue(false);
    render(<AdminCompaniesSection initialCompanies={[company]} />);

    fireEvent.click(screen.getByRole('button', { name: 'Disable' }));

    await waitFor(() => expect(mocks.requestAppConfirm).toHaveBeenCalledWith(expect.objectContaining({
      message: 'Disable this company? Users will lose company access and running employees will be stopped. Re-enabling does not restart them automatically.',
    })));
    expect(mocks.toggleCompany).not.toHaveBeenCalled();
  });

  it('does not mutate when the operator cancels confirmation', async () => {
    mocks.requestAppConfirm.mockResolvedValue(false);
    const input = openAssignmentForm();

    fireEvent.keyDown(input, { key: 'Enter' });

    await waitFor(() => expect(mocks.requestAppConfirm).toHaveBeenCalledTimes(1));
    expect(mocks.assignUserToTenant).not.toHaveBeenCalled();
  });

  it('deduplicates repeated submit while confirmation is pending', async () => {
    let resolveConfirmation: (confirmed: boolean) => void = () => {};
    mocks.requestAppConfirm.mockImplementation(
      () => new Promise<boolean>((resolve) => { resolveConfirmation = resolve; }),
    );
    mocks.assignUserToTenant.mockResolvedValue({
      status: 'ok',
      user_id: 'user-1',
      tenant_id: company.id,
      role: 'org_admin',
      membership_committed: true,
      client_token_refresh_required: true,
    });
    const input = openAssignmentForm();

    fireEvent.keyDown(input, { key: 'Enter' });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(mocks.requestAppConfirm).toHaveBeenCalledTimes(1);

    resolveConfirmation(true);
    await waitFor(() => expect(mocks.assignUserToTenant).toHaveBeenCalledTimes(1));
    expect(mocks.assignUserToTenant).toHaveBeenCalledWith(company.id, {
      email: 'admin@example.com',
      role: 'org_admin',
    });
    await screen.findByText("Company administrator assignment committed. Refresh the account's signed-in session; clients without token refresh must sign in again.");
  });

  it('restores the form after a failed assignment', async () => {
    mocks.requestAppConfirm.mockResolvedValue(true);
    mocks.assignUserToTenant.mockRejectedValue(new Error('assignment failed'));
    const input = openAssignmentForm();

    fireEvent.keyDown(input, { key: 'Enter' });

    await screen.findByText('assignment failed');
    expect(screen.getByPlaceholderText('Registered account email')).toBeTruthy();
    expect((screen.getAllByRole('button', { name: 'Assign admin' })[0] as HTMLButtonElement).disabled).toBe(false);
  });

  it('renders an exact replay as already assigned', async () => {
    mocks.requestAppConfirm.mockResolvedValue(true);
    mocks.assignUserToTenant.mockResolvedValue({
      status: 'already_assigned',
      user_id: 'user-1',
      tenant_id: company.id,
      role: 'org_admin',
      membership_committed: true,
      client_token_refresh_required: true,
    });
    const input = openAssignmentForm();

    fireEvent.keyDown(input, { key: 'Enter' });

    await screen.findByText("This account already has company administrator access. Refresh the account's signed-in session; clients without token refresh must sign in again.");
    expect(mocks.assignUserToTenant).toHaveBeenCalledTimes(1);
  });

  it('keeps the committed verdict when the company list refresh fails', async () => {
    mocks.requestAppConfirm.mockResolvedValue(true);
    mocks.assignUserToTenant.mockResolvedValue({
      status: 'ok',
      user_id: 'user-1',
      tenant_id: company.id,
      role: 'org_admin',
      membership_committed: true,
      client_token_refresh_required: true,
    });
    mocks.listCompanies.mockRejectedValue(new Error('refresh failed'));
    const input = openAssignmentForm();

    fireEvent.keyDown(input, { key: 'Enter' });

    await screen.findByText('Administrator assignment committed, but the company list could not be refreshed.');
    expect(screen.queryByText('Failed to assign company administrator.')).toBeNull();
    expect(screen.queryByPlaceholderText('Registered account email')).toBeNull();
  });
});
