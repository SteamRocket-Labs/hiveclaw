import { useTranslation } from 'react-i18next';

import './WorkspaceLlmSection.css';

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
  onSetDefaultModel?: (id: string) => void;
}

export default function WorkspaceLlmSection({
  models,
  providerOptions,
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

  const applyProviderDefaults = (newProvider: string) => {
    onModelFormChange(buildProviderDefaultPatch(providerOptions, newProvider, modelForm));
  };

  const renderReasoningControls = () => {
    if (!hasReasoningControls) return null;
    return (
      <div className="form-group ws-llm-span2">
        <div className="ws-llm-grid">
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
            <div className="form-group ws-llm-selfend">
              <label className="ws-llm-check-label">
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
          <div className="ws-llm-warn">
            {t('enterprise.llm.reasoningNoToolsWarning', 'This provider does not expose tool calling while reasoning is enabled.')}
          </div>
        ) : null}
        {isDeepSeekThinking ? (
          <div className="ws-llm-hint">
            {t('enterprise.llm.deepseekThinkingFact', 'DeepSeek thinking mode keeps tool calling available. Preserve reasoning_content for tool-call turns; temperature is ignored while thinking is enabled.')}
          </div>
        ) : null}
      </div>
    );
  };

  return (
    <div>
      <div className="ws-llm-toolbar">
        <button className="btn btn-primary" onClick={onStartCreateModel}>+ {t('enterprise.llm.addModel', 'Add Model')}</button>
      </div>

      {showAddModel && !editingModelId ? (
        <div className="card ws-llm-card">
          <h3 className="ws-llm-form-title">{t('enterprise.llm.addModel', 'Add Model')}</h3>
          <div className="ws-llm-grid">
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
            <div className="form-group ws-llm-span2">
              <label className="form-label">{t('enterprise.llm.apiKey')}</label>
              <input
                className="form-input"
                type="password"
                placeholder={t('enterprise.llm.apiKeyPlaceholder')}
                value={modelForm.api_key}
                onChange={(event) => onModelFormChange({ api_key: event.target.value })}
              />
            </div>
            <div className="form-group ws-llm-span2">
              <label className="ws-llm-check-label">
                <input
                  type="checkbox"
                  checked={modelForm.supports_vision}
                  onChange={(event) => onModelFormChange({ supports_vision: event.target.checked })}
                />
                {t('enterprise.llm.supportsVision')}
                <span className="ws-llm-hint-inline">{t('enterprise.llm.supportsVisionDesc')}</span>
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
              <div className="ws-llm-hint">{t('enterprise.llm.maxOutputTokensDesc', 'Limits generation length')}</div>
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
              <div className="ws-llm-hint">{t('enterprise.llm.maxInputTokensDesc', 'Max input tokens the model supports. Used to calculate conversation memory depth.')}</div>
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
              <div className="ws-llm-hint">
                {deepSeekThinkingActive
                  ? t('enterprise.llm.deepseekTemperatureDesc', 'DeepSeek thinking mode ignores temperature; disable thinking mode before tuning sampling.')
                  : t('enterprise.llm.temperatureDesc', 'Leave empty to use the provider default. o1/o3 reasoning models usually require 1.0')}
              </div>
            </div>
            {renderReasoningControls()}
          </div>
          <div className="ws-llm-form-actions">
            <button className="btn btn-secondary" onClick={onCancelModelForm}>{t('common.cancel')}</button>
            <button className="btn btn-secondary" disabled={!modelForm.model || !modelForm.api_key} onClick={onTestDraftModel}>{t('enterprise.llm.test')}</button>
            <button className="btn btn-primary" onClick={onCreateModel} disabled={!modelForm.model || !modelForm.api_key}>{t('common.save')}</button>
          </div>
        </div>
      ) : null}

      <div className="ws-llm-list">
        {models.map((model) => (
          <div key={model.id}>
            {editingModelId === model.id ? (
              <div className="card ws-llm-card-editing">
                <h3 className="ws-llm-form-title">Edit Model</h3>
                <div className="ws-llm-grid">
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
                  <div className="form-group ws-llm-span2">
                    <label className="form-label">{t('enterprise.llm.apiKey')}</label>
                    <input className="form-input" type="password" placeholder="•••••••• (Leave blank to keep unchanged)" value={modelForm.api_key} onChange={(event) => onModelFormChange({ api_key: event.target.value })} />
                  </div>
                  <div className="form-group ws-llm-span2">
                    <label className="ws-llm-check-label">
                      <input type="checkbox" checked={modelForm.supports_vision} onChange={(event) => onModelFormChange({ supports_vision: event.target.checked })} />
                      {t('enterprise.llm.supportsVision')}
                      <span className="ws-llm-hint-inline">{t('enterprise.llm.supportsVisionDesc')}</span>
                    </label>
                  </div>
                  <div className="form-group">
                    <label className="form-label">{t('enterprise.llm.maxOutputTokens', 'Max Output Tokens')}</label>
                    <input className="form-input" type="number" placeholder={t('enterprise.llm.maxOutputTokensPlaceholder', 'e.g. 4096')} value={modelForm.max_output_tokens} onChange={(event) => onModelFormChange({ max_output_tokens: event.target.value })} />
                    <div className="ws-llm-hint">{t('enterprise.llm.maxOutputTokensDesc', 'Limits generation length')}</div>
                  </div>
                  <div className="form-group">
                    <label className="form-label">{t('enterprise.llm.maxInputTokens', 'Context Window')}</label>
                    <input className="form-input" type="number" placeholder={t('enterprise.llm.maxInputTokensPlaceholder', 'Default 256000 if empty')} value={modelForm.max_input_tokens} onChange={(event) => onModelFormChange({ max_input_tokens: event.target.value })} />
                    <div className="ws-llm-hint">{t('enterprise.llm.maxInputTokensDesc', 'Max input tokens. Used to calculate conversation memory depth.')}</div>
                  </div>
                  <div className="form-group">
                    <label className="form-label">{t('enterprise.llm.temperature', 'Temperature')}</label>
                    <input className="form-input" type="number" step="0.1" min="0" max="2" placeholder={t('enterprise.llm.temperaturePlaceholder', 'e.g. 0.7 or 1.0 (Leave empty for default)')} disabled={deepSeekThinkingActive} value={deepSeekThinkingActive ? '' : modelForm.temperature} onChange={(event) => onModelFormChange({ temperature: event.target.value })} />
                    <div className="ws-llm-hint">
                      {deepSeekThinkingActive
                        ? t('enterprise.llm.deepseekTemperatureDesc', 'DeepSeek thinking mode ignores temperature; disable thinking mode before tuning sampling.')
                        : t('enterprise.llm.temperatureDesc', 'Leave empty to use the provider default. o1/o3 reasoning models usually require 1.0')}
                    </div>
                  </div>
                  {renderReasoningControls()}
                </div>
                <div className="ws-llm-form-actions">
                  <button className="btn btn-secondary" onClick={onCancelModelForm}>{t('common.cancel')}</button>
                  <button className="btn btn-secondary" disabled={!modelForm.model} onClick={onTestExistingModel}>{t('enterprise.llm.test')}</button>
                  <button className="btn btn-primary" onClick={onUpdateModel} disabled={!modelForm.model}>{t('common.save')}</button>
                </div>
              </div>
            ) : (
              <div className="card ws-llm-row">
                <div>
                  <div className="ws-llm-model-name">{model.label}</div>
                  <div className="ws-llm-model-meta">
                    {model.provider}/{model.model}
                    {model.base_url ? <span> · {model.base_url}</span> : null}
                  </div>
                </div>
                <div className="ws-llm-row-actions">
                  <button
                    onClick={() => onToggleModel(model.id, !model.enabled)}
                    title={model.enabled ? t('enterprise.llm.clickToDisable', 'Click to disable') : t('enterprise.llm.clickToEnable', 'Click to enable')}
                    className={`ws-llm-toggle ${model.enabled ? 'is-on' : ''}`}
                  >
                    <span className="ws-llm-toggle-knob" />
                  </button>
                  {model.supports_vision ? <span className="badge badge-info">Vision</span> : null}
                  {model.is_default ? (
                    <span className="badge badge-success">{t('enterprise.llm.default', 'Default')}</span>
                  ) : onSetDefaultModel ? (
                    <button className="btn btn-ghost ws-llm-set-default" onClick={() => onSetDefaultModel(model.id)}>{t('enterprise.llm.setDefault', 'Set as Default')}</button>
                  ) : null}
                  <button className="btn btn-ghost" onClick={() => onEditModel(model)}>✏️ {t('enterprise.tools.edit')}</button>
                  <button className="btn btn-ghost ws-llm-del" onClick={() => onDeleteModel(model.id)}>{t('common.delete')}</button>
                </div>
              </div>
            )}
          </div>
        ))}
        {models.length === 0 ? <div className="ws-llm-empty">{t('common.noData')}</div> : null}
      </div>
    </div>
  );
}
