/**
 * App root — route tree grouped by surface (public / app / workspace / admin).
 *
 * Single Layout shell shared across all authenticated surfaces.
 * Role guards enforce access per surface.
 */

import { Routes, Route, Navigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useAuthStore } from './stores';
import { lazy, Suspense, useEffect, useState } from 'react';
import { authApi } from './api/domains/auth';
import { get } from './api/core';
import { ProtectedRoute, WorkspaceGuard, AdminGuard } from './guards';
import { WORKSPACE_SECTIONS } from './surfaces/workspace/sections';

const Login = lazy(() => import('./pages/Login'));
const CompanySetup = lazy(() => import('./pages/CompanySetup'));
const Dashboard = lazy(() => import('./pages/Dashboard'));
const Plaza = lazy(() => import('./pages/Plaza'));
const AgentDetail = lazy(() => import('./pages/AgentDetail'));
const AgentCreate = lazy(() => import('./pages/AgentCreate'));
const Chat = lazy(() => import('./pages/Chat'));
const Messages = lazy(() => import('./pages/Messages'));
const EnterpriseSettings = lazy(() => import('./pages/EnterpriseSettings'));
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
    const { token, setAuth, user } = useAuthStore();
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        const savedTheme = localStorage.getItem('theme') || 'light';
        document.documentElement.setAttribute('data-theme', savedTheme);

        if (token && !user) {
            authApi.getMe()
                .then((u) => setAuth(u, token))
                .catch(() => useAuthStore.getState().logout())
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
            <Suspense fallback={<RouteFallback />}>
                <Routes>
                    {/* ─── Public surface ─── */}
                    <Route path="/login" element={<Login />} />
                    <Route path="/setup-company" element={<CompanySetup />} />

                    {/* ─── App surface ─── */}
                    <Route path="/" element={<ProtectedRoute><AppLayout /></ProtectedRoute>}>

                        <Route index element={<Navigate to="/plaza" replace />} />
                        <Route path="dashboard" element={<Dashboard />} />
                        <Route path="plaza" element={<Plaza />} />
                        <Route path="agents/new" element={<AgentCreate />} />
                        <Route path="agents/:id" element={<AgentDetail />} />
                        <Route path="agents/:id/chat" element={<Chat />} />
                        <Route path="messages" element={<Messages />} />
                    </Route>

                    {/* ─── Workspace surface ─── */}
                    <Route path="/enterprise" element={<ProtectedRoute><WorkspaceGuard><WorkspaceLayout /></WorkspaceGuard></ProtectedRoute>}>
                        <Route index element={<Navigate to="info" replace />} />
                        {WORKSPACE_SECTIONS.map((section) => (
                            <Route
                                key={section.tab}
                                path={section.slug}
                                element={<EnterpriseSettings forcedTab={section.tab} hideTabs />}
                            />
                        ))}
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
