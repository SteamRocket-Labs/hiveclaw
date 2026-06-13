import { useTranslation } from 'react-i18next';

export interface WorkspaceLlmModel {
  id: string;
  provider: string;
  model: string;
  label: string;
  enabled: boolean;
  supports_vision?: boolean;
  is_default?: boolean;
  base_url?: string;
  api_key_masked?: string;
  max_output_tokens?: number | null;
  max_input_tokens?: number | null;
  temperature?: number | null;
  reasoning_mode?: string | null;
  reasoning_effort?: string | null;
  reasoning_budget_tokens?: number | null;
  reasoning_display?: string | null;
  preserve_reasoning?: boolean | null;
  text_verbosity?: string | null;
  provider_options?: Record<string, unknown> | null;
}

export interface WorkspaceLlmProviderSpec {
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

export interface WorkspaceLlmModelForm {
  provider: string;
  model: string;
  api_key: string;
  base_url: string;
  label: string;
  supports_vision: boolean;
  max_output_tokens: string;
  max_input_tokens: string;
  temperature: string;
  reasoning_mode: string;
  reasoning_effort: string;
  reasoning_budget_tokens: string;
  reasoning_display: string;
  preserve_reasoning: boolean;
  text_verbosity: string;
  provider_options: string;
}

export interface WorkspaceEvalRuntimeConfig {
  agent_id: string;
  user_id: string;
}

export interface WorkspaceEvalRuntimeAgent {
  id: string;
  name: string;
  primary_model_id?: string | null;
  fallback_model_id?: string | null;
  status?: string;
}

export interface WorkspaceEvalRuntimeUser {
  id: string;
  display_name?: string | null;
  username?: string | null;
  email?: string | null;
}

type ProviderDefaultPatch = Partial<WorkspaceLlmModelForm>;

export function buildProviderDefaultPatch(
  providerOptions: WorkspaceLlmProviderSpec[],
  newProvider: string,
  current: Pick<WorkspaceLlmModelForm, 'max_output_tokens' | 'max_input_tokens'>,
): ProviderDefaultPatch {
  const spec = providerOptions.find((provider) => provider.provider === newProvider);
  const firstRecommendation = spec?.recommended_models?.[0];
  const recommendedModel = firstRecommendation ? String(firstRecommendation.model || '') : '';
  const recommendedLabel = firstRecommendation ? String(firstRecommendation.label || recommendedModel) : '';

  return {
    provider: newProvider,
    model: recommendedModel,
    label: recommendedLabel,
    base_url: spec?.default_base_url || '',
    supports_vision: Boolean(firstRecommendation?.supports_vision),
    max_output_tokens: spec ? String(spec.default_max_tokens) : current.max_output_tokens,
    max_input_tokens: spec?.max_input_tokens ? String(spec.max_input_tokens) : current.max_input_tokens,
    temperature: '',
    reasoning_mode: 'provider_default',
    reasoning_effort: '',
    reasoning_budget_tokens: '',
    reasoning_display: '',
    preserve_reasoning: false,
    text_verbosity: '',
    provider_options: '',
  };
}

interface WorkspaceLlmSectionProps {
  models: WorkspaceLlmModel[];
  providerOptions: WorkspaceLlmProviderSpec[];
  selectedTenantId?: string;
  evalRuntimeConfig?: WorkspaceEvalRuntimeConfig;
  evalAgents?: WorkspaceEvalRuntimeAgent[];
  evalUsers?: WorkspaceEvalRuntimeUser[];
  evalRuntimeSaving?: boolean;
  showAddModel: boolean;
  editingModelId: string | null;
  modelForm: WorkspaceLlmModelForm;
  onStartCreateModel: () => void;
  onCancelModelForm: () => void;
  onModelFormChange: (patch: Partial<WorkspaceLlmModelForm>) => void;
  onTestDraftModel: () => void;
  onCreateModel: () => void;
  onTestExistingModel: () => void;
  onUpdateModel: () => void;
  onToggleModel: (id: string, enabled: boolean) => void;
  onEditModel: (model: WorkspaceLlmModel) => void;
  onDeleteModel: (id: string) => void;
  onEvalRuntimeConfigChange?: (patch: Partial<WorkspaceEvalRuntimeConfig>) => void;
  onSaveEvalRuntimeConfig?: () => void;
  onSetDefaultModel?: (id: string) => void;
}

export default function WorkspaceLlmSection({
  models,
  providerOptions,
  selectedTenantId = '',
  evalRuntimeConfig,
  evalAgents = [],
  evalUsers = [],
  evalRuntimeSaving = false,
  showAddModel,
  editingModelId,
  modelForm,
  onStartCreateModel,
  onCancelModelForm,
  onModelFormChange,
  onTestDraftModel,
  onCreateModel,
  onTestExistingModel,
  onUpdateModel,
  onToggleModel,
  onEditModel,
  onDeleteModel,
  onEvalRuntimeConfigChange,
  onSaveEvalRuntimeConfig,
  onSetDefaultModel,
}: WorkspaceLlmSectionProps) {
  const { t } = useTranslation();
  const selectedProvider = providerOptions.find((provider) => provider.provider === modelForm.provider);
  const recommendedModels = selectedProvider?.recommended_models || [];
  const reasoningModes = selectedProvider?.supported_reasoning_modes || ['provider_default'];
  const reasoningEfforts = selectedProvider?.supported_reasoning_efforts || [];
  const hasReasoningControls = Boolean(selectedProvider?.reasoning_strategy && selectedProvider.reasoning_strategy !== 'none');
  const isDeepSeekThinking = selectedProvider?.reasoning_strategy === 'deepseek_thinking';
  const deepSeekThinkingActive = isDeepSeekThinking && modelForm.reasoning_mode !== 'disabled';
  const showReasoningEffort = reasoningEfforts.length > 0 && modelForm.reasoning_mode !== 'disabled' && (modelForm.reasoning_mode !== 'provider_default' || isDeepSeekThinking);
  const showReasoningBudget = Boolean(selectedProvider?.supports_reasoning_budget) && modelForm.reasoning_mode !== 'provider_default';
  const showPreserveReasoning = Boolean(selectedProvider?.supports_reasoning_preservation);
  const showTextVerbosity = Boolean(selectedProvider?.supports_text_verbosity);
  const selectedEvalAgent = evalAgents.find((agent) => agent.id === evalRuntimeConfig?.agent_id);
  const selectedEvalModel = selectedEvalAgent
    ? models.find((model) => model.id === selectedEvalAgent.primary_model_id)
      || models.find((model) => model.id === selectedEvalAgent.fallback_model_id)
    : undefined;
  const selectedEvalUser = evalUsers.find((user) => user.id === evalRuntimeConfig?.user_id);
  const canSaveEvalRuntime = Boolean(evalRuntimeConfig?.agent_id && evalRuntimeConfig.user_id && onSaveEvalRuntimeConfig);

  const formatEvalUser = (user: WorkspaceEvalRuntimeUser) => {
    const name = user.display_name || user.username || user.email || user.id;
    return user.email && user.email !== name ? `${name} (${user.email})` : name;
  };

  const applyProviderDefaults = (newProvider: string) => {
    onModelFormChange(buildProviderDefaultPatch(providerOptions, newProvider, modelForm));
  };

  const renderReasoningControls = () => {
    if (!hasReasoningControls) return null;
    return (
      <div className="form-group" style={{ gridColumn: 'span 2' }}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
          <div className="form-group">
            <label className="form-label">{t('enterprise.llm.reasoningMode', 'Reasoning Mode')}</label>
            <select
              className="form-input"
              value={modelForm.reasoning_mode}
              onChange={(event) => {
                const nextMode = event.target.value;
                onModelFormChange({
                  reasoning_mode: nextMode,
                  temperature: isDeepSeekThinking && nextMode !== 'disabled' ? '' : modelForm.temperature,
                });
              }}
            >
              {reasoningModes.map((mode) => (
                <option key={mode} value={mode}>{t(`enterprise.llm.reasoningModes.${mode}`, mode)}</option>
              ))}
            </select>
          </div>
          {showReasoningEffort ? (
            <div className="form-group">
              <label className="form-label">{t('enterprise.llm.reasoningEffort', 'Reasoning Effort')}</label>
              <select
                className="form-input"
                value={modelForm.reasoning_effort}
                onChange={(event) => onModelFormChange({ reasoning_effort: event.target.value })}
              >
                <option value="">{t('enterprise.llm.providerDefault', 'Provider default')}</option>
                {reasoningEfforts.map((effort) => (
                  <option key={effort} value={effort}>{t(`enterprise.llm.reasoningEfforts.${effort}`, effort)}</option>
                ))}
              </select>
            </div>
          ) : null}
          {showReasoningBudget ? (
            <div className="form-group">
              <label className="form-label">{t('enterprise.llm.reasoningBudgetTokens', 'Thinking Budget Tokens')}</label>
              <input
                className="form-input"
                type="number"
                min="1"
                placeholder={t('enterprise.llm.reasoningBudgetPlaceholder', 'Provider default')}
                value={modelForm.reasoning_budget_tokens}
                onChange={(event) => onModelFormChange({ reasoning_budget_tokens: event.target.value })}
              />
            </div>
          ) : null}
          {showTextVerbosity ? (
            <div className="form-group">
              <label className="form-label">{t('enterprise.llm.textVerbosity', 'Text Verbosity')}</label>
              <select
                className="form-input"
                value={modelForm.text_verbosity}
                onChange={(event) => onModelFormChange({ text_verbosity: event.target.value })}
              >
                <option value="">{t('enterprise.llm.providerDefault', 'Provider default')}</option>
                {['low', 'medium', 'high'].map((level) => (
                  <option key={level} value={level}>{t(`enterprise.llm.reasoningEfforts.${level}`, level)}</option>
                ))}
              </select>
            </div>
          ) : null}
          {showPreserveReasoning ? (
            <div className="form-group" style={{ alignSelf: 'end' }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', fontSize: '13px' }}>
                <input
                  type="checkbox"
                  checked={modelForm.preserve_reasoning}
                  onChange={(event) => onModelFormChange({ preserve_reasoning: event.target.checked })}
                />
                {t('enterprise.llm.preserveReasoning', 'Preserve reasoning for multi-turn tool use')}
              </label>
            </div>
          ) : null}
        </div>
        {selectedProvider?.supports_tools_with_reasoning === false ? (
          <div style={{ fontSize: '11px', color: 'var(--warning, #f59e0b)', marginTop: '4px' }}>
            {t('enterprise.llm.reasoningNoToolsWarning', 'This provider does not expose tool calling while reasoning is enabled.')}
          </div>
        ) : null}
        {isDeepSeekThinking ? (
          <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
            {t('enterprise.llm.deepseekThinkingFact', 'DeepSeek thinking mode keeps tool calling available. Preserve reasoning_content for tool-call turns; temperature is ignored while thinking is enabled.')}
          </div>
        ) : null}
      </div>
    );
  };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: '16px' }}>
        <button className="btn btn-primary" onClick={onStartCreateModel}>+ {t('enterprise.llm.addModel', 'Add Model')}</button>
      </div>

      {onEvalRuntimeConfigChange ? (
        <div className="card" style={{ marginBottom: '16px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: '16px', alignItems: 'flex-start', marginBottom: '14px' }}>
            <div>
              <h3 style={{ margin: 0, marginBottom: '4px' }}>{t('enterprise.llm.evalRuntime.title', 'Live Behavior Eval Runtime')}</h3>
              <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                {t('enterprise.llm.evalRuntime.summary', 'Nightly eval uses this tenant agent and its model settings.')}
              </div>
            </div>
            <button
              className="btn btn-primary"
              disabled={!canSaveEvalRuntime || evalRuntimeSaving}
              onClick={onSaveEvalRuntimeConfig}
              style={{ fontSize: '12px', flexShrink: 0 }}
            >
              {evalRuntimeSaving ? t('common.saving', 'Saving...') : t('common.save', 'Save')}
            </button>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
            <div className="form-group" style={{ gridColumn: 'span 2' }}>
              <label className="form-label">{t('enterprise.llm.evalRuntime.railwayTenantEnv', 'Railway tenant env')}</label>
              <input
                className="form-input"
                readOnly
                value={selectedTenantId ? `HIVE_EVAL_TENANT_ID=${selectedTenantId}` : 'HIVE_EVAL_TENANT_ID='}
              />
            </div>
            <div className="form-group">
              <label className="form-label">{t('enterprise.llm.evalRuntime.agent', 'Eval Agent')}</label>
              <select
                className="form-input"
                value={evalRuntimeConfig?.agent_id || ''}
                onChange={(event) => onEvalRuntimeConfigChange({ agent_id: event.target.value })}
              >
                <option value="">--</option>
                {evalAgents.map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.name}{agent.status ? ` (${agent.status})` : ''}
                  </option>
                ))}
              </select>
            </div>
            <div className="form-group">
              <label className="form-label">{t('enterprise.llm.evalRuntime.user', 'Eval User')}</label>
              <select
                className="form-input"
                value={evalRuntimeConfig?.user_id || ''}
                onChange={(event) => onEvalRuntimeConfigChange({ user_id: event.target.value })}
              >
                <option value="">--</option>
                {evalUsers.map((user) => (
                  <option key={user.id} value={user.id}>{formatEvalUser(user)}</option>
                ))}
              </select>
            </div>
            <div className="form-group" style={{ gridColumn: 'span 2' }}>
              <label className="form-label">{t('enterprise.llm.evalRuntime.model', 'Resolved Agent Model')}</label>
              <input
                className="form-input"
                readOnly
                value={
                  selectedEvalModel
                    ? `${selectedEvalModel.label} (${selectedEvalModel.provider}/${selectedEvalModel.model})`
                    : t('enterprise.llm.evalRuntime.noModel', 'No enabled model is bound to this eval agent')
                }
              />
              {selectedEvalAgent && !selectedEvalModel ? (
                <div style={{ fontSize: '11px', color: 'var(--error)', marginTop: '4px' }}>
                  {t('enterprise.llm.evalRuntime.noModelWarning', 'Select a primary or fallback model in this agent settings before enabling live eval.')}
                </div>
              ) : null}
              {selectedEvalModel && !selectedEvalModel.enabled ? (
                <div style={{ fontSize: '11px', color: 'var(--error)', marginTop: '4px' }}>
                  {t('enterprise.llm.evalRuntime.disabledModelWarning', 'The bound model is disabled. Live eval will fail closed.')}
                </div>
              ) : null}
            </div>
          </div>
          {selectedEvalUser ? (
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '8px' }}>
              {formatEvalUser(selectedEvalUser)}
            </div>
          ) : null}
        </div>
      ) : null}

      {showAddModel && !editingModelId ? (
        <div className="card" style={{ marginBottom: '16px' }}>
          <h3 style={{ marginBottom: '16px' }}>{t('enterprise.llm.addModel', 'Add Model')}</h3>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
            <div className="form-group">
              <label className="form-label">{t('enterprise.llm.provider')}</label>
              <select
                className="form-input"
                value={modelForm.provider}
                onChange={(event) => {
                  applyProviderDefaults(event.target.value);
                }}
              >
                {providerOptions.map((provider) => (
                  <option key={provider.provider} value={provider.provider}>{provider.display_name}</option>
                ))}
              </select>
            </div>
            <div className="form-group">
              <label className="form-label">{t('enterprise.llm.model')}</label>
              <input
                className="form-input"
                list="llm-model-recommendations"
                placeholder={t('enterprise.llm.modelPlaceholder', 'e.g. claude-sonnet-4-20250514')}
                value={modelForm.model}
                onChange={(event) => onModelFormChange({ model: event.target.value })}
              />
              {recommendedModels.length > 0 ? (
                <datalist id="llm-model-recommendations">
                  {recommendedModels.map((item) => {
                    const model = String(item.model || '');
                    const label = String(item.label || model);
                    return model ? <option key={model} value={model}>{label}</option> : null;
                  })}
                </datalist>
              ) : null}
            </div>
            <div className="form-group">
              <label className="form-label">{t('enterprise.llm.label')}</label>
              <input
                className="form-input"
                placeholder={t('enterprise.llm.labelPlaceholder')}
                value={modelForm.label}
                onChange={(event) => onModelFormChange({ label: event.target.value })}
              />
            </div>
            <div className="form-group">
              <label className="form-label">{t('enterprise.llm.baseUrl')}</label>
              <input
                className="form-input"
                placeholder={t('enterprise.llm.baseUrlPlaceholder')}
                value={modelForm.base_url}
                onChange={(event) => onModelFormChange({ base_url: event.target.value })}
              />
            </div>
            <div className="form-group" style={{ gridColumn: 'span 2' }}>
              <label className="form-label">{t('enterprise.llm.apiKey')}</label>
              <input
                className="form-input"
                type="password"
                placeholder={t('enterprise.llm.apiKeyPlaceholder')}
                value={modelForm.api_key}
                onChange={(event) => onModelFormChange({ api_key: event.target.value })}
              />
            </div>
            <div className="form-group" style={{ gridColumn: 'span 2' }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', fontSize: '13px' }}>
                <input
                  type="checkbox"
                  checked={modelForm.supports_vision}
                  onChange={(event) => onModelFormChange({ supports_vision: event.target.checked })}
                />
                {t('enterprise.llm.supportsVision')}
                <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', fontWeight: 400 }}>{t('enterprise.llm.supportsVisionDesc')}</span>
              </label>
            </div>
            <div className="form-group">
              <label className="form-label">{t('enterprise.llm.maxOutputTokens', 'Max Output Tokens')}</label>
              <input
                className="form-input"
                type="number"
                placeholder={t('enterprise.llm.maxOutputTokensPlaceholder', 'e.g. 4096')}
                value={modelForm.max_output_tokens}
                onChange={(event) => onModelFormChange({ max_output_tokens: event.target.value })}
              />
              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>{t('enterprise.llm.maxOutputTokensDesc', 'Limits generation length')}</div>
            </div>
            <div className="form-group">
              <label className="form-label">{t('enterprise.llm.maxInputTokens', 'Context Window')}</label>
              <input
                className="form-input"
                type="number"
                placeholder={t('enterprise.llm.maxInputTokensPlaceholder', 'Default 256000 if empty')}
                value={modelForm.max_input_tokens}
                onChange={(event) => onModelFormChange({ max_input_tokens: event.target.value })}
              />
              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>{t('enterprise.llm.maxInputTokensDesc', 'Max input tokens the model supports. Used to calculate conversation memory depth.')}</div>
            </div>
            <div className="form-group">
              <label className="form-label">{t('enterprise.llm.temperature', 'Temperature')}</label>
              <input
                className="form-input"
                type="number"
                step="0.1"
                min="0"
                max="2"
                placeholder={t('enterprise.llm.temperaturePlaceholder', 'e.g. 0.7 or 1.0 (Leave empty for default)')}
                disabled={deepSeekThinkingActive}
                value={deepSeekThinkingActive ? '' : modelForm.temperature}
                onChange={(event) => onModelFormChange({ temperature: event.target.value })}
              />
              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                {deepSeekThinkingActive
                  ? t('enterprise.llm.deepseekTemperatureDesc', 'DeepSeek thinking mode ignores temperature; disable thinking mode before tuning sampling.')
                  : t('enterprise.llm.temperatureDesc', 'Leave empty to use the provider default. o1/o3 reasoning models usually require 1.0')}
              </div>
            </div>
            {renderReasoningControls()}
          </div>
          <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end', alignItems: 'center' }}>
            <button className="btn btn-secondary" onClick={onCancelModelForm}>{t('common.cancel')}</button>
            <button className="btn btn-secondary" style={{ display: 'flex', alignItems: 'center', gap: '6px' }} disabled={!modelForm.model || !modelForm.api_key} onClick={onTestDraftModel}>{t('enterprise.llm.test')}</button>
            <button className="btn btn-primary" onClick={onCreateModel} disabled={!modelForm.model || !modelForm.api_key}>{t('common.save')}</button>
          </div>
        </div>
      ) : null}

      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        {models.map((model) => (
          <div key={model.id}>
            {editingModelId === model.id ? (
              <div className="card" style={{ border: '1px solid var(--accent-primary)' }}>
                <h3 style={{ marginBottom: '16px' }}>Edit Model</h3>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                  <div className="form-group">
                    <label className="form-label">{t('enterprise.llm.provider')}</label>
                    <select className="form-input" value={modelForm.provider} onChange={(event) => applyProviderDefaults(event.target.value)}>
                      {providerOptions.map((provider) => (
                        <option key={provider.provider} value={provider.provider}>{provider.display_name}</option>
                      ))}
                      {!providerOptions.some((provider) => provider.provider === modelForm.provider) ? (
                        <option value={modelForm.provider}>{modelForm.provider}</option>
                      ) : null}
                    </select>
                  </div>
                  <div className="form-group">
                    <label className="form-label">{t('enterprise.llm.model')}</label>
                    <input className="form-input" list="llm-model-recommendations" placeholder={t('enterprise.llm.modelPlaceholder', 'e.g. claude-sonnet-4-20250514')} value={modelForm.model} onChange={(event) => onModelFormChange({ model: event.target.value })} />
                    {recommendedModels.length > 0 ? (
                      <datalist id="llm-model-recommendations">
                        {recommendedModels.map((item) => {
                          const model = String(item.model || '');
                          const label = String(item.label || model);
                          return model ? <option key={model} value={model}>{label}</option> : null;
                        })}
                      </datalist>
                    ) : null}
                  </div>
                  <div className="form-group">
                    <label className="form-label">{t('enterprise.llm.label')}</label>
                    <input className="form-input" placeholder={t('enterprise.llm.labelPlaceholder')} value={modelForm.label} onChange={(event) => onModelFormChange({ label: event.target.value })} />
                  </div>
                  <div className="form-group">
                    <label className="form-label">{t('enterprise.llm.baseUrl')}</label>
                    <input className="form-input" placeholder={t('enterprise.llm.baseUrlPlaceholder')} value={modelForm.base_url} onChange={(event) => onModelFormChange({ base_url: event.target.value })} />
                  </div>
                  <div className="form-group" style={{ gridColumn: 'span 2' }}>
                    <label className="form-label">{t('enterprise.llm.apiKey')}</label>
                    <input className="form-input" type="password" placeholder="•••••••• (Leave blank to keep unchanged)" value={modelForm.api_key} onChange={(event) => onModelFormChange({ api_key: event.target.value })} />
                  </div>
                  <div className="form-group" style={{ gridColumn: 'span 2' }}>
                    <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', fontSize: '13px' }}>
                      <input type="checkbox" checked={modelForm.supports_vision} onChange={(event) => onModelFormChange({ supports_vision: event.target.checked })} />
                      {t('enterprise.llm.supportsVision')}
                      <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', fontWeight: 400 }}>{t('enterprise.llm.supportsVisionDesc')}</span>
                    </label>
                  </div>
                  <div className="form-group">
                    <label className="form-label">{t('enterprise.llm.maxOutputTokens', 'Max Output Tokens')}</label>
                    <input className="form-input" type="number" placeholder={t('enterprise.llm.maxOutputTokensPlaceholder', 'e.g. 4096')} value={modelForm.max_output_tokens} onChange={(event) => onModelFormChange({ max_output_tokens: event.target.value })} />
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>{t('enterprise.llm.maxOutputTokensDesc', 'Limits generation length')}</div>
                  </div>
                  <div className="form-group">
                    <label className="form-label">{t('enterprise.llm.maxInputTokens', 'Context Window')}</label>
                    <input className="form-input" type="number" placeholder={t('enterprise.llm.maxInputTokensPlaceholder', 'Default 256000 if empty')} value={modelForm.max_input_tokens} onChange={(event) => onModelFormChange({ max_input_tokens: event.target.value })} />
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>{t('enterprise.llm.maxInputTokensDesc', 'Max input tokens. Used to calculate conversation memory depth.')}</div>
                  </div>
                  <div className="form-group">
                    <label className="form-label">{t('enterprise.llm.temperature', 'Temperature')}</label>
                    <input className="form-input" type="number" step="0.1" min="0" max="2" placeholder={t('enterprise.llm.temperaturePlaceholder', 'e.g. 0.7 or 1.0 (Leave empty for default)')} disabled={deepSeekThinkingActive} value={deepSeekThinkingActive ? '' : modelForm.temperature} onChange={(event) => onModelFormChange({ temperature: event.target.value })} />
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                      {deepSeekThinkingActive
                        ? t('enterprise.llm.deepseekTemperatureDesc', 'DeepSeek thinking mode ignores temperature; disable thinking mode before tuning sampling.')
                        : t('enterprise.llm.temperatureDesc', 'Leave empty to use the provider default. o1/o3 reasoning models usually require 1.0')}
                    </div>
                  </div>
                  {renderReasoningControls()}
                </div>
                <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end', alignItems: 'center' }}>
                  <button className="btn btn-secondary" onClick={onCancelModelForm}>{t('common.cancel')}</button>
                  <button className="btn btn-secondary" style={{ display: 'flex', alignItems: 'center', gap: '6px' }} disabled={!modelForm.model} onClick={onTestExistingModel}>{t('enterprise.llm.test')}</button>
                  <button className="btn btn-primary" onClick={onUpdateModel} disabled={!modelForm.model}>{t('common.save')}</button>
                </div>
              </div>
            ) : (
              <div className="card" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <div>
                  <div style={{ fontWeight: 500 }}>{model.label}</div>
                  <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                    {model.provider}/{model.model}
                    {model.base_url ? <span> · {model.base_url}</span> : null}
                  </div>
                </div>
                <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                  <button
                    onClick={() => onToggleModel(model.id, !model.enabled)}
                    title={model.enabled ? t('enterprise.llm.clickToDisable', 'Click to disable') : t('enterprise.llm.clickToEnable', 'Click to enable')}
                    style={{
                      position: 'relative',
                      width: '36px',
                      height: '20px',
                      borderRadius: '10px',
                      border: 'none',
                      cursor: 'pointer',
                      transition: 'background 0.2s',
                      background: model.enabled ? 'var(--success, #00b478)' : 'var(--bg-tertiary, #444)',
                      padding: 0,
                      flexShrink: 0,
                    }}
                  >
                    <span
                      style={{
                        position: 'absolute',
                        left: model.enabled ? '18px' : '2px',
                        top: '2px',
                        width: '16px',
                        height: '16px',
                        borderRadius: '50%',
                        background: '#fff',
                        transition: 'left 0.2s',
                        boxShadow: '0 1px 3px rgba(0,0,0,0.2)',
                      }}
                    />
                  </button>
                  {model.supports_vision ? <span className="badge" style={{ background: 'rgba(99,102,241,0.15)', color: 'rgb(99,102,241)', fontSize: '10px' }}>Vision</span> : null}
                  {model.is_default ? (
                    <span className="badge" style={{ background: 'rgba(34,197,94,0.15)', color: 'rgb(34,197,94)', fontSize: '10px', fontWeight: 600 }}>{t('enterprise.llm.default', 'Default')}</span>
                  ) : onSetDefaultModel ? (
                    <button className="btn btn-ghost" onClick={() => onSetDefaultModel(model.id)} style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{t('enterprise.llm.setDefault', 'Set as Default')}</button>
                  ) : null}
                  <button className="btn btn-ghost" onClick={() => onEditModel(model)} style={{ fontSize: '12px' }}>✏️ {t('enterprise.tools.edit')}</button>
                  <button className="btn btn-ghost" onClick={() => onDeleteModel(model.id)} style={{ color: 'var(--error)' }}>{t('common.delete')}</button>
                </div>
              </div>
            )}
          </div>
        ))}
        {models.length === 0 ? <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>{t('common.noData')}</div> : null}
      </div>
    </div>
  );
}
