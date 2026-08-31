import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';

import { agentApi, type AgentOwnerCandidate } from '../../api/domains/agents';
import { usersApi } from '../../api/domains/users';
import { requestAppConfirm, showAppToast } from '../../components/AppDialogs';
import { Modal } from '../../components/ui';
import { useAuthStore } from '../../stores';
import type { Agent } from '../../types';

import './WorkspaceDigitalEmployeesSection.css';

interface WorkspaceDigitalEmployeesSectionProps {
    selectedTenantId: string;
}

export default function WorkspaceDigitalEmployeesSection({ selectedTenantId }: WorkspaceDigitalEmployeesSectionProps) {
    const { t } = useTranslation();
    const queryClient = useQueryClient();
    const currentUser = useAuthStore((state) => state.user);
    const canManageEmployees = currentUser?.role === 'org_admin';
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
        queryFn: () => agentApi.list(selectedTenantId || undefined),
        enabled: canManageEmployees,
    });

    const { data: users = [] } = useQuery({
        queryKey: ['users', selectedTenantId, 'agent-owner-display'],
        queryFn: () => usersApi.list(selectedTenantId || undefined),
        enabled: canManageEmployees,
    });

    const userById = new Map(users.map((user) => [user.id, user]));

    const deleteAgentMutation = useMutation({
        mutationFn: (agentId: string) => agentApi.remove(agentId),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['agents'] });
            queryClient.invalidateQueries({ queryKey: ['agents', selectedTenantId, 'admin-management'] });
            queryClient.invalidateQueries({ queryKey: ['enterprise-stats'] });
            showAppToast(t('workspace.digitalEmployees.employeeDeleted', 'Digital employee deleted.'), 'success');
        },
        onError: (error: any) => {
            showAppToast(error?.message || t('workspace.digitalEmployees.employeeDeleteFailed', 'Failed to delete digital employee.'), 'error');
        },
    });

    const transferOwnerMutation = useMutation({
        mutationFn: ({ agent, ownerId, reason }: { agent: Agent; ownerId: string; reason: string }) =>
            agentApi.transferOwnership(agent.id, {
                new_owner_id: ownerId,
                expected_owner_id: agent.owner_user_id || agent.creator_id,
                reason,
                request_id: globalThis.crypto?.randomUUID?.() || `handover-${Date.now()}`,
            }),
        onSuccess: async (receipt) => {
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

    const isSystemProtectedAgent = (agent: Agent) => agent.id === hrAgent?.id || agent.name === '__system_hr__';

    const handleDeleteAgent = async (agent: Agent) => {
        if (isSystemProtectedAgent(agent)) return;
        const confirmed = await requestAppConfirm({
            title: t('workspace.digitalEmployees.deleteEmployeeTitle', 'Delete digital employee'),
            message: t(
                'workspace.digitalEmployees.deleteEmployeeConfirm',
                'Delete {{name}}? This stops the employee, disables its triggers and schedules, archives its files, and preserves audit history.',
                { name: agent.name },
            ),
            confirmLabel: t('workspace.digitalEmployees.deleteEmployeeButton', 'Delete employee'),
            danger: true,
        });
        if (!confirmed) return;
        await deleteAgentMutation.mutateAsync(agent.id);
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
                            {t('workspace.digitalEmployees.description', 'Company admins can review and remove tenant digital employees from this governed backend surface.')}
                        </p>
                    </div>
                    <Link className="btn btn-primary ws-employees-create" to="/agents/new">
                        {t('employees.createViaHr', 'Create via HR')}
                    </Link>
                </div>

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
                                    <th className="ws-employees-th">{t('workspace.digitalEmployees.employeeStatus', 'Status')}</th>
                                    <th className="ws-employees-th">{t('workspace.digitalEmployees.employeeType', 'Type')}</th>
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
                                                <div className="ws-employees-name">{agent.name}</div>
                                                <div className="ws-employees-role">
                                                    {agent.role_description || t('employees.noRole', 'No role description yet')}
                                                </div>
                                            </td>
                                            <td className="ws-employees-td">
                                                <div className="ws-employees-owner-name">
                                                    {owner?.display_name || owner?.email || t('workspace.digitalEmployees.ownerUnknown', 'Unknown user')}
                                                </div>
                                                {owner?.email && <div className="ws-employees-owner-email">{owner.email}</div>}
                                            </td>
                                            <td className="ws-employees-td">{agent.status}</td>
                                            <td className="ws-employees-td">{agent.agent_type || 'native'}</td>
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
                                                    {protectedAgent ? (
                                                        <span className="ws-employees-protected">
                                                            {t('workspace.digitalEmployees.systemProtected', 'System protected')}
                                                        </span>
                                                    ) : (
                                                        <button
                                                            type="button"
                                                            className="btn btn-ghost ws-employees-del"
                                                            onClick={() => handleDeleteAgent(agent)}
                                                            disabled={deleteAgentMutation.isPending}
                                                        >
                                                            {t('workspace.digitalEmployees.deleteEmployeeButton', 'Delete employee')}
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
                title={t('workspace.digitalEmployees.changeOwnerTitle', 'Change Agent owner')}
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
