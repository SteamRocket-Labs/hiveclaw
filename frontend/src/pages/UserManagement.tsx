/**
 * User Management — admin page to view and manage user quotas and roles.
 */
import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { usersApi } from '../api/domains/users';
import { requestAppConfirm } from '../components/AppDialogs';
import { useAuthStore } from '../stores';
import './UserManagement.css';

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
        const badges: Record<string, { cls: string; key: string }> = {
            platform_admin: { cls: 'is-platform-admin', key: 'userManagement.rolePlatformAdmin' },
            org_admin:      { cls: 'is-org-admin', key: 'userManagement.roleAdmin' },
        };
        const s = badges[role];
        if (!s) return null;
        return (
            <span className={`user-mgmt-role-badge ${s.cls}`}>
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
                <div
                    className="user-mgmt-toast"
                    style={{ background: toast.startsWith('✅') ? 'var(--success)' : 'var(--error)' }}
                >
                    {toast}
                </div>
            )}

            {loading ? (
                <div className="user-mgmt-center-msg">
                    {t('common.loading')}...
                </div>
            ) : (
                <div className="user-mgmt-list">
                    {/* Search bar */}
                    <div className="user-mgmt-search-wrap">
                        <input
                            className="form-input user-mgmt-search-input"
                            type="text"
                            placeholder={t('userManagement.searchPlaceholder')}
                            value={searchQuery}
                            onChange={e => { setSearchQuery(e.target.value); setPage(1); }}
                        />
                        {searchQuery && (
                            <span className="user-mgmt-search-count">
                                {t('userManagement.userCount', { filtered: filtered.length, total: users.length })}
                            </span>
                        )}
                    </div>

                    {/* Header */}
                    <div className="user-mgmt-thead">
                        <div>{t('userManagement.headerUser')}</div>
                        <div>{t('userManagement.headerEmail')}</div>
                        {/* Created At with sort toggle */}
                        <div
                            className="user-mgmt-th-sort"
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
                            <div className="card user-mgmt-row">
                                <div>
                                    <div className="user-mgmt-name">
                                        {user.display_name || user.username}
                                        {roleBadge(user.role)}
                                    </div>
                                    <div className="user-mgmt-handle">@{user.username}</div>
                                </div>
                                <div className="user-mgmt-cell-email">{user.email}</div>
                                <div className="user-mgmt-cell-joined">{formatDate(user.created_at)}</div>
                                {/* Role selector — only for admin users, not for platform_admin targets */}
                                <div>
                                    {currentUser?.role && ['platform_admin', 'org_admin'].includes(currentUser.role) && user.role !== 'platform_admin' ? (
                                        <select
                                            className="form-input user-mgmt-role-select"
                                            value={user.role}
                                            disabled={changingRoleUserId === user.id}
                                            onChange={e => {
                                                const newRole = e.target.value;
                                                const roleName = newRole === 'org_admin' ? t('userManagement.roleAdmin') : t('userManagement.roleMember');
                                                const confirmMsg = t('userManagement.confirmRoleChange', { name: user.display_name || user.username, role: roleName });
                                                void requestAppConfirm({
                                                    title: t('userManagement.confirmRoleChangeTitle', 'Change role'),
                                                    message: confirmMsg,
                                                    confirmLabel: t('common.confirm', 'Confirm'),
                                                }).then((confirmed) => {
                                                    if (confirmed) handleRoleChange(user.id, newRole);
                                                });
                                            }}
                                        >
                                            <option value="member">{t('userManagement.roleMember')}</option>
                                            <option value="org_admin">{t('userManagement.roleAdmin')}</option>
                                        </select>
                                    ) : (
                                        <span className="user-mgmt-role-text">
                                            {user.role === 'platform_admin' ? t('userManagement.rolePlatformAdmin')
                                                : user.role === 'org_admin' ? t('userManagement.roleAdmin') : t('userManagement.roleMember')}
                                        </span>
                                    )}
                                </div>
                                <div>
                                    {user.source === 'feishu' ? (
                                        <span className="user-mgmt-source is-feishu">
                                            {t('userManagement.sourceFeishu')}
                                        </span>
                                    ) : (
                                        <span className="user-mgmt-source is-registered">
                                            {t('userManagement.sourceRegistered')}
                                        </span>
                                    )}
                                </div>
                                <div>
                                    <span className="user-mgmt-quota-used">{user.quota_messages_used}</span>
                                    <span className="user-mgmt-quota-total"> / {user.quota_message_limit}</span>
                                </div>
                                <div>
                                    <span className="badge badge-info">{periodLabel(user.quota_message_period)}</span>
                                </div>
                                <div>
                                    <span className="user-mgmt-quota-used">{user.agents_count}</span>
                                    <span className="user-mgmt-quota-total"> / {user.quota_max_agents}</span>
                                </div>
                                <div className="user-mgmt-cell-ttl">{user.quota_agent_ttl_hours}h</div>
                                <div>
                                    <button
                                        className="btn btn-secondary user-mgmt-edit-btn"
                                        onClick={() => editingUserId === user.id ? setEditingUserId(null) : startEdit(user)}
                                    >
                                        {editingUserId === user.id ? t('common.cancel') : `✏️ ${t('common.edit')}`}
                                    </button>
                                </div>
                            </div>

                            {/* Inline edit form */}
                            {editingUserId === user.id && (
                                <div className="card user-mgmt-edit-form">
                                    <div className="user-mgmt-edit-grid">
                                        <div className="form-group">
                                            <label className="form-label user-mgmt-edit-label">
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
                                            <label className="form-label user-mgmt-edit-label">
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
                                            <label className="form-label user-mgmt-edit-label">
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
                                            <label className="form-label user-mgmt-edit-label">
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
                                    <div className="user-mgmt-edit-actions">
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
                        <div className="user-mgmt-center-msg">
                            {t('common.noData')}
                        </div>
                    )}

                    {/* Pagination */}
                    {totalPages > 1 && (
                        <div className="user-mgmt-pagination">
                            <button
                                className="btn btn-secondary user-mgmt-page-btn"
                                disabled={page <= 1}
                                onClick={() => setPage(p => p - 1)}
                            >
                                ‹ {t('userManagement.prev')}
                            </button>
                            {Array.from({ length: totalPages }, (_, i) => i + 1).map(p => (
                                <button
                                    key={p}
                                    className={`btn ${p === page ? 'btn-primary' : 'btn-secondary'} user-mgmt-page-btn user-mgmt-page-num`}
                                    onClick={() => setPage(p)}
                                >
                                    {p}
                                </button>
                            ))}
                            <button
                                className="btn btn-secondary user-mgmt-page-btn"
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
