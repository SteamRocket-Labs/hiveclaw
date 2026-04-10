import { useTranslation } from 'react-i18next';

import type { LLMProviderSpec } from '../../api/domains/enterprise';

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

interface WorkspaceLlmModelEditorProps {
  editingModelId: string | null;
  modelForm: WorkspaceLlmModelForm;
  providerOptions: LLMProviderSpec[];
  onCancel: () => void;
  onModelFormChange: (patch: Partial<WorkspaceLlmModelForm>) => void;
  onTestDraftModel: () => void;
  onCreateModel: () => void;
  onTestExistingModel: () => void;
  onUpdateModel: () => void;
}

export default function WorkspaceLlmModelEditor({
  editingModelId,
  modelForm,
  providerOptions,
  onCancel,
  onModelFormChange,
  onTestDraftModel,
  onCreateModel,
  onTestExistingModel,
  onUpdateModel,
}: WorkspaceLlmModelEditorProps) {
  const { t } = useTranslation();
  const isEditing = Boolean(editingModelId);

  return (
    <div className="card" style={{ marginTop: '16px', border: '1px solid var(--border-default)' }}>
      <h3 style={{ marginBottom: '16px' }}>
        {isEditing ? t('enterprise.llm.editModel', 'Edit Model') : t('enterprise.llm.addManualModel', 'Add Manual Model')}
      </h3>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
        <div className="form-group">
          <label className="form-label">{t('enterprise.llm.provider')}</label>
          <select className="form-input" value={modelForm.provider} onChange={(event) => onModelFormChange({ provider: event.target.value })}>
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
            placeholder={isEditing ? '•••••••• (Leave blank to keep unchanged)' : t('enterprise.llm.apiKeyPlaceholder')}
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
        </div>
      </div>
      <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end', marginTop: '16px' }}>
        <button className="btn btn-secondary" onClick={onCancel}>{t('common.cancel')}</button>
        <button
          className="btn btn-secondary"
          disabled={!modelForm.model || (!isEditing && !modelForm.api_key)}
          onClick={isEditing ? onTestExistingModel : onTestDraftModel}
        >
          {t('enterprise.llm.test')}
        </button>
        <button
          className="btn btn-primary"
          disabled={!modelForm.model || (!isEditing && !modelForm.api_key)}
          onClick={isEditing ? onUpdateModel : onCreateModel}
        >
          {t('common.save')}
        </button>
      </div>
    </div>
  );
}
