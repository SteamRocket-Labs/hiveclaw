/**
 * User Management — tenant member roles, quotas, identity bindings, and offboarding.
 */
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
    usersApi,
    type ManagedUser,
    type UserOffboardingPreview,
} from '../api/domains/users';
import { externalPrincipalsApi, type ExternalPrincipal } from '../api/domains/externalPrincipals';
import { requestAppConfirm } from '../components/AppDialogs';
import { Modal } from '../components/ui';
import { useAuthStore } from '../stores';
import ExternalPrincipalBindingsPanel from './ExternalPrincipalBindingsPanel';

import './UserManagement.css';

const PAGE_SIZE = 15;

const requestId = (prefix: string) => globalThis.crypto?.randomUUID?.() || `${prefix}-${Date.now()}`;

export default function UserManagement() {
    const { t, i18n } = useTranslation();
    const { user: currentUser, setUser } = useAuthStore();
    const [users, setUsers] = useState<ManagedUser[]>([]);
    const [loading, setLoading] = useState(true);
    const [editingUserId, setEditingUserId] = useState<string | null>(null);
    const [editForm, setEditForm] = useState({ quota_tokens_per_day: '', quota_tokens_per_month: '' });
    const [saving, setSaving] = useState(false);
    const [toast, setToast] = useState('');
    const [changingRoleUserId, setChangingRoleUserId] = useState<string | null>(null);
    const [externalPrincipals, setExternalPrincipals] = useState<ExternalPrincipal[]>([]);
    const [externalPrincipalsLoading, setExternalPrincipalsLoading] = useState(true);
    const [busyExternalPrincipalId, setBusyExternalPrincipalId] = useState<string | null>(null);
    const [searchQuery, setSearchQuery] = useState('');
    const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('desc');
    const [page, setPage] = useState(1);
    const [offboardingTarget, setOffboardingTarget] = useState<ManagedUser | null>(null);
    const [offboardingPreview, setOffboardingPreview] = useState<UserOffboardingPreview | null>(null);
    const [offboardingLoading, setOffboardingLoading] = useState(false);
    const [offboardingBusy, setOffboardingBusy] = useState(false);
    const [successorUserId, setSuccessorUserId] = useState('');
    const [offboardingReason, setOffboardingReason] = useState('');
    const [offboardingRequestId, setOffboardingRequestId] = useState('');

    const selectedTenantId = () => localStorage.getItem('current_tenant_id') || '';

    const notify = (message: string, error = false) => {
        setToast(`${error ? '❌' : '✅'} ${message}`);
        window.setTimeout(() => setToast(''), error ? 4000 : 2400);
    };

    const loadUsers = async () => {
        setLoading(true);
        try {
            setUsers(await usersApi.list(selectedTenantId() || undefined));
        } catch (error: any) {
            notify(error?.message || t('userManagement.loadFailed', 'Failed to load members.'), true);
        } finally {
            setLoading(false);
        }
    };

    const loadExternalPrincipals = async () => {
        setExternalPrincipalsLoading(true);
        try {
            setExternalPrincipals(await externalPrincipalsApi.list({ tenantId: selectedTenantId() }));
        } catch (error) {
            console.error('Failed to load external principals', error);
        } finally {
            setExternalPrincipalsLoading(false);
        }
    };

    useEffect(() => {
        void loadUsers();
        void loadExternalPrincipals();
    }, []);

    const handleExternalPrincipalUnlink = async (principalId: string) => {
        const confirmed = await requestAppConfirm({
            title: t('userManagement.externalPrincipalUnlinkTitle', 'Unlink external identity'),
            message: t(
                'userManagement.externalPrincipalUnlinkConfirm',
                'The external sender will immediately lose the linked member authority. Continue?',
            ),
            confirmLabel: t('userManagement.externalPrincipalUnlink', 'Unlink'),
        });
        if (!confirmed) return;
        setBusyExternalPrincipalId(principalId);
        try {
            await externalPrincipalsApi.unlink(
                principalId,
                'Explicit tenant-admin unlink from user management',
                selectedTenantId(),
            );
            notify(t('userManagement.externalPrincipalUnlinked', 'External identity unlinked'));
            await loadExternalPrincipals();
        } catch (error: any) {
            notify(error?.message || t('userManagement.externalPrincipalUnlinkFailed', 'Failed to unlink identity.'), true);
        } finally {
            setBusyExternalPrincipalId(null);
        }
    };

    const startEdit = (user: ManagedUser) => {
        setEditingUserId(user.id);
        setEditForm({
            quota_tokens_per_day: user.quota_tokens_per_day == null ? '' : String(user.quota_tokens_per_day),
            quota_tokens_per_month: user.quota_tokens_per_month == null ? '' : String(user.quota_tokens_per_month),
        });
    };

    const handleSave = async () => {
        if (!editingUserId) return;
        setSaving(true);
        try {
            await usersApi.updateQuota(editingUserId, {
                quota_tokens_per_day: editForm.quota_tokens_per_day === '' ? null : Number(editForm.quota_tokens_per_day),
                quota_tokens_per_month: editForm.quota_tokens_per_month === '' ? null : Number(editForm.quota_tokens_per_month),
            });
            notify(t('userManagement.quotaUpdated', 'Quota updated'));
            setEditingUserId(null);
            await loadUsers();
        } catch (error: any) {
            notify(error?.message || t('userManagement.quotaUpdateFailed', 'Failed to update quota.'), true);
        } finally {
            setSaving(false);
        }
    };

    const handleRoleChange = async (userId: string, newRole: string) => {
        setChangingRoleUserId(userId);
        try {
            await usersApi.updateRole(userId, newRole, selectedTenantId() || undefined);
            notify(t('userManagement.roleUpdated', 'Role updated'));
            if (userId === currentUser?.id) setUser({ ...currentUser, role: newRole as any });
            await loadUsers();
        } catch (error: any) {
            notify(error?.message || t('userManagement.roleUpdateFailed', 'Failed to update role.'), true);
        } finally {
            setChangingRoleUserId(null);
        }
    };

    const openOffboarding = async (user: ManagedUser) => {
        setOffboardingTarget(user);
        setOffboardingPreview(null);
        setSuccessorUserId('');
        setOffboardingReason('');
        setOffboardingRequestId(requestId('offboard'));
        setOffboardingLoading(true);
        try {
            const preview = await usersApi.previewOffboarding(user.id, selectedTenantId() || undefined);
            setOffboardingPreview(preview);
            setSuccessorUserId(preview.default_successor_id || preview.eligible_successors[0]?.id || '');
        } catch (error: any) {
            setOffboardingTarget(null);
            notify(error?.message || t('userManagement.offboardingPreviewFailed', 'Failed to load offboarding impact.'), true);
        } finally {
            setOffboardingLoading(false);
        }
    };

    const submitOffboarding = async () => {
        if (
            !offboardingTarget
            || !offboardingPreview
            || !successorUserId
            || !offboardingReason.trim()
            || !offboardingRequestId
        ) return;
        setOffboardingBusy(true);
        try {
            const receipt = await usersApi.offboard(
                offboardingTarget.id,
                {
                    successor_user_id: successorUserId,
                    expected_agent_ids: offboardingPreview.owned_agents.map((agent) => agent.id),
                    reason: offboardingReason.trim(),
                    request_id: offboardingRequestId,
                },
                selectedTenantId() || undefined,
            );
            notify(t(
                'userManagement.offboardingComplete',
                'Member deactivated and {{count}} Agent(s) transferred.',
                { count: receipt.transferred_agent_count },
            ));
            setOffboardingTarget(null);
            setOffboardingPreview(null);
            setOffboardingRequestId('');
            await Promise.all([loadUsers(), loadExternalPrincipals()]);
        } catch (error: any) {
            notify(error?.message || t('userManagement.offboardingFailed', 'Failed to deactivate member.'), true);
        } finally {
            setOffboardingBusy(false);
        }
    };

    const roleBadge = (role: string) => {
        if (role === 'platform_admin') {
            return <span className="user-mgmt-role-badge is-platform-admin">{t('userManagement.rolePlatformAdmin')}</span>;
        }
        if (role === 'org_admin') {
            return <span className="user-mgmt-role-badge is-org-admin">{t('userManagement.roleAdmin')}</span>;
        }
        return null;
    };

    const formatDate = (iso?: string | null) => {
        if (!iso) return '-';
        const locale = i18n.language?.startsWith('zh') ? 'zh-CN' : 'en-US';
        return new Date(iso).toLocaleString(locale, {
            year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
        });
    };

    const quotaText = (used: number, limit?: number | null) => `${used.toLocaleString()} / ${limit == null ? '∞' : limit.toLocaleString()}`;
    const filtered = searchQuery.trim()
        ? users.filter((user) => {
            const query = searchQuery.toLowerCase();
            return user.username.toLowerCase().includes(query)
                || user.display_name.toLowerCase().includes(query)
                || user.email.toLowerCase().includes(query);
        })
        : users;
    const sorted = [...filtered].sort((left, right) => {
        const a = left.created_at ? new Date(left.created_at).getTime() : 0;
        const b = right.created_at ? new Date(right.created_at).getTime() : 0;
        return sortOrder === 'asc' ? a - b : b - a;
    });
    const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
    const paged = sorted.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

    return (
        <div>
            {toast && (
                <div className="user-mgmt-toast" style={{ background: toast.startsWith('✅') ? 'var(--success)' : 'var(--error)' }}>
                    {toast}
                </div>
            )}

            {currentUser?.role && ['platform_admin', 'org_admin'].includes(currentUser.role) && (
                <ExternalPrincipalBindingsPanel
                    principals={externalPrincipals}
                    users={users}
                    loading={externalPrincipalsLoading}
                    busyPrincipalId={busyExternalPrincipalId}
                    onUnlink={handleExternalPrincipalUnlink}
                />
            )}

            {loading ? (
                <div className="user-mgmt-center-msg">{t('common.loading')}...</div>
            ) : (
                <div className="user-mgmt-list">
                    <div className="user-mgmt-search-wrap">
                        <input
                            className="form-input user-mgmt-search-input"
                            type="text"
                            placeholder={t('userManagement.searchPlaceholder')}
                            value={searchQuery}
                            onChange={(event) => { setSearchQuery(event.target.value); setPage(1); }}
                        />
                        {searchQuery && (
                            <span className="user-mgmt-search-count">
                                {t('userManagement.userCount', { filtered: filtered.length, total: users.length })}
                            </span>
                        )}
                    </div>

                    <div className="user-mgmt-thead">
                        <div>{t('userManagement.headerUser')}</div>
                        <div>{t('userManagement.headerEmail')}</div>
                        <div
                            className="user-mgmt-th-sort"
                            onClick={() => { setSortOrder((value) => value === 'asc' ? 'desc' : 'asc'); setPage(1); }}
                        >
                            {t('userManagement.headerJoined')} {sortOrder === 'asc' ? '↑' : '↓'}
                        </div>
                        <div>{t('userManagement.headerStatus', 'Status')}</div>
                        <div>{t('userManagement.headerRole')}</div>
                        <div>{t('userManagement.headerSource')}</div>
                        <div>{t('userManagement.headerAgents')}</div>
                        <div>{t('userManagement.headerDailyTokens', 'Daily tokens')}</div>
                        <div>{t('userManagement.headerMonthlyTokens', 'Monthly tokens')}</div>
                        <div>{t('userManagement.headerActions', 'Actions')}</div>
                    </div>

                    {paged.map((user) => (
                        <div key={user.id}>
                            <div className={`card user-mgmt-row ${user.is_active ? '' : 'is-inactive'}`}>
                                <div>
                                    <div className="user-mgmt-name">{user.display_name || user.username}{roleBadge(user.role)}</div>
                                    <div className="user-mgmt-handle">@{user.username}</div>
                                </div>
                                <div className="user-mgmt-cell-email">{user.email}</div>
                                <div className="user-mgmt-cell-joined">{formatDate(user.created_at)}</div>
                                <div>
                                    <span className={`user-mgmt-status ${user.is_active ? 'is-active' : 'is-inactive'}`}>
                                        {user.is_active ? t('userManagement.statusActive', 'Active') : t('userManagement.statusInactive', 'Inactive')}
                                    </span>
                                </div>
                                <div>
                                    {currentUser?.role && ['platform_admin', 'org_admin'].includes(currentUser.role) && user.role !== 'platform_admin' ? (
                                        <select
                                            className="form-input user-mgmt-role-select"
                                            value={user.role}
                                            disabled={!user.is_active || changingRoleUserId === user.id}
                                            onChange={(event) => {
                                                const newRole = event.target.value;
                                                const roleName = newRole === 'org_admin' ? t('userManagement.roleAdmin') : t('userManagement.roleMember');
                                                void requestAppConfirm({
                                                    title: t('userManagement.confirmRoleChangeTitle', 'Change role'),
                                                    message: t('userManagement.confirmRoleChange', { name: user.display_name || user.username, role: roleName }),
                                                    confirmLabel: t('common.confirm', 'Confirm'),
                                                }).then((confirmed) => {
                                                    if (confirmed) void handleRoleChange(user.id, newRole);
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
                                    <span className={`user-mgmt-source ${user.source === 'feishu' ? 'is-feishu' : 'is-registered'}`}>
                                        {user.source === 'feishu' ? t('userManagement.sourceFeishu') : t('userManagement.sourceRegistered')}
                                    </span>
                                </div>
                                <div className="user-mgmt-number">{user.agents_count}</div>
                                <div className="user-mgmt-quota-value">{quotaText(user.tokens_used_today, user.quota_tokens_per_day)}</div>
                                <div className="user-mgmt-quota-value">{quotaText(user.tokens_used_month, user.quota_tokens_per_month)}</div>
                                <div className="user-mgmt-actions">
                                    <button
                                        className="btn btn-secondary user-mgmt-edit-btn"
                                        onClick={() => editingUserId === user.id ? setEditingUserId(null) : startEdit(user)}
                                        disabled={!user.is_active}
                                    >
                                        {editingUserId === user.id ? t('common.cancel') : t('common.edit')}
                                    </button>
                                    {(user.is_active || user.agents_count > 0)
                                        && user.role !== 'platform_admin'
                                        && user.id !== currentUser?.id && (
                                        <button
                                            className="btn btn-ghost user-mgmt-offboard-btn"
                                            onClick={() => void openOffboarding(user)}
                                        >
                                            {user.is_active
                                                ? t('userManagement.deactivateMember', 'Deactivate')
                                                : t('userManagement.resolveOffboarding', 'Resolve ownership')}
                                        </button>
                                    )}
                                </div>
                            </div>

                            {editingUserId === user.id && (
                                <div className="card user-mgmt-edit-form">
                                    <div className="user-mgmt-edit-grid">
                                        <div className="form-group">
                                            <label className="form-label user-mgmt-edit-label">{t('userManagement.dailyTokenLimit', 'Daily token limit')}</label>
                                            <input
                                                className="form-input"
                                                type="number"
                                                min={0}
                                                value={editForm.quota_tokens_per_day}
                                                placeholder={t('userManagement.unlimited', 'Unlimited')}
                                                onChange={(event) => setEditForm({ ...editForm, quota_tokens_per_day: event.target.value })}
                                            />
                                        </div>
                                        <div className="form-group">
                                            <label className="form-label user-mgmt-edit-label">{t('userManagement.monthlyTokenLimit', 'Monthly token limit')}</label>
                                            <input
                                                className="form-input"
                                                type="number"
                                                min={0}
                                                value={editForm.quota_tokens_per_month}
                                                placeholder={t('userManagement.unlimited', 'Unlimited')}
                                                onChange={(event) => setEditForm({ ...editForm, quota_tokens_per_month: event.target.value })}
                                            />
                                        </div>
                                    </div>
                                    <div className="user-mgmt-edit-actions">
                                        <button className="btn btn-secondary" onClick={() => setEditingUserId(null)}>{t('common.cancel')}</button>
                                        <button className="btn btn-primary" onClick={() => void handleSave()} disabled={saving}>
                                            {saving ? t('common.loading') : t('common.save', 'Save')}
                                        </button>
                                    </div>
                                </div>
                            )}
                        </div>
                    ))}

                    {users.length === 0 && <div className="user-mgmt-center-msg">{t('common.noData')}</div>}
                    {totalPages > 1 && (
                        <div className="user-mgmt-pagination">
                            <button className="btn btn-secondary user-mgmt-page-btn" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>
                                ‹ {t('userManagement.prev')}
                            </button>
                            {Array.from({ length: totalPages }, (_, index) => index + 1).map((value) => (
                                <button
                                    key={value}
                                    className={`btn ${value === page ? 'btn-primary' : 'btn-secondary'} user-mgmt-page-btn user-mgmt-page-num`}
                                    onClick={() => setPage(value)}
                                >
                                    {value}
                                </button>
                            ))}
                            <button className="btn btn-secondary user-mgmt-page-btn" disabled={page >= totalPages} onClick={() => setPage((value) => value + 1)}>
                                {t('userManagement.next')} ›
                            </button>
                        </div>
                    )}
                </div>
            )}

            <Modal
                open={Boolean(offboardingTarget)}
                onClose={() => { if (!offboardingBusy) setOffboardingTarget(null); }}
                title={t('userManagement.deactivateMemberTitle', 'Deactivate member')}
                width={620}
                footer={(
                    <>
                        <button className="btn btn-secondary" onClick={() => setOffboardingTarget(null)} disabled={offboardingBusy}>
                            {t('common.cancel', 'Cancel')}
                        </button>
                        <button
                            className="btn btn-danger"
                            onClick={() => void submitOffboarding()}
                            disabled={
                                offboardingLoading
                                || offboardingBusy
                                || !offboardingPreview
                                || offboardingPreview.blockers.length > 0
                                || !successorUserId
                                || !offboardingReason.trim()
                            }
                        >
                            {offboardingBusy ? t('common.loading', 'Working...') : t('userManagement.confirmDeactivate', 'Transfer and deactivate')}
                        </button>
                    </>
                )}
            >
                {offboardingLoading || !offboardingPreview ? (
                    <div className="user-mgmt-offboard-loading">{t('common.loading', 'Loading impact...')}</div>
                ) : (
                    <div className="user-mgmt-offboard-modal">
                        <p className="user-mgmt-offboard-summary">
                            {t(
                                'userManagement.offboardingSummary',
                                '{{name}} will be unable to sign in. {{count}} Agent(s) will move to the selected administrator.',
                                { name: offboardingTarget?.display_name || offboardingTarget?.username, count: offboardingPreview.owned_agents.length },
                            )}
                        </p>
                        {offboardingPreview.owned_agents.length > 0 && (
                            <div className="user-mgmt-offboard-agents">
                                {offboardingPreview.owned_agents.map((agent) => (
                                    <span key={agent.id}>{agent.name}</span>
                                ))}
                            </div>
                        )}
                        <div className="user-mgmt-offboard-impact">
                            <div><strong>{offboardingPreview.revocations.agent_permissions + offboardingPreview.revocations.resource_permissions}</strong><span>{t('userManagement.directPermissions', 'direct permissions')}</span></div>
                            <div><strong>{offboardingPreview.revocations.knowledge_grants}</strong><span>{t('userManagement.knowledgeGrants', 'knowledge grants')}</span></div>
                            <div><strong>{offboardingPreview.revocations.refresh_tokens}</strong><span>{t('userManagement.loginSessions', 'refresh tokens')}</span></div>
                            <div><strong>{offboardingPreview.revocations.external_principals + offboardingPreview.revocations.local_bridge_connections}</strong><span>{t('userManagement.identityBindings', 'identity bindings')}</span></div>
                            <div><strong>{offboardingPreview.revocations.runtime_tasks + offboardingPreview.revocations.pending_approvals}</strong><span>{t('userManagement.inFlightAuthority', 'in-flight tasks and approvals')}</span></div>
                        </div>
                        {offboardingPreview.blockers.length > 0 && (
                            <div className="user-mgmt-offboard-blocker">{offboardingPreview.blockers.join(' · ')}</div>
                        )}
                        <div className="form-group">
                            <label className="form-label" htmlFor="offboarding-successor">{t('userManagement.successorAdmin', 'Receiving administrator')}</label>
                            <select
                                id="offboarding-successor"
                                className="form-input"
                                value={successorUserId}
                                onChange={(event) => setSuccessorUserId(event.target.value)}
                            >
                                <option value="">{t('userManagement.selectSuccessor', 'Select an active company administrator')}</option>
                                {offboardingPreview.eligible_successors.map((successor) => (
                                    <option key={successor.id} value={successor.id}>{successor.display_name} · {successor.email}</option>
                                ))}
                            </select>
                        </div>
                        <div className="form-group">
                            <label className="form-label" htmlFor="offboarding-reason">{t('userManagement.offboardingReason', 'Reason')}</label>
                            <textarea
                                id="offboarding-reason"
                                className="form-input user-mgmt-offboard-reason"
                                maxLength={500}
                                value={offboardingReason}
                                onChange={(event) => setOffboardingReason(event.target.value)}
                                placeholder={t('userManagement.offboardingReasonPlaceholder', 'Record why this member is being deactivated')}
                            />
                        </div>
                        <p className="user-mgmt-offboard-recovery">
                            {t(
                                'userManagement.offboardingRecovery',
                                'History and personal data are preserved. Restoring the account will not restore old Agent ownership or revoked grants.',
                            )}
                        </p>
                    </div>
                )}
            </Modal>
        </div>
    );
}
