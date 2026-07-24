import { useState, useEffect, useRef } from 'react';
import { Outlet, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuthStore } from '../stores';
import { agentApi } from '../api/domains/agents';
import { authApi } from '../api/domains/auth';
import { notificationsApi } from '../api/domains/notifications';
import { systemApi } from '../api/domains/system';
import { adminApi } from '../api/domains/admin';
import { isChineseLanguage, nextInterfaceLanguage } from '../i18n/language';
import AppSidebar, {
    COMPACT_SIDEBAR_MAX_WIDTH,
    isCompactSidebarViewport,
} from './layout/AppSidebar';
import NotificationCenter from './layout/NotificationCenter';
import './Layout.css';

/* ────── Account Settings Modal ────── */
function AccountSettingsModal({ user, onClose }: { user: any; onClose: () => void }) {
    const { t } = useTranslation();
    const { setUser } = useAuthStore();
    const [username, setUsername] = useState(user?.username || '');
    const [email, setEmail] = useState(user?.email || '');
    const [displayName, setDisplayName] = useState(user?.display_name || '');
    const [oldPassword, setOldPassword] = useState('');
    const [newPassword, setNewPassword] = useState('');
    const [confirmPassword, setConfirmPassword] = useState('');
    const [saving, setSaving] = useState(false);
    const [msg, setMsg] = useState('');
    const [msgType, setMsgType] = useState<'success' | 'error'>('success');
    const [feishuBinding, setFeishuBinding] = useState(false);
    const feishuPollRef = useRef(false);

    const showMsg = (text: string, type: 'success' | 'error' = 'success') => {
        setMsg(text); setMsgType(type); setTimeout(() => setMsg(''), 3000);
    };

    const handleSaveProfile = async () => {
        setSaving(true);
        try {
            const body: Record<string, string> = {};
            if (username !== user?.username) body.username = username;
            if (email !== user?.email) body.email = email;
            if (displayName !== user?.display_name) body.display_name = displayName;
            if (Object.keys(body).length === 0) { showMsg(t('account.noChanges'), 'error'); setSaving(false); return; }
            const updated = await authApi.updateMe(body);
            setUser(updated);
            showMsg(t('account.profileUpdated'));
        } catch (e: any) { showMsg(e.message || 'Failed', 'error'); }
        setSaving(false);
    };

    const handleChangePassword = async () => {
        if (!oldPassword || !newPassword) { showMsg(t('account.fillAllPasswordFields'), 'error'); return; }
        if (newPassword.length < 6) { showMsg(t('account.minPasswordLength'), 'error'); return; }
        if (newPassword !== confirmPassword) { showMsg(t('account.passwordsDoNotMatch'), 'error'); return; }
        setSaving(true);
        try {
            await authApi.changePassword({ old_password: oldPassword, new_password: newPassword });
            showMsg(t('account.passwordChanged'));
            setOldPassword(''); setNewPassword(''); setConfirmPassword('');
        } catch (e: any) { showMsg(e.message || 'Failed', 'error'); }
        setSaving(false);
    };

    return (
        <div className="ui-modal-overlay" onClick={onClose}>
            <div className="app-layout-account-modal" onClick={e => e.stopPropagation()}>
                <div className="app-layout-account-header">
                    <h3 className="app-layout-account-heading">{t('account.title')}</h3>
                    <button className="app-layout-account-close" onClick={onClose}>×</button>
                </div>
                {msg && <div className={`app-layout-account-msg app-layout-account-msg--${msgType}`}>{msg}</div>}
                {/* Profile */}
                <h4 className="app-layout-account-subhead">{t('account.profile')}</h4>
                <div className="app-layout-account-fields app-layout-account-fields--mb">
                    <div><label className="app-layout-account-label">{t('account.username')}</label><input className="form-input app-layout-account-input" value={username} onChange={e => setUsername(e.target.value)} /></div>
                    <div><label className="app-layout-account-label">{t('account.email')}</label><input className="form-input app-layout-account-input" type="email" value={email} onChange={e => setEmail(e.target.value)} /></div>
                    <div><label className="app-layout-account-label">{t('account.displayName')}</label><input className="form-input app-layout-account-input" value={displayName} onChange={e => setDisplayName(e.target.value)} /></div>
                    <div className="app-layout-account-actions"><button className="btn btn-primary" onClick={handleSaveProfile} disabled={saving}>{saving ? '...' : t('common.save')}</button></div>
                </div>
                <div className="app-layout-account-divider" />
                {/* Password */}
                <h4 className="app-layout-account-subhead">{t('account.changePassword')}</h4>
                <div className="app-layout-account-fields">
                    <div><label className="app-layout-account-label">{t('account.currentPassword')}</label><input className="form-input app-layout-account-input" type="password" value={oldPassword} onChange={e => setOldPassword(e.target.value)} /></div>
                    <div><label className="app-layout-account-label">{t('account.newPassword')}</label><input className="form-input app-layout-account-input" type="password" value={newPassword} onChange={e => setNewPassword(e.target.value)} placeholder={t('account.minPasswordPlaceholder')} /></div>
                    <div><label className="app-layout-account-label">{t('account.confirmNewPassword')}</label><input className="form-input app-layout-account-input" type="password" value={confirmPassword} onChange={e => setConfirmPassword(e.target.value)} /></div>
                    <div className="app-layout-account-actions"><button className="btn btn-primary" onClick={handleChangePassword} disabled={saving}>{saving ? '...' : t('account.changePassword')}</button></div>
                </div>
                <div className="app-layout-account-divider app-layout-account-divider--top" />
                {/* Feishu Bind */}
                <h4 className="app-layout-account-subhead">{t('account.feishuBind', 'Feishu Account')}</h4>
                {user?.feishu_user_id || user?.feishu_open_id ? (
                    <div className="app-layout-account-feishu-bound">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none"><path d="M3 4.5L10.5 8.5L14 20.5L21 6L10.5 12.5L3 4.5Z" fill="#3370FF"/><path d="M3 4.5L10.5 12.5L14 20.5L10.5 8.5L3 4.5Z" fill="#1456F0" opacity="0.7"/></svg>
                        <div className="app-layout-account-feishu-info">
                            <div className="app-layout-account-feishu-title">{t('account.feishuBound', 'Feishu account linked')}</div>
                            <div className="app-layout-account-feishu-id">{user.feishu_user_id || user.feishu_open_id}</div>
                        </div>
                    </div>
                ) : (
                    <div>
                        <p className="app-layout-account-feishu-desc">
                            {t('account.feishuBindDesc', 'Link your Feishu account to enable Feishu login and seamless identity matching.')}
                        </p>
                        <button
                            className="btn app-layout-account-feishu-btn"
                            disabled={feishuBinding}
                            onClick={async () => {
                                setFeishuBinding(true);
                                try {
                                    const { session_id, authorize_url } = await authApi.feishuBindInit();
                                    const popup = window.open(authorize_url, 'feishu_bind', 'width=600,height=700,popup=yes');
                                    feishuPollRef.current = true;
                                    let attempts = 0;
                                    while (feishuPollRef.current && attempts < 120) {
                                        await new Promise(r => setTimeout(r, 1500));
                                        if (!feishuPollRef.current) break;
                                        try {
                                            const res = await authApi.feishuSsoPoll(session_id);
                                            if (res.status === 'completed') {
                                                popup?.close();
                                                const updated = await authApi.getMe();
                                                setUser(updated);
                                                showMsg(t('account.feishuBindSuccess', 'Feishu account linked successfully'));
                                                break;
                                            }
                                            if (res.status === 'expired' || res.status === 'error') {
                                                popup?.close();
                                                showMsg(res.detail || t('account.feishuBindFailed', 'Failed to link Feishu'), 'error');
                                                break;
                                            }
                                        } catch { /* network blip */ }
                                        if (popup?.closed) break;
                                        attempts++;
                                    }
                                } catch (e: any) {
                                    showMsg(e.message || 'Failed', 'error');
                                } finally {
                                    feishuPollRef.current = false;
                                    setFeishuBinding(false);
                                }
                            }}
                        >
                            {feishuBinding ? <span className="login-spinner app-layout-account-spinner" /> : (
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none"><path d="M3 4.5L10.5 8.5L14 20.5L21 6L10.5 12.5L3 4.5Z" fill="#3370FF"/><path d="M3 4.5L10.5 12.5L14 20.5L10.5 8.5L3 4.5Z" fill="#1456F0" opacity="0.7"/></svg>
                            )}
                            {t('account.feishuBindBtn', 'Link Feishu Account')}
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
}

/* ────── Version Display (runtime) ────── */
function VersionDisplay() {
    const [info, setInfo] = useState<{ version?: string; commit?: string }>({});
    useEffect(() => {
        systemApi.getVersion().then(r => setInfo({ version: r.version })).catch(() => {});
    }, []);
    if (!info.version) return null;
    return (
        <div className="app-layout-version">
            v{info.version}
            {info.commit && <span className="app-layout-version-commit"> ({info.commit})</span>}
        </div>
    );
}

export default function Layout() {
    const { t, i18n } = useTranslation();
    const navigate = useNavigate();
    const { user, logout } = useAuthStore();
    const queryClient = useQueryClient();
    const isChinese = isChineseLanguage(i18n.language);
    const [showAccountSettings, setShowAccountSettings] = useState(false);
    const [showAccountMenu, setShowAccountMenu] = useState(false);
    const accountMenuRef = useRef<HTMLDivElement>(null);
    const [showNotifications, setShowNotifications] = useState(false);
    const [notifCategory, setNotifCategory] = useState<string>('all');
    const [selectedNotification, setSelectedNotification] = useState<any | null>(null);

    // Notification polling
    const { data: unreadCount = 0 } = useQuery({
        queryKey: ['notifications-unread'],
        queryFn: async () => {
            const res = await notificationsApi.getUnreadCount().catch(() => ({ unread_count: 0 }));
            return res.unread_count || 0;
        },
        refetchInterval: 30000,
        enabled: !!user,
    });
    const { data: notifications = [], refetch: refetchNotifications } = useQuery({
        queryKey: ['notifications', notifCategory],
        queryFn: () => notificationsApi.list({
            limit: 50,
            ...(notifCategory !== 'all' ? { category: notifCategory } : {}),
        }).catch(() => []),
        enabled: !!user && showNotifications,
    });
    const markAllRead = async () => {
        await notificationsApi.markAllRead();
        queryClient.invalidateQueries({ queryKey: ['notifications-unread'] });
        queryClient.invalidateQueries({ queryKey: ['notifications'] });
    };
    const markOneRead = async (id: string) => {
        await notificationsApi.markRead(id);
        queryClient.invalidateQueries({ queryKey: ['notifications-unread'] });
        queryClient.invalidateQueries({ queryKey: ['notifications'] });
    };
    const handleNotificationClick = async (notification: any) => {
        if (!notification.is_read) {
            await markOneRead(notification.id);
        }
        if (notification.type === 'broadcast' || !notification.link) {
            setSelectedNotification(notification);
            return;
        }
        navigate(notification.link);
        setShowNotifications(false);
    };

    // Theme
    const [theme, setTheme] = useState<'dark' | 'light'>(() => {
        return (localStorage.getItem('theme') as 'dark' | 'light') || 'light';
    });

    useEffect(() => {
        document.documentElement.setAttribute('data-theme', theme);
        localStorage.setItem('theme', theme);
    }, [theme]);

    const toggleTheme = () => setTheme(prev => prev === 'dark' ? 'light' : 'dark');

    // Sidebar collapse state
    const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(() => {
        return localStorage.getItem('sidebar_collapsed') === 'true';
    });
    const [isCompactSidebar, setIsCompactSidebar] = useState(() => (
        typeof window !== 'undefined' && isCompactSidebarViewport(window.innerWidth)
    ));
    const [isCompactSidebarOpen, setIsCompactSidebarOpen] = useState(false);

    useEffect(() => {
        const media = window.matchMedia(`(max-width: ${COMPACT_SIDEBAR_MAX_WIDTH}px)`);
        const syncViewport = (matches: boolean) => {
            setIsCompactSidebar(matches);
            setIsCompactSidebarOpen(false);
        };
        const handleChange = (event: MediaQueryListEvent) => syncViewport(event.matches);
        syncViewport(media.matches);
        media.addEventListener('change', handleChange);
        return () => media.removeEventListener('change', handleChange);
    }, []);

    const effectiveSidebarCollapsed = isCompactSidebar ? !isCompactSidebarOpen : isSidebarCollapsed;

    const toggleSidebar = () => {
        if (isCompactSidebar) {
            setIsCompactSidebarOpen(prev => !prev);
            return;
        }
        setIsSidebarCollapsed(prev => {
            const newState = !prev;
            localStorage.setItem('sidebar_collapsed', String(newState));
            return newState;
        });
    };

    // Tenant switching (platform_admin)
    const isPlatformAdmin = user?.role === 'platform_admin';
    const [currentTenant, setCurrentTenant] = useState(() =>
        localStorage.getItem('current_tenant_id') || user?.tenant_id || ''
    );
    const [tenants, setTenants] = useState<{ id: string; name: string }[]>([]);

    useEffect(() => {
        if (!isPlatformAdmin) {
            const tid = user?.tenant_id || '';
            setCurrentTenant(tid);
            if (tid) localStorage.setItem('current_tenant_id', tid);
            return;
        }
        adminApi.listCompanies().then((companies: any[]) => {
            setTenants(companies.map((c: any) => ({ id: c.id, name: c.name })));
            if (!localStorage.getItem('current_tenant_id') && user?.tenant_id) {
                localStorage.setItem('current_tenant_id', user.tenant_id);
            }
        }).catch(() => {});
    }, [isPlatformAdmin, user?.tenant_id]);

    const switchTenant = (tenantId: string) => {
        setCurrentTenant(tenantId);
        localStorage.setItem('current_tenant_id', tenantId);
        window.dispatchEvent(new StorageEvent('storage', { key: 'current_tenant_id', newValue: tenantId }));
        queryClient.invalidateQueries({ queryKey: ['agents'] });
    };

    const { data: agents = [] } = useQuery({
        queryKey: ['agents', currentTenant],
        queryFn: () => agentApi.list(currentTenant || undefined),
        staleTime: 30000,
        refetchInterval: 120000,
    });

    const handleLogout = () => {
        logout();
        navigate('/login');
    };

    const toggleLang = () => {
        i18n.changeLanguage(nextInterfaceLanguage(i18n.language));
    };

    useEffect(() => {
        const handleClickOutside = (e: MouseEvent) => {
            if (accountMenuRef.current && !accountMenuRef.current.contains(e.target as Node)) {
                setShowAccountMenu(false);
            }
        };
        if (showAccountMenu) document.addEventListener('mousedown', handleClickOutside);
        return () => document.removeEventListener('mousedown', handleClickOutside);
    }, [showAccountMenu]);

    return (
        <div className={`app-layout${effectiveSidebarCollapsed ? ' sidebar-collapsed' : ''}`}>
            <AppSidebar
                user={user}
                theme={theme}
                isSidebarCollapsed={effectiveSidebarCollapsed}
                onToggleSidebar={toggleSidebar}
                agents={agents}
                tenants={tenants}
                currentTenant={currentTenant}
                onSwitchTenant={switchTenant}
                isChinese={!!isChinese}
                onToggleTheme={toggleTheme}
                onOpenNotifications={() => {
                    setShowNotifications(v => !v);
                    if (!showNotifications) refetchNotifications();
                }}
                unreadCount={Number(unreadCount) || 0}
                accountMenuRef={accountMenuRef}
                showAccountMenu={showAccountMenu}
                onToggleAccountMenu={() => setShowAccountMenu(v => !v)}
                onToggleLang={() => {
                    toggleLang();
                    setShowAccountMenu(false);
                }}
                onOpenAccountSettings={() => {
                    setShowAccountSettings(true);
                    setShowAccountMenu(false);
                }}
                onLogout={() => {
                    handleLogout();
                    setShowAccountMenu(false);
                }}
                versionDisplay={<VersionDisplay />}
            />

            <NotificationCenter
                isOpen={showNotifications}
                unreadCount={Number(unreadCount) || 0}
                notifications={notifications as any[]}
                notifCategory={notifCategory}
                onSetNotifCategory={setNotifCategory}
                onMarkAllRead={markAllRead}
                onClose={() => setShowNotifications(false)}
                onNotificationClick={handleNotificationClick}
                selectedNotification={selectedNotification}
                onCloseDetail={() => setSelectedNotification(null)}
            />

            <main className="main-content">
                <Outlet />
            </main>

            {showAccountSettings && (
                <AccountSettingsModal
                    user={user}
                    onClose={() => setShowAccountSettings(false)}
                />
            )}
        </div>
    );
}
