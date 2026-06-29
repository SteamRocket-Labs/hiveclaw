import React, { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { extensionsApi, type AgentExtensions } from '../../api/domains/extensions';
import { fileApi } from '../../api/domains/files';
import { skillApi } from '../../api/domains/skills';
import { showAppToast } from '../../components/AppDialogs';

type AgentSkillsSectionProps = {
  agentId: string;
};

function skillSourceLabel(t: ReturnType<typeof useTranslation>['t'], source?: string | null): string {
  const normalized = String(source || '').toLowerCase();
  if (normalized === 'internal' || normalized === 'system' || normalized === 'platform') {
    return t('agent.skills.sourceInternal', 'Internal');
  }
  if (normalized === 'agent' || normalized === 'imported') {
    return t('agent.skills.sourceAgent', 'Agent/imported');
  }
  if (normalized === 'clawhub') {
    return t('agent.skills.sourceClawhub', 'ClawHub');
  }
  if (normalized === 'url' || normalized === 'github') {
    return t('agent.skills.sourceUrl', 'URL');
  }
  return source || t('agent.skills.sourceUnknown', 'Unknown source');
}

function pluginName(plugin: NonNullable<AgentExtensions['plugins']>[number]): string {
  return plugin.plugin_key || plugin.id;
}

export default function AgentSkillsSection({ agentId }: AgentSkillsSectionProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [showImportSkillModal, setShowImportSkillModal] = useState(false);
  const [importingSkillId, setImportingSkillId] = useState<string | null>(null);
  const [showAgentClawhub, setShowAgentClawhub] = useState(false);
  const [agentClawhubQuery, setAgentClawhubQuery] = useState('');
  const [agentClawhubResults, setAgentClawhubResults] = useState<any[]>([]);
  const [agentClawhubSearching, setAgentClawhubSearching] = useState(false);
  const [agentClawhubInstalling, setAgentClawhubInstalling] = useState<string | null>(null);
  const [showAgentUrlImport, setShowAgentUrlImport] = useState(false);
  const [agentUrlInput, setAgentUrlInput] = useState('');
  const [agentUrlImporting, setAgentUrlImporting] = useState(false);

  const { data: globalSkillsForImport } = useQuery({
    queryKey: ['global-skills-for-import'],
    queryFn: () => skillApi.list(),
    enabled: showImportSkillModal,
  });
  const { data: agentExtensions, isLoading: agentExtensionsLoading } = useQuery({
    queryKey: ['agent-extensions', agentId],
    queryFn: () => extensionsApi.getAgentExtensions(agentId),
    enabled: Boolean(agentId),
  });

  const installedSkills = agentExtensions?.skills ?? [];
  const mcpServers = agentExtensions?.mcp_servers ?? [];
  const plugins = agentExtensions?.plugins ?? [];

  return (
    <div>
      <div style={{ marginBottom: '16px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
            <h3 style={{ marginBottom: '4px' }}>{t('agent.skills.title')}</h3>
            <p style={{ fontSize: '13px', color: 'var(--text-tertiary)' }}>{t('agent.skills.description')}</p>
          </div>
          <div style={{ display: 'flex', gap: '8px', flexShrink: 0 }}>
            <button
              className="btn btn-secondary"
              style={{ fontSize: '13px' }}
              onClick={() => {
                setShowAgentUrlImport(true);
                setAgentUrlInput('');
              }}
            >
              Import from URL
            </button>
            <button
              className="btn btn-secondary"
              style={{ fontSize: '13px' }}
              onClick={() => {
                setShowAgentClawhub(true);
                setAgentClawhubQuery('');
                setAgentClawhubResults([]);
              }}
            >
              Browse ClawHub
            </button>
            <button
              className="btn btn-primary"
              style={{ display: 'flex', alignItems: 'center', gap: '6px', whiteSpace: 'nowrap' }}
              onClick={() => setShowImportSkillModal(true)}
            >
              Import from Presets
            </button>
          </div>
        </div>
        <div style={{ marginTop: '8px', padding: '10px 14px', background: 'var(--bg-secondary)', borderRadius: '8px', fontSize: '12px', color: 'var(--text-secondary)', lineHeight: 1.6 }}>
          <strong>Skill Format:</strong>
          <br />
          • <code>skills/my-skill/SKILL.md</code> — {t('agent.skills.folderFormat', 'Each skill is a folder with a SKILL.md file and optional auxiliary files (scripts/, examples/)')}
        </div>
      </div>

      <div
        style={{
          padding: '12px 14px',
          borderRadius: '8px',
          background: 'var(--bg-secondary)',
          color: 'var(--text-secondary)',
          fontSize: '13px',
          lineHeight: 1.5,
          marginBottom: '16px',
        }}
      >
        {t(
          'agent.skills.governedNotice',
          'Active skill packages are governed by Skill promotion. Use imports and Evolution candidates instead of editing active skill files directly.',
        )}
      </div>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
          gap: '12px',
          marginBottom: '16px',
        }}
      >
        <section
          style={{
            border: '1px solid var(--border-subtle)',
            borderRadius: '8px',
            background: 'var(--bg-primary)',
            padding: '12px',
            minHeight: '116px',
          }}
        >
          <h4 style={{ margin: '0 0 4px', fontSize: '13px', color: 'var(--text-primary)' }}>
            {t('agent.skills.installedTitle', 'Installed skills')}
          </h4>
          <p style={{ margin: '0 0 10px', fontSize: '11px', color: 'var(--text-tertiary)', lineHeight: 1.45 }}>
            {t(
              'agent.skills.installedDescription',
              'Internal, imported, URL/ClawHub and agent skills currently visible to this agent.',
            )}
          </p>
          {agentExtensionsLoading ? (
            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
              {t('common.loading', 'Loading...')}
            </div>
          ) : installedSkills.length === 0 ? (
            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
              {t('agent.skills.noInstalledSkills', 'No installed skills.')}
            </div>
          ) : (
            <div style={{ display: 'grid', gap: '6px' }}>
              {installedSkills.map((skill) => (
                <div
                  key={skill.id || skill.name}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    gap: '10px',
                    minWidth: 0,
                    fontSize: '12px',
                  }}
                >
                  <span style={{ color: 'var(--text-primary)', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {skill.name}
                  </span>
                  <span style={{ color: 'var(--text-tertiary)', flexShrink: 0 }}>
                    {skillSourceLabel(t, skill.source)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>

        <section
          style={{
            border: '1px solid var(--border-subtle)',
            borderRadius: '8px',
            background: 'var(--bg-primary)',
            padding: '12px',
            minHeight: '116px',
          }}
        >
          <h4 style={{ margin: '0 0 10px', fontSize: '13px', color: 'var(--text-primary)' }}>
            {t('agent.skills.mcpBackedCapabilities', 'MCP-backed capabilities')}
          </h4>
          {agentExtensionsLoading ? (
            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
              {t('common.loading', 'Loading...')}
            </div>
          ) : mcpServers.length === 0 ? (
            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
              {t('agent.skills.noMcpCapabilities', 'No MCP-backed capabilities.')}
            </div>
          ) : (
            <div style={{ display: 'grid', gap: '6px' }}>
              {mcpServers.map((server) => (
                <div
                  key={server.id || server.name}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    gap: '10px',
                    minWidth: 0,
                    fontSize: '12px',
                  }}
                >
                  <span style={{ color: 'var(--text-primary)', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {server.name}
                  </span>
                  <span style={{ color: server.enabled ? 'var(--accent-text)' : 'var(--text-tertiary)', flexShrink: 0 }}>
                    {server.status} · {server.tool_count} {t('agent.skills.toolsUnit', 'tools')}
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>

        <section
          style={{
            border: '1px solid var(--border-subtle)',
            borderRadius: '8px',
            background: 'var(--bg-primary)',
            padding: '12px',
            minHeight: '116px',
          }}
        >
          <h4 style={{ margin: '0 0 10px', fontSize: '13px', color: 'var(--text-primary)' }}>
            {t('agent.skills.plugins', 'Plugins')}
          </h4>
          {agentExtensionsLoading ? (
            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
              {t('common.loading', 'Loading...')}
            </div>
          ) : plugins.length === 0 ? (
            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
              {t('agent.skills.noPlugins', 'No plugins assigned.')}
            </div>
          ) : (
            <div style={{ display: 'grid', gap: '6px' }}>
              {plugins.map((plugin) => (
                <div
                  key={plugin.id || plugin.plugin_key}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    gap: '10px',
                    minWidth: 0,
                    fontSize: '12px',
                  }}
                >
                  <span style={{ color: 'var(--text-primary)', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {pluginName(plugin)}
                  </span>
                  <span style={{ color: plugin.enabled ? 'var(--accent-text)' : 'var(--text-tertiary)', flexShrink: 0 }}>
                    {plugin.enabled ? t('agent.skills.enabled', 'Enabled') : t('agent.skills.disabled', 'Disabled')}
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>

      {showAgentClawhub && (
        <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.5)', zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center' }} onClick={() => setShowAgentClawhub(false)}>
          <div onClick={(event) => event.stopPropagation()} style={{ background: 'var(--bg-primary)', borderRadius: '12px', padding: '24px', maxWidth: '600px', width: '90%', maxHeight: '70vh', display: 'flex', flexDirection: 'column', boxShadow: '0 20px 60px rgba(0,0,0,0.3)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
              <h3>Browse ClawHub</h3>
              <button onClick={() => setShowAgentClawhub(false)} style={{ background: 'none', border: 'none', fontSize: '18px', cursor: 'pointer', color: 'var(--text-secondary)', padding: '4px 8px' }}>x</button>
            </div>
            <p style={{ fontSize: '13px', color: 'var(--text-secondary)', margin: '0 0 12px' }}>
              Search and install skills from ClawHub directly into this agent&apos;s workspace.
            </p>
            <div style={{ display: 'flex', gap: '8px', marginBottom: '16px' }}>
              <input
                className="input"
                placeholder="Search skills..."
                value={agentClawhubQuery}
                onChange={(event) => setAgentClawhubQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && agentClawhubQuery.trim()) {
                    setAgentClawhubSearching(true);
                    skillApi.clawhub.search(agentClawhubQuery).then((result) => {
                      setAgentClawhubResults(result);
                      setAgentClawhubSearching(false);
                    }).catch(() => setAgentClawhubSearching(false));
                  }
                }}
                style={{ flex: 1, fontSize: '13px' }}
              />
              <button
                className="btn btn-primary"
                style={{ fontSize: '13px' }}
                disabled={!agentClawhubQuery.trim() || agentClawhubSearching}
                onClick={() => {
                  setAgentClawhubSearching(true);
                  skillApi.clawhub.search(agentClawhubQuery).then((result) => {
                    setAgentClawhubResults(result);
                    setAgentClawhubSearching(false);
                  }).catch(() => setAgentClawhubSearching(false));
                }}
              >
                {agentClawhubSearching ? 'Searching...' : 'Search'}
              </button>
            </div>
            <div style={{ flex: 1, overflowY: 'auto' }}>
              {agentClawhubResults.length === 0 && !agentClawhubSearching && (
                <div style={{ textAlign: 'center', padding: '24px', color: 'var(--text-tertiary)', fontSize: '13px' }}>Search ClawHub to find skills</div>
              )}
              {agentClawhubResults.map((result: any) => (
                <div key={result.slug} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 12px', borderRadius: '8px', marginBottom: '6px', border: '1px solid var(--border-subtle)', background: 'var(--bg-secondary)' }}>
                  <div style={{ flex: 1 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                      <span style={{ fontWeight: 600, fontSize: '13px' }}>{result.displayName || result.slug}</span>
                      {result.version && <span style={{ fontSize: '10px', color: 'var(--accent-text)', background: 'var(--accent-subtle)', padding: '1px 5px', borderRadius: '4px' }}>v{result.version}</span>}
                    </div>
                    <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                      {result.summary?.substring(0, 100)}
                      {result.summary?.length > 100 ? '...' : ''}
                    </div>
                    {result.updatedAt && <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px', opacity: 0.7 }}>Updated {new Date(result.updatedAt).toLocaleDateString()}</div>}
                  </div>
                  <button
                    className="btn btn-secondary"
                    style={{ fontSize: '12px', padding: '5px 12px', marginLeft: '12px' }}
                    disabled={agentClawhubInstalling === result.slug}
                    onClick={async () => {
                      setAgentClawhubInstalling(result.slug);
                      try {
                        const response = await skillApi.agentImport.fromClawhub(agentId, result.slug);
                        showAppToast(`Installed "${result.displayName || result.slug}" (${response.files_written} files)`, 'success');
                        queryClient.invalidateQueries({ queryKey: ['files', agentId, 'skills'] });
                      } catch (error: any) {
                        showAppToast(`Import failed: ${error?.message || error}`, 'error');
                      } finally {
                        setAgentClawhubInstalling(null);
                      }
                    }}
                  >
                    {agentClawhubInstalling === result.slug ? 'Installing...' : 'Install'}
                  </button>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {showAgentUrlImport && (
        <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.5)', zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center' }} onClick={() => setShowAgentUrlImport(false)}>
          <div onClick={(event) => event.stopPropagation()} style={{ background: 'var(--bg-primary)', borderRadius: '12px', padding: '24px', maxWidth: '500px', width: '90%', boxShadow: '0 20px 60px rgba(0,0,0,0.3)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
              <h3>Import from GitHub URL</h3>
              <button onClick={() => setShowAgentUrlImport(false)} style={{ background: 'none', border: 'none', fontSize: '18px', cursor: 'pointer', color: 'var(--text-secondary)', padding: '4px 8px' }}>x</button>
            </div>
            <p style={{ fontSize: '13px', color: 'var(--text-secondary)', margin: '0 0 12px' }}>
              Paste a GitHub URL pointing to a skill directory (must contain SKILL.md).
            </p>
            <input
              className="input"
              placeholder="https://github.com/owner/repo/tree/main/path/to/skill"
              value={agentUrlInput}
              onChange={(event) => setAgentUrlInput(event.target.value)}
              style={{ width: '100%', fontSize: '13px', marginBottom: '12px', boxSizing: 'border-box' }}
            />
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px' }}>
              <button className="btn btn-secondary" onClick={() => setShowAgentUrlImport(false)}>Cancel</button>
              <button
                className="btn btn-primary"
                disabled={!agentUrlInput.trim() || agentUrlImporting}
                onClick={async () => {
                  setAgentUrlImporting(true);
                  try {
                    const response = await skillApi.agentImport.fromUrl(agentId, agentUrlInput.trim());
                    showAppToast(`Imported ${response.files_written} files`, 'success');
                    queryClient.invalidateQueries({ queryKey: ['files', agentId, 'skills'] });
                    setShowAgentUrlImport(false);
                  } catch (error: any) {
                    showAppToast(`Import failed: ${error?.message || error}`, 'error');
                  } finally {
                    setAgentUrlImporting(false);
                  }
                }}
              >
                {agentUrlImporting ? 'Importing...' : 'Import'}
              </button>
            </div>
          </div>
        </div>
      )}

      {showImportSkillModal && (
        <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.5)', zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center' }} onClick={() => setShowImportSkillModal(false)}>
          <div onClick={(event) => event.stopPropagation()} style={{ background: 'var(--bg-primary)', borderRadius: '12px', padding: '24px', maxWidth: '600px', width: '90%', maxHeight: '70vh', display: 'flex', flexDirection: 'column', boxShadow: '0 20px 60px rgba(0,0,0,0.3)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
              <h3>📦 {t('agent.skills.importPreset', 'Import from Presets')}</h3>
              <button onClick={() => setShowImportSkillModal(false)} style={{ background: 'none', border: 'none', fontSize: '18px', cursor: 'pointer', color: 'var(--text-secondary)', padding: '4px 8px' }}>✕</button>
            </div>
            <p style={{ fontSize: '13px', color: 'var(--text-secondary)', margin: '0 0 16px' }}>
              {t('agent.skills.importDesc', "Select a preset skill to import into this agent. All skill files will be copied to the agent's skills folder.")}
            </p>
            <div style={{ flex: 1, overflowY: 'auto' }}>
              {!globalSkillsForImport ? (
                <div style={{ textAlign: 'center', padding: '24px', color: 'var(--text-tertiary)' }}>Loading...</div>
              ) : globalSkillsForImport.length === 0 ? (
                <div style={{ textAlign: 'center', padding: '24px', color: 'var(--text-tertiary)' }}>No preset skills available</div>
              ) : (
                globalSkillsForImport.map((skill: any) => (
                  <div
                    key={skill.id}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      padding: '12px 14px',
                      borderRadius: '8px',
                      marginBottom: '8px',
                      border: '1px solid var(--border-subtle)',
                      background: 'var(--bg-secondary)',
                      transition: 'border-color 0.15s',
                    }}
                    onMouseEnter={(event) => (event.currentTarget.style.borderColor = 'var(--accent-primary)')}
                    onMouseLeave={(event) => (event.currentTarget.style.borderColor = 'var(--border-subtle)')}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flex: 1 }}>
                      <span style={{ fontSize: '20px' }}>{skill.icon || '📋'}</span>
                      <div>
                        <div style={{ fontWeight: 600, fontSize: '14px' }}>{skill.name}</div>
                        <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                          {skill.description?.substring(0, 100)}
                          {skill.description?.length > 100 ? '...' : ''}
                        </div>
                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                          📁 {skill.folder_name}
                          {skill.is_default && <span style={{ marginLeft: '8px', color: 'var(--accent-primary)', fontWeight: 600 }}>✓ Default</span>}
                        </div>
                      </div>
                    </div>
                    <button
                      className="btn btn-secondary"
                      style={{ whiteSpace: 'nowrap', fontSize: '12px', padding: '6px 14px' }}
                      disabled={importingSkillId === skill.id}
                      onClick={async () => {
                        setImportingSkillId(skill.id);
                        try {
                          const response = await fileApi.importSkill(agentId, skill.id);
                          showAppToast(`Imported "${skill.name}" (${response.files_written} files)`, 'success');
                          queryClient.invalidateQueries({ queryKey: ['files', agentId, 'skills'] });
                          setShowImportSkillModal(false);
                        } catch (error: any) {
                          showAppToast(`Import failed: ${error?.message || error}`, 'error');
                        } finally {
                          setImportingSkillId(null);
                        }
                      }}
                    >
                      {importingSkillId === skill.id ? '⏳ ...' : '⬇️ Import'}
                    </button>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
