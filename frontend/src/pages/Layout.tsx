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
import AppSidebar from './layout/AppSidebar';
import NotificationCenter from './layout/NotificationCenter';

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

    const inputStyle = { width: '100%', fontSize: '13px' };
    const labelStyle = { display: 'block' as const, fontSize: '12px', fontWeight: 500, marginBottom: '4px', color: 'var(--text-secondary)' };

    return (
        <div style={{ position: 'fixed', inset: 0, zIndex: 10000, background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center' }} onClick={onClose}>
            <div style={{ background: 'var(--bg-primary)', borderRadius: '12px', border: '1px solid var(--border-subtle)', width: '420px', maxHeight: '90vh', overflow: 'auto', padding: '24px', boxShadow: '0 20px 60px rgba(0,0,0,0.3)' }} onClick={e => e.stopPropagation()}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px' }}>
                    <h3 style={{ margin: 0 }}>{t('account.title')}</h3>
                    <button onClick={onClose} style={{ background: 'none', border: 'none', color: 'var(--text-tertiary)', fontSize: '18px', cursor: 'pointer', padding: '4px 8px' }}>×</button>
                </div>
                {msg && <div style={{ padding: '8px 12px', borderRadius: '6px', fontSize: '12px', marginBottom: '16px', background: msgType === 'success' ? 'rgba(0,180,120,0.12)' : 'rgba(255,80,80,0.12)', color: msgType === 'success' ? 'var(--success)' : 'var(--error)' }}>{msg}</div>}
                {/* Profile */}
                <h4 style={{ margin: '0 0 12px', fontSize: '13px', color: 'var(--text-secondary)' }}>{t('account.profile')}</h4>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', marginBottom: '20px' }}>
                    <div><label style={labelStyle}>{t('account.username')}</label><input className="form-input" value={username} onChange={e => setUsername(e.target.value)} style={inputStyle} /></div>
                    <div><label style={labelStyle}>{t('account.email')}</label><input className="form-input" type="email" value={email} onChange={e => setEmail(e.target.value)} style={inputStyle} /></div>
                    <div><label style={labelStyle}>{t('account.displayName')}</label><input className="form-input" value={displayName} onChange={e => setDisplayName(e.target.value)} style={inputStyle} /></div>
                    <div style={{ display: 'flex', justifyContent: 'flex-end' }}><button className="btn btn-primary" onClick={handleSaveProfile} disabled={saving} style={{ padding: '6px 16px', fontSize: '12px' }}>{saving ? '...' : t('common.save')}</button></div>
                </div>
                <div style={{ borderTop: '1px solid var(--border-subtle)', marginBottom: '20px' }} />
                {/* Password */}
                <h4 style={{ margin: '0 0 12px', fontSize: '13px', color: 'var(--text-secondary)' }}>{t('account.changePassword')}</h4>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                    <div><label style={labelStyle}>{t('account.currentPassword')}</label><input className="form-input" type="password" value={oldPassword} onChange={e => setOldPassword(e.target.value)} style={inputStyle} /></div>
                    <div><label style={labelStyle}>{t('account.newPassword')}</label><input className="form-input" type="password" value={newPassword} onChange={e => setNewPassword(e.target.value)} placeholder={t('account.minPasswordPlaceholder')} style={inputStyle} /></div>
                    <div><label style={labelStyle}>{t('account.confirmNewPassword')}</label><input className="form-input" type="password" value={confirmPassword} onChange={e => setConfirmPassword(e.target.value)} style={inputStyle} /></div>
                    <div style={{ display: 'flex', justifyContent: 'flex-end' }}><button className="btn btn-primary" onClick={handleChangePassword} disabled={saving} style={{ padding: '6px 16px', fontSize: '12px' }}>{saving ? '...' : t('account.changePassword')}</button></div>
                </div>
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
        <div style={{ textAlign: 'center', fontSize: '10px', color: 'var(--text-quaternary)', marginTop: '8px', letterSpacing: '0.3px' }}>
            v{info.version}
            {info.commit && <span style={{ opacity: 0.6 }}> ({info.commit})</span>}
        </div>
    );
}

export default function Layout() {
    const { t, i18n } = useTranslation();
    const navigate = useNavigate();
    const { user, logout } = useAuthStore();
    const queryClient = useQueryClient();
    const isChinese = i18n.language?.startsWith('zh');
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

    const toggleSidebar = () => {
        setIsSidebarCollapsed(prev => {
            const newState = !prev;
            localStorage.setItem('sidebar_collapsed', String(newState));
            return newState;
        });
    };

    // Sidebar agent search & pin
    const [sidebarSearch, setSidebarSearch] = useState('');
    const [pinnedAgents, setPinnedAgents] = useState<Set<string>>(() => {
        try {
            const stored = localStorage.getItem('pinned_agents');
            return stored ? new Set(JSON.parse(stored)) : new Set();
        } catch { return new Set(); }
    });
    const togglePin = (agentId: string) => {
        setPinnedAgents(prev => {
            const next = new Set(prev);
            if (next.has(agentId)) next.delete(agentId);
            else next.add(agentId);
            localStorage.setItem('pinned_agents', JSON.stringify([...next]));
            return next;
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
        refetchInterval: 30000,
    });

    const handleLogout = () => {
        logout();
        navigate('/login');
    };

    const toggleLang = () => {
        i18n.changeLanguage(i18n.language === 'zh' ? 'en' : 'zh');
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
        <div className="app-layout">
            <AppSidebar
                user={user}
                theme={theme}
                isSidebarCollapsed={isSidebarCollapsed}
                onToggleSidebar={toggleSidebar}
                agents={agents}
                pinnedAgents={pinnedAgents}
                onTogglePin={togglePin}
                tenants={tenants}
                currentTenant={currentTenant}
                onSwitchTenant={switchTenant}
                isChinese={!!isChinese}
                sidebarSearch={sidebarSearch}
                onSetSidebarSearch={setSidebarSearch}
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
