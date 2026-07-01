import { useState, useEffect } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link, useNavigate } from 'react-router-dom';

import { agentApi } from '../../api/domains/agents';
import { enterpriseApi } from '../../api/domains/enterprise';
import { fileApi } from '../../api/domains/files';
import { requestAppConfirm, showAppToast } from '../../components/AppDialogs';
import { useAuthStore } from '../../stores';
import type { Agent } from '../../types';

interface WorkspaceHrAgentSectionProps {
    selectedTenantId: string;
}

export default function WorkspaceHrAgentSection({ selectedTenantId }: WorkspaceHrAgentSectionProps) {
    const { t } = useTranslation();
    const navigate = useNavigate();
    const queryClient = useQueryClient();
    const currentUser = useAuthStore((state) => state.user);
    const canManageEmployees = currentUser?.role === 'org_admin' || currentUser?.role === 'platform_admin';

    const { data: hrAgent, isLoading, error, refetch } = useQuery({
        queryKey: ['hr-agent', selectedTenantId],
        queryFn: () => agentApi.getHrAgent(),
        retry: 1,
    });

    const { data: models } = useQuery({
        queryKey: ['llm-models', selectedTenantId],
        queryFn: () => enterpriseApi.listLLMModels(selectedTenantId),
    });
    const { data: agents = [], isLoading: agentsLoading } = useQuery({
        queryKey: ['agents', selectedTenantId, 'admin-management'],
        queryFn: () => agentApi.list(selectedTenantId || undefined),
        enabled: canManageEmployees,
    });

    const [soulContent, setSoulContent] = useState('');
    const [soulLoading, setSoulLoading] = useState(false);
    const [welcomeMessage, setWelcomeMessage] = useState('');
    const [selectedModelId, setSelectedModelId] = useState('');
    const [settingsSaving, setSettingsSaving] = useState(false);

    // Load soul.md and agent settings when HR agent is available
    useEffect(() => {
        if (!hrAgent?.id) return;
        setSoulLoading(true);
        fileApi.read(hrAgent.id, 'soul.md')
            .then((res) => setSoulContent(typeof res === 'string' ? res : (res as any).content || ''))
            .catch(() => setSoulContent(''))
            .finally(() => setSoulLoading(false));

        agentApi.getById(hrAgent.id).then((agent: any) => {
            setWelcomeMessage(agent.welcome_message || '');
            setSelectedModelId(agent.primary_model_id || '');
        }).catch(() => {});
    }, [hrAgent?.id]);

    const saveSettings = async () => {
        if (!hrAgent?.id) return;
        setSettingsSaving(true);
        try {
            await agentApi.update(hrAgent.id, {
                welcome_message: welcomeMessage || null,
                primary_model_id: selectedModelId || null,
            } as any);
            queryClient.invalidateQueries({ queryKey: ['hr-agent'] });
        } catch (e: any) {
            showAppToast(t('workspace.hr.saveFailed', 'Failed to save: ') + (e.message || e), 'error');
        }
        setSettingsSaving(false);
    };

    const deleteAgentMutation = useMutation({
        mutationFn: (agentId: string) => agentApi.remove(agentId),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['agents'] });
            queryClient.invalidateQueries({ queryKey: ['agents', selectedTenantId, 'admin-management'] });
            queryClient.invalidateQueries({ queryKey: ['enterprise-stats'] });
            showAppToast(t('workspace.hr.employeeDeleted', 'Digital employee deleted.'), 'success');
        },
        onError: (error: any) => {
            showAppToast(error?.message || t('workspace.hr.employeeDeleteFailed', 'Failed to delete digital employee.'), 'error');
        },
    });

    const isSystemProtectedAgent = (agent: Agent) => agent.id === hrAgent?.id || agent.name === '__system_hr__';

    const handleDeleteAgent = async (agent: Agent) => {
        if (isSystemProtectedAgent(agent)) return;
        const confirmed = await requestAppConfirm({
            title: t('workspace.hr.deleteEmployeeTitle', 'Delete digital employee'),
            message: t(
                'workspace.hr.deleteEmployeeConfirm',
                'Delete {{name}}? This stops the employee, disables its triggers and schedules, archives its files, and preserves audit history.',
                { name: agent.name },
            ),
            confirmLabel: t('workspace.hr.deleteEmployeeButton', 'Delete employee'),
            danger: true,
        });
        if (!confirmed) return;
        await deleteAgentMutation.mutateAsync(agent.id);
    };

    const refreshSoul = async () => {
        if (!hrAgent?.id) return;
        setSoulLoading(true);
        try {
            await refetch();
            if (hrAgent?.id) {
                const res = await fileApi.read(hrAgent.id, 'soul.md');
                setSoulContent(typeof res === 'string' ? res : (res as any).content || '');
            }
        } catch (e: any) {
            showAppToast(e.message || 'Refresh failed', 'error');
        } finally {
            setSoulLoading(false);
        }
    };

    if (isLoading) {
        return (
            <div style={{ padding: '32px', textAlign: 'center', color: 'var(--text-secondary)' }}>
                <div className="spinner" style={{ margin: '0 auto 12px' }} />
                <p>{t('hrChat.loading', 'Loading HR agent...')}</p>
            </div>
        );
    }

    if (error) {
        return (
            <div style={{ padding: '32px', textAlign: 'center' }}>
                <p style={{ color: 'var(--error)' }}>{t('workspace.hr.noAgent', 'HR Agent not available. Ensure at least one LLM model is configured.')}</p>
                <button className="btn btn-primary" style={{ marginTop: '12px' }} onClick={() => refetch()}>
                    {t('common.retry', 'Retry')}
                </button>
            </div>
        );
    }

    return (
        <div style={{ maxWidth: '960px' }}>
            {/* Status */}
            <div className="card" style={{ marginBottom: '16px', padding: '16px' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <div>
                        <h3 style={{ fontSize: '15px', fontWeight: 600, margin: 0 }}>{t('workspace.hr.title', 'HR Onboarding Agent')}</h3>
                        <p style={{ fontSize: '13px', color: 'var(--text-secondary)', marginTop: '4px' }}>
                            {t('workspace.hr.description', 'Guides users through creating digital employees via conversation. Customize its behavior for your company.')}
                        </p>
                    </div>
                    <div style={{ display: 'flex', gap: '8px', flexShrink: 0 }}>
                        <button className="btn btn-secondary" onClick={() => navigate(`/agents/${hrAgent!.id}?manage=true`)}>
                            {t('workspace.hr.manage', 'Manage')}
                        </button>
                        <button className="btn btn-primary" onClick={() => navigate(`/agents/${hrAgent!.id}#chat`)}>
                            {t('workspace.hr.openChat', 'Open Chat')}
                        </button>
                    </div>
                </div>
            </div>

            {/* Company Digital Employee Management */}
            {canManageEmployees && (
                <div className="card" style={{ marginBottom: '16px', padding: '16px' }}>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px', marginBottom: '12px' }}>
                        <div>
                            <h4 style={{ fontSize: '14px', fontWeight: 600, margin: 0 }}>
                                {t('workspace.hr.employeeManagement', 'Digital Employee Management')}
                            </h4>
                            <p style={{ fontSize: '13px', color: 'var(--text-secondary)', marginTop: '4px', marginBottom: 0 }}>
                                {t('workspace.hr.employeeManagementDesc', 'Company admins can review and remove tenant digital employees from this governed backend surface.')}
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
                                        <th style={{ textAlign: 'left', padding: '8px 6px' }}>{t('workspace.hr.employeeName', 'Employee')}</th>
                                        <th style={{ textAlign: 'left', padding: '8px 6px' }}>{t('workspace.hr.employeeStatus', 'Status')}</th>
                                        <th style={{ textAlign: 'left', padding: '8px 6px' }}>{t('workspace.hr.employeeType', 'Type')}</th>
                                        <th style={{ textAlign: 'right', padding: '8px 6px' }}>{t('workspace.hr.employeeActions', 'Actions')}</th>
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
                                                                {t('workspace.hr.systemProtected', 'System protected')}
                                                            </span>
                                                        ) : (
                                                            <button
                                                                type="button"
                                                                className="btn btn-ghost"
                                                                onClick={() => handleDeleteAgent(agent)}
                                                                disabled={deleteAgentMutation.isPending}
                                                                style={{ color: 'var(--error)', fontSize: '12px' }}
                                                            >
                                                                {t('workspace.hr.deleteEmployeeButton', 'Delete employee')}
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
                                    {t('workspace.hr.noEmployees', 'No digital employees found.')}
                                </div>
                            )}
                        </div>
                    )}
                </div>
            )}

            {/* Model & Welcome Message */}
            <div className="card" style={{ marginBottom: '16px', padding: '16px' }}>
                <h4 style={{ fontSize: '14px', fontWeight: 600, margin: '0 0 12px' }}>{t('workspace.hr.settings', 'Settings')}</h4>

                <div style={{ marginBottom: '12px' }}>
                    <label style={{ fontSize: '13px', fontWeight: 500, display: 'block', marginBottom: '4px' }}>
                        {t('wizard.step1.primaryModel', 'Primary Model')}
                    </label>
                    <select
                        className="form-input"
                        value={selectedModelId}
                        onChange={(e) => setSelectedModelId(e.target.value)}
                        style={{ width: '100%' }}
                    >
                        <option value="">—</option>
                        {(models || []).filter((m: any) => m.enabled).map((m: any) => (
                            <option key={m.id} value={m.id}>{m.display_name || m.model} ({m.provider})</option>
                        ))}
                    </select>
                </div>

                <div style={{ marginBottom: '12px' }}>
                    <label style={{ fontSize: '13px', fontWeight: 500, display: 'block', marginBottom: '4px' }}>
                        {t('workspace.hr.welcomeMessage', 'Welcome Message')}
                    </label>
                    <input
                        className="form-input"
                        value={welcomeMessage}
                        onChange={(e) => setWelcomeMessage(e.target.value)}
                        placeholder={t('workspace.hr.welcomePlaceholder', 'Greeting shown when users start a new conversation')}
                        style={{ width: '100%' }}
                    />
                </div>

                <button className="btn btn-primary" onClick={saveSettings} disabled={settingsSaving}>
                    {settingsSaving ? t('common.saving', 'Saving...') : t('common.save', 'Save')}
                </button>
            </div>

            {/* Soul.md Read Model */}
            <div className="card" style={{ marginBottom: '16px', padding: '16px' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
                    <h4 style={{ fontSize: '14px', fontWeight: 600, margin: 0 }}>{t('workspace.hr.soulEditor', 'System Prompt (soul.md)')}</h4>
                    <div style={{ display: 'flex', gap: '8px' }}>
                        <button className="btn btn-ghost" style={{ fontSize: '12px' }} onClick={refreshSoul} disabled={soulLoading}>
                            {t('common.refresh', 'Refresh')}
                        </button>
                    </div>
                </div>
                <div
                    style={{
                        padding: '10px 12px',
                        borderRadius: '8px',
                        background: 'var(--bg-secondary)',
                        color: 'var(--text-secondary)',
                        fontSize: '13px',
                        marginBottom: '12px',
                    }}
                >
                    {t('workspace.hr.soulGovernedNotice', 'soul.md is governed by Dream/Soul promotion.')}
                </div>
                {soulLoading ? (
                    <div style={{ padding: '24px', textAlign: 'center', color: 'var(--text-tertiary)' }}>
                        <div className="spinner" style={{ margin: '0 auto' }} />
                    </div>
                ) : (
                    <textarea
                        className="form-input"
                        value={soulContent}
                        readOnly
                        style={{
                            width: '100%', minHeight: '360px', fontFamily: 'var(--font-mono)',
                            fontSize: '12px', lineHeight: 1.6, resize: 'vertical',
                        }}
                    />
                )}
            </div>
        </div>
    );
}
