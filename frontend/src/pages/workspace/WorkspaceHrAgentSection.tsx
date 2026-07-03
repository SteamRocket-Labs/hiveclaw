import { useState, useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';

import { agentApi } from '../../api/domains/agents';
import { enterpriseApi } from '../../api/domains/enterprise';
import { fileApi } from '../../api/domains/files';
import { showAppToast } from '../../components/AppDialogs';

import './WorkspaceHrAgentSection.css';

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
            <div className="ws-hr-loading">
                <div className="spinner ws-hr-spinner-top" />
                <p>{t('hrChat.loading', 'Loading HR agent...')}</p>
            </div>
        );
    }

    if (error) {
        return (
            <div className="ws-hr-error">
                <p className="ws-hr-error-text">{t('workspace.hr.noAgent', 'HR Agent not available. Ensure at least one LLM model is configured.')}</p>
                <button className="btn btn-primary ws-hr-error-retry" onClick={() => refetch()}>
                    {t('common.retry', 'Retry')}
                </button>
            </div>
        );
    }

    return (
        <div className="ws-hr-page">
            {/* Status */}
            <div className="card ws-hr-card">
                <div className="ws-hr-head">
                    <div>
                        <h3 className="ws-hr-title">{t('workspace.hr.title', 'HR Onboarding Agent')}</h3>
                        <p className="ws-hr-desc">
                            {t('workspace.hr.description', 'Guides users through creating digital employees via conversation. Customize its behavior for your company.')}
                        </p>
                    </div>
                    <div className="ws-hr-actions">
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
            <div className="card ws-hr-card">
                <h4 className="ws-hr-settings-title">{t('workspace.hr.settings', 'Settings')}</h4>

                <div className="ws-hr-field">
                    <label className="ws-hr-label">
                        {t('wizard.step1.primaryModel', 'Primary Model')}
                    </label>
                    <select
                        className="form-input"
                        value={selectedModelId}
                        onChange={(e) => setSelectedModelId(e.target.value)}
                    >
                        <option value="">—</option>
                        {(models || []).filter((m: any) => m.enabled).map((m: any) => (
                            <option key={m.id} value={m.id}>{m.display_name || m.model} ({m.provider})</option>
                        ))}
                    </select>
                </div>

                <div className="ws-hr-field">
                    <label className="ws-hr-label">
                        {t('workspace.hr.welcomeMessage', 'Welcome Message')}
                    </label>
                    <input
                        className="form-input"
                        value={welcomeMessage}
                        onChange={(e) => setWelcomeMessage(e.target.value)}
                        placeholder={t('workspace.hr.welcomePlaceholder', 'Greeting shown when users start a new conversation')}
                    />
                </div>

                <button className="btn btn-primary" onClick={saveSettings} disabled={settingsSaving}>
                    {settingsSaving ? t('common.saving', 'Saving...') : t('common.save', 'Save')}
                </button>
            </div>

            {/* Soul.md Read Model */}
            <div className="card ws-hr-card">
                <div className="ws-hr-section-head">
                    <h4 className="ws-hr-section-title">{t('workspace.hr.soulEditor', 'System Prompt (soul.md)')}</h4>
                    <div className="ws-hr-inline-actions">
                        <button className="btn btn-ghost" onClick={refreshSoul} disabled={soulLoading}>
                            {t('common.refresh', 'Refresh')}
                        </button>
                    </div>
                </div>
                <div className="ws-hr-notice">
                    {t('workspace.hr.soulGovernedNotice', 'soul.md is governed by Dream/Soul promotion.')}
                </div>
                {soulLoading ? (
                    <div className="ws-hr-soul-loading">
                        <div className="spinner ws-hr-spinner-mid" />
                    </div>
                ) : (
                    <textarea
                        className="form-input ws-hr-soul-textarea"
                        value={soulContent}
                        readOnly
                    />
                )}
            </div>
        </div>
    );
}
