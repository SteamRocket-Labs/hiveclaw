import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { authApi } from '../api/domains/auth';
import { enterpriseApi } from '../api/domains/enterprise';
import { notificationsApi } from '../api/domains/notifications';
import { systemApi } from '../api/domains/system';
import FileBrowser from '../components/FileBrowser';
import type { FileBrowserApi } from '../components/FileBrowser';
import { useAuthStore } from '../stores';
import { saveAccentColor, getSavedAccentColor, resetAccentColor, PRESET_COLORS } from '../utils/theme';
import WorkspaceApprovalsSection from './workspace/WorkspaceApprovalsSection';
import WorkspaceAuditSection from './workspace/WorkspaceAuditSection';
import WorkspaceInfoSection from './workspace/WorkspaceInfoSection';
import WorkspaceInvitesSection from './workspace/WorkspaceInvitesSection';
import WorkspaceLlmSection from './workspace/WorkspaceLlmSection';
import WorkspaceOrgSection from './workspace/WorkspaceOrgSection';
import WorkspaceQuotasSection from './workspace/WorkspaceQuotasSection';
import WorkspaceSkillsSection from './workspace/WorkspaceSkillsSection';
import WorkspaceHrAgentSection from './workspace/WorkspaceHrAgentSection';
import WorkspaceMemorySection from './workspace/WorkspaceMemorySection';
import WorkspaceToolsSection from './workspace/WorkspaceToolsSection';
import WorkspaceUsersSection from './workspace/WorkspaceUsersSection';

interface LLMModel {
    id: string; provider: string; model: string; label: string;
    base_url?: string; api_key_masked?: string; max_tokens_per_day?: number; enabled: boolean; supports_vision?: boolean; max_output_tokens?: number | null; max_input_tokens?: number | null; temperature?: number | null; created_at?: string;
}

interface LLMProviderSpec {
    provider: string;
    display_name: string;
    protocol: string;
    default_base_url?: string | null;
    supports_tool_choice: boolean;
    default_max_tokens: number;
}

export type EnterpriseSettingsTab =
    | 'llm'
    | 'memory'
    | 'org'
    | 'info'
    | 'hr'
    | 'approvals'
    | 'audit'
    | 'tools'
    | 'skills'
    | 'quotas'
    | 'users'
    | 'invites';

interface EnterpriseSettingsProps {
    forcedTab?: EnterpriseSettingsTab;
    hideTabs?: boolean;
}

const FALLBACK_LLM_PROVIDERS: LLMProviderSpec[] = [
    { provider: 'anthropic', display_name: 'Anthropic', protocol: 'anthropic', default_base_url: 'https://api.anthropic.com', supports_tool_choice: false, default_max_tokens: 8192 },
    { provider: 'openai', display_name: 'OpenAI', protocol: 'openai_compatible', default_base_url: 'https://api.openai.com/v1', supports_tool_choice: true, default_max_tokens: 16384 },
    { provider: 'azure', display_name: 'Azure OpenAI', protocol: 'openai_compatible', default_base_url: '', supports_tool_choice: true, default_max_tokens: 16384 },
    { provider: 'deepseek', display_name: 'DeepSeek', protocol: 'openai_compatible', default_base_url: 'https://api.deepseek.com/v1', supports_tool_choice: true, default_max_tokens: 8192 },
    { provider: 'minimax', display_name: 'MiniMax', protocol: 'openai_compatible', default_base_url: 'https://api.minimaxi.com/v1', supports_tool_choice: true, default_max_tokens: 16384 },
    { provider: 'qwen', display_name: 'Qwen (DashScope)', protocol: 'openai_compatible', default_base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', supports_tool_choice: true, default_max_tokens: 8192 },
    { provider: 'zhipu', display_name: 'Zhipu', protocol: 'openai_compatible', default_base_url: 'https://open.bigmodel.cn/api/paas/v4', supports_tool_choice: true, default_max_tokens: 8192 },
    { provider: 'baidu', display_name: 'Baidu (Qianfan)', protocol: 'openai_compatible', default_base_url: 'https://qianfan.baidubce.com/v2', supports_tool_choice: false, default_max_tokens: 4096 },
    { provider: 'gemini', display_name: 'Gemini', protocol: 'gemini', default_base_url: 'https://generativelanguage.googleapis.com/v1beta', supports_tool_choice: true, default_max_tokens: 8192 },
    { provider: 'openrouter', display_name: 'OpenRouter', protocol: 'openai_compatible', default_base_url: 'https://openrouter.ai/api/v1', supports_tool_choice: true, default_max_tokens: 4096 },
    { provider: 'kimi', display_name: 'Kimi (Moonshot)', protocol: 'openai_compatible', default_base_url: 'https://api.moonshot.cn/v1', supports_tool_choice: true, default_max_tokens: 8192 },
    { provider: 'vllm', display_name: 'vLLM', protocol: 'openai_compatible', default_base_url: 'http://localhost:8000/v1', supports_tool_choice: true, default_max_tokens: 4096 },
    { provider: 'ollama', display_name: 'Ollama', protocol: 'openai_compatible', default_base_url: 'http://localhost:11434/v1', supports_tool_choice: true, default_max_tokens: 4096 },
    { provider: 'sglang', display_name: 'SGLang', protocol: 'openai_compatible', default_base_url: 'http://localhost:30000/v1', supports_tool_choice: true, default_max_tokens: 4096 },
    { provider: 'custom', display_name: 'Custom', protocol: 'openai_compatible', default_base_url: '', supports_tool_choice: true, default_max_tokens: 4096 },
];

// ─── Theme Color Picker ────────────────────────────
function ThemeColorPicker() {
    const { t } = useTranslation();
    const [currentColor, setCurrentColor] = useState(getSavedAccentColor() || '');
    const [customHex, setCustomHex] = useState('');

    const apply = (hex: string) => {
        setCurrentColor(hex);
        saveAccentColor(hex);
    };

    const handleReset = () => {
        setCurrentColor('');
        setCustomHex('');
        resetAccentColor();
    };

    const handleCustom = () => {
        const hex = customHex.trim();
        if (/^#[0-9a-fA-F]{6}$/.test(hex)) {
            apply(hex);
        }
    };

    return (
        <div className="card" style={{ marginTop: '16px', marginBottom: '16px' }}>
            <h4 style={{ marginBottom: '12px' }}>{t('enterprise.config.themeColor')}</h4>
            <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '12px' }}>
                {PRESET_COLORS.map(c => (
                    <div
                        key={c.hex}
                        onClick={() => apply(c.hex)}
                        title={c.name}
                        style={{
                            width: '32px', height: '32px', borderRadius: '8px',
                            background: c.hex, cursor: 'pointer',
                            border: currentColor === c.hex ? '2px solid var(--text-primary)' : '2px solid transparent',
                            outline: currentColor === c.hex ? '2px solid var(--bg-primary)' : 'none',
                            transition: 'all 120ms ease',
                        }}
                    />
                ))}
            </div>
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                <input
                    className="input"
                    value={customHex}
                    onChange={e => setCustomHex(e.target.value)}
                    placeholder="#hex"
                    style={{ width: '120px', fontSize: '13px', fontFamily: 'var(--font-mono)' }}
                    onKeyDown={e => e.key === 'Enter' && handleCustom()}
                />
                <button className="btn btn-secondary" style={{ fontSize: '12px' }} onClick={handleCustom}>Apply</button>
                {currentColor && (
                    <button className="btn btn-ghost" style={{ fontSize: '12px', color: 'var(--text-tertiary)' }} onClick={handleReset}>Reset</button>
                )}
                {currentColor && (
                    <div style={{ width: '20px', height: '20px', borderRadius: '4px', background: currentColor, border: '1px solid var(--border-default)' }} />
                )}
            </div>
        </div>
    );
}

// ─── Main Component ────────────────────────────────
// ─── Enterprise KB Browser ─────────────────────────
function EnterpriseKBBrowser({ onRefresh }: { onRefresh: () => void; refreshKey: number }) {
    const kbAdapter: FileBrowserApi = {
        list: (path) => enterpriseApi.kbFiles(path),
        read: (path) => enterpriseApi.kbRead(path),
        write: (path, content) => enterpriseApi.kbWrite(path, content),
        delete: (path) => enterpriseApi.kbDelete(path),
        upload: (file, path) => enterpriseApi.kbUpload(file, path),
    };
    return <FileBrowser api={kbAdapter} features={{ upload: true, newFolder: true, edit: true, delete: true, directoryNavigation: true }} onRefresh={onRefresh} />;
}

// ─── Company Name Editor ───────────────────────────
function CompanyNameEditor() {
    const { t } = useTranslation();
    const qc = useQueryClient();
    const tenantId = localStorage.getItem('current_tenant_id') || '';
    const [name, setName] = useState('');
    const [saving, setSaving] = useState(false);
    const [saved, setSaved] = useState(false);

    useEffect(() => {
        if (!tenantId) return;
        systemApi.getTenant(tenantId)
            .then(d => { if (d?.name) setName(d.name); })
            .catch(() => { });
    }, [tenantId]);

    const handleSave = async () => {
        if (!tenantId || !name.trim()) return;
        setSaving(true);
        try {
            await systemApi.updateTenant(tenantId, { name: name.trim() });
            qc.invalidateQueries({ queryKey: ['tenants'] });
            setSaved(true);
            setTimeout(() => setSaved(false), 2000);
        } catch (e) { }
        setSaving(false);
    };

    return (
        <div className="card" style={{ padding: '16px', marginBottom: '24px' }}>
            <div style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
                <input
                    className="form-input"
                    value={name}
                    onChange={e => setName(e.target.value)}
                    placeholder={t('enterprise.companyName.placeholder', 'Enter company name')}
                    style={{ flex: 1, fontSize: '14px' }}
                    onKeyDown={e => e.key === 'Enter' && handleSave()}
                />
                <button className="btn btn-primary" onClick={handleSave} disabled={saving || !name.trim()}>
                    {saving ? t('common.loading') : t('common.save', 'Save')}
                </button>
                {saved && <span style={{ color: 'var(--success)', fontSize: '12px' }}>✅</span>}
            </div>
        </div>
    );
}


// ─── Company Timezone Editor ───────────────────────
const COMMON_TIMEZONES = [
    'UTC',
    'Asia/Shanghai',
    'Asia/Tokyo',
    'Asia/Seoul',
    'Asia/Singapore',
    'Asia/Kolkata',
    'Asia/Dubai',
    'Europe/London',
    'Europe/Paris',
    'Europe/Berlin',
    'Europe/Moscow',
    'America/New_York',
    'America/Chicago',
    'America/Denver',
    'America/Los_Angeles',
    'America/Sao_Paulo',
    'Australia/Sydney',
    'Pacific/Auckland',
];

function CompanyTimezoneEditor() {
    const { t } = useTranslation();
    const tenantId = localStorage.getItem('current_tenant_id') || '';
    const [timezone, setTimezone] = useState('UTC');
    const [saving, setSaving] = useState(false);
    const [saved, setSaved] = useState(false);

    useEffect(() => {
        if (!tenantId) return;
        systemApi.getTenant(tenantId)
            .then(d => { if (d?.timezone) setTimezone(d.timezone); })
            .catch(() => { });
    }, [tenantId]);

    const handleSave = async (tz: string) => {
        if (!tenantId) return;
        setTimezone(tz);
        setSaving(true);
        try {
            await systemApi.updateTenant(tenantId, { timezone: tz });
            setSaved(true);
            setTimeout(() => setSaved(false), 2000);
        } catch (e) { }
        setSaving(false);
    };

    return (
        <div className="card" style={{ padding: '16px', marginBottom: '24px' }}>
            <div style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
                <div style={{ flex: 1 }}>
                    <div style={{ fontWeight: 500, fontSize: '13px', marginBottom: '4px' }}>🌐 {t('enterprise.timezone.title', 'Company Timezone')}</div>
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                        {t('enterprise.timezone.description', 'Default timezone for all agents. Agents can override individually.')}
                    </div>
                </div>
                <select
                    className="form-input"
                    value={timezone}
                    onChange={e => handleSave(e.target.value)}
                    style={{ width: '220px', fontSize: '13px' }}
                    disabled={saving}
                >
                    {COMMON_TIMEZONES.map(tz => (
                        <option key={tz} value={tz}>{tz}</option>
                    ))}
                </select>
                {saved && <span style={{ color: 'var(--success)', fontSize: '12px' }}>✅</span>}
            </div>
        </div>
    );
}


// ── Broadcast Section ──────────────────────────
function BroadcastSection() {
    const { t } = useTranslation();
    const [title, setTitle] = useState('');
    const [body, setBody] = useState('');
    const [sending, setSending] = useState(false);
    const [result, setResult] = useState<{ users: number; agents: number } | null>(null);

    const handleSend = async () => {
        if (!title.trim()) return;
        setSending(true);
        setResult(null);
        try {
            const data = await notificationsApi.broadcast({ title: title.trim(), body: body.trim() });
            setResult({ users: data.users_notified, agents: data.agents_notified });
            setTitle('');
            setBody('');
        } catch (e: any) {
            alert(e.message || 'Failed');
        }
        setSending(false);
    };

    return (
        <div style={{ marginTop: '24px', marginBottom: '24px' }}>
            <h3 style={{ marginBottom: '4px' }}>{t('enterprise.broadcast.title', 'Broadcast Notification')}</h3>
            <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
                {t('enterprise.broadcast.description', 'Send a notification to all users and agents in this company.')}
            </p>
            <div className="card" style={{ padding: '16px' }}>
                <input
                    className="form-input"
                    placeholder={t('enterprise.broadcast.titlePlaceholder', 'Notification title')}
                    value={title}
                    onChange={e => setTitle(e.target.value)}
                    maxLength={200}
                    style={{ marginBottom: '8px', fontSize: '13px' }}
                />
                <textarea
                    className="form-input"
                    placeholder={t('enterprise.broadcast.bodyPlaceholder', 'Optional details...')}
                    value={body}
                    onChange={e => setBody(e.target.value)}
                    maxLength={1000}
                    rows={3}
                    style={{ resize: 'vertical', fontSize: '13px', marginBottom: '12px' }}
                />
                <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                    <button className="btn btn-primary" onClick={handleSend} disabled={sending || !title.trim()}>
                        {sending ? t('common.loading') : t('enterprise.broadcast.send', 'Send Broadcast')}
                    </button>
                    {result && (
                        <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                            {t('enterprise.broadcast.sent', `Sent to ${result.users} users and ${result.agents} agents`, { users: result.users, agents: result.agents })}
                        </span>
                    )}
                </div>
            </div>
        </div>
    );
}


export default function EnterpriseSettings({ forcedTab, hideTabs = false }: EnterpriseSettingsProps) {
    const { t } = useTranslation();
    const navigate = useNavigate();
    const setUser = useAuthStore((s) => s.setUser);
    const qc = useQueryClient();
    const [activeTab, setActiveTab] = useState<EnterpriseSettingsTab>(forcedTab || 'info');

    // Track selected tenant as state so page refreshes on company switch
    const [selectedTenantId, setSelectedTenantId] = useState(localStorage.getItem('current_tenant_id') || '');
    useEffect(() => {
        const handler = (e: StorageEvent) => {
            if (e.key === 'current_tenant_id') {
                setSelectedTenantId(e.newValue || '');
            }
        };
        window.addEventListener('storage', handler);
        return () => window.removeEventListener('storage', handler);
    }, []);

    useEffect(() => {
        if (forcedTab && forcedTab !== activeTab) {
            setActiveTab(forcedTab);
        }
    }, [forcedTab, activeTab]);

    const [companyIntro, setCompanyIntro] = useState('');
    const [companyIntroSaving, setCompanyIntroSaving] = useState(false);
    const [companyIntroSaved, setCompanyIntroSaved] = useState(false);

    // Company intro key: always per-tenant scoped
    const companyIntroKey = selectedTenantId ? `company_intro_${selectedTenantId}` : 'company_intro';

    // Load Company Intro (tenant-scoped only, no fallback to global)
    useEffect(() => {
        setCompanyIntro('');
        if (!selectedTenantId) return;
        const tenantKey = `company_intro_${selectedTenantId}`;
        enterpriseApi.getSetting(tenantKey)
            .then(d => {
                if (d?.value?.content) {
                    setCompanyIntro(d.value.content);
                }
                // No fallback — each company starts empty with placeholder watermark
            })
            .catch(() => { });
    }, [selectedTenantId]);

    const saveCompanyIntro = async () => {
        setCompanyIntroSaving(true);
        try {
            await enterpriseApi.updateSetting(companyIntroKey, { content: companyIntro });
            setCompanyIntroSaved(true);
            setTimeout(() => setCompanyIntroSaved(false), 2000);
        } catch (e) { }
        setCompanyIntroSaving(false);
    };
    const [infoRefresh, setInfoRefresh] = useState(0);
    // ─── Stats (scoped to selected tenant)
    const { data: stats } = useQuery({
        queryKey: ['enterprise-stats', selectedTenantId],
        queryFn: () => enterpriseApi.getStats(selectedTenantId || undefined),
    });

    // ─── LLM Models
    const { data: models = [] } = useQuery({
        queryKey: ['llm-models', selectedTenantId],
        queryFn: () => enterpriseApi.llmModels(selectedTenantId || undefined),
        enabled: activeTab === 'llm',
    });
    const [showAddModel, setShowAddModel] = useState(false);
    const [editingModelId, setEditingModelId] = useState<string | null>(null);
    const [modelForm, setModelForm] = useState({ provider: 'anthropic', model: '', api_key: '', base_url: '', label: '', supports_vision: false, max_output_tokens: '' as string, max_input_tokens: '' as string, temperature: '' as string });
    const { data: providerSpecs = [] } = useQuery({
        queryKey: ['llm-provider-specs'],
        queryFn: () => enterpriseApi.getLLMProviders() as Promise<LLMProviderSpec[]>,
        enabled: activeTab === 'llm',
    });
    const providerOptions = providerSpecs.length > 0 ? providerSpecs : FALLBACK_LLM_PROVIDERS;
    const addModel = useMutation({
        mutationFn: (data: any) => enterpriseApi.createLLMModel(data, selectedTenantId || undefined),
        onSuccess: () => { qc.invalidateQueries({ queryKey: ['llm-models', selectedTenantId] }); setShowAddModel(false); setEditingModelId(null); },
    });
    const updateModel = useMutation({
        mutationFn: ({ id, data }: { id: string; data: any }) => enterpriseApi.updateLLMModel(id, data),
        onSuccess: () => { qc.invalidateQueries({ queryKey: ['llm-models', selectedTenantId] }); setShowAddModel(false); setEditingModelId(null); },
    });
    const deleteModel = useMutation({
        mutationFn: async ({ id, force = false }: { id: string; force?: boolean }) => {
            try {
                await enterpriseApi.deleteLLMModel(id, force);
            } catch (err: any) {
                if (err?.status === 409) {
                    const agents = err?.detail?.agents || [];
                    const msg = `This model is used by ${agents.length} agent(s):\n\n${agents.join(', ')}\n\nDelete anyway?`;
                    if (confirm(msg)) {
                        await enterpriseApi.deleteLLMModel(id, true);
                    }
                    return;
                }
                throw err;
            }
        },
        onSuccess: () => qc.invalidateQueries({ queryKey: ['llm-models', selectedTenantId] }),
    });

    const handleModelFormChange = (patch: Partial<typeof modelForm>) => {
        setModelForm((current) => ({ ...current, ...patch }));
    };

    const handleStartCreateModel = () => {
        setEditingModelId(null);
        const defaultSpec = providerOptions[0];
        setModelForm({
            provider: defaultSpec?.provider || 'anthropic',
            model: '',
            api_key: '',
            base_url: defaultSpec?.default_base_url || '',
            label: '',
            supports_vision: false,
            max_output_tokens: defaultSpec ? String(defaultSpec.default_max_tokens) : '4096',
            max_input_tokens: '',
            temperature: '',
        });
        setShowAddModel(true);
    };

    const handleCancelModelForm = () => {
        setShowAddModel(false);
        setEditingModelId(null);
    };

    const runModelTest = async (testData: Record<string, unknown>) => {
        const activeButton = document.activeElement as HTMLButtonElement | null;
        const originalText = activeButton?.textContent || '';
        if (activeButton) activeButton.textContent = t('enterprise.llm.testing');
        try {
            const result = await enterpriseApi.testLLM(testData);
            if (result.success) {
                if (activeButton) {
                    activeButton.textContent = t('enterprise.llm.testSuccess', { latency: result.latency_ms });
                    activeButton.style.color = 'var(--success)';
                }
                setTimeout(() => {
                    if (activeButton) {
                        activeButton.textContent = originalText;
                        activeButton.style.color = '';
                    }
                }, 3000);
                return;
            }
            alert(t('enterprise.llm.testFailed', { error: result.error || 'Unknown error', latency: result.latency_ms }));
            if (activeButton) activeButton.textContent = originalText;
        } catch (e: any) {
            alert(t('enterprise.llm.testError', { message: e.message }));
            if (activeButton) activeButton.textContent = originalText;
        }
    };

    const handleTestDraftModel = async () => {
        const testData: Record<string, unknown> = {
            provider: modelForm.provider,
            model: modelForm.model,
            base_url: modelForm.base_url || undefined,
        };
        if (modelForm.api_key) testData.api_key = modelForm.api_key;
        await runModelTest(testData);
    };

    const handleCreateModel = () => {
        addModel.mutate({
            ...modelForm,
            max_output_tokens: modelForm.max_output_tokens ? Number(modelForm.max_output_tokens) : null,
            max_input_tokens: modelForm.max_input_tokens ? Number(modelForm.max_input_tokens) : null,
            temperature: modelForm.temperature !== '' ? Number(modelForm.temperature) : null,
        });
    };

    const handleTestExistingModel = async () => {
        const testData: Record<string, unknown> = {
            provider: modelForm.provider,
            model: modelForm.model,
            base_url: modelForm.base_url || undefined,
            model_id: editingModelId || undefined,
        };
        if (modelForm.api_key) testData.api_key = modelForm.api_key;
        await runModelTest(testData);
    };

    const handleUpdateModel = () => {
        if (!editingModelId) return;
        updateModel.mutate({
            id: editingModelId,
            data: {
                ...modelForm,
                max_output_tokens: modelForm.max_output_tokens ? Number(modelForm.max_output_tokens) : null,
                max_input_tokens: modelForm.max_input_tokens ? Number(modelForm.max_input_tokens) : null,
                temperature: modelForm.temperature !== '' ? Number(modelForm.temperature) : null,
            },
        });
    };

    const handleToggleModel = async (modelId: string, enabled: boolean) => {
        try {
            await enterpriseApi.updateLLMModel(modelId, { enabled });
            qc.invalidateQueries({ queryKey: ['llm-models', selectedTenantId] });
        } catch (e) {
            console.error(e);
        }
    };

    const handleEditModel = (model: LLMModel) => {
        setEditingModelId(model.id);
        setModelForm({
            provider: model.provider,
            model: model.model,
            label: model.label,
            base_url: model.base_url || '',
            api_key: model.api_key_masked || '',
            supports_vision: model.supports_vision || false,
            max_output_tokens: model.max_output_tokens ? String(model.max_output_tokens) : '',
            max_input_tokens: model.max_input_tokens ? String(model.max_input_tokens) : '',
            temperature: model.temperature !== null && model.temperature !== undefined ? String(model.temperature) : '',
        });
        setShowAddModel(true);
    };

    const handleDeleteModel = (modelId: string) => {
        deleteModel.mutate({ id: modelId });
    };

    const handleDeleteCompany = async () => {
        if (!confirm(t('enterprise.deleteCompanyConfirm', 'Are you sure you want to delete this company and ALL its data? This cannot be undone.'))) return;
        try {
            const res = await systemApi.deleteTenant(selectedTenantId);
            const me = await authApi.getMe().catch(() => null);
            if (me) setUser(me);

            qc.invalidateQueries({ queryKey: ['tenants'] });

            if (res.fallback_tenant_id) {
                localStorage.setItem('current_tenant_id', res.fallback_tenant_id);
                setSelectedTenantId(res.fallback_tenant_id);
                window.dispatchEvent(new StorageEvent('storage', { key: 'current_tenant_id', newValue: res.fallback_tenant_id }));
                navigate('/enterprise', { replace: true });
                return;
            }

            localStorage.removeItem('current_tenant_id');
            setSelectedTenantId('');
            window.dispatchEvent(new StorageEvent('storage', { key: 'current_tenant_id', newValue: null }));
            navigate(res.needs_company_setup ? '/setup-company' : '/', { replace: true });
        } catch (e: any) {
            alert(e.message || 'Delete failed');
        }
    };

    return (
        <>
            <div>
                <div className="page-header">
                    <div>
                        <h1 className="page-title">{t('nav.enterprise')}</h1>
                        {stats && (
                            <div style={{ display: 'flex', gap: '24px', marginTop: '8px' }}>
                                <span className="badge badge-info">{t('enterprise.stats.users', { count: stats.total_users })}</span>
                                <span className="badge badge-success">{t('enterprise.stats.runningAgents', { running: stats.running_agents, total: stats.total_agents })}</span>
                                {stats.pending_approvals > 0 && <span className="badge badge-warning">{stats.pending_approvals} {t('enterprise.tabs.approvals')}</span>}
                            </div>
                        )}
                    </div>
                </div>

                {!hideTabs && (
                    <div className="tabs">
                        {([
                            { tabs: ['info', 'org', 'users', 'invites'] as const },
                            { tabs: ['llm', 'tools', 'skills', 'hr'] as const },
                            { tabs: ['quotas', 'approvals', 'audit'] as const },
                        ]).flatMap((group, gi) => [
                            ...(gi > 0 ? [<div key={`sep-${gi}`} className="tab-separator" />] : []),
                            ...group.tabs.map(tab => (
                                <div key={tab} className={`tab ${activeTab === tab ? 'active' : ''}`} onClick={() => setActiveTab(tab)}>
                                    {t(`enterprise.tabs.${tab}`)}
                                </div>
                            )),
                        ])}
                    </div>
                )}

                {/* ── LLM Model Pool ── */}
                {activeTab === 'llm' && (
                    <WorkspaceLlmSection
                        models={models}
                        providerOptions={providerOptions}
                        showAddModel={showAddModel}
                        editingModelId={editingModelId}
                        modelForm={modelForm}
                        onStartCreateModel={handleStartCreateModel}
                        onCancelModelForm={handleCancelModelForm}
                        onModelFormChange={handleModelFormChange}
                        onTestDraftModel={handleTestDraftModel}
                        onCreateModel={handleCreateModel}
                        onTestExistingModel={handleTestExistingModel}
                        onUpdateModel={handleUpdateModel}
                        onToggleModel={handleToggleModel}
                        onEditModel={handleEditModel}
                        onDeleteModel={handleDeleteModel}
                        onSetDefaultModel={async (id: string) => {
                            await enterpriseApi.setDefaultModel(id, selectedTenantId);
                            qc.invalidateQueries({ queryKey: ['llm-models'] });
                        }}
                    />
                )}

                {/* ── Org Structure ── */}
                {activeTab === 'org' && <WorkspaceOrgSection selectedTenantId={selectedTenantId} />}

                {/* ── Approvals ── */}
                {activeTab === 'approvals' && <WorkspaceApprovalsSection selectedTenantId={selectedTenantId} />}

                {/* ── Audit Logs ── */}
                {activeTab === 'audit' && <WorkspaceAuditSection selectedTenantId={selectedTenantId} />}

                {/* ── Company Management ── */}
                {activeTab === 'info' && (
                    <WorkspaceInfoSection
                        selectedTenantId={selectedTenantId}
                        companyNameEditor={<CompanyNameEditor key={`name-${selectedTenantId}`} />}
                        companyTimezoneEditor={<CompanyTimezoneEditor key={`tz-${selectedTenantId}`} />}
                        companyIntro={companyIntro}
                        onCompanyIntroChange={setCompanyIntro}
                        onSaveCompanyIntro={saveCompanyIntro}
                        companyIntroSaving={companyIntroSaving}
                        companyIntroSaved={companyIntroSaved}
                        kbBrowser={<EnterpriseKBBrowser onRefresh={() => setInfoRefresh((v: number) => v + 1)} refreshKey={infoRefresh} />}
                        themeColorPicker={<ThemeColorPicker />}
                        broadcastSection={<BroadcastSection />}
                        onDeleteCompany={handleDeleteCompany}
                    />
                )}

                {/* ── Quotas Tab ── */}
                {activeTab === 'quotas' && <WorkspaceQuotasSection />}

                {/* ── Users Tab ── */}
                {activeTab === 'users' && <WorkspaceUsersSection selectedTenantId={selectedTenantId} />}

                {/* ── Tools Tab ── */}
                {activeTab === 'tools' && <WorkspaceToolsSection selectedTenantId={selectedTenantId} />}

                {/* ── HR Agent Tab ── */}
                {activeTab === 'hr' && <WorkspaceHrAgentSection selectedTenantId={selectedTenantId} />}

                {/* ── Skills Tab ── */}
                {activeTab === 'skills' && <WorkspaceSkillsSection />}

                {/* ── Memory Config Tab ── */}
                {activeTab === 'memory' && <WorkspaceMemorySection selectedTenantId={selectedTenantId} />}

                {/* ── Invitation Codes Tab ── */}
                {activeTab === 'invites' && <WorkspaceInvitesSection />}
            </div>
        </>
    );
}
