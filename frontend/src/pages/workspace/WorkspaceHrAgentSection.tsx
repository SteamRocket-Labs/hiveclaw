import { useState, useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';

import { agentApi } from '../../api/domains/agents';
import { enterpriseApi } from '../../api/domains/enterprise';
import { fileApi } from '../../api/domains/files';

interface WorkspaceHrAgentSectionProps {
    selectedTenantId: string;
}

export default function WorkspaceHrAgentSection({ selectedTenantId }: WorkspaceHrAgentSectionProps) {
    const { t } = useTranslation();
    const navigate = useNavigate();
    const queryClient = useQueryClient();

    const { data: hrAgent, isLoading, error, refetch } = useQuery({
        queryKey: ['hr-agent', selectedTenantId],
        queryFn: () => agentApi.getHrAgent(),
        retry: 1,
    });

    const { data: models } = useQuery({
        queryKey: ['llm-models', selectedTenantId],
        queryFn: () => enterpriseApi.listLLMModels(selectedTenantId),
    });

    const [soulContent, setSoulContent] = useState('');
    const [soulLoading, setSoulLoading] = useState(false);
    const [soulSaving, setSoulSaving] = useState(false);
    const [soulSaved, setSoulSaved] = useState(false);
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

    const saveSoul = async () => {
        if (!hrAgent?.id) return;
        setSoulSaving(true);
        try {
            await fileApi.write(hrAgent.id, 'soul.md', soulContent);
            setSoulSaved(true);
            setTimeout(() => setSoulSaved(false), 2000);
        } catch (e: any) {
            alert(t('workspace.hr.saveFailed', 'Failed to save: ') + (e.message || e));
        }
        setSoulSaving(false);
    };

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
            alert(t('workspace.hr.saveFailed', 'Failed to save: ') + (e.message || e));
        }
        setSettingsSaving(false);
    };

    const resetToDefault = async () => {
        if (!hrAgent?.id) return;
        if (!confirm(t('workspace.hr.resetConfirm', 'Reset HR Agent to default template? Current customizations will be lost.'))) return;
        try {
            await refetch();
            if (hrAgent?.id) {
                const res = await fileApi.read(hrAgent.id, 'soul.md');
                setSoulContent(typeof res === 'string' ? res : (res as any).content || '');
            }
        } catch (e: any) {
            alert(e.message || 'Reset failed');
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
        <div style={{ maxWidth: '720px' }}>
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

            {/* Soul.md Editor */}
            <div className="card" style={{ marginBottom: '16px', padding: '16px' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
                    <h4 style={{ fontSize: '14px', fontWeight: 600, margin: 0 }}>{t('workspace.hr.soulEditor', 'System Prompt (soul.md)')}</h4>
                    <div style={{ display: 'flex', gap: '8px' }}>
                        <button className="btn btn-ghost" style={{ fontSize: '12px' }} onClick={resetToDefault}>
                            {t('workspace.hr.resetDefault', 'Reset to Default')}
                        </button>
                        <button className="btn btn-primary" onClick={saveSoul} disabled={soulSaving || soulLoading}>
                            {soulSaving ? t('common.saving', 'Saving...') : soulSaved ? '✓' : t('common.save', 'Save')}
                        </button>
                    </div>
                </div>
                {soulLoading ? (
                    <div style={{ padding: '24px', textAlign: 'center', color: 'var(--text-tertiary)' }}>
                        <div className="spinner" style={{ margin: '0 auto' }} />
                    </div>
                ) : (
                    <textarea
                        className="form-input"
                        value={soulContent}
                        onChange={(e) => setSoulContent(e.target.value)}
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
