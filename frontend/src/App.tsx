/**
 * App root — route tree grouped by surface (public / app / workspace / admin).
 *
 * Single Layout shell shared across all authenticated surfaces.
 * Role guards enforce access per surface.
 */

import { Routes, Route, Navigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useAuthStore } from './stores';
import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { authApi } from './api/domains/auth';
import { get, ApiError } from './api/core';
import { ProtectedRoute, WorkspaceGuard, ScopedAdminGuard, CompanyBusinessGuard, AdminGuard } from './guards';
import { WORKSPACE_SETTINGS_SECTIONS } from './surfaces/workspace/sections';
import AppDialogs from './components/AppDialogs';

const Login = lazy(() => import('./pages/Login'));
const DesignGallery = import.meta.env.DEV
    ? lazy(() => import('./pages/DesignGallery'))
    : null;
const SsoEntry = lazy(() => import('./pages/SsoEntry'));
const CompanySetup = lazy(() => import('./pages/CompanySetup'));
const Dashboard = lazy(() => import('./pages/Dashboard'));
const DigitalEmployees = lazy(() => import('./pages/DigitalEmployees'));
const WorkspaceFeatureHub = lazy(() => import('./pages/WorkspaceFeatureHub'));
const PersonalKnowledge = lazy(() => import('./pages/PersonalKnowledge'));
const CompanyKnowledgeLibrary = lazy(() => import('./pages/CompanyKnowledgeLibrary'));
const CompanyKnowledgeControlPlane = lazy(() => import('./pages/CompanyKnowledgeControlPlane'));
const Plaza = lazy(() => import('./pages/Plaza'));
const AgentDetail = lazy(() => import('./pages/AgentDetail'));
const AgentCreate = lazy(() => import('./pages/AgentCreate'));
const LocalAgents = lazy(() => import('./pages/LocalAgents'));
const Chat = lazy(() => import('./pages/Chat'));
const Messages = lazy(() => import('./pages/Messages'));
const ControlPlane = lazy(() => import('./pages/ControlPlane'));
const AdminCompanies = lazy(() => import('./pages/AdminCompanies'));
const AppLayout = lazy(() => import('./surfaces/app/AppLayout'));
const WorkspaceLayout = lazy(() => import('./surfaces/workspace/WorkspaceLayout'));
const AdminLayout = lazy(() => import('./surfaces/admin/AdminLayout'));

function RouteFallback() {
    const { t } = useTranslation();
    return (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: '40vh', color: 'var(--text-tertiary)' }}>
            {t('common.loading')}
        </div>
    );
}

/* ─── Notification Bar (public, no auth required) ─── */
function NotificationBar() {
    const [config, setConfig] = useState<{ enabled: boolean; text: string } | null>(null);
    const [dismissed, setDismissed] = useState(false);

    useEffect(() => {
        get<{ enabled: boolean; text: string }>('/enterprise/system-settings/notification_bar/public')
            .then(d => setConfig(d))
            .catch(() => {});
    }, []);

    useEffect(() => {
        if (config?.text) {
            const key = `notification_bar_dismissed_${btoa(encodeURIComponent(config.text))}`;
            if (sessionStorage.getItem(key)) setDismissed(true);
        }
    }, [config?.text]);

    const isVisible = !!config?.enabled && !!config?.text && !dismissed;
    useEffect(() => {
        if (isVisible) document.body.classList.add('has-notification-bar');
        else document.body.classList.remove('has-notification-bar');
        return () => { document.body.classList.remove('has-notification-bar'); };
    }, [isVisible]);

    if (!isVisible) return null;

    const handleDismiss = () => {
        const key = `notification_bar_dismissed_${btoa(encodeURIComponent(config!.text))}`;
        sessionStorage.setItem(key, '1');
        setDismissed(true);
    };

    return (
        <div className="notification-bar">
            <span className="notification-bar-text">{config!.text}</span>
            <button className="notification-bar-close" onClick={handleDismiss} aria-label="Close">✕</button>
        </div>
    );
}

export default function App() {
    const { t } = useTranslation();
    const { token, setUser, user } = useAuthStore();
    const [loading, setLoading] = useState(true);
    const revalidationInFlightRef = useRef(false);

    useEffect(() => {
        const savedTheme = localStorage.getItem('theme') || 'light';
        document.documentElement.setAttribute('data-theme', savedTheme);

        // Cold-start revalidation pins the member/org-admin home tenant exactly
        // like setAuth does, but never writes a company for a platform
        // administrator: a valid explicit selection survives reload, an
        // explicitly invalid one is cleared below without silently falling
        // back to the admin's home tenant.
        const applyRevalidatedUser = (u: Parameters<typeof setUser>[0]) => {
            if (u.tenant_id && u.role !== 'platform_admin') {
                localStorage.setItem('current_tenant_id', u.tenant_id);
            }
            setUser(u);
        };

        if (token && !user) {
            // StrictMode dev double-mount fires this effect twice; the first
            // chain owns the outcome and the duplicate must not race its
            // clear-and-retry recovery into a false logout.
            if (revalidationInFlightRef.current) return;
            revalidationInFlightRef.current = true;
            authApi.getMe()
                .then((u) => applyRevalidatedUser(u))
                .catch(async (firstError) => {
                    // A stored company selection the server now rejects (stale,
                    // deleted, or disabled — the typed 400/403/404 X-Tenant-Id
                    // responses) is not an expired login: clear only the
                    // selection and re-validate the bearer without it, so the
                    // authenticated user lands on the company selector. A
                    // genuine 401, or any failure with no selection attached,
                    // still logs out.
                    const hadSelection = Boolean(localStorage.getItem('current_tenant_id'));
                    const status = firstError instanceof ApiError ? firstError.status : null;
                    if (hadSelection && (status === 400 || status === 403 || status === 404)) {
                        localStorage.removeItem('current_tenant_id');
                        try {
                            applyRevalidatedUser(await authApi.getMe());
                            return;
                        } catch {
                            // The bearer itself is rejected even without a
                            // selection — fall through to the real logout.
                        }
                    }
                    useAuthStore.getState().logout();
                })
                .finally(() => setLoading(false));
        } else {
            setLoading(false);
        }
    }, []);

    if (loading) {
        return (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100vh', color: 'var(--text-tertiary)' }}>
                {t('common.loading')}
            </div>
        );
    }

    return (
        <>
            <NotificationBar />
            <AppDialogs />
            <Suspense fallback={<RouteFallback />}>
                <Routes>
                    {/* ─── Public surface ─── */}
                    <Route path="/login" element={<Login />} />
                    <Route path="/sso/entry" element={<SsoEntry />} />
                    <Route path="/setup-company" element={<CompanySetup />} />
                    {/* 设计基线验收面仅供本地开发；生产路径回到认证应用入口。 */}
                    {DesignGallery ? (
                        <Route path="/design-gallery" element={<DesignGallery />} />
                    ) : (
                        <Route path="/design-gallery" element={<Navigate to="/" replace />} />
                    )}

                    {/* ─── App surface ─── */}
                    <Route path="/" element={<ProtectedRoute><AppLayout /></ProtectedRoute>}>

                        <Route index element={<Navigate to="/home" replace />} />
                        <Route path="home" element={<Dashboard />} />
                        <Route path="dashboard" element={<Navigate to="/home" replace />} />
                        <Route path="agents" element={<DigitalEmployees />} />
                        <Route path="plans" element={<WorkspaceFeatureHub kind="plans" />} />
                        <Route path="automations" element={<WorkspaceFeatureHub kind="automations" />} />
                        <Route path="knowledge" element={<PersonalKnowledge />} />
                        <Route path="knowledge/company" element={<CompanyKnowledgeLibrary />} />
                        <Route path="workspace/knowledge" element={<Navigate to="/knowledge" replace />} />
                        <Route path="memory" element={<WorkspaceFeatureHub kind="memory" />} />
                        <Route path="documents" element={<WorkspaceFeatureHub kind="documents" />} />
                        <Route path="approvals" element={<WorkspaceFeatureHub kind="approvals" />} />
                        <Route path="team" element={<WorkspaceFeatureHub kind="team" />} />
                        <Route path="plaza" element={<CompanyBusinessGuard><Plaza /></CompanyBusinessGuard>} />
                        <Route path="local-agents" element={<LocalAgents />} />
                        <Route path="local-bridge/activate" element={<LocalAgents />} />
                        <Route path="agents/new" element={<AgentCreate />} />
                        <Route path="agents/:id" element={<AgentDetail />} />
                        {/* §8.4: the Active Session Workbench is a real route, not a
                            query-string disguise inside Agent Detail. */}
                        <Route path="agents/:id/sessions/:sessionId" element={<AgentDetail />} />
                        <Route path="agents/:id/chat" element={<Chat />} />
                        <Route path="messages" element={<Messages />} />
                    </Route>

                    {/* ─── Workspace surface ─── */}
                    <Route path="/enterprise" element={<ProtectedRoute><WorkspaceGuard><WorkspaceLayout /></WorkspaceGuard></ProtectedRoute>}>
                        <Route index element={<Navigate to="dashboard" replace />} />
                        <Route path="dashboard" element={<ControlPlane />} />
                        <Route path="knowledge" element={<ScopedAdminGuard><CompanyKnowledgeControlPlane /></ScopedAdminGuard>} />
                        {WORKSPACE_SETTINGS_SECTIONS.map((section) => (
                            <Route
                                key={section.tab}
                                path={section.slug}
                                element={<ControlPlane tab={section.tab} />}
                            />
                        ))}
                        <Route path="tools" element={<Navigate to="extensions" replace />} />
                        <Route path="skills" element={<Navigate to="extensions" replace />} />
                        <Route path="subagents" element={<Navigate to="extensions" replace />} />
                    </Route>
                    <Route
                        path="/invitations"
                        element={<ProtectedRoute><WorkspaceGuard><Navigate to="/enterprise/invitations" replace /></WorkspaceGuard></ProtectedRoute>}
                    />

                    {/* ─── Admin surface ─── */}
                    <Route path="/admin" element={<ProtectedRoute><AdminGuard><AdminLayout /></AdminGuard></ProtectedRoute>}>
                        <Route index element={<Navigate to="platform-settings" replace />} />
                        <Route path="platform-settings" element={<AdminCompanies />} />
                    </Route>
                </Routes>
            </Suspense>
        </>
    );
}
