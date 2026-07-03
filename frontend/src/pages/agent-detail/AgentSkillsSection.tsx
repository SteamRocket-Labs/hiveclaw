import { useState } from 'react';
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { extensionsApi, type AgentExtensions } from '../../api/domains/extensions';
import { fileApi } from '../../api/domains/files';
import { skillApi } from '../../api/domains/skills';
import { showAppToast } from '../../components/AppDialogs';
import './AgentSkillsSection.css';

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

export function invalidateAgentSkillQueries(
  queryClient: Pick<QueryClient, 'invalidateQueries'>,
  agentId: string,
) {
  queryClient.invalidateQueries({ queryKey: ['files', agentId, 'skills'] });
  queryClient.invalidateQueries({ queryKey: ['agent-extensions', agentId] });
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
      <div className="agent-skills-header">
        <div className="agent-skills-head-row">
          <div>
            <h3 className="agent-skills-title">{t('agent.skills.title')}</h3>
            <p className="agent-skills-subtitle">{t('agent.skills.description')}</p>
          </div>
          <div className="agent-skills-head-actions">
            <button
              className="btn btn-secondary agent-skills-toolbar-btn"
              onClick={() => {
                setShowAgentUrlImport(true);
                setAgentUrlInput('');
              }}
            >
              Import from URL
            </button>
            <button
              className="btn btn-secondary agent-skills-toolbar-btn"
              onClick={() => {
                setShowAgentClawhub(true);
                setAgentClawhubQuery('');
                setAgentClawhubResults([]);
              }}
            >
              Browse ClawHub
            </button>
            <button
              className="btn btn-primary agent-skills-primary-btn"
              onClick={() => setShowImportSkillModal(true)}
            >
              Import from Presets
            </button>
          </div>
        </div>
        <div className="agent-skills-format-note">
          <strong>Skill Format:</strong>
          <br />
          • <code>skills/my-skill/SKILL.md</code> — {t('agent.skills.folderFormat', 'Each skill is a folder with a SKILL.md file and optional auxiliary files (scripts/, examples/)')}
        </div>
      </div>

      <div className="agent-skills-governed-note">
        {t(
          'agent.skills.governedNotice',
          'Active skill packages are governed by Skill promotion. Use imports and Evolution candidates instead of editing active skill files directly.',
        )}
      </div>

      <div className="agent-skills-grid">
        <section className="agent-skills-panel">
          <h4 className="agent-skills-panel-title agent-skills-panel-title-tight">
            {t('agent.skills.installedTitle', 'Installed skills')}
          </h4>
          <p className="agent-skills-panel-desc">
            {t(
              'agent.skills.installedDescription',
              'Internal, imported, URL/ClawHub and agent skills currently visible to this agent.',
            )}
          </p>
          {agentExtensionsLoading ? (
            <div className="agent-skills-panel-note">
              {t('common.loading', 'Loading...')}
            </div>
          ) : installedSkills.length === 0 ? (
            <div className="agent-skills-panel-note">
              {t('agent.skills.noInstalledSkills', 'No installed skills.')}
            </div>
          ) : (
            <div className="agent-skills-list">
              {installedSkills.map((skill) => (
                <div key={skill.id || skill.name} className="agent-skills-row">
                  <span className="agent-skills-row-name">
                    {skill.name}
                  </span>
                  <span className="agent-skills-row-meta">
                    {skillSourceLabel(t, skill.source)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="agent-skills-panel">
          <h4 className="agent-skills-panel-title">
            {t('agent.skills.mcpBackedCapabilities', 'MCP-backed capabilities')}
          </h4>
          {agentExtensionsLoading ? (
            <div className="agent-skills-panel-note">
              {t('common.loading', 'Loading...')}
            </div>
          ) : mcpServers.length === 0 ? (
            <div className="agent-skills-panel-note">
              {t('agent.skills.noMcpCapabilities', 'No MCP-backed capabilities.')}
            </div>
          ) : (
            <div className="agent-skills-list">
              {mcpServers.map((server) => (
                <div key={server.id || server.name} className="agent-skills-row">
                  <span className="agent-skills-row-name">
                    {server.name}
                  </span>
                  <span className={`agent-skills-row-meta${server.enabled ? ' is-active' : ''}`}>
                    {server.status} · {server.tool_count} {t('agent.skills.toolsUnit', 'tools')}
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="agent-skills-panel">
          <h4 className="agent-skills-panel-title">
            {t('agent.skills.plugins', 'Plugins')}
          </h4>
          {agentExtensionsLoading ? (
            <div className="agent-skills-panel-note">
              {t('common.loading', 'Loading...')}
            </div>
          ) : plugins.length === 0 ? (
            <div className="agent-skills-panel-note">
              {t('agent.skills.noPlugins', 'No plugins assigned.')}
            </div>
          ) : (
            <div className="agent-skills-list">
              {plugins.map((plugin) => (
                <div key={plugin.id || plugin.plugin_key} className="agent-skills-row">
                  <span className="agent-skills-row-name">
                    {pluginName(plugin)}
                  </span>
                  <span className={`agent-skills-row-meta${plugin.enabled ? ' is-active' : ''}`}>
                    {plugin.enabled ? t('agent.skills.enabled', 'Enabled') : t('agent.skills.disabled', 'Disabled')}
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>

      {showAgentClawhub && (
        <div className="agent-skills-modal-overlay" onClick={() => setShowAgentClawhub(false)}>
          <div onClick={(event) => event.stopPropagation()} className="agent-skills-modal agent-skills-modal-lg">
            <div className="agent-skills-modal-head">
              <h3>Browse ClawHub</h3>
              <button onClick={() => setShowAgentClawhub(false)} className="agent-skills-modal-close">x</button>
            </div>
            <p className="agent-skills-modal-desc">
              Search and install skills from ClawHub directly into this agent&apos;s workspace.
            </p>
            <div className="agent-skills-search-row">
              <input
                className="input agent-skills-search-input"
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
              />
              <button
                className="btn btn-primary agent-skills-action-btn"
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
            <div className="agent-skills-scroll">
              {agentClawhubResults.length === 0 && !agentClawhubSearching && (
                <div className="agent-skills-empty-hint">Search ClawHub to find skills</div>
              )}
              {agentClawhubResults.map((result: any) => (
                <div key={result.slug} className="agent-skills-result-row">
                  <div className="agent-skills-result-main">
                    <div className="agent-skills-result-title-row">
                      <span className="agent-skills-result-name">{result.displayName || result.slug}</span>
                      {result.version && <span className="agent-skills-version-tag">v{result.version}</span>}
                    </div>
                    <div className="agent-skills-result-desc">
                      {result.summary?.substring(0, 100)}
                      {result.summary?.length > 100 ? '...' : ''}
                    </div>
                    {result.updatedAt && <div className="agent-skills-result-updated">Updated {new Date(result.updatedAt).toLocaleDateString()}</div>}
                  </div>
                  <button
                    className="btn btn-secondary agent-skills-install-btn"
                    disabled={agentClawhubInstalling === result.slug}
                    onClick={async () => {
                      setAgentClawhubInstalling(result.slug);
                      try {
                        const response = await skillApi.agentImport.fromClawhub(agentId, result.slug);
                        showAppToast(`Installed "${result.displayName || result.slug}" (${response.files_written} files)`, 'success');
                        invalidateAgentSkillQueries(queryClient, agentId);
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
        <div className="agent-skills-modal-overlay" onClick={() => setShowAgentUrlImport(false)}>
          <div onClick={(event) => event.stopPropagation()} className="agent-skills-modal agent-skills-modal-sm">
            <div className="agent-skills-modal-head">
              <h3>Import from GitHub URL</h3>
              <button onClick={() => setShowAgentUrlImport(false)} className="agent-skills-modal-close">x</button>
            </div>
            <p className="agent-skills-modal-desc">
              Paste a GitHub URL pointing to a skill directory (must contain SKILL.md).
            </p>
            <input
              className="input agent-skills-url-input"
              placeholder="https://github.com/owner/repo/tree/main/path/to/skill"
              value={agentUrlInput}
              onChange={(event) => setAgentUrlInput(event.target.value)}
            />
            <div className="agent-skills-modal-actions">
              <button className="btn btn-secondary" onClick={() => setShowAgentUrlImport(false)}>Cancel</button>
              <button
                className="btn btn-primary"
                disabled={!agentUrlInput.trim() || agentUrlImporting}
                onClick={async () => {
                  setAgentUrlImporting(true);
                  try {
                    const response = await skillApi.agentImport.fromUrl(agentId, agentUrlInput.trim());
                    showAppToast(`Imported ${response.files_written} files`, 'success');
                    invalidateAgentSkillQueries(queryClient, agentId);
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
        <div className="agent-skills-modal-overlay" onClick={() => setShowImportSkillModal(false)}>
          <div onClick={(event) => event.stopPropagation()} className="agent-skills-modal agent-skills-modal-lg">
            <div className="agent-skills-modal-head">
              <h3>📦 {t('agent.skills.importPreset', 'Import from Presets')}</h3>
              <button onClick={() => setShowImportSkillModal(false)} className="agent-skills-modal-close">✕</button>
            </div>
            <p className="agent-skills-modal-desc agent-skills-modal-desc-wide">
              {t('agent.skills.importDesc', "Select a preset skill to import into this agent. All skill files will be copied to the agent's skills folder.")}
            </p>
            <div className="agent-skills-scroll">
              {!globalSkillsForImport ? (
                <div className="agent-skills-empty-hint">Loading...</div>
              ) : globalSkillsForImport.length === 0 ? (
                <div className="agent-skills-empty-hint">No preset skills available</div>
              ) : (
                globalSkillsForImport.map((skill: any) => (
                  <div key={skill.id} className="agent-skills-preset-row">
                    <div className="agent-skills-preset-main">
                      <span className="agent-skills-preset-icon">{skill.icon || '📋'}</span>
                      <div>
                        <div className="agent-skills-preset-name">{skill.name}</div>
                        <div className="agent-skills-preset-desc">
                          {skill.description?.substring(0, 100)}
                          {skill.description?.length > 100 ? '...' : ''}
                        </div>
                        <div className="agent-skills-preset-folder">
                          📁 {skill.folder_name}
                          {skill.is_default && <span className="agent-skills-preset-default">✓ Default</span>}
                        </div>
                      </div>
                    </div>
                    <button
                      className="btn btn-secondary agent-skills-import-btn"
                      disabled={importingSkillId === skill.id}
                      onClick={async () => {
                        setImportingSkillId(skill.id);
                        try {
                          const response = await fileApi.importSkill(agentId, skill.id);
                          showAppToast(`Imported "${skill.name}" (${response.files_written} files)`, 'success');
                          invalidateAgentSkillQueries(queryClient, agentId);
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
