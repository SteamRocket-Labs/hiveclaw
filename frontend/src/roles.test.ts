import { describe, expect, it } from 'vitest';

import { isAdministratorRole, isManagedEmployeeAgent, isScopedBusinessAdminForAgent } from './roles';

const adminUsers = [{ role: 'org_admin' }, { role: 'platform_admin' }];

describe('PDEC-013 role derivation', () => {
  it('recognizes exactly the two administrator roles', () => {
    expect(isAdministratorRole('org_admin')).toBe(true);
    expect(isAdministratorRole('platform_admin')).toBe(true);
    expect(isAdministratorRole('member')).toBe(false);
    expect(isAdministratorRole(undefined)).toBe(false);
    expect(isAdministratorRole(null)).toBe(false);
    // A legacy `manage` grant string is a delegated capability, never a role.
    expect(isAdministratorRole('manage')).toBe(false);
    expect(isAdministratorRole('operator')).toBe(false);
  });

  it.each(adminUsers)('grants the scoped business session lane to %s with the server manage projection', (user) => {
    const agent = { access_level: 'manage', action_capabilities: { can_manage_permissions: true } };
    expect(isScopedBusinessAdminForAgent(user, agent)).toBe(true);
  });

  it('denies the scoped lane to a member-owner and to a legacy manage grant', () => {
    const ownerProjection = { access_level: 'manage', is_owner: true, action_capabilities: { can_manage_permissions: true } };
    expect(isScopedBusinessAdminForAgent({ role: 'member' }, ownerProjection)).toBe(false);

    const legacyGrant = { access_level: 'manage', is_owner: false, action_capabilities: { can_manage_permissions: false } };
    expect(isScopedBusinessAdminForAgent({ role: 'member' }, legacyGrant)).toBe(false);
    expect(isScopedBusinessAdminForAgent({ role: 'org_admin' }, legacyGrant)).toBe(false);
  });

  it('denies the scoped lane for operator-only shells and missing projections', () => {
    const operatorShell = { access_level: 'operator', is_owner: false, action_capabilities: { can_manage_permissions: false } };
    expect(isScopedBusinessAdminForAgent({ role: 'platform_admin' }, operatorShell)).toBe(false);
    expect(isScopedBusinessAdminForAgent({ role: 'platform_admin' }, null)).toBe(false);
    expect(isScopedBusinessAdminForAgent(null, { access_level: 'manage', action_capabilities: { can_manage_permissions: true } })).toBe(false);
  });

  it('labels only non-owned manage rows of administrator viewers as managed employee Agents', () => {
    const employeePrivate = { is_owner: false, access_level: 'manage' };
    expect(isManagedEmployeeAgent({ role: 'org_admin' }, employeePrivate)).toBe(true);
    expect(isManagedEmployeeAgent({ role: 'platform_admin' }, employeePrivate)).toBe(true);
    // Members keep the public/company-shared label for the same row shape.
    expect(isManagedEmployeeAgent({ role: 'member' }, employeePrivate)).toBe(false);
    // The administrator's own Agent is not a managed row.
    expect(isManagedEmployeeAgent({ role: 'org_admin' }, { is_owner: true, access_level: 'manage' })).toBe(false);
    // An operator shell is a technical evidence view, not a managed Agent.
    expect(isManagedEmployeeAgent({ role: 'org_admin' }, { is_owner: false, access_level: 'operator' })).toBe(false);
  });
});
