import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { enterpriseApi } from '../../api/domains/enterprise';
import type { MemoryConfig, LLMModel } from '../../api/domains/enterprise';

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
      alert(t('common.saveFailed', 'Save failed'));
    } finally {
      setSaving(false);
    }
  };

  const enabledModels = (models || []).filter((m: LLMModel) => m.enabled);

  return (
    <div>
      <h2 style={{ fontSize: '1.25rem', fontWeight: 600, marginBottom: 4 }}>
        {t('enterprise.memory.title', 'Memory Configuration')}
      </h2>
      <p style={{ color: '#888', fontSize: '0.85rem', marginBottom: 24 }}>
        {t('enterprise.memory.desc', 'Configure how agents summarize conversations, extract facts, and rank memory relevance.')}
      </p>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 20, maxWidth: 600 }}>
        {/* Summary Model */}
        <div>
          <label style={{ fontWeight: 500, fontSize: '0.9rem', display: 'block', marginBottom: 6 }}>
            {t('enterprise.memory.summaryModel', 'Summary / Extraction Model')}
          </label>
          <p style={{ color: '#888', fontSize: '0.8rem', marginBottom: 8 }}>
            {t('enterprise.memory.summaryModelDesc', 'Used for session summaries, memory fact extraction, and conversation compression. Choose a fast, cheap model.')}
          </p>
          <select
            value={form.summary_model_id || ''}
            onChange={(e) => setForm({ ...form, summary_model_id: e.target.value || null })}
            style={{ width: '100%', padding: '8px 12px', borderRadius: 6, border: '1px solid #ddd', fontSize: '0.9rem' }}
          >
            <option value="">{t('enterprise.memory.noModel', '-- Not configured (rule-based fallback) --')}</option>
            {enabledModels.map((m: LLMModel) => (
              <option key={m.id} value={m.id}>{m.label || m.model} ({m.provider})</option>
            ))}
          </select>
        </div>

        {/* Rerank Model */}
        <div>
          <label style={{ fontWeight: 500, fontSize: '0.9rem', display: 'block', marginBottom: 6 }}>
            {t('enterprise.memory.rerankModel', 'Memory Rerank Model')}
          </label>
          <p style={{ color: '#888', fontSize: '0.8rem', marginBottom: 8 }}>
            {t('enterprise.memory.rerankModelDesc', 'Optional. Re-scores semantic memories by relevance before injection. Only triggers when candidates > 5.')}
          </p>
          <select
            value={form.rerank_model_id || ''}
            onChange={(e) => setForm({ ...form, rerank_model_id: e.target.value || null })}
            style={{ width: '100%', padding: '8px 12px', borderRadius: 6, border: '1px solid #ddd', fontSize: '0.9rem' }}
          >
            <option value="">{t('enterprise.memory.noRerank', '-- Disabled (score-based only) --')}</option>
            {enabledModels.map((m: LLMModel) => (
              <option key={m.id} value={m.id}>{m.label || m.model} ({m.provider})</option>
            ))}
          </select>
        </div>

        {/* Compress Threshold */}
        <div>
          <label style={{ fontWeight: 500, fontSize: '0.9rem', display: 'block', marginBottom: 6 }}>
            {t('enterprise.memory.compressThreshold', 'Compression Threshold')}
          </label>
          <p style={{ color: '#888', fontSize: '0.8rem', marginBottom: 8 }}>
            {t('enterprise.memory.compressThresholdDesc', 'Compress conversation history when context usage exceeds this percentage. Default 82%.')}
          </p>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <input
              type="range"
              min={50}
              max={95}
              value={form.compress_threshold}
              onChange={(e) => setForm({ ...form, compress_threshold: Number(e.target.value) })}
              style={{ flex: 1 }}
            />
            <span style={{ fontWeight: 600, minWidth: 40, textAlign: 'right' }}>{form.compress_threshold}%</span>
          </div>
        </div>

        {/* Keep Recent */}
        <div>
          <label style={{ fontWeight: 500, fontSize: '0.9rem', display: 'block', marginBottom: 6 }}>
            {t('enterprise.memory.keepRecent', 'Keep Recent Messages')}
          </label>
          <p style={{ color: '#888', fontSize: '0.8rem', marginBottom: 8 }}>
            {t('enterprise.memory.keepRecentDesc', 'Always preserve this many recent messages during compression.')}
          </p>
          <input
            type="number"
            min={3}
            max={50}
            value={form.keep_recent}
            onChange={(e) => setForm({ ...form, keep_recent: Number(e.target.value) })}
            style={{ width: 100, padding: '8px 12px', borderRadius: 6, border: '1px solid #ddd', fontSize: '0.9rem' }}
          />
        </div>

        {/* Save Button */}
        <div style={{ marginTop: 8 }}>
          <button
            onClick={save}
            disabled={saving}
            style={{
              padding: '10px 24px',
              borderRadius: 8,
              border: 'none',
              background: saved ? '#22c55e' : '#2563eb',
              color: '#fff',
              fontWeight: 600,
              cursor: saving ? 'not-allowed' : 'pointer',
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
