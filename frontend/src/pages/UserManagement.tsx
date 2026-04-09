/**
 * User Management — admin page to view and manage user quotas and roles.
 */
import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { usersApi } from '../api/domains/users';
import { useAuthStore } from '../stores';

interface UserInfo {
    id: string;
    username: string;
    email: string;
    display_name: string;
    role: string;
    is_active: boolean;
    quota_message_limit: number;
    quota_message_period: string;
    quota_messages_used: number;
    quota_max_agents: number;
    quota_agent_ttl_hours: number;
    agents_count: number;
    feishu_open_id?: string;
    created_at?: string;
    source?: string;
}
const PERIOD_OPTIONS = [
    { value: 'permanent', label: 'Permanent' },
    { value: 'daily', label: 'Daily' },
    { value: 'weekly', label: 'Weekly' },
    { value: 'monthly', label: 'Monthly' },
];

const PAGE_SIZE = 15;

export default function UserManagement() {
    const { t, i18n } = useTranslation();
    const { user: currentUser, setUser } = useAuthStore();

    const [users, setUsers] = useState<UserInfo[]>([]);
    const [loading, setLoading] = useState(true);
    const [editingUserId, setEditingUserId] = useState<string | null>(null);
    const [editForm, setEditForm] = useState({
        quota_message_limit: 50,
        quota_message_period: 'permanent',
        quota_max_agents: 2,
        quota_agent_ttl_hours: 48,
    });
    const [saving, setSaving] = useState(false);
    const [toast, setToast] = useState('');
    const [changingRoleUserId, setChangingRoleUserId] = useState<string | null>(null);

    // Search, sort & pagination
    const [searchQuery, setSearchQuery] = useState('');
    const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('desc');
    const [page, setPage] = useState(1);

    const loadUsers = async () => {
        setLoading(true);
        try {
            const tenantId = localStorage.getItem('current_tenant_id') || '';
            const data = await usersApi.list(tenantId) as UserInfo[];
            setUsers(data);
        } catch (e) {
            console.error('Failed to load users', e);
        }
        setLoading(false);
    };

    useEffect(() => { loadUsers(); }, []);

    const startEdit = (user: UserInfo) => {
        setEditingUserId(user.id);
        setEditForm({
            quota_message_limit: user.quota_message_limit,
            quota_message_period: user.quota_message_period,
            quota_max_agents: user.quota_max_agents,
            quota_agent_ttl_hours: user.quota_agent_ttl_hours,
        });
    };

    const handleSave = async () => {
        if (!editingUserId) return;
        setSaving(true);
        try {
            await usersApi.updateQuota(editingUserId, editForm);
            setToast(`✅ ${t('userManagement.quotaUpdated')}`);
            setTimeout(() => setToast(''), 2000);
            setEditingUserId(null);
            loadUsers();
        } catch (e: any) {
            setToast(`❌ ${e.message}`);
            setTimeout(() => setToast(''), 3000);
        }
        setSaving(false);
    };

    // ── Role change handler ──
    const handleRoleChange = async (userId: string, newRole: string) => {
        setChangingRoleUserId(userId);
        try {
            await usersApi.updateRole(userId, newRole);
            setToast(t('userManagement.roleUpdated'));
            setTimeout(() => setToast(''), 2000);
            // If changed own role, update auth store
            if (userId === currentUser?.id) {
                setUser({ ...currentUser, role: newRole as any });
            }
            loadUsers();
        } catch (e: any) {
            const detail = (() => { try { return JSON.parse(e.message)?.detail; } catch { return e.message; } })();
            setToast(`Error: ${detail || e.message}`);
            setTimeout(() => setToast(''), 4000);
        }
        setChangingRoleUserId(null);
    };

    const periodLabel = (period: string) => {
        const map: Record<string, string> = { permanent: 'permanent', daily: 'daily', weekly: 'weekly', monthly: 'monthly' };
        return t(`userManagement.period_${map[period] || period}`, period);
    };

    // Role label & styling helpers
    const roleBadge = (role: string) => {
        const styles: Record<string, { bg: string; color: string; key: string }> = {
            platform_admin: { bg: 'rgba(239,68,68,0.12)', color: '#ef4444', key: 'userManagement.rolePlatformAdmin' },
            org_admin:      { bg: 'rgba(168,85,247,0.12)', color: '#a855f7', key: 'userManagement.roleAdmin' },
        };
        const s = styles[role];
        if (!s) return null;
        return (
            <span style={{ marginLeft: '6px', fontSize: '10px', background: s.bg, color: s.color, borderRadius: '4px', padding: '1px 6px', fontWeight: 500 }}>
                {t(s.key)}
            </span>
        );
    };

    const formatDate = (iso?: string) => {
        if (!iso) return '-';
        const d = new Date(iso);
        const locale = i18n.language?.startsWith('zh') ? 'zh-CN' : 'en-US';
        return d.toLocaleString(locale, { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    };

    // Search filter
    const filtered = searchQuery.trim()
        ? users.filter(u => {
            const q = searchQuery.toLowerCase();
            return (u.username?.toLowerCase().includes(q))
                || (u.display_name?.toLowerCase().includes(q))
                || (u.email?.toLowerCase().includes(q));
        })
        : users;

    // Sort
    const sorted = [...filtered].sort((a, b) => {
        const ta = a.created_at ? new Date(a.created_at).getTime() : 0;
        const tb = b.created_at ? new Date(b.created_at).getTime() : 0;
        return sortOrder === 'asc' ? ta - tb : tb - ta;
    });

    // Paginate
    const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
    const paged = sorted.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

    const toggleSort = () => {
        setSortOrder(o => o === 'asc' ? 'desc' : 'asc');
        setPage(1);
    };

    return (
        <div>
            {toast && (
                <div style={{
                    position: 'fixed', top: '20px', right: '20px', padding: '10px 20px',
                    borderRadius: '8px', background: toast.startsWith('✅') ? 'var(--success)' : 'var(--error)',
                    color: '#fff', fontSize: '13px', zIndex: 9999, transition: 'all 0.3s',
                }}>
                    {toast}
                </div>
            )}

            {loading ? (
                <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>
                    {t('common.loading')}...
                </div>
            ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                    {/* Search bar */}
                    <div style={{ position: 'relative', marginBottom: '4px' }}>
                        <input
                            className="form-input"
                            type="text"
                            placeholder={t('userManagement.searchPlaceholder')}
                            value={searchQuery}
                            onChange={e => { setSearchQuery(e.target.value); setPage(1); }}
                            style={{
                                width: '100%', maxWidth: '360px', fontSize: '13px',
                                padding: '8px 12px 8px 12px',
                                background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)',
                                borderRadius: '8px',
                            }}
                        />
                        {searchQuery && (
                            <span style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginLeft: '12px' }}>
                                {t('userManagement.userCount', { filtered: filtered.length, total: users.length })}
                            </span>
                        )}
                    </div>

                    {/* Header */}
                    <div style={{
                        display: 'grid', gridTemplateColumns: '1.4fr 1.4fr 0.8fr 0.7fr 0.7fr 0.8fr 0.8fr 0.8fr 0.8fr 100px',
                        gap: '10px', padding: '10px 16px', fontSize: '11px', fontWeight: 600,
                        color: 'var(--text-tertiary)', textTransform: 'uppercase', letterSpacing: '0.05em',
                    }}>
                        <div>{t('userManagement.headerUser')}</div>
                        <div>{t('userManagement.headerEmail')}</div>
                        {/* Created At with sort toggle */}
                        <div
                            style={{ cursor: 'pointer', userSelect: 'none', display: 'flex', alignItems: 'center', gap: '3px' }}
                            onClick={toggleSort}
                            title={t('userManagement.sortTooltip')}
                        >
                            {t('userManagement.headerJoined')} {sortOrder === 'asc' ? '↑' : '↓'}
                        </div>
                        <div>{t('userManagement.headerRole')}</div>
                        <div>{t('userManagement.headerSource')}</div>
                        <div>{t('userManagement.headerMsgQuota')}</div>
                        <div>{t('userManagement.headerPeriod')}</div>
                        <div>{t('userManagement.headerAgents')}</div>
                        <div>{t('userManagement.headerTTL')}</div>
                        <div></div>
                    </div>

                    {paged.map(user => (
                        <div key={user.id}>
                            <div className="card" style={{
                                display: 'grid', gridTemplateColumns: '1.4fr 1.4fr 0.8fr 0.7fr 0.7fr 0.8fr 0.8fr 0.8fr 0.8fr 100px',
                                gap: '10px', alignItems: 'center', padding: '12px 16px',
                            }}>
                                <div>
                                    <div style={{ fontWeight: 500, fontSize: '14px' }}>
                                        {user.display_name || user.username}
                                        {roleBadge(user.role)}
                                    </div>
                                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>@{user.username}</div>
                                </div>
                                <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{user.email}</div>
                                <div style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>{formatDate(user.created_at)}</div>
                                {/* Role selector — only for admin users, not for platform_admin targets */}
                                <div>
                                    {currentUser?.role && ['platform_admin', 'org_admin'].includes(currentUser.role) && user.role !== 'platform_admin' ? (
                                        <select
                                            className="form-input"
                                            value={user.role}
                                            disabled={changingRoleUserId === user.id}
                                            onChange={e => {
                                                const newRole = e.target.value;
                                                const roleName = newRole === 'org_admin' ? t('userManagement.roleAdmin') : t('userManagement.roleMember');
                                                const confirmMsg = t('userManagement.confirmRoleChange', { name: user.display_name || user.username, role: roleName });
                                                if (confirm(confirmMsg)) handleRoleChange(user.id, newRole);
                                            }}
                                            style={{ fontSize: '11px', padding: '2px 4px', width: '100%', minWidth: 0 }}
                                        >
                                            <option value="member">{t('userManagement.roleMember')}</option>
                                            <option value="org_admin">{t('userManagement.roleAdmin')}</option>
                                        </select>
                                    ) : (
                                        <span style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>
                                            {user.role === 'platform_admin' ? t('userManagement.rolePlatformAdmin')
                                                : user.role === 'org_admin' ? t('userManagement.roleAdmin') : t('userManagement.roleMember')}
                                        </span>
                                    )}
                                </div>
                                <div>
                                    {user.source === 'feishu' ? (
                                        <span style={{ fontSize: '10px', background: 'rgba(58,132,255,0.12)', color: '#3a84ff', borderRadius: '4px', padding: '2px 7px', whiteSpace: 'nowrap' }}>
                                            {t('userManagement.sourceFeishu')}
                                        </span>
                                    ) : (
                                        <span style={{ fontSize: '10px', background: 'rgba(0,180,120,0.12)', color: 'var(--success)', borderRadius: '4px', padding: '2px 7px', whiteSpace: 'nowrap' }}>
                                            {t('userManagement.sourceRegistered')}
                                        </span>
                                    )}
                                </div>
                                <div>
                                    <span style={{ fontSize: '13px', fontWeight: 500 }}>{user.quota_messages_used}</span>
                                    <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}> / {user.quota_message_limit}</span>
                                </div>
                                <div>
                                    <span className="badge badge-info" style={{ fontSize: '10px' }}>{periodLabel(user.quota_message_period)}</span>
                                </div>
                                <div>
                                    <span style={{ fontSize: '13px', fontWeight: 500 }}>{user.agents_count}</span>
                                    <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}> / {user.quota_max_agents}</span>
                                </div>
                                <div style={{ fontSize: '12px' }}>{user.quota_agent_ttl_hours}h</div>
                                <div>
                                    <button
                                        className="btn btn-secondary"
                                        style={{ padding: '4px 10px', fontSize: '11px' }}
                                        onClick={() => editingUserId === user.id ? setEditingUserId(null) : startEdit(user)}
                                    >
                                        {editingUserId === user.id ? t('common.cancel') : `✏️ ${t('common.edit')}`}
                                    </button>
                                </div>
                            </div>

                            {/* Inline edit form */}
                            {editingUserId === user.id && (
                                <div className="card" style={{
                                    marginTop: '4px', padding: '16px',
                                    background: 'var(--bg-secondary)',
                                    borderLeft: '3px solid var(--accent-color)',
                                }}>
                                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: '16px' }}>
                                        <div className="form-group">
                                            <label className="form-label" style={{ fontSize: '11px' }}>
                                                {t('userManagement.msgLimit')}
                                            </label>
                                            <input
                                                className="form-input"
                                                type="number" min={0}
                                                value={editForm.quota_message_limit}
                                                onChange={e => setEditForm({ ...editForm, quota_message_limit: Number(e.target.value) })}
                                            />
                                        </div>
                                        <div className="form-group">
                                            <label className="form-label" style={{ fontSize: '11px' }}>
                                                {t('userManagement.resetPeriod')}
                                            </label>
                                            <select
                                                className="form-input"
                                                value={editForm.quota_message_period}
                                                onChange={e => setEditForm({ ...editForm, quota_message_period: e.target.value })}
                                            >
                                                {PERIOD_OPTIONS.map(p => (
                                                    <option key={p.value} value={p.value}>{periodLabel(p.value)}</option>
                                                ))}
                                            </select>
                                        </div>
                                        <div className="form-group">
                                            <label className="form-label" style={{ fontSize: '11px' }}>
                                                {t('userManagement.maxAgents')}
                                            </label>
                                            <input
                                                className="form-input"
                                                type="number" min={0}
                                                value={editForm.quota_max_agents}
                                                onChange={e => setEditForm({ ...editForm, quota_max_agents: Number(e.target.value) })}
                                            />
                                        </div>
                                        <div className="form-group">
                                            <label className="form-label" style={{ fontSize: '11px' }}>
                                                {t('userManagement.agentTTL')}
                                            </label>
                                            <input
                                                className="form-input"
                                                type="number" min={1}
                                                value={editForm.quota_agent_ttl_hours}
                                                onChange={e => setEditForm({ ...editForm, quota_agent_ttl_hours: Number(e.target.value) })}
                                            />
                                        </div>
                                    </div>
                                    <div style={{ marginTop: '12px', display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
                                        <button className="btn btn-secondary" onClick={() => setEditingUserId(null)}>
                                            {t('common.cancel')}
                                        </button>
                                        <button className="btn btn-primary" onClick={handleSave} disabled={saving}>
                                            {saving ? t('common.loading') : t('common.save', 'Save')}
                                        </button>
                                    </div>
                                </div>
                            )}
                        </div>
                    ))}

                    {users.length === 0 && (
                        <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>
                            {t('common.noData')}
                        </div>
                    )}

                    {/* Pagination */}
                    {totalPages > 1 && (
                        <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '8px', marginTop: '16px' }}>
                            <button
                                className="btn btn-secondary"
                                style={{ padding: '4px 10px', fontSize: '12px' }}
                                disabled={page <= 1}
                                onClick={() => setPage(p => p - 1)}
                            >
                                ‹ {t('userManagement.prev')}
                            </button>
                            {Array.from({ length: totalPages }, (_, i) => i + 1).map(p => (
                                <button
                                    key={p}
                                    className={`btn ${p === page ? 'btn-primary' : 'btn-secondary'}`}
                                    style={{ padding: '4px 10px', fontSize: '12px', minWidth: '32px' }}
                                    onClick={() => setPage(p)}
                                >
                                    {p}
                                </button>
                            ))}
                            <button
                                className="btn btn-secondary"
                                style={{ padding: '4px 10px', fontSize: '12px' }}
                                disabled={page >= totalPages}
                                onClick={() => setPage(p => p + 1)}
                            >
                                {t('userManagement.next')} ›
                            </button>
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}
