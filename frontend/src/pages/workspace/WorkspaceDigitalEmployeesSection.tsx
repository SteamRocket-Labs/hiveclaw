import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';

import { agentApi } from '../../api/domains/agents';
import { requestAppConfirm, showAppToast } from '../../components/AppDialogs';
import { useAuthStore } from '../../stores';
import type { Agent } from '../../types';

interface WorkspaceDigitalEmployeesSectionProps {
    selectedTenantId: string;
}

export default function WorkspaceDigitalEmployeesSection({ selectedTenantId }: WorkspaceDigitalEmployeesSectionProps) {
    const { t } = useTranslation();
    const queryClient = useQueryClient();
    const currentUser = useAuthStore((state) => state.user);
    const canManageEmployees = currentUser?.role === 'org_admin' || currentUser?.role === 'platform_admin';

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

    if (!canManageEmployees) {
        return (
            <div style={{ maxWidth: '960px' }}>
                <div className="card" style={{ padding: '16px' }}>
                    <h3 style={{ fontSize: '15px', fontWeight: 600, margin: 0 }}>
                        {t('workspace.digitalEmployees.title', 'Digital Employee Management')}
                    </h3>
                    <p style={{ fontSize: '13px', color: 'var(--text-secondary)', marginTop: '4px', marginBottom: 0 }}>
                        {t('workspace.digitalEmployees.adminOnly', 'Only company administrators can manage digital employees here.')}
                    </p>
                </div>
            </div>
        );
    }

    return (
        <div style={{ maxWidth: '960px' }}>
            <div className="card" style={{ marginBottom: '16px', padding: '16px' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px', marginBottom: '12px' }}>
                    <div>
                        <h3 style={{ fontSize: '15px', fontWeight: 600, margin: 0 }}>
                            {t('workspace.digitalEmployees.title', 'Digital Employee Management')}
                        </h3>
                        <p style={{ fontSize: '13px', color: 'var(--text-secondary)', marginTop: '4px', marginBottom: 0 }}>
                            {t('workspace.digitalEmployees.description', 'Company admins can review and remove tenant digital employees from this governed backend surface.')}
                        </p>
                    </div>
                    <Link className="btn btn-primary" to="/agents/new" style={{ flexShrink: 0 }}>
                        {t('employees.createViaHr', 'Create via HR')}
                    </Link>
                </div>

                {agentsLoading ? (
                    <div style={{ padding: '16px 0', color: 'var(--text-secondary)' }}>
                        {t('common.loading', 'Loading...')}
                    </div>
                ) : (
                    <div style={{ overflowX: 'auto' }}>
                        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px' }}>
                            <thead>
                                <tr style={{ color: 'var(--text-secondary)', borderBottom: '1px solid var(--border)' }}>
                                    <th style={{ textAlign: 'left', padding: '8px 6px' }}>{t('workspace.digitalEmployees.employeeName', 'Employee')}</th>
                                    <th style={{ textAlign: 'left', padding: '8px 6px' }}>{t('workspace.digitalEmployees.employeeStatus', 'Status')}</th>
                                    <th style={{ textAlign: 'left', padding: '8px 6px' }}>{t('workspace.digitalEmployees.employeeType', 'Type')}</th>
                                    <th style={{ textAlign: 'right', padding: '8px 6px' }}>{t('workspace.digitalEmployees.employeeActions', 'Actions')}</th>
                                </tr>
                            </thead>
                            <tbody>
                                {(agents as Agent[]).map((agent) => {
                                    const protectedAgent = isSystemProtectedAgent(agent);
                                    return (
                                        <tr key={agent.id} style={{ borderBottom: '1px solid var(--border-muted, #f1f5f9)' }}>
                                            <td style={{ padding: '10px 6px', verticalAlign: 'top' }}>
                                                <div style={{ fontWeight: 600 }}>{agent.name}</div>
                                                <div style={{ color: 'var(--text-secondary)', fontSize: '12px', maxWidth: 460, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                                    {agent.role_description || t('employees.noRole', 'No role description yet')}
                                                </div>
                                            </td>
                                            <td style={{ padding: '10px 6px', verticalAlign: 'top' }}>{agent.status}</td>
                                            <td style={{ padding: '10px 6px', verticalAlign: 'top' }}>{agent.agent_type || 'native'}</td>
                                            <td style={{ padding: '10px 6px', textAlign: 'right', verticalAlign: 'top' }}>
                                                <div style={{ display: 'inline-flex', alignItems: 'center', gap: '8px' }}>
                                                    <Link to={`/agents/${agent.id}`} className="btn btn-ghost" style={{ fontSize: '12px' }}>
                                                        {t('employees.actions.detail', 'Detail')}
                                                    </Link>
                                                    {protectedAgent ? (
                                                        <span style={{ color: 'var(--text-tertiary)', fontSize: '12px' }}>
                                                            {t('workspace.digitalEmployees.systemProtected', 'System protected')}
                                                        </span>
                                                    ) : (
                                                        <button
                                                            type="button"
                                                            className="btn btn-ghost"
                                                            onClick={() => handleDeleteAgent(agent)}
                                                            disabled={deleteAgentMutation.isPending}
                                                            style={{ color: 'var(--error)', fontSize: '12px' }}
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
                            <div style={{ padding: '16px 0', color: 'var(--text-secondary)' }}>
                                {t('workspace.digitalEmployees.noEmployees', 'No digital employees found.')}
                            </div>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
}
