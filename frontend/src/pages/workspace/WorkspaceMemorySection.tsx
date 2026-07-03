import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { enterpriseApi } from '../../api/domains/enterprise';
import { showAppToast } from '../../components/AppDialogs';
import type { MemoryConfig, LLMModel } from '../../api/domains/enterprise';
import './WorkspaceMemorySection.css';

const DEFAULT_CONFIG: MemoryConfig = {
  summary_model_id: null,
  rerank_model_id: null,
  compress_threshold: 82,
  keep_recent: 10,
  extract_to_viking: false,
};

interface Props {
  selectedTenantId?: string;
}

export default function WorkspaceMemorySection({ selectedTenantId }: Props) {
  const { t } = useTranslation();
  const [form, setForm] = useState<MemoryConfig>(DEFAULT_CONFIG);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const { data: models } = useQuery({
    queryKey: ['llm-models', selectedTenantId],
    queryFn: () => enterpriseApi.listLLMModels(selectedTenantId),
  });

  useEffect(() => {
    enterpriseApi.getMemoryConfig(selectedTenantId).then((data) => {
      if (data && Object.keys(data).length > 0) {
        setForm((prev) => ({ ...prev, ...data }));
      }
    }).catch(() => {});
  }, [selectedTenantId]);

  const save = async () => {
    setSaving(true);
    try {
      await enterpriseApi.updateMemoryConfig(form, selectedTenantId);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch {
      showAppToast(t('common.saveFailed', 'Save failed'), 'error');
    } finally {
      setSaving(false);
    }
  };

  const enabledModels = (models || []).filter((m: LLMModel) => m.enabled);

  return (
    <div>
      <h2 className="ws-memory-title">
        {t('enterprise.memory.title', 'Memory Configuration')}
      </h2>
      <p className="ws-memory-desc">
        {t('enterprise.memory.desc', 'Configure how agents summarize conversations, extract facts, and rank memory relevance.')}
      </p>

      <div className="ws-memory-fields">
        {/* Summary Model */}
        <div>
          <label className="ws-memory-label">
            {t('enterprise.memory.summaryModel', 'Summary / Extraction Model')}
          </label>
          <p className="ws-memory-hint">
            {t('enterprise.memory.summaryModelDesc', 'Used for session summaries, memory fact extraction, and conversation compression. Choose a fast, cheap model.')}
          </p>
          <select
            className="ws-memory-select"
            value={form.summary_model_id || ''}
            onChange={(e) => setForm({ ...form, summary_model_id: e.target.value || null })}
          >
            <option value="">{t('enterprise.memory.noModel', '-- Not configured (rule-based fallback) --')}</option>
            {enabledModels.map((m: LLMModel) => (
              <option key={m.id} value={m.id}>{m.label || m.model} ({m.provider})</option>
            ))}
          </select>
        </div>

        {/* Rerank Model */}
        <div>
          <label className="ws-memory-label">
            {t('enterprise.memory.rerankModel', 'Memory Rerank Model')}
          </label>
          <p className="ws-memory-hint">
            {t('enterprise.memory.rerankModelDesc', 'Optional. Re-scores semantic memories by relevance before injection. Only triggers when candidates > 5.')}
          </p>
          <select
            className="ws-memory-select"
            value={form.rerank_model_id || ''}
            onChange={(e) => setForm({ ...form, rerank_model_id: e.target.value || null })}
          >
            <option value="">{t('enterprise.memory.noRerank', '-- Disabled (score-based only) --')}</option>
            {enabledModels.map((m: LLMModel) => (
              <option key={m.id} value={m.id}>{m.label || m.model} ({m.provider})</option>
            ))}
          </select>
        </div>

        {/* Compress Threshold */}
        <div>
          <label className="ws-memory-label">
            {t('enterprise.memory.compressThreshold', 'Compression Threshold')}
          </label>
          <p className="ws-memory-hint">
            {t('enterprise.memory.compressThresholdDesc', 'Compress conversation history when context usage exceeds this percentage. Default 82%.')}
          </p>
          <div className="ws-memory-slider-row">
            <input
              className="ws-memory-range"
              type="range"
              min={50}
              max={95}
              value={form.compress_threshold}
              onChange={(e) => setForm({ ...form, compress_threshold: Number(e.target.value) })}
            />
            <span className="ws-memory-value">{form.compress_threshold}%</span>
          </div>
        </div>

        {/* Keep Recent */}
        <div>
          <label className="ws-memory-label">
            {t('enterprise.memory.keepRecent', 'Keep Recent Messages')}
          </label>
          <p className="ws-memory-hint">
            {t('enterprise.memory.keepRecentDesc', 'Always preserve this many recent messages during compression.')}
          </p>
          <input
            className="ws-memory-keep"
            type="number"
            min={3}
            max={50}
            value={form.keep_recent}
            onChange={(e) => setForm({ ...form, keep_recent: Number(e.target.value) })}
          />
        </div>

        {/* Save Button */}
        <div className="ws-memory-actions">
          <button
            className={`btn btn-primary ws-memory-save${saved ? ' is-saved' : ''}`}
            onClick={save}
            disabled={saving}
          >
            {saved ? t('common.saved', 'Saved') : saving ? t('common.saving', 'Saving...') : t('common.save', 'Save')}
          </button>
        </div>
      </div>
    </div>
  );
}
