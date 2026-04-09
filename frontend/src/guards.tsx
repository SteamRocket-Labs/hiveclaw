/**
 * Route guards — role-based access control for surface routing.
 *
 * - ProtectedRoute: requires authentication + tenant
 * - WorkspaceGuard: requires org_admin or platform_admin
 * - AdminGuard: requires platform_admin only
 */

import { Navigate } from 'react-router-dom';
import { useAuthStore } from './stores';

export function ProtectedRoute({ children }: { children: React.ReactNode }) {
    const token = useAuthStore((s) => s.token);
    const user = useAuthStore((s) => s.user);
    if (!token) return <Navigate to="/login" replace />;
    if (user && !user.tenant_id) return <Navigate to="/setup-company" replace />;
    return <>{children}</>;
}

export function WorkspaceGuard({ children }: { children: React.ReactNode }) {
    const user = useAuthStore((s) => s.user);
    if (!user) return <Navigate to="/login" replace />;
    const allowed = ['org_admin', 'platform_admin'];
    if (!allowed.includes(user.role)) return <Navigate to="/dashboard" replace />;
    return <>{children}</>;
}

export function AdminGuard({ children }: { children: React.ReactNode }) {
    const user = useAuthStore((s) => s.user);
    if (!user) return <Navigate to="/login" replace />;
    if (user.role !== 'platform_admin') return <Navigate to="/dashboard" replace />;
    return <>{children}</>;
}
