import { useTranslation } from 'react-i18next';
import type { EvalRuntimeStatus, LLMModel } from '../../api/domains/enterprise';

interface Props {
  models: LLMModel[];
  runtimeStatus?: EvalRuntimeStatus | null;
  selectedModelId: string;
  saving: boolean;
  saved: boolean;
  onSelectedModelChange: (modelId: string) => void;
  onSave: () => void;
}

function formatModel(model?: Partial<LLMModel> | null) {
  if (!model) return '';
  const label = model.label || model.model || '';
  const provider = model.provider || '';
  const modelName = model.model || '';
  return provider || modelName ? `${label} (${provider}/${modelName})` : label;
}

export default function WorkspaceEvalCiSection({
  models,
  runtimeStatus,
  selectedModelId,
  saving,
  saved,
  onSelectedModelChange,
  onSave,
}: Props) {
  const { t } = useTranslation();
  const enabledModels = models.filter((model) => model.enabled);
  const currentModel = formatModel(runtimeStatus?.model);

  return (
    <div>
      <h2 style={{ fontSize: '1.25rem', fontWeight: 600, marginBottom: 4 }}>
        {t('enterprise.evalCi.title', 'Eval CI')}
      </h2>
      <p style={{ color: '#888', fontSize: '0.85rem', marginBottom: 24 }}>
        {t('enterprise.evalCi.desc', 'Configure the model used by the isolated live behavior evaluation runtime.')}
      </p>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 20, maxWidth: 600 }}>
        <div>
          <label style={{ fontWeight: 500, fontSize: '0.9rem', display: 'block', marginBottom: 6 }}>
            {t('enterprise.evalCi.status', 'Runtime Status')}
          </label>
          <p style={{ color: '#888', fontSize: '0.8rem', marginBottom: 8 }}>
            {t('enterprise.evalCi.statusDesc', 'CI runs against an isolated backend; this page only controls which company model is mirrored into that runtime.')}
          </p>
          <div
            style={{
              padding: '8px 12px',
              borderRadius: 6,
              border: '1px solid #ddd',
              fontSize: '0.9rem',
              color: runtimeStatus?.configured ? '#15803d' : '#b45309',
              background: runtimeStatus?.configured ? 'rgba(34,197,94,0.08)' : 'rgba(245,158,11,0.08)',
            }}
          >
            {runtimeStatus?.configured
              ? t('enterprise.evalCi.ready', 'Ready')
              : t('enterprise.evalCi.needsModel', 'Needs model')}
          </div>
        </div>

        <div>
          <label style={{ fontWeight: 500, fontSize: '0.9rem', display: 'block', marginBottom: 6 }}>
            {t('enterprise.evalCi.currentModel', 'Current Eval Model')}
          </label>
          <p style={{ color: '#888', fontSize: '0.8rem', marginBottom: 8 }}>
            {t('enterprise.evalCi.currentModelDesc', 'This is the model currently resolved by the live behavior evaluation runtime.')}
          </p>
          <input
            readOnly
            value={currentModel || t('enterprise.evalCi.noModel', 'No model synced yet')}
            style={{ width: '100%', padding: '8px 12px', borderRadius: 6, border: '1px solid #ddd', fontSize: '0.9rem' }}
          />
        </div>

        <div>
          <label style={{ fontWeight: 500, fontSize: '0.9rem', display: 'block', marginBottom: 6 }}>
            {t('enterprise.evalCi.model', 'Live Eval Model')}
          </label>
          <p style={{ color: '#888', fontSize: '0.8rem', marginBottom: 8 }}>
            {t('enterprise.evalCi.modelDesc', 'Choose one enabled model from the company AI model pool. The API key stays server-side.')}
          </p>
          <select
            value={selectedModelId}
            onChange={(event) => onSelectedModelChange(event.target.value)}
            style={{ width: '100%', padding: '8px 12px', borderRadius: 6, border: '1px solid #ddd', fontSize: '0.9rem' }}
          >
            <option value="">{t('enterprise.evalCi.selectModel', '-- Select a model --')}</option>
            {enabledModels.map((model) => (
              <option key={model.id} value={model.id}>
                {model.label || model.model} ({model.provider})
              </option>
            ))}
          </select>
        </div>

        <div style={{ marginTop: 8 }}>
          <button
            onClick={onSave}
            disabled={saving || !selectedModelId}
            style={{
              padding: '10px 24px',
              borderRadius: 8,
              border: 'none',
              background: saved ? '#22c55e' : '#2563eb',
              color: '#fff',
              fontWeight: 600,
              cursor: saving || !selectedModelId ? 'not-allowed' : 'pointer',
              fontSize: '0.9rem',
            }}
          >
            {saved ? t('common.saved', 'Saved') : saving ? t('common.saving', 'Saving...') : t('common.save', 'Save')}
          </button>
        </div>
      </div>
    </div>
  );
}
