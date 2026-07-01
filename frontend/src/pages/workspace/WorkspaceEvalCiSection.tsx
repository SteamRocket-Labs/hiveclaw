import { useTranslation } from 'react-i18next';
import type { EvalBehaviorReport, EvalRuntimeStatus, LLMModel } from '../../api/domains/enterprise';

interface Props {
  models: LLMModel[];
  runtimeStatus?: EvalRuntimeStatus | null;
  selectedModelId: string;
  saving: boolean;
  saved: boolean;
  latestReport?: EvalBehaviorReport | null;
  latestReportLoading: boolean;
  runningBehaviorEval: boolean;
  onRunBehaviorEval: () => void;
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
  latestReport,
  latestReportLoading,
  runningBehaviorEval,
  onRunBehaviorEval,
  onSelectedModelChange,
  onSave,
}: Props) {
  const { t } = useTranslation();
  const enabledModels = models.filter((model) => model.enabled);
  const currentModel = formatModel(runtimeStatus?.model);
  const summary = latestReport?.summary;
  const scenarioEntries = Object.entries(summary?.scenarios || {});
  const runtimeModel =
    typeof summary?.runtime?.model === 'string'
      ? summary.runtime.model
      : typeof summary?.runtime?.model_name === 'string'
        ? summary.runtime.model_name
        : '';

  return (
    <div style={{ maxWidth: 920 }}>
      <h2 style={{ fontSize: '1.25rem', fontWeight: 600, marginBottom: 4 }}>
        {t('enterprise.evalCi.title', 'Behavior Evaluation')}
      </h2>
      <p style={{ color: '#888', fontSize: '0.85rem', marginBottom: 24 }}>
        {t('enterprise.evalCi.desc', 'Run and inspect isolated live behavior evaluations, then sync the model used by the eval runtime.')}
      </p>

      <div className="card" style={{ marginBottom: 16, padding: 16 }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16 }}>
          <div>
            <h3 style={{ fontSize: 15, fontWeight: 600, margin: 0 }}>
              {t('enterprise.evalCi.behaviorTitle', 'Behavior Evaluation')}
            </h3>
            <p style={{ color: '#888', fontSize: '0.85rem', marginTop: 4, marginBottom: 0 }}>
              {t('enterprise.evalCi.behaviorDesc', 'Executes the production behavior suite in the isolated eval runtime. CI credentials stay server-side.')}
            </p>
          </div>
          <button
            className="btn btn-primary"
            onClick={onRunBehaviorEval}
            disabled={runningBehaviorEval || !runtimeStatus?.configured}
            style={{ flexShrink: 0 }}
          >
            {runningBehaviorEval
              ? t('enterprise.evalCi.running', 'Running...')
              : t('enterprise.evalCi.runBehavior', 'Run Behavior Evaluation')}
          </button>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16, padding: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginBottom: 12 }}>
          <div>
            <h3 style={{ fontSize: 15, fontWeight: 600, margin: 0 }}>
              {t('enterprise.evalCi.latestReport', 'Latest Behavior Report')}
            </h3>
            <p style={{ color: '#888', fontSize: '0.8rem', marginTop: 4, marginBottom: 0 }}>
              {latestReport?.stored_at
                ? t('enterprise.evalCi.latestStoredAt', 'Stored at {{time}}', { time: latestReport.stored_at })
                : t('enterprise.evalCi.latestReportDesc', 'Compact behavior-eval summary from the isolated runtime.')}
            </p>
          </div>
          {summary && (
            <span
              style={{
                padding: '4px 8px',
                borderRadius: 999,
                fontSize: 12,
                background: summary.benchmark_complete ? 'rgba(34,197,94,0.10)' : 'rgba(245,158,11,0.12)',
                color: summary.benchmark_complete ? '#15803d' : '#b45309',
              }}
            >
              {summary.benchmark_complete
                ? t('enterprise.evalCi.complete', 'Complete')
                : t('enterprise.evalCi.incomplete', 'Incomplete')}
            </span>
          )}
        </div>
        {latestReportLoading ? (
          <div style={{ color: '#888', fontSize: 13 }}>{t('common.loading', 'Loading...')}</div>
        ) : !latestReport?.available || !summary ? (
          <div style={{ color: '#888', fontSize: 13 }}>
            {t('enterprise.evalCi.noReport', 'No behavior evaluation report has been stored yet.')}
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 12 }}>
              <div>
                <label style={{ color: '#888', fontSize: 12 }}>{t('enterprise.evalCi.transport', 'Transport')}</label>
                <div style={{ fontWeight: 600 }}>{summary.transport || '—'}</div>
              </div>
              <div>
                <label style={{ color: '#888', fontSize: 12 }}>{t('enterprise.evalCi.runtimeModel', 'Runtime Model')}</label>
                <div style={{ fontWeight: 600 }}>{runtimeModel || '—'}</div>
              </div>
              <div>
                <label style={{ color: '#888', fontSize: 12 }}>{t('enterprise.evalCi.fallback', 'Fallback')}</label>
                <div style={{ fontWeight: 600 }}>
                  {summary.fallback_used ? t('common.yes', 'Yes') : t('common.no', 'No')}
                </div>
              </div>
            </div>
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                <thead>
                  <tr style={{ borderBottom: '1px solid #e5e7eb', color: '#777' }}>
                    <th style={{ textAlign: 'left', padding: '8px 6px' }}>{t('enterprise.evalCi.scenario', 'Scenario')}</th>
                    <th style={{ textAlign: 'left', padding: '8px 6px' }}>{t('enterprise.evalCi.ready', 'Ready')}</th>
                    <th style={{ textAlign: 'left', padding: '8px 6px' }}>{t('enterprise.evalCi.score', 'Score')}</th>
                    <th style={{ textAlign: 'left', padding: '8px 6px' }}>{t('enterprise.evalCi.transcriptChars', 'Transcript chars')}</th>
                  </tr>
                </thead>
                <tbody>
                  {scenarioEntries.map(([name, entry]) => (
                    <tr key={name} style={{ borderBottom: '1px solid #f1f5f9' }}>
                      <td style={{ padding: '8px 6px', fontWeight: 600 }}>{name}</td>
                      <td style={{ padding: '8px 6px' }}>{entry.ready ? t('common.yes', 'Yes') : t('common.no', 'No')}</td>
                      <td style={{ padding: '8px 6px' }}>{entry.score ?? '—'}</td>
                      <td style={{ padding: '8px 6px' }}>{entry.transcript_chars ?? 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 20, maxWidth: 640 }}>
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
