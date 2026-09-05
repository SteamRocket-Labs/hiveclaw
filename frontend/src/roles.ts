/**
 * PDEC-013 three-role product model — shared role derivation.
 *
 * The server remains authoritative for every grant; these helpers only decide
 * which existing surfaces a role may reach. A legacy `manage` grant on a plain
 * member is not administrator authority, and `can_operator_inspect` is a
 * separate technical capability, not a product role.
 */

/** The two administrator roles hold company business authority in scope. */
export function isAdministratorRole(role: string | null | undefined): boolean {
    return role === 'org_admin' || role === 'platform_admin';
}

interface AdministratorIdentity {
    role?: string | null;
}

interface AgentAuthorityProjection {
    access_level?: string | null;
    is_owner?: boolean;
    action_capabilities?: {
        can_manage_permissions?: boolean;
    } | null;
}

/**
 * Scoped business-administrator signal for one Agent: the authenticated user
 * holds an administrator role AND the server's per-Agent projection grants
 * company management (`access_level="manage"` + `can_manage_permissions`).
 * A member owning the Agent fails the role check; a member with a legacy
 * `manage` grant fails `can_manage_permissions` (the server reports it false
 * for non-owner grants), so neither gains the administrator session lane.
 */
export function isScopedBusinessAdminForAgent(
    user: AdministratorIdentity | null | undefined,
    agent: AgentAuthorityProjection | null | undefined,
): boolean {
    return isAdministratorRole(user?.role)
        && agent?.access_level === 'manage'
        && agent?.action_capabilities?.can_manage_permissions === true;
}

/**
 * A non-owned Agent row inside an administrator's company inventory is a
 * managed employee Agent — not automatically a public/company-shared one.
 * Member viewers keep the existing own/public projection.
 */
export function isManagedEmployeeAgent(
    user: AdministratorIdentity | null | undefined,
    agent: AgentAuthorityProjection | null | undefined,
): boolean {
    return isAdministratorRole(user?.role)
        && agent?.is_owner === false
        && agent?.access_level === 'manage';
}
