import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { authApi } from '../api/domains/auth';
import { enterpriseApi } from '../api/domains/enterprise';
import { notificationsApi } from '../api/domains/notifications';
import { systemApi } from '../api/domains/system';
import { requestAppConfirm, showAppToast } from '../components/AppDialogs';
import { useAuthStore } from '../stores';
import { saveAccentColor, getSavedAccentColor, resetAccentColor, PRESET_COLORS } from '../utils/theme';
import WorkspaceApprovalsSection from './workspace/WorkspaceApprovalsSection';
import WorkspaceAuditSection from './workspace/WorkspaceAuditSection';
import WorkspaceDigitalEmployeesSection from './workspace/WorkspaceDigitalEmployeesSection';
import WorkspaceInfoSection from './workspace/WorkspaceInfoSection';
import LegacyCompanyFilesExportCard from './workspace/LegacyCompanyFilesExportCard';
import WorkspaceInvitesSection from './workspace/WorkspaceInvitesSection';
import WorkspaceLlmSection from './workspace/WorkspaceLlmSection';
import WorkspaceOrgSection from './workspace/WorkspaceOrgSection';
import WorkspaceQuotasSection from './workspace/WorkspaceQuotasSection';
import WorkspaceRuntimeBudgetsSection from './workspace/WorkspaceRuntimeBudgetsSection';
import WorkspaceExtensionsSection from './workspace/WorkspaceExtensionsSection';
import WorkspaceHrAgentSection from './workspace/WorkspaceHrAgentSection';
import WorkspaceGuardPolicySection from './workspace/WorkspaceGuardPolicySection';
import WorkspaceMemorySection from './workspace/WorkspaceMemorySection';
import WorkspaceUsersSection from './workspace/WorkspaceUsersSection';
import type { WorkspaceSettingsSectionTab } from '../surfaces/workspace/sections';
import './EnterpriseSettings.css';

interface LLMModel {
    id: string; provider: string; model: string; label: string;
    base_url?: string; api_key_masked?: string; max_tokens_per_day?: number; enabled: boolean; supports_vision?: boolean; max_output_tokens?: number | null; max_input_tokens?: number | null; temperature?: number | null; reasoning_mode?: string | null; reasoning_effort?: string | null; reasoning_budget_tokens?: number | null; reasoning_display?: string | null; preserve_reasoning?: boolean | null; text_verbosity?: string | null; provider_options?: Record<string, unknown> | null; created_at?: string;
}

interface LLMProviderSpec {
    provider: string;
    display_name: string;
    protocol: string;
    default_base_url?: string | null;
    supports_tool_choice: boolean;
    default_max_tokens: number;
    max_input_tokens?: number;
    reasoning_strategy?: string;
    supported_reasoning_modes?: string[];
    supported_reasoning_efforts?: string[];
    supports_reasoning_budget?: boolean;
    supports_reasoning_preservation?: boolean;
    supports_text_verbosity?: boolean;
    supports_tools_with_reasoning?: boolean;
    recommended_models?: Array<Record<string, unknown>>;
}

export type EnterpriseSettingsTab = WorkspaceSettingsSectionTab;

function enterpriseTabPath(tab: EnterpriseSettingsTab) {
    if (tab === 'invites') return 'invitations';
    if (tab === 'digital_employees') return 'digital-employees';
    if (tab === 'runtime_budgets') return 'runtime-budgets';
    if (tab === 'guard_policy') return 'action-guardrails';
    return tab;
}

interface EnterpriseSettingsProps {
    forcedTab?: EnterpriseSettingsTab;
    hideTabs?: boolean;
    chrome?: 'full' | 'embedded';
}

const FALLBACK_LLM_PROVIDERS: LLMProviderSpec[] = [
    { provider: 'anthropic', display_name: 'Anthropic', protocol: 'anthropic', default_base_url: 'https://api.anthropic.com', supports_tool_choice: false, default_max_tokens: 8192, reasoning_strategy: 'anthropic_thinking', supported_reasoning_modes: ['provider_default', 'enabled', 'adaptive'], supported_reasoning_efforts: ['low', 'medium', 'high'], supports_reasoning_budget: true, supports_reasoning_preservation: true },
    { provider: 'openai', display_name: 'OpenAI', protocol: 'openai_compatible', default_base_url: 'https://api.openai.com/v1', supports_tool_choice: true, default_max_tokens: 16384, reasoning_strategy: 'openai_chat_reasoning', supported_reasoning_modes: ['provider_default', 'enabled', 'disabled'], supported_reasoning_efforts: ['minimal', 'low', 'medium', 'high'], supports_text_verbosity: true, recommended_models: [{ model: 'gpt-5.5', label: 'GPT-5.5', supports_reasoning: true }, { model: 'gpt-5.4', label: 'GPT-5.4', supports_reasoning: true }, { model: 'gpt-5.4-mini', label: 'GPT-5.4 Mini', supports_reasoning: true }] },
    { provider: 'azure', display_name: 'Azure OpenAI', protocol: 'openai_compatible', default_base_url: '', supports_tool_choice: true, default_max_tokens: 16384 },
    { provider: 'deepseek', display_name: 'DeepSeek', protocol: 'openai_compatible', default_base_url: 'https://api.deepseek.com', supports_tool_choice: true, default_max_tokens: 8192, max_input_tokens: 1000000, reasoning_strategy: 'deepseek_thinking', supported_reasoning_modes: ['provider_default', 'enabled', 'disabled'], supported_reasoning_efforts: ['high', 'max'], supports_reasoning_preservation: true, supports_tools_with_reasoning: true, recommended_models: [{ model: 'deepseek-v4-flash', label: 'DeepSeek V4 Flash', supports_reasoning: true, supports_tools: true }, { model: 'deepseek-v4-pro', label: 'DeepSeek V4 Pro', supports_reasoning: true, supports_tools: true }] },
    { provider: 'minimax', display_name: 'MiniMax', protocol: 'openai_compatible', default_base_url: 'https://api.minimaxi.com/v1', supports_tool_choice: true, default_max_tokens: 16384, reasoning_strategy: 'minimax_reasoning_split', supported_reasoning_modes: ['provider_default'], supports_reasoning_preservation: true },
    { provider: 'qwen', display_name: 'Qwen (DashScope)', protocol: 'openai_compatible', default_base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', supports_tool_choice: true, default_max_tokens: 8192, reasoning_strategy: 'qwen_thinking', supported_reasoning_modes: ['provider_default', 'enabled', 'disabled'], supported_reasoning_efforts: ['auto'], supports_reasoning_budget: true, supports_reasoning_preservation: true },
    { provider: 'zhipu', display_name: 'Zhipu / GLM', protocol: 'openai_compatible', default_base_url: 'https://open.bigmodel.cn/api/paas/v4', supports_tool_choice: true, default_max_tokens: 8192, reasoning_strategy: 'glm_thinking', supported_reasoning_modes: ['provider_default', 'enabled', 'disabled'], supports_reasoning_preservation: true },
    { provider: 'baidu', display_name: 'Baidu (Qianfan)', protocol: 'openai_compatible', default_base_url: 'https://qianfan.baidubce.com/v2', supports_tool_choice: false, default_max_tokens: 4096 },
    { provider: 'gemini', display_name: 'Gemini', protocol: 'gemini', default_base_url: 'https://generativelanguage.googleapis.com/v1beta', supports_tool_choice: true, default_max_tokens: 8192 },
    { provider: 'openrouter', display_name: 'OpenRouter', protocol: 'openai_compatible', default_base_url: 'https://openrouter.ai/api/v1', supports_tool_choice: true, default_max_tokens: 4096 },
    { provider: 'kimi', display_name: 'Kimi (Moonshot)', protocol: 'openai_compatible', default_base_url: 'https://api.moonshot.cn/v1', supports_tool_choice: true, default_max_tokens: 8192, reasoning_strategy: 'kimi_thinking', supported_reasoning_modes: ['provider_default', 'enabled', 'disabled'], supports_reasoning_preservation: true },
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
        <div className="card enterprise-settings-theme-card">
            <h4 className="enterprise-settings-card-title">{t('enterprise.config.themeColor')}</h4>
            <div className="enterprise-settings-swatches">
                {PRESET_COLORS.map(c => (
                    <div
                        key={c.hex}
                        onClick={() => apply(c.hex)}
                        title={c.name}
                        className="enterprise-settings-swatch"
                        style={{
                            background: c.hex,
                            border: currentColor === c.hex ? '2px solid var(--text-primary)' : '2px solid transparent',
                            outline: currentColor === c.hex ? '2px solid var(--bg-primary)' : 'none',
                        }}
                    />
                ))}
            </div>
            <div className="enterprise-settings-row">
                <input
                    className="input enterprise-settings-hex-input"
                    value={customHex}
                    onChange={e => setCustomHex(e.target.value)}
                    placeholder="#hex"
                    onKeyDown={e => e.key === 'Enter' && handleCustom()}
                />
                <button className="btn btn-secondary" onClick={handleCustom}>Apply</button>
                {currentColor && (
                    <button className="btn btn-ghost enterprise-settings-btn-muted" onClick={handleReset}>Reset</button>
                )}
                {currentColor && (
                    <div className="enterprise-settings-swatch-preview" style={{ background: currentColor }} />
                )}
            </div>
        </div>
    );
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
        } catch (e: unknown) { console.error('[EnterpriseSettings] save failed:', e); }
        setSaving(false);
    };

    return (
        <div className="card enterprise-settings-form-card">
            <div className="enterprise-settings-form-row">
                <input
                    className="form-input enterprise-settings-name-input"
                    value={name}
                    onChange={e => setName(e.target.value)}
                    placeholder={t('enterprise.companyName.placeholder', 'Enter company name')}
                    onKeyDown={e => e.key === 'Enter' && handleSave()}
                />
                <button className="btn btn-primary" onClick={handleSave} disabled={saving || !name.trim()}>
                    {saving ? t('common.loading') : t('common.save', 'Save')}
                </button>
                {saved && <span className="enterprise-settings-saved">✅</span>}
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
        } catch (e: unknown) { console.error('[EnterpriseSettings] save failed:', e); }
        setSaving(false);
    };

    return (
        <div className="card enterprise-settings-form-card">
            <div className="enterprise-settings-form-row">
                <div className="enterprise-settings-grow">
                    <div className="enterprise-settings-field-label">🌐 {t('enterprise.timezone.title', 'Company Timezone')}</div>
                    <div className="u-meta u-tertiary">
                        {t('enterprise.timezone.description', 'Default timezone for all agents. Agents can override individually.')}
                    </div>
                </div>
                <select
                    className="form-input enterprise-settings-tz-select"
                    value={timezone}
                    onChange={e => handleSave(e.target.value)}
                    disabled={saving}
                >
                    {COMMON_TIMEZONES.map(tz => (
                        <option key={tz} value={tz}>{tz}</option>
                    ))}
                </select>
                {saved && <span className="enterprise-settings-saved">✅</span>}
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
            showAppToast(e.message || 'Failed', 'error');
        }
        setSending(false);
    };

    return (
        <div className="enterprise-settings-broadcast">
            <h3 className="enterprise-settings-broadcast-title">{t('enterprise.broadcast.title', 'Broadcast Notification')}</h3>
            <p className="enterprise-settings-broadcast-desc">
                {t('enterprise.broadcast.description', 'Send a notification to all users and agents in this company.')}
            </p>
            <div className="card">
                <input
                    className="form-input enterprise-settings-broadcast-input"
                    placeholder={t('enterprise.broadcast.titlePlaceholder', 'Notification title')}
                    value={title}
                    onChange={e => setTitle(e.target.value)}
                    maxLength={200}
                />
                <textarea
                    className="form-input enterprise-settings-broadcast-textarea"
                    placeholder={t('enterprise.broadcast.bodyPlaceholder', 'Optional details...')}
                    value={body}
                    onChange={e => setBody(e.target.value)}
                    maxLength={1000}
                    rows={3}
                />
                <div className="enterprise-settings-row">
                    <button className="btn btn-primary" onClick={handleSend} disabled={sending || !title.trim()}>
                        {sending ? t('common.loading') : t('enterprise.broadcast.send', 'Send Broadcast')}
                    </button>
                    {result && (
                        <span className="u-row u-secondary">
                            {t('enterprise.broadcast.sent', `Sent to ${result.users} users and ${result.agents} agents`, { users: result.users, agents: result.agents })}
                        </span>
                    )}
                </div>
            </div>
        </div>
    );
}


export default function EnterpriseSettings({ forcedTab, hideTabs = false, chrome = 'full' }: EnterpriseSettingsProps) {
    const { t } = useTranslation();
    const navigate = useNavigate();
    const canManageCompanyContent = useAuthStore((s) => s.user?.role === 'org_admin');
    const setUser = useAuthStore((s) => s.setUser);
    const qc = useQueryClient();
    // Use forcedTab directly as the source of truth — no intermediate state.
    // This ensures useQuery enabled checks react immediately to route changes.
    const activeTab: EnterpriseSettingsTab = forcedTab || 'info';

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

    const [companyIntro, setCompanyIntro] = useState('');
    const [companyIntroSaving, setCompanyIntroSaving] = useState(false);
    const [companyIntroSaved, setCompanyIntroSaved] = useState(false);
    const [legacyCompanyFilesExporting, setLegacyCompanyFilesExporting] = useState(false);

    // Company intro key: always per-tenant scoped
    const companyIntroKey = selectedTenantId ? `company_intro_${selectedTenantId}` : 'company_intro';

    // Load Company Intro (tenant-scoped only, no fallback to global)
    useEffect(() => {
        setCompanyIntro('');
        if (!selectedTenantId || !canManageCompanyContent) return;
        const tenantKey = `company_intro_${selectedTenantId}`;
        enterpriseApi.getSetting(tenantKey)
            .then(d => {
                if (d?.value?.content) {
                    setCompanyIntro(d.value.content);
                }
                // No fallback — each company starts empty with placeholder watermark
            })
            .catch(() => { });
    }, [canManageCompanyContent, selectedTenantId]);

    const saveCompanyIntro = async () => {
        if (!canManageCompanyContent) return;
        setCompanyIntroSaving(true);
        try {
            await enterpriseApi.updateSetting(companyIntroKey, { content: companyIntro });
            setCompanyIntroSaved(true);
            setTimeout(() => setCompanyIntroSaved(false), 2000);
        } catch (e: unknown) { console.error('[EnterpriseSettings] company intro save failed:', e); }
        setCompanyIntroSaving(false);
    };
    const {
        data: legacyCompanyFilesStatus,
        error: legacyCompanyFilesError,
        isLoading: legacyCompanyFilesLoading,
        refetch: retryLegacyCompanyFilesStatus,
    } = useQuery({
        queryKey: ['legacy-company-files', selectedTenantId],
        queryFn: () => enterpriseApi.getLegacyCompanyFilesStatus(selectedTenantId || undefined),
        enabled: activeTab === 'info' && Boolean(selectedTenantId) && canManageCompanyContent,
    });

    const exportLegacyCompanyFiles = async () => {
        setLegacyCompanyFilesExporting(true);
        try {
            const archive = await enterpriseApi.exportLegacyCompanyFiles(selectedTenantId || undefined);
            const url = URL.createObjectURL(archive);
            const anchor = document.createElement('a');
            anchor.href = url;
            anchor.download = `hive-legacy-company-files-${selectedTenantId}.zip`;
            document.body.appendChild(anchor);
            anchor.click();
            anchor.remove();
            URL.revokeObjectURL(url);
        } catch (error: any) {
            showAppToast(error?.message || t('enterprise.legacyCompanyFiles.exportFailed', 'Export failed'), 'error');
        } finally {
            setLegacyCompanyFilesExporting(false);
        }
    };
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
    const [modelForm, setModelForm] = useState({ provider: 'anthropic', model: '', api_key: '', base_url: '', label: '', supports_vision: false, max_output_tokens: '' as string, max_input_tokens: '' as string, temperature: '' as string, reasoning_mode: 'provider_default', reasoning_effort: '', reasoning_budget_tokens: '', reasoning_display: '', preserve_reasoning: false, text_verbosity: '', provider_options: '' });
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
                    const msg = `This model is used by ${agents.length} agent(s): ${agents.join(', ')}. Delete anyway?`;
                    if (await requestAppConfirm({
                        title: t('enterprise.llm.deleteModelTitle', 'Delete model'),
                        message: msg,
                        confirmLabel: t('common.delete', 'Delete'),
                        danger: true,
                    })) {
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

    const parseProviderOptions = (): Record<string, unknown> | null => {
        if (!modelForm.provider_options.trim()) return null;
        try {
            return JSON.parse(modelForm.provider_options);
        } catch {
            return null;
        }
    };

    const appendModelRuntimeSettings = (payload: Record<string, unknown>) => ({
        ...payload,
        temperature: modelForm.temperature !== '' ? Number(modelForm.temperature) : null,
        reasoning_mode: modelForm.reasoning_mode || 'provider_default',
        reasoning_effort: modelForm.reasoning_effort || null,
        reasoning_budget_tokens: modelForm.reasoning_budget_tokens ? Number(modelForm.reasoning_budget_tokens) : null,
        reasoning_display: modelForm.reasoning_display || null,
        preserve_reasoning: modelForm.preserve_reasoning,
        text_verbosity: modelForm.text_verbosity || null,
        provider_options: parseProviderOptions(),
    });

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
            reasoning_mode: 'provider_default',
            reasoning_effort: '',
            reasoning_budget_tokens: '',
            reasoning_display: '',
            preserve_reasoning: false,
            text_verbosity: '',
            provider_options: '',
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
            showAppToast(t('enterprise.llm.testFailed', { error: result.error || 'Unknown error', latency: result.latency_ms }), 'error');
            if (activeButton) activeButton.textContent = originalText;
        } catch (e: any) {
            showAppToast(t('enterprise.llm.testError', { message: e.message }), 'error');
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
        await runModelTest(appendModelRuntimeSettings(testData));
    };

    const buildModelPayload = () => {
        return {
            ...modelForm,
            max_output_tokens: modelForm.max_output_tokens ? Number(modelForm.max_output_tokens) : null,
            max_input_tokens: modelForm.max_input_tokens ? Number(modelForm.max_input_tokens) : null,
            temperature: modelForm.temperature !== '' ? Number(modelForm.temperature) : null,
            reasoning_mode: modelForm.reasoning_mode || 'provider_default',
            reasoning_effort: modelForm.reasoning_effort || null,
            reasoning_budget_tokens: modelForm.reasoning_budget_tokens ? Number(modelForm.reasoning_budget_tokens) : null,
            reasoning_display: modelForm.reasoning_display || null,
            preserve_reasoning: modelForm.preserve_reasoning,
            text_verbosity: modelForm.text_verbosity || null,
            provider_options: parseProviderOptions(),
        };
    };

    const handleCreateModel = () => {
        addModel.mutate(buildModelPayload());
    };

    const handleTestExistingModel = async () => {
        const testData: Record<string, unknown> = {
            provider: modelForm.provider,
            model: modelForm.model,
            base_url: modelForm.base_url || undefined,
            model_id: editingModelId || undefined,
        };
        if (modelForm.api_key) testData.api_key = modelForm.api_key;
        await runModelTest(appendModelRuntimeSettings(testData));
    };

    const handleUpdateModel = () => {
        if (!editingModelId) return;
        updateModel.mutate({
            id: editingModelId,
            data: buildModelPayload(),
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
            reasoning_mode: model.reasoning_mode || 'provider_default',
            reasoning_effort: model.reasoning_effort || '',
            reasoning_budget_tokens: model.reasoning_budget_tokens ? String(model.reasoning_budget_tokens) : '',
            reasoning_display: model.reasoning_display || '',
            preserve_reasoning: model.preserve_reasoning || false,
            text_verbosity: model.text_verbosity || '',
            provider_options: model.provider_options ? JSON.stringify(model.provider_options, null, 2) : '',
        });
        setShowAddModel(true);
    };

    const handleDeleteModel = (modelId: string) => {
        deleteModel.mutate({ id: modelId });
    };

    const handleDeleteCompany = async () => {
        const confirmed = await requestAppConfirm({
            title: t('enterprise.deleteCompany', 'Delete This Company'),
            message: t('enterprise.deleteCompanyConfirm', 'Are you sure you want to delete this company and ALL its data? This cannot be undone.'),
            confirmLabel: t('common.delete', 'Delete'),
            danger: true,
        });
        if (!confirmed) return;
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
            showAppToast(e.message || 'Delete failed', 'error');
        }
    };

    return (
        <>
            <div className={chrome === 'embedded' ? 'enterprise-settings-embedded' : undefined}>
                {chrome !== 'embedded' && (
                <div className="page-header">
                    <div>
                        <h1 className="page-title">{t('nav.enterprise')}</h1>
                        {stats && (
                            <div className="enterprise-settings-stats">
                                <span className="badge badge-info">{t('enterprise.stats.users', { count: stats.total_users })}</span>
                                <span className="badge badge-success">{t('enterprise.stats.runningAgents', { running: stats.running_agents, total: stats.total_agents })}</span>
                                {stats.pending_approvals > 0 && <span className="badge badge-warning">{stats.pending_approvals} {t('enterprise.tabs.approvals')}</span>}
                            </div>
                        )}
                    </div>
                </div>
                )}

                {chrome !== 'embedded' && !hideTabs && (
                    <div className="tabs">
                        {([
                            { tabs: ['info', 'org', 'users', 'invites'] as const },
                            { tabs: ['llm', 'extensions', 'digital_employees', 'hr'] as const },
                            { tabs: ['runtime_budgets', 'quotas', 'guard_policy', 'approvals', 'audit'] as const },
                        ]).flatMap((group, gi) => [
                            ...(gi > 0 ? [<div key={`sep-${gi}`} className="tab-separator" />] : []),
                            ...group.tabs.map(tab => (
                                <div key={tab} className={`tab ${activeTab === tab ? 'active' : ''}`} onClick={() => navigate(`/enterprise/${enterpriseTabPath(tab)}`)}>
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

                {/* ── Company Action Guardrails ── */}
                {activeTab === 'guard_policy' && <WorkspaceGuardPolicySection />}

                {/* ── Company Management ── */}
                {activeTab === 'info' && (
                    <WorkspaceInfoSection
                        selectedTenantId={selectedTenantId}
                        canManageCompanyContent={canManageCompanyContent}
                        companyNameEditor={<CompanyNameEditor key={`name-${selectedTenantId}`} />}
                        companyTimezoneEditor={<CompanyTimezoneEditor key={`tz-${selectedTenantId}`} />}
                        companyIntro={companyIntro}
                        onCompanyIntroChange={setCompanyIntro}
                        onSaveCompanyIntro={saveCompanyIntro}
                        companyIntroSaving={companyIntroSaving}
                        companyIntroSaved={companyIntroSaved}
                        legacyCompanyFilesCard={(
                            <LegacyCompanyFilesExportCard
                                status={legacyCompanyFilesStatus}
                                loading={legacyCompanyFilesLoading}
                                error={legacyCompanyFilesError}
                                exporting={legacyCompanyFilesExporting}
                                onExport={exportLegacyCompanyFiles}
                                onRetry={() => { void retryLegacyCompanyFilesStatus(); }}
                            />
                        )}
                        themeColorPicker={<ThemeColorPicker />}
                        broadcastSection={<BroadcastSection />}
                        onDeleteCompany={handleDeleteCompany}
                    />
                )}

                {/* ── Quotas Tab ── */}
                {activeTab === 'runtime_budgets' && <WorkspaceRuntimeBudgetsSection />}

                {/* ── Quotas Tab ── */}
                {activeTab === 'quotas' && <WorkspaceQuotasSection />}

                {/* ── Users Tab ── */}
                {activeTab === 'users' && <WorkspaceUsersSection selectedTenantId={selectedTenantId} />}

                {/* ── Extensions Tab ── */}
                {activeTab === 'extensions' && <WorkspaceExtensionsSection selectedTenantId={selectedTenantId} />}

                {/* ── HR Agent Tab ── */}
                {activeTab === 'hr' && <WorkspaceHrAgentSection selectedTenantId={selectedTenantId} />}

                {/* ── Digital Employee Management Tab ── */}
                {activeTab === 'digital_employees' && <WorkspaceDigitalEmployeesSection selectedTenantId={selectedTenantId} />}

                {/* ── Memory Config Tab ── */}
                {activeTab === 'memory' && <WorkspaceMemorySection selectedTenantId={selectedTenantId} />}

                {/* ── Invitation Codes Tab ── */}
                {activeTab === 'invites' && <WorkspaceInvitesSection />}
            </div>
        </>
    );
}
