import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { requestAppConfirm } from '../../components/AppDialogs';
import { subagentApi, type SubagentRow } from '../../api/domains/subagents';
import { scopeBadgeStyle, toolFaceSummary } from '../agent-detail/AgentSubagentsSection';

import './WorkspaceSubagentsSection.css';

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
  const [genPrompt, setGenPrompt] = useState('');
  const [generating, setGenerating] = useState(false);

  // AI generation (vendor-neutral): description → complete 定义.md prefilled
  // into the editor; the admin stays the final confirmation gate.
  const generateWithAI = async () => {
    const description = genPrompt.trim();
    if (!description) return;
    setGenerating(true);
    setActionError(null);
    try {
      const { definition } = await subagentApi.enterpriseGenerate(description);
      setEditorText(definition);
      const nameMatch = definition.match(/^name:\s*(\S+)\s*$/m);
      if (nameMatch) setEditorName(nameMatch[1]);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setGenerating(false);
    }
  };

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
    const confirmed = await requestAppConfirm({
      title: t('agent.subagents.deleteButton', 'Delete'),
      message: t('enterprise.subagents.deleteConfirm', { name: row.name }),
      confirmLabel: t('common.delete', 'Delete'),
      danger: true,
    });
    if (!confirmed) return;
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
      <div className="ws-subagents-head">
        <div>
          <h3 className="ws-subagents-title">{t('enterprise.subagents.title')}</h3>
          <p className="ws-subagents-desc">{t('enterprise.subagents.description')}</p>
        </div>
        <button className="btn btn-primary ws-subagents-new" onClick={startCreate}>
          {t('agent.subagents.newButton')}
        </button>
      </div>

      {isLoading && <p className="ws-subagents-desc">{t('common.loading')}</p>}

      {!isLoading && rows.length === 0 && (
        <p className="ws-subagents-desc">{t('enterprise.subagents.empty')}</p>
      )}

      {rows.length > 0 && (
        <div className="ws-subagents-list">
          {rows.map((row) => (
            <div key={`${row.scope}:${row.name}`} className="ws-subagents-row">
              <span className="ws-subagents-name">{row.name}</span>
              <span style={scopeBadgeStyle(row.scope)}>{t(`agent.subagents.scope.${row.scope}`)}</span>
              <span className="ws-subagents-type">{row.type}</span>
              {row.model && <span className="ws-subagents-model">{row.model}</span>}
              <span title={row.description} className="ws-subagents-row-desc">
                {row.description}
              </span>
              <span className="ws-subagents-tools">
                {toolFaceSummary(row)}
              </span>
              <span className="ws-subagents-row-actions">
                <button className="btn btn-secondary" onClick={() => startEdit(row)}>
                  {row.scope === 'builtin' ? t('agent.subagents.forkButton') : t('agent.subagents.editButton')}
                </button>
                {row.scope === 'tenant' && (
                  <button
                    className="btn btn-secondary ws-subagents-del"
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
        <div className="ws-subagents-editor">
          <div className="ws-subagents-editor-head">
            <h4 className="ws-subagents-editor-title">
              {editorMode === 'create' ? t('enterprise.subagents.createTitle') : t('enterprise.subagents.editTitle')}
            </h4>
            <span className="ws-subagents-editor-actions">
              <button className="btn btn-primary" onClick={save} disabled={saving}>
                {t('common.save')}
              </button>
              <button className="btn btn-secondary" onClick={() => setEditorMode('closed')}>
                {t('common.cancel')}
              </button>
            </span>
          </div>
          {editorMode === 'create' && (
            <>
              <div className="ws-subagents-gen-row">
                <input
                  type="text"
                  value={genPrompt}
                  onChange={(e) => setGenPrompt(e.target.value)}
                  placeholder={t('agent.subagents.generatePlaceholder')}
                  disabled={generating}
                  className="ws-subagents-gen-input"
                />
                <button
                  className="btn btn-secondary ws-subagents-gen-btn"
                  onClick={generateWithAI}
                  disabled={generating || !genPrompt.trim()}
                >
                  {generating ? t('agent.subagents.generating') : t('agent.subagents.generateButton')}
                </button>
              </div>
              <input
                type="text"
                value={editorName}
                onChange={(e) => setEditorName(e.target.value)}
                placeholder={t('agent.subagents.namePlaceholder')}
                className="ws-subagents-name-input"
              />
            </>
          )}
          <textarea
            value={editorText}
            onChange={(e) => setEditorText(e.target.value)}
            spellCheck={false}
            className="ws-subagents-editor-textarea"
          />
          {actionError && (
            <p className="ws-subagents-error">{actionError}</p>
          )}
        </div>
      )}
    </div>
  );
}
