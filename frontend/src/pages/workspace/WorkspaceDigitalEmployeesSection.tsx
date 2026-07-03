import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';

import { agentApi } from '../../api/domains/agents';
import { requestAppConfirm, showAppToast } from '../../components/AppDialogs';
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
                                    <th className="ws-employees-th">{t('workspace.digitalEmployees.employeeStatus', 'Status')}</th>
                                    <th className="ws-employees-th">{t('workspace.digitalEmployees.employeeType', 'Type')}</th>
                                    <th className="ws-employees-th-right">{t('workspace.digitalEmployees.employeeActions', 'Actions')}</th>
                                </tr>
                            </thead>
                            <tbody>
                                {(agents as Agent[]).map((agent) => {
                                    const protectedAgent = isSystemProtectedAgent(agent);
                                    return (
                                        <tr key={agent.id} className="ws-employees-row">
                                            <td className="ws-employees-td">
                                                <div className="ws-employees-name">{agent.name}</div>
                                                <div className="ws-employees-role">
                                                    {agent.role_description || t('employees.noRole', 'No role description yet')}
                                                </div>
                                            </td>
                                            <td className="ws-employees-td">{agent.status}</td>
                                            <td className="ws-employees-td">{agent.agent_type || 'native'}</td>
                                            <td className="ws-employees-td-right">
                                                <div className="ws-employees-actions">
                                                    <Link to={`/agents/${agent.id}`} className="btn btn-ghost">
                                                        {t('employees.actions.detail', 'Detail')}
                                                    </Link>
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
        </div>
    );
}
