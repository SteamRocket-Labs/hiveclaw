import React, { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { subagentApi, type SubagentRow } from '../../api/domains/subagents';
import { scopeBadgeStyle, toolFaceSummary } from '../agent-detail/AgentSubagentsSection';

/**
 * Company subagent library (cut C4, §12.3/§12.8): org-admin curation over
 * tenant-level definitions shared by every agent in the company. Builtin
 * types show as read-only template rows; agent-level definitions are not
 * visible here — they belong to each agent's own AgentDetail surface.
 */
export default function WorkspaceSubagentsSection() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const [editorMode, setEditorMode] = useState<'closed' | 'edit' | 'create'>('closed');
  const [editorName, setEditorName] = useState('');
  const [editorText, setEditorText] = useState('');
  const [saving, setSaving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ['enterprise-subagents'],
    queryFn: () => subagentApi.enterpriseList(),
  });

  const rows = data?.subagents ?? [];

  const startCreate = () => {
    setActionError(null);
    setEditorName('');
    setEditorText(
      `---\nname: \ndescription: \ntype: explorer\nallowed_tools: []\nexcluded_tools: []\nmodel: null\nmax_tool_rounds: null\nisolation: none\n---\n\n`,
    );
    setEditorMode('create');
  };

  const startEdit = async (row: SubagentRow) => {
    setActionError(null);
    if (row.scope === 'builtin') {
      // Builtin rows are templates: editing forks them into a named tenant definition.
      setEditorName('');
      setEditorText(
        `---\nname: \ndescription: \ntype: ${row.type}\nallowed_tools: []\nexcluded_tools: []\nmodel: null\nmax_tool_rounds: null\nisolation: none\n---\n\n`,
      );
      setEditorMode('create');
      return;
    }
    setEditorName(row.name);
    setSaving(true);
    try {
      // Round-trip the full definition text (frontmatter + system-prompt
      // body) — rebuilding from the list row would silently drop the body.
      const detail = await subagentApi.enterpriseGet(row.name);
      setEditorText(detail.definition);
      setEditorMode('edit');
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const save = async () => {
    const name = (editorMode === 'edit' ? editorName : editorName.trim());
    if (!name) {
      setActionError(t('agent.subagents.nameRequired'));
      return;
    }
    setSaving(true);
    setActionError(null);
    try {
      let text = editorText;
      if (editorMode === 'create') {
        text = text.replace(/^(\s*---[\s\S]*?\bname:)[^\n]*/, `$1 ${name}`);
      }
      await subagentApi.enterpriseSave(name, text);
      await queryClient.invalidateQueries({ queryKey: ['enterprise-subagents'] });
      setEditorMode('closed');
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const remove = async (row: SubagentRow) => {
    if (row.scope !== 'tenant') return;
    if (!window.confirm(t('enterprise.subagents.deleteConfirm', { name: row.name }))) return;
    setActionError(null);
    try {
      await subagentApi.enterpriseRemove(row.name);
      await queryClient.invalidateQueries({ queryKey: ['enterprise-subagents'] });
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
        <div>
          <h3 style={{ marginBottom: '4px' }}>{t('enterprise.subagents.title')}</h3>
          <p style={{ fontSize: '13px', color: 'var(--text-tertiary)' }}>{t('enterprise.subagents.description')}</p>
        </div>
        <button className="btn btn-primary" style={{ fontSize: '13px', flexShrink: 0 }} onClick={startCreate}>
          {t('agent.subagents.newButton')}
        </button>
      </div>

      {isLoading && <p style={{ color: 'var(--text-tertiary)', fontSize: '13px' }}>{t('common.loading')}</p>}

      {!isLoading && rows.length === 0 && (
        <p style={{ color: 'var(--text-tertiary)', fontSize: '13px' }}>{t('enterprise.subagents.empty')}</p>
      )}

      {rows.length > 0 && (
        <div style={{ border: '1px solid var(--border-primary)', borderRadius: '8px', overflow: 'hidden' }}>
          {rows.map((row) => (
            <div
              key={`${row.scope}:${row.name}`}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '12px',
                padding: '10px 14px',
                borderBottom: '1px solid var(--border-primary)',
              }}
            >
              <span style={{ fontWeight: 600, fontSize: '13px', minWidth: '160px' }}>{row.name}</span>
              <span style={scopeBadgeStyle(row.scope)}>{t(`agent.subagents.scope.${row.scope}`)}</span>
              <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{row.type}</span>
              {row.model && <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{row.model}</span>}
              <span
                title={row.description}
                style={{
                  flex: 1,
                  minWidth: 0,
                  fontSize: '12px',
                  color: 'var(--text-tertiary)',
                  whiteSpace: 'nowrap',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                }}
              >
                {row.description}
              </span>
              <span style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginLeft: 'auto', flexShrink: 0 }}>
                {toolFaceSummary(row)}
              </span>
              <span style={{ display: 'flex', gap: '8px' }}>
                <button className="btn btn-secondary" style={{ fontSize: '12px' }} onClick={() => startEdit(row)}>
                  {row.scope === 'builtin' ? t('agent.subagents.forkButton') : t('agent.subagents.editButton')}
                </button>
                {row.scope === 'tenant' && (
                  <button
                    className="btn btn-secondary"
                    style={{ fontSize: '12px', color: 'var(--danger, #dc2626)' }}
                    onClick={() => remove(row)}
                  >
                    {t('agent.subagents.deleteButton')}
                  </button>
                )}
              </span>
            </div>
          ))}
        </div>
      )}

      {editorMode !== 'closed' && (
        <div
          style={{
            marginTop: '16px',
            border: '1px solid var(--border-primary)',
            borderRadius: '8px',
            padding: '16px',
            background: 'var(--bg-secondary)',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '10px' }}>
            <h4 style={{ margin: 0 }}>
              {editorMode === 'create' ? t('enterprise.subagents.createTitle') : t('enterprise.subagents.editTitle')}
            </h4>
            <span style={{ marginLeft: 'auto', display: 'flex', gap: '8px' }}>
              <button className="btn btn-primary" style={{ fontSize: '12px' }} onClick={save} disabled={saving}>
                {t('common.save')}
              </button>
              <button className="btn btn-secondary" style={{ fontSize: '12px' }} onClick={() => setEditorMode('closed')}>
                {t('common.cancel')}
              </button>
            </span>
          </div>
          {editorMode === 'create' && (
            <input
              type="text"
              value={editorName}
              onChange={(e) => setEditorName(e.target.value)}
              placeholder={t('agent.subagents.namePlaceholder')}
              style={{
                width: '100%',
                marginBottom: '8px',
                padding: '8px 10px',
                fontSize: '13px',
                border: '1px solid var(--border-primary)',
                borderRadius: '6px',
                background: 'var(--bg-primary)',
              }}
            />
          )}
          <textarea
            value={editorText}
            onChange={(e) => setEditorText(e.target.value)}
            spellCheck={false}
            style={{
              width: '100%',
              minHeight: '320px',
              fontFamily: 'var(--font-mono, monospace)',
              fontSize: '12px',
              padding: '12px',
              border: '1px solid var(--border-primary)',
              borderRadius: '6px',
              background: 'var(--bg-primary)',
              resize: 'vertical',
            }}
          />
          {actionError && (
            <p style={{ marginTop: '8px', fontSize: '12px', color: 'var(--danger, #dc2626)' }}>{actionError}</p>
          )}
        </div>
      )}
    </div>
  );
}
