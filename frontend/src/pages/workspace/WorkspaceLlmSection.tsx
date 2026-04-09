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
}

export interface WorkspaceLlmProviderSpec {
  provider: string;
  display_name: string;
  protocol: string;
  default_base_url?: string | null;
  supports_tool_choice: boolean;
  default_max_tokens: number;
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

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: '16px' }}>
        <button className="btn btn-primary" onClick={onStartCreateModel}>+ {t('enterprise.llm.addModel', 'Add Model')}</button>
      </div>

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
                  const newProvider = event.target.value;
                  const spec = providerOptions.find((provider) => provider.provider === newProvider);
                  onModelFormChange({
                    provider: newProvider,
                    base_url: spec?.default_base_url || '',
                    max_output_tokens: spec ? String(spec.default_max_tokens) : modelForm.max_output_tokens,
                  });
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
                placeholder={t('enterprise.llm.modelPlaceholder', 'e.g. claude-sonnet-4-20250514')}
                value={modelForm.model}
                onChange={(event) => onModelFormChange({ model: event.target.value })}
              />
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
                value={modelForm.temperature}
                onChange={(event) => onModelFormChange({ temperature: event.target.value })}
              />
              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>{t('enterprise.llm.temperatureDesc', 'Leave empty to use the provider default. o1/o3 reasoning models usually require 1.0')}</div>
            </div>
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
                    <select className="form-input" value={modelForm.provider} onChange={(event) => onModelFormChange({ provider: event.target.value })}>
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
                    <input className="form-input" placeholder={t('enterprise.llm.modelPlaceholder', 'e.g. claude-sonnet-4-20250514')} value={modelForm.model} onChange={(event) => onModelFormChange({ model: event.target.value })} />
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
                    <input className="form-input" type="number" step="0.1" min="0" max="2" placeholder={t('enterprise.llm.temperaturePlaceholder', 'e.g. 0.7 or 1.0 (Leave empty for default)')} value={modelForm.temperature} onChange={(event) => onModelFormChange({ temperature: event.target.value })} />
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>{t('enterprise.llm.temperatureDesc', 'Leave empty to use the provider default. o1/o3 reasoning models usually require 1.0')}</div>
                  </div>
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
