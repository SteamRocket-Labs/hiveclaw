import React, { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { subagentApi, type SubagentRow, type SubagentScope } from '../../api/domains/subagents';
import { requestAppConfirm } from '../../components/AppDialogs';

type AgentSubagentsSectionProps = {
  agentId: string;
  canManage: boolean;
};

const SCOPE_COLORS: Record<SubagentScope, string> = {
  agent: 'var(--accent-primary, #6366f1)',
  tenant: 'var(--accent-secondary, #0ea5e9)',
  builtin: 'var(--text-tertiary, #9ca3af)',
};

export function scopeBadgeStyle(scope: SubagentScope): React.CSSProperties {
  return {
    display: 'inline-block',
    padding: '1px 8px',
    borderRadius: '10px',
    fontSize: '11px',
    fontWeight: 600,
    color: SCOPE_COLORS[scope],
    border: `1px solid ${SCOPE_COLORS[scope]}`,
    textTransform: 'uppercase',
    letterSpacing: '0.04em',
  };
}

/** Summarize the tool face for the list row: explicit allow-list count or type preset. */
export function toolFaceSummary(row: SubagentRow): string {
  if (row.allowed_tools.length > 0) {
    const head = row.allowed_tools.slice(0, 3).join(', ');
    return row.allowed_tools.length > 3 ? `${head} +${row.allowed_tools.length - 3}` : head;
  }
  return '';
}

export default function AgentSubagentsSection({ agentId, canManage }: AgentSubagentsSectionProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [editorText, setEditorText] = useState('');
  const [editorName, setEditorName] = useState('');
  const [editorMode, setEditorMode] = useState<'closed' | 'view' | 'edit' | 'create'>('closed');
  const [saving, setSaving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [genPrompt, setGenPrompt] = useState('');
  const [generating, setGenerating] = useState(false);

  const { data: listData, isLoading } = useQuery({
    queryKey: ['agent-subagents', agentId],
    queryFn: () => subagentApi.list(agentId),
  });

  const { data: detail } = useQuery({
    queryKey: ['agent-subagent-detail', agentId, selectedName],
    queryFn: () => subagentApi.get(agentId, selectedName!),
    enabled: !!selectedName,
  });

  const rows = listData?.subagents ?? [];

  const openDetail = (row: SubagentRow) => {
    setActionError(null);
    setSelectedName(row.name);
    setEditorMode('view');
  };

  const startCreate = (template?: { name: string; definition: string }) => {
    setActionError(null);
    setSelectedName(null);
    setEditorName(template?.name ?? '');
    setEditorText(
      template?.definition ??
        `---\nname: \ndescription: \ntype: explorer\nallowed_tools: []\nexcluded_tools: []\nmodel: null\nmax_tool_rounds: null\nisolation: none\n---\n\n`,
    );
    setEditorMode('create');
  };

  const startEdit = () => {
    if (!detail) return;
    setActionError(null);
    setEditorName(detail.name);
    setEditorText(detail.definition);
    setEditorMode(detail.scope === 'builtin' ? 'create' : 'edit');
    if (detail.scope === 'builtin') {
      // Builtins are read-only templates (§12.4): editing one forks it into
      // an agent-level definition under a name the user picks.
      setEditorName('');
    }
  };

  const closePanel = () => {
    setSelectedName(null);
    setEditorMode('closed');
    setActionError(null);
  };

  // AI generation (vendor-neutral): one-line description → complete 定义.md
  // prefilled into the editor. The human stays the final confirmation gate.
  const generateWithAI = async () => {
    const description = genPrompt.trim();
    if (!description) return;
    setGenerating(true);
    setActionError(null);
    try {
      const { definition } = await subagentApi.generate(agentId, description);
      setEditorText(definition);
      const nameMatch = definition.match(/^name:\s*(\S+)\s*$/m);
      if (nameMatch) setEditorName(nameMatch[1]);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setGenerating(false);
    }
  };

  const save = async () => {
    const name = (editorMode === 'edit' ? detail?.name : editorName)?.trim();
    if (!name) {
      setActionError(t('agent.subagents.nameRequired'));
      return;
    }
    setSaving(true);
    setActionError(null);
    try {
      // The definition's frontmatter name must match — patch it in when the
      // user renamed via the name field on create/fork.
      let text = editorText;
      if (editorMode === 'create') {
        text = text.replace(/^(\s*---[\s\S]*?\bname:)[^\n]*/, `$1 ${name}`);
      }
      await subagentApi.save(agentId, name, text);
      await queryClient.invalidateQueries({ queryKey: ['agent-subagents', agentId] });
      await queryClient.invalidateQueries({ queryKey: ['agent-subagent-detail', agentId, name] });
      setSelectedName(name);
      setEditorMode('view');
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const removeAgentDefinition = async () => {
    if (!detail || detail.scope !== 'agent') return;
    const confirmed = await requestAppConfirm({
      title: t('agent.subagents.deleteButton', 'Delete'),
      message: t('agent.subagents.deleteConfirm', { name: detail.name }),
      confirmLabel: t('common.delete', 'Delete'),
      danger: true,
    });
    if (!confirmed) return;
    setSaving(true);
    setActionError(null);
    try {
      await subagentApi.remove(agentId, detail.name);
      await queryClient.invalidateQueries({ queryKey: ['agent-subagents', agentId] });
      closePanel();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
        <div>
          <h3 style={{ marginBottom: '4px' }}>{t('agent.subagents.title')}</h3>
          <p style={{ fontSize: '13px', color: 'var(--text-tertiary)' }}>{t('agent.subagents.description')}</p>
        </div>
        <span style={{ display: 'flex', alignItems: 'center', gap: '12px', flexShrink: 0 }}>
          {canManage && (
            <label
              title={t('agent.subagents.autoApproveHint')}
              style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '12px', cursor: 'pointer' }}
            >
              <input
                type="checkbox"
                checked={listData?.evolution_auto_approve ?? false}
                onChange={async (e) => {
                  try {
                    await subagentApi.setEvolutionMode(agentId, e.target.checked);
                    await queryClient.invalidateQueries({ queryKey: ['agent-subagents', agentId] });
                  } catch (err) {
                    setActionError(err instanceof Error ? err.message : String(err));
                  }
                }}
              />
              {t('agent.subagents.autoApprove')}
            </label>
          )}
          {canManage && (
            <button className="btn btn-primary" style={{ fontSize: '13px' }} onClick={() => startCreate()}>
              {t('agent.subagents.newButton')}
            </button>
          )}
        </span>
      </div>

      {isLoading && <p style={{ color: 'var(--text-tertiary)', fontSize: '13px' }}>{t('common.loading')}</p>}

      {!isLoading && rows.length === 0 && (
        <p style={{ color: 'var(--text-tertiary)', fontSize: '13px' }}>{t('agent.subagents.empty')}</p>
      )}

      {rows.length > 0 && (
        <div style={{ border: '1px solid var(--border-primary)', borderRadius: '8px', overflow: 'hidden' }}>
          {rows.map((row) => (
            <div
              key={`${row.scope}:${row.name}`}
              onClick={() => openDetail(row)}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '12px',
                padding: '10px 14px',
                borderBottom: '1px solid var(--border-primary)',
                cursor: 'pointer',
                background: selectedName === row.name ? 'var(--bg-secondary)' : 'transparent',
              }}
            >
              <span style={{ fontWeight: 600, fontSize: '13px', minWidth: '160px' }}>{row.name}</span>
              <span style={scopeBadgeStyle(row.scope)}>{t(`agent.subagents.scope.${row.scope}`)}</span>
              {row.pending_proposal && (
                <span
                  style={{
                    fontSize: '11px',
                    fontWeight: 600,
                    padding: '1px 8px',
                    borderRadius: '10px',
                    color: 'var(--warning, #d97706)',
                    border: '1px solid var(--warning, #d97706)',
                    flexShrink: 0,
                  }}
                >
                  {t('agent.subagents.proposalBadge')}
                </span>
              )}
              <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{row.type}</span>
              {row.model && (
                <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{row.model}</span>
              )}
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
          {editorMode === 'view' && detail && (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '10px' }}>
                <h4 style={{ margin: 0 }}>{detail.name}</h4>
                <span style={scopeBadgeStyle(detail.scope)}>{t(`agent.subagents.scope.${detail.scope}`)}</span>
                {detail.memory.exists && (
                  <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                    {t('agent.subagents.memoryEntries', { count: detail.memory.entries })}
                  </span>
                )}
                <span style={{ marginLeft: 'auto', display: 'flex', gap: '8px' }}>
                  {canManage && (
                    <button className="btn btn-secondary" style={{ fontSize: '12px' }} onClick={startEdit}>
                      {detail.scope === 'builtin'
                        ? t('agent.subagents.forkButton')
                        : t('agent.subagents.editButton')}
                    </button>
                  )}
                  {canManage && detail.scope === 'agent' && (
                    <button
                      className="btn btn-secondary"
                      style={{ fontSize: '12px', color: 'var(--danger, #dc2626)' }}
                      onClick={removeAgentDefinition}
                      disabled={saving}
                    >
                      {t('agent.subagents.deleteButton')}
                    </button>
                  )}
                  <button className="btn btn-secondary" style={{ fontSize: '12px' }} onClick={closePanel}>
                    {t('common.close')}
                  </button>
                </span>
              </div>
              {detail.scope === 'tenant' && (
                <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '8px' }}>
                  {t('agent.subagents.tenantHint')}
                </p>
              )}
              {detail.proposal && (
                <div
                  style={{
                    marginBottom: '12px',
                    border: '1px solid var(--warning, #d97706)',
                    borderRadius: '6px',
                    padding: '12px',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '6px' }}>
                    <strong style={{ fontSize: '13px' }}>{t('agent.subagents.proposalTitle')}</strong>
                    <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                      {t('agent.subagents.proposalAbsorbs', { count: detail.proposal.absorbed_entry_ids.length })}
                    </span>
                    {canManage && (
                      <span style={{ marginLeft: 'auto', display: 'flex', gap: '8px' }}>
                        <button
                          className="btn btn-primary"
                          style={{ fontSize: '12px' }}
                          disabled={saving}
                          onClick={async () => {
                            setSaving(true);
                            setActionError(null);
                            try {
                              await subagentApi.approveProposal(agentId, detail.name);
                              await queryClient.invalidateQueries({ queryKey: ['agent-subagents', agentId] });
                              await queryClient.invalidateQueries({
                                queryKey: ['agent-subagent-detail', agentId, detail.name],
                              });
                            } catch (err) {
                              setActionError(err instanceof Error ? err.message : String(err));
                            } finally {
                              setSaving(false);
                            }
                          }}
                        >
                          {t('agent.subagents.approveButton')}
                        </button>
                        <button
                          className="btn btn-secondary"
                          style={{ fontSize: '12px' }}
                          disabled={saving}
                          onClick={async () => {
                            setSaving(true);
                            setActionError(null);
                            try {
                              await subagentApi.rejectProposal(agentId, detail.name);
                              await queryClient.invalidateQueries({ queryKey: ['agent-subagents', agentId] });
                              await queryClient.invalidateQueries({
                                queryKey: ['agent-subagent-detail', agentId, detail.name],
                              });
                            } catch (err) {
                              setActionError(err instanceof Error ? err.message : String(err));
                            } finally {
                              setSaving(false);
                            }
                          }}
                        >
                          {t('agent.subagents.rejectButton')}
                        </button>
                      </span>
                    )}
                  </div>
                  <p style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '8px' }}>
                    {detail.proposal.rationale}
                  </p>
                  <pre
                    style={{
                      fontSize: '12px',
                      whiteSpace: 'pre-wrap',
                      background: 'var(--bg-primary)',
                      border: '1px solid var(--border-primary)',
                      borderRadius: '6px',
                      padding: '12px',
                      maxHeight: '260px',
                      overflow: 'auto',
                    }}
                  >
                    {detail.proposal.body}
                  </pre>
                </div>
              )}
              <pre
                style={{
                  fontSize: '12px',
                  whiteSpace: 'pre-wrap',
                  background: 'var(--bg-primary)',
                  border: '1px solid var(--border-primary)',
                  borderRadius: '6px',
                  padding: '12px',
                  maxHeight: '420px',
                  overflow: 'auto',
                }}
              >
                {detail.definition}
              </pre>
            </>
          )}

          {(editorMode === 'edit' || editorMode === 'create') && (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '10px' }}>
                <h4 style={{ margin: 0 }}>
                  {editorMode === 'create' ? t('agent.subagents.createTitle') : t('agent.subagents.editTitle')}
                </h4>
                <span style={{ marginLeft: 'auto', display: 'flex', gap: '8px' }}>
                  <button className="btn btn-primary" style={{ fontSize: '12px' }} onClick={save} disabled={saving}>
                    {t('common.save')}
                  </button>
                  <button className="btn btn-secondary" style={{ fontSize: '12px' }} onClick={closePanel}>
                    {t('common.cancel')}
                  </button>
                </span>
              </div>
              {editorMode === 'create' && (
                <>
                  <div style={{ display: 'flex', gap: '8px', marginBottom: '8px' }}>
                    <input
                      type="text"
                      value={genPrompt}
                      onChange={(e) => setGenPrompt(e.target.value)}
                      placeholder={t('agent.subagents.generatePlaceholder')}
                      disabled={generating}
                      style={{
                        flex: 1,
                        padding: '8px 10px',
                        fontSize: '13px',
                        border: '1px solid var(--border-primary)',
                        borderRadius: '6px',
                        background: 'var(--bg-primary)',
                      }}
                    />
                    <button
                      className="btn btn-secondary"
                      style={{ fontSize: '12px', flexShrink: 0 }}
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
                </>
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
            </>
          )}

          {actionError && (
            <p style={{ marginTop: '8px', fontSize: '12px', color: 'var(--danger, #dc2626)' }}>{actionError}</p>
          )}
        </div>
      )}
    </div>
  );
}
