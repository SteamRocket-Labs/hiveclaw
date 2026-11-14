import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';

import { agentApi, type AgentOwnerCandidate, type AgentRetirementPending } from '../../api/domains/agents';
import {
  assistantNameForSharedDisplay, assistantRoleForDisplay, PLATFORM_ASSISTANT_ROLE, useAssistantAgentId,
} from '../assistant/assistantScope';
import { usersApi } from '../../api/domains/users';
import { requestAppConfirm, showAppToast } from '../../components/AppDialogs';
import { Modal } from '../../components/ui';
import { isAdministratorRole } from '../../roles';
import { useAuthStore } from '../../stores';
import type { Agent } from '../../types';

import './WorkspaceDigitalEmployeesSection.css';

interface WorkspaceDigitalEmployeesSectionProps {
    selectedTenantId: string;
}

function EmployeeUsage({ agentId }: { agentId: string }) {
    const { t } = useTranslation();
    const { data, isError } = useQuery({
        queryKey: ['agent-permissions', agentId],
        queryFn: () => agentApi.getPermissions(agentId),
    });
    if (isError) return <span>{t('workspace.digitalEmployees.usageUnavailable', 'Usage access could not be loaded')}</span>;
    if (!data) return <span>{t('common.loading', 'Loading...')}</span>;
    return <span>{data.scope_type === 'company'
        ? t('workspace.digitalEmployees.usageCompany', 'Everyone in the company')
        : data.scope_type === 'user' ? t('workspace.digitalEmployees.usageOwner', 'Owner and company administrators')
            : data.scope_type === 'department' ? t('workspace.digitalEmployees.usageDepartment', 'Owner, company administrators and authorized departments')
                : t('workspace.digitalEmployees.usageUnavailable', 'Usage access could not be loaded')}
        {data.scope_type === 'user' && data.scope_names?.length ? ` · ${data.scope_names.map((scope) => scope.name).join(', ')}` : ''}
    </span>;
}

export default function WorkspaceDigitalEmployeesSection({ selectedTenantId }: WorkspaceDigitalEmployeesSectionProps) {
    const { t } = useTranslation();
    const queryClient = useQueryClient();
    const currentUser = useAuthStore((state) => state.user);
    const assistantAgentId = useAssistantAgentId(currentUser, selectedTenantId);
    const canManageEmployees = isAdministratorRole(currentUser?.role);
    const [ownershipAgent, setOwnershipAgent] = useState<Agent | null>(null);
    const [ownerCandidates, setOwnerCandidates] = useState<AgentOwnerCandidate[]>([]);
    const [newOwnerId, setNewOwnerId] = useState('');
    const [ownershipReason, setOwnershipReason] = useState('');
    const [ownerCandidatesLoading, setOwnerCandidatesLoading] = useState(false);

    const { data: hrAgent } = useQuery({
        queryKey: ['hr-agent', selectedTenantId],
        queryFn: () => agentApi.getHrAgent(),
        retry: 1,
        enabled: canManageEmployees,
    });

    const { data: agents = [], isLoading: agentsLoading } = useQuery({
        queryKey: ['agents', selectedTenantId, 'admin-management'],
        queryFn: () => agentApi.list(selectedTenantId || undefined, { includeDeactivated: true }),
        enabled: canManageEmployees,
    });

    const retirementQueryKey = ['agent-retirement-pending', selectedTenantId];
    const { data: pendingRetirements = [], isError: retirementError, refetch: reloadRetirements } = useQuery({
        queryKey: retirementQueryKey,
        queryFn: () => agentApi.listPendingRetirement(selectedTenantId || undefined),
        enabled: canManageEmployees,
        retry: false,
    });

    const { data: users = [] } = useQuery({
        queryKey: ['users', selectedTenantId, 'agent-owner-display'],
        queryFn: () => usersApi.list(selectedTenantId || undefined),
        enabled: canManageEmployees,
    });

    const userById = new Map(users.map((user) => [user.id, user]));
    const employeeName = (employee: { id: string; name: string }) => {
        const agent = agents.find((item) => item.id === employee.id);
        const owner = agent && userById.get(agent.owner_user_id || agent.creator_id);
        return assistantNameForSharedDisplay({
            name: employee.name, isAgent: true,
            authorAgentId: employee.id, viewerAssistantId: assistantAgentId,
            ownerName: owner?.display_name || owner?.email || null, t,
        });
    };

    const invalidateEmployeeLists = () => {
        queryClient.invalidateQueries({ queryKey: retirementQueryKey });
        queryClient.invalidateQueries({ queryKey: ['agents'] });
        queryClient.invalidateQueries({ queryKey: ['agents', selectedTenantId, 'admin-management'] });
        queryClient.invalidateQueries({ queryKey: ['enterprise-stats'] });
    };

    const deactivateAgentMutation = useMutation({
        mutationFn: (agentId: string) => agentApi.deactivate(agentId),
        onSuccess: () => {
            showAppToast(t('workspace.digitalEmployees.employeeDeactivated', 'Digital employee deactivated. Its files, memory and conversations are kept.'), 'success');
        },
        onError: (error: any) => {
            showAppToast(
                error?.data?.code === 'agent_retirement_pending'
                    ? t('workspace.digitalEmployees.retirementPending', 'Deactivated; external work is not yet confirmed. Review it in its original work; ending local tracking does not confirm that the external runner stopped. Files are retained.')
                    : error?.message || t('workspace.digitalEmployees.employeeDeactivateFailed', 'Failed to deactivate digital employee.'),
                'error',
            );
        },
        onSettled: invalidateEmployeeLists,
    });

    const restoreAgentMutation = useMutation({
        mutationFn: (agentId: string) => agentApi.restore(agentId),
        onSuccess: () => {
            showAppToast(t('workspace.digitalEmployees.employeeRestored', 'Digital employee restored. Scheduled tasks stay off until their owner turns each one back on.'), 'success');
        },
        onError: (error: any) => {
            showAppToast(
                error?.data?.code === 'agent_restore_work_pending'
                    ? t('workspace.digitalEmployees.restoreWorkPending', 'Outward work is not yet confirmed. Review the recall, then restore.')
                    : error?.message || t('workspace.digitalEmployees.employeeRestoreFailed', 'Failed to restore digital employee.'),
                'error',
            );
        },
        onSettled: invalidateEmployeeLists,
    });

    const transferOwnerMutation = useMutation({
        mutationFn: ({ agent, ownerId, reason }: { agent: Agent; ownerId: string; reason: string }) =>
            agentApi.transferOwnership(agent.id, {
                new_owner_id: ownerId,
                expected_owner_id: agent.owner_user_id || agent.creator_id,
                reason,
                request_id: globalThis.crypto?.randomUUID?.() || `handover-${Date.now()}`,
            }),
        onSuccess: async (receipt, { agent }) => {
            await queryClient.invalidateQueries({ queryKey: ['agent-permissions', agent.id] });
            await queryClient.invalidateQueries({ queryKey: ['agents'] });
            await queryClient.invalidateQueries({ queryKey: ['agents', selectedTenantId, 'admin-management'] });
            setOwnershipAgent(null);
            setOwnerCandidates([]);
            setNewOwnerId('');
            setOwnershipReason('');
            showAppToast(
                t('workspace.digitalEmployees.ownerChanged', 'Owner changed to {{name}}.', { name: receipt.new_owner }),
                'success',
            );
        },
        onError: (error: any) => {
            showAppToast(
                error?.message || t('workspace.digitalEmployees.ownerChangeFailed', 'Failed to change owner.'),
                'error',
            );
        },
    });

    const endTrackingMutation = useMutation({
        mutationFn: (employee: AgentRetirementPending) => agentApi.endRetirementTracking(employee.id, {
            request_id: crypto.randomUUID(),
            expected_user_id: currentUser!.id,
            expected_tenant_id: selectedTenantId,
            expected_tracking_version: employee.tracking_version!,
            external_execution_remains_unknown: true,
        }),
        onSuccess: () => showAppToast(t('workspace.digitalEmployees.trackingEnded', 'Local tracking ended. External execution is still unknown. Retirement can now be reviewed again.'), 'success'),
        onError: (error: any) => showAppToast(error?.message || t('workspace.digitalEmployees.trackingChanged', 'The work or requester changed. Review the refreshed status before deciding again.'), 'error'),
        onSettled: () => queryClient.invalidateQueries({ queryKey: retirementQueryKey }),
    });

    const endInactiveTracking = async (employee: AgentRetirementPending) => {
        if (!currentUser || !employee.tracking_version) return;
        if (await requestAppConfirm({
            title: t('workspace.digitalEmployees.endInactiveTracking', 'End tracking for inactive requesters'),
            message: t('workspace.digitalEmployees.endInactiveTrackingConfirm', 'End local tracking of {{count}} unresolved requests for {{name}} whose original requesters are inactive? External execution remains unknown. This records your management decision without transferring private work or credentials. Retry retirement separately after reviewing the result.',
                { count: employee.inactive_requester_work_count, name: employeeName(employee) }),
            confirmLabel: t('workspace.digitalEmployees.endInactiveTracking', 'End tracking for inactive requesters'),
            danger: true,
        })) endTrackingMutation.mutate(employee);
    };

    const isSystemProtectedAgent = (agent: Agent) => agent.id === hrAgent?.id || agent.name === '__system_hr__';

    const handleDeactivateAgent = async (agent: Agent) => {
        if (isSystemProtectedAgent(agent)) return;
        const confirmed = await requestAppConfirm({
            title: t('workspace.digitalEmployees.deactivateEmployeeTitle', 'Deactivate digital employee'),
            message: t(
                'workspace.digitalEmployees.deactivateEmployeeConfirm',
                'Deactivate {{name}}? It stops taking new work and in-flight work is withdrawn. Files, memory and conversations are kept, and it can be restored later. Scheduled tasks stay off after restore until their owner re-enables them.',
                { name: employeeName(agent) },
            ),
            confirmLabel: t('workspace.digitalEmployees.deactivateEmployeeButton', 'Deactivate'),
            danger: true,
        });
        if (!confirmed) return;
        deactivateAgentMutation.mutate(agent.id);
    };

    const openOwnershipModal = async (agent: Agent) => {
        setOwnershipAgent(agent);
        setNewOwnerId('');
        setOwnershipReason('');
        setOwnerCandidates([]);
        setOwnerCandidatesLoading(true);
        try {
            const candidates = await agentApi.getOwnerCandidates(agent.id);
            setOwnerCandidates(candidates);
            if (candidates.length === 1) setNewOwnerId(candidates[0].id);
        } catch (error: any) {
            setOwnershipAgent(null);
            showAppToast(
                error?.message || t('workspace.digitalEmployees.ownerCandidatesFailed', 'Failed to load eligible owners.'),
                'error',
            );
        } finally {
            setOwnerCandidatesLoading(false);
        }
    };

    const submitOwnershipTransfer = async () => {
        if (!ownershipAgent || !newOwnerId || !ownershipReason.trim()) return;
        await transferOwnerMutation.mutateAsync({
            agent: ownershipAgent,
            ownerId: newOwnerId,
            reason: ownershipReason.trim(),
        });
    };

    if (!canManageEmployees) {
        return (
            <div className="ws-employees-page">
                <div className="card">
                    <h3 className="ws-employees-title">
                        {t('workspace.digitalEmployees.title', 'Digital Employee Management')}
                    </h3>
                    <p className="ws-employees-desc">
                        {t('workspace.digitalEmployees.adminOnly', 'Only company administrators can manage digital employees here.')}
                    </p>
                </div>
            </div>
        );
    }

    return (
        <div className="ws-employees-page">
            <div className="card ws-employees-card">
                <div className="ws-employees-head">
                    <div>
                        <h3 className="ws-employees-title">
                            {t('workspace.digitalEmployees.title', 'Digital Employee Management')}
                        </h3>
                        <p className="ws-employees-desc">
                            {t('workspace.digitalEmployees.description', 'Company admins can review and deactivate tenant digital employees from this governed backend surface.')}
                        </p>
                    </div>
                    <Link className="btn btn-primary ws-employees-create" to="/">
                        {t('nav.newDigitalEmployee', 'New digital employee')}
                    </Link>
                </div>

                {retirementError && <p role="alert">
                    {t('workspace.digitalEmployees.retirementLoadFailed', 'Pending retirement could not be loaded.')}{' '}
                    <button type="button" className="btn btn-secondary" onClick={() => void reloadRetirements()}>
                        {t('common.retry', 'Retry')}
                    </button>
                </p>}
                {pendingRetirements.length > 0 && <section aria-label={t('workspace.digitalEmployees.deactivatedTitle', 'Deactivated digital employees')}>
                    <h4>{t('workspace.digitalEmployees.deactivatedTitle', 'Deactivated digital employees')}</h4>
                    <p>{t('workspace.digitalEmployees.deactivatedDesc', 'Deactivated employees keep their files, memory and conversations. Unconfirmed external work stays listed for review; ending local tracking does not confirm that the external runner stopped.')}</p>
                    <ul>{pendingRetirements.map((employee) => <li key={employee.id}>
                        <Link to={`/agents/${employee.id}`} className="ws-employees-deactivated-link">{employeeName(employee)}</Link>
                        {' '}
                        {!!employee.remote_work_count && t('workspace.digitalEmployees.unconfirmedRemoteWork', '{{count}} unconfirmed external task(s)', { count: employee.remote_work_count })}
                        {' '}
                        {!!employee.local_work_count && t('workspace.digitalEmployees.unconfirmedLocalWork', '{{count}} unconfirmed delegation(s)', { count: employee.local_work_count })}
                        {' '}
                        {(!!employee.remote_work_count || !!employee.local_work_count) && <button type="button"
                            className="btn btn-secondary" disabled={deactivateAgentMutation.isPending}
                            onClick={() => deactivateAgentMutation.mutate(employee.id)}>
                            {t('workspace.digitalEmployees.retryDeactivateButton', 'Retry deactivation')}
                        </button>}
                        <button type="button" className="btn btn-secondary" disabled={restoreAgentMutation.isPending}
                            onClick={() => restoreAgentMutation.mutate(employee.id)}>
                            {t('workspace.digitalEmployees.restoreEmployeeButton', 'Restore')}
                        </button>
                        {!!employee.inactive_requester_work_count && employee.tracking_version && <button type="button"
                            className="btn btn-secondary" disabled={endTrackingMutation.isPending}
                            onClick={() => void endInactiveTracking(employee)}>
                            {t('workspace.digitalEmployees.endInactiveTracking', 'End tracking for inactive requesters')}
                        </button>}
                    </li>)}</ul>
                </section>}

                {agentsLoading ? (
                    <div className="ws-employees-loading">
                        {t('common.loading', 'Loading...')}
                    </div>
                ) : (
                    <div className="ws-employees-table-wrap">
                        <table className="ws-employees-table">
                            <thead>
                                <tr className="ws-employees-thead-row">
                                    <th className="ws-employees-th">{t('workspace.digitalEmployees.employeeName', 'Employee')}</th>
                                    <th className="ws-employees-th">{t('workspace.digitalEmployees.employeeOwner', 'Owner')}</th>
                                    <th className="ws-employees-th">{t('workspace.digitalEmployees.employeeUsage', 'Who can use it')}</th>
                                    <th className="ws-employees-th">{t('workspace.digitalEmployees.employeeStatus', 'Status')}</th>
                                    <th className="ws-employees-th-right">{t('workspace.digitalEmployees.employeeActions', 'Actions')}</th>
                                </tr>
                            </thead>
                            <tbody>
                                {(agents as Agent[]).map((agent) => {
                                    const protectedAgent = isSystemProtectedAgent(agent);
                                    const ownerId = agent.owner_user_id || agent.creator_id;
                                    const owner = userById.get(ownerId);
                                    return (
                                        <tr key={agent.id} className="ws-employees-row">
                                            <td className="ws-employees-td">
                                                <div className="ws-employees-name">{employeeName(agent)}</div>
                                                <div className="ws-employees-role">
                                                    {/* X1-r3 ruling 7: only the viewer's own assistant row keeps the
                                                        possessive role label; another member's default assistant is
                                                        neutral — it works for that member, not for this viewer. */}
                                                    {String(agent.id) === String(assistantAgentId || '')
                                                        ? assistantRoleForDisplay(agent.role_description || '', t('assistant.role', 'The user\'s assistant')) || t('employees.noRole', 'No role description yet')
                                                        : agent.role_description === PLATFORM_ASSISTANT_ROLE
                                                            ? t('assistant.memberRole', 'A member\'s personal assistant')
                                                            : agent.role_description || t('employees.noRole', 'No role description yet')}
                                                </div>
                                            </td>
                                            <td className="ws-employees-td" data-label={t('workspace.digitalEmployees.employeeOwner', 'Owner')}>
                                                <div className="ws-employees-owner-name">
                                                    {owner?.display_name || owner?.email || t('workspace.digitalEmployees.ownerUnknown', 'Unknown user')}
                                                </div>
                                                {owner?.email && <div className="ws-employees-owner-email">{owner.email}</div>}
                                            </td>
                                            <td className="ws-employees-td" data-label={t('workspace.digitalEmployees.employeeUsage', 'Who can use it')}><EmployeeUsage agentId={agent.id} /></td>
                                            <td className="ws-employees-td" data-label={t('workspace.digitalEmployees.employeeStatus', 'Status')}>{agent.deactivated_at
                                                ? t('workspace.digitalEmployees.inactive', 'Deactivated')
                                                : t('workspace.digitalEmployees.active', 'Active')}</td>
                                            <td className="ws-employees-td-right">
                                                <div className="ws-employees-actions">
                                                    <Link to={`/agents/${agent.id}`} className="btn btn-ghost">
                                                        {t('employees.actions.detail', 'Detail')}
                                                    </Link>
                                                    <button
                                                        type="button"
                                                        className="btn btn-ghost"
                                                        onClick={() => void openOwnershipModal(agent)}
                                                    >
                                                        {t('workspace.digitalEmployees.changeOwner', 'Change owner')}
                                                    </button>
                                                    {agent.deactivated_at ? (
                                                        <button type="button" className="btn btn-ghost" disabled={restoreAgentMutation.isPending}
                                                            onClick={() => restoreAgentMutation.mutate(agent.id)}>
                                                            {t('workspace.digitalEmployees.restoreEmployeeButton', 'Restore')}
                                                        </button>
                                                    ) : protectedAgent ? (
                                                        <span className="ws-employees-protected">
                                                            {t('workspace.digitalEmployees.systemProtected', 'System protected')}
                                                        </span>
                                                    ) : (
                                                        <button
                                                            type="button"
                                                            className="btn btn-ghost ws-employees-del"
                                                            onClick={() => void handleDeactivateAgent(agent)}
                                                            disabled={deactivateAgentMutation.isPending}
                                                        >
                                                            {t('workspace.digitalEmployees.deactivateEmployeeButton', 'Deactivate')}
                                                        </button>
                                                    )}
                                                </div>
                                            </td>
                                        </tr>
                                    );
                                })}
                            </tbody>
                        </table>
                        {(agents as Agent[]).length === 0 && (
                            <div className="ws-employees-loading">
                                {t('workspace.digitalEmployees.noEmployees', 'No digital employees found.')}
                            </div>
                        )}
                    </div>
                )}
            </div>
            <Modal
                open={Boolean(ownershipAgent)}
                onClose={() => {
                    if (!transferOwnerMutation.isPending) setOwnershipAgent(null);
                }}
                title={t('workspace.digitalEmployees.changeOwnerTitle', 'Change digital employee owner')}
                width={520}
                footer={(
                    <>
                        <button
                            type="button"
                            className="btn btn-secondary"
                            onClick={() => setOwnershipAgent(null)}
                            disabled={transferOwnerMutation.isPending}
                        >
                            {t('common.cancel', 'Cancel')}
                        </button>
                        <button
                            type="button"
                            className="btn btn-primary"
                            onClick={() => void submitOwnershipTransfer()}
                            disabled={
                                ownerCandidatesLoading
                                || !newOwnerId
                                || !ownershipReason.trim()
                                || transferOwnerMutation.isPending
                            }
                        >
                            {transferOwnerMutation.isPending
                                ? t('common.loading', 'Saving...')
                                : t('workspace.digitalEmployees.confirmOwnerChange', 'Confirm transfer')}
                        </button>
                    </>
                )}
            >
                <div className="ws-employees-owner-modal">
                    <p className="ws-employees-owner-note">
                        {t(
                            'workspace.digitalEmployees.changeOwnerNote',
                            'Only current responsibility changes. Creator and sponsor history remain unchanged.',
                        )}
                    </p>
                    <div className="form-group">
                        <label className="form-label" htmlFor="agent-owner-candidate">
                            {t('workspace.digitalEmployees.newOwner', 'New owner')}
                        </label>
                        <select
                            id="agent-owner-candidate"
                            className="form-input"
                            value={newOwnerId}
                            onChange={(event) => setNewOwnerId(event.target.value)}
                            disabled={ownerCandidatesLoading || transferOwnerMutation.isPending}
                        >
                            <option value="">
                                {ownerCandidatesLoading
                                    ? t('common.loading', 'Loading...')
                                    : t('workspace.digitalEmployees.selectOwner', 'Select an active company member')}
                            </option>
                            {ownerCandidates.map((candidate) => (
                                <option key={candidate.id} value={candidate.id}>
                                    {candidate.display_name} · {candidate.email}
                                </option>
                            ))}
                        </select>
                    </div>
                    <div className="form-group">
                        <label className="form-label" htmlFor="agent-owner-reason">
                            {t('workspace.digitalEmployees.ownerChangeReason', 'Reason')}
                        </label>
                        <textarea
                            id="agent-owner-reason"
                            className="form-input ws-employees-owner-reason"
                            value={ownershipReason}
                            onChange={(event) => setOwnershipReason(event.target.value)}
                            maxLength={500}
                            placeholder={t(
                                'workspace.digitalEmployees.ownerChangeReasonPlaceholder',
                                'Explain why responsibility is being transferred',
                            )}
                        />
                    </div>
                    {!ownerCandidatesLoading && ownerCandidates.length === 0 && (
                        <div className="ws-employees-owner-empty">
                            {t('workspace.digitalEmployees.noOwnerCandidates', 'No other active company member is eligible.')}
                        </div>
                    )}
                </div>
            </Modal>
        </div>
    );
}
