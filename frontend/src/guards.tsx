/**
 * Route guards — role-based access control for surface routing.
 *
 * - ProtectedRoute: requires authentication + tenant (a tenantless
 *   platform_admin stays in the app: company selection, not company
 *   creation, is that identity's recovery path)
 * - WorkspaceGuard: requires org_admin or platform_admin
 * - ScopedAdminGuard: requires a scoped administrator (org_admin, or
 *   platform_admin acting inside the authenticated selected company; the
 *   server enforces the selection and answers with typed errors without one)
 * - CompanyBusinessGuard: authenticated company business surface; a platform
 *   administrator is a normal business actor inside the selected company
 * - AdminGuard: requires platform_admin only
 */

import { Navigate, useLocation } from 'react-router-dom';
import { useAuthStore } from './stores';
import { isAdministratorRole } from './roles';
import { protectedLoginRedirect } from './routing/authRedirect';

export function ProtectedRoute({ children }: { children: React.ReactNode }) {
    const token = useAuthStore((s) => s.token);
    const user = useAuthStore((s) => s.user);
    const location = useLocation();
    if (!token) {
        const currentPath = `${location.pathname}${location.search}${location.hash}`;
        return <Navigate to={protectedLoginRedirect(currentPath)} replace />;
    }
    if (user && !user.tenant_id && user.role !== 'platform_admin') return <Navigate to="/setup-company" replace />;
    return <>{children}</>;
}

export function WorkspaceGuard({ children }: { children: React.ReactNode }) {
    const user = useAuthStore((s) => s.user);
    if (!user) return <Navigate to="/login" replace />;
    const allowed = ['org_admin', 'platform_admin'];
    if (!allowed.includes(user.role)) return <Navigate to="/dashboard" replace />;
    return <>{children}</>;
}

export function ScopedAdminGuard({ children }: { children: React.ReactNode }) {
    const user = useAuthStore((s) => s.user);
    if (!user) return <Navigate to="/login" replace />;
    if (!isAdministratorRole(user.role)) return <Navigate to="/enterprise/dashboard" replace />;
    return <>{children}</>;
}

export function CompanyBusinessGuard({ children }: { children: React.ReactNode }) {
    const user = useAuthStore((s) => s.user);
    if (!user) return <Navigate to="/login" replace />;
    return <>{children}</>;
}

export function AdminGuard({ children }: { children: React.ReactNode }) {
    const user = useAuthStore((s) => s.user);
    if (!user) return <Navigate to="/login" replace />;
    if (user.role !== 'platform_admin') return <Navigate to="/dashboard" replace />;
    return <>{children}</>;
}
