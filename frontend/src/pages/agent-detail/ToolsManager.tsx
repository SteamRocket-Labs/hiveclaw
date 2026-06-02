import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { extensionsApi, type AgentExtensions, type AgentMcpServer } from '../../api/domains/extensions';
import { toolsApi } from '../../api/domains/tools';
import ToolIcon from '../../components/ToolIcon';
import { useAuthStore } from '../../stores';

type ToolsManagerProps = {
  agentId: string;
  canManage?: boolean;
};

const STATUS_COLORS: Record<string, string> = {
  connected: '#22c55e',
  available: '#22c55e',
  needs_auth: '#f59e0b',
  expired: '#f59e0b',
  failed: '#ef4444',
  error: '#ef4444',
  disabled: 'var(--text-tertiary)',
};

const statusColor = (status?: string) => STATUS_COLORS[status || ''] || 'var(--text-tertiary)';

export default function ToolsManager({ agentId, canManage = false }: ToolsManagerProps) {
  const { t } = useTranslation();
  const [tools, setTools] = useState<any[]>([]);
  const [extensions, setExtensions] = useState<AgentExtensions | null>(null);
  const [loading, setLoading] = useState(true);
  const [configTool, setConfigTool] = useState<any | null>(null);
  const [configData, setConfigData] = useState<Record<string, any>>({});
  const [configJson, setConfigJson] = useState('');
  const [configSaving, setConfigSaving] = useState(false);
  const [savingServerId, setSavingServerId] = useState<string | null>(null);
  // MCP servers whose "Advanced tool controls" drawer is expanded (hidden by default).
  const [openAdvanced, setOpenAdvanced] = useState<Set<string>>(new Set());

  const loadTools = async () => {
    try {
      const data = await toolsApi.listWithConfig(agentId).catch(() => toolsApi.list(agentId));
      setTools(data);
    } catch (error) {
      console.error(error);
    }
  };

  const loadExtensions = async () => {
    try {
      const data = await extensionsApi.getAgentExtensions(agentId);
      setExtensions(data);
    } catch (error) {
      console.error(error);
    }
  };

  useEffect(() => {
    setLoading(true);
    void Promise.all([loadTools(), loadExtensions()]).finally(() => setLoading(false));
  }, [agentId]);

  const toggleServer = async (server: AgentMcpServer, enabled: boolean) => {
    setExtensions((prev) =>
      prev
        ? { ...prev, mcp_servers: prev.mcp_servers.map((s) => (s.id === server.id ? { ...s, enabled } : s)) }
        : prev,
    );
    setSavingServerId(server.id);
    try {
      await extensionsApi.setAgentMcpAssignment(agentId, server.id, {
        enabled,
        default_tool_mode: server.default_tool_mode,
      });
    } catch (error) {
      console.error(error);
      await loadExtensions();
    }
    setSavingServerId(null);
  };

  const toggleTool = async (toolId: string, enabled: boolean) => {
    setTools((prev) => prev.map((tool) => (tool.id === toolId ? { ...tool, enabled } : tool)));
    try {
      await toolsApi.updateTools(agentId, { tools: [{ tool_id: toolId, enabled }] });
    } catch (error) {
      console.error(error);
    }
  };

  const openConfig = (tool: any) => {
    setConfigTool(tool);
    const merged = { ...(tool.global_config || {}), ...(tool.agent_config || {}) };
    setConfigData(merged);
    setConfigJson(JSON.stringify(tool.agent_config || {}, null, 2));
  };

  const saveConfig = async () => {
    if (!configTool) return;
    setConfigSaving(true);
    try {
      const hasSchema = configTool.config_schema?.fields?.length > 0;
      const payload = hasSchema ? configData : JSON.parse(configJson || '{}');
      await toolsApi.updateToolConfig(agentId, configTool.id, payload);
      setConfigTool(null);
      await loadTools();
    } catch (error) {
      alert(`Save failed: ${error}`);
    }
    setConfigSaving(false);
  };

  if (loading) {
    return <div style={{ color: 'var(--text-tertiary)', padding: '20px' }}>{t('common.loading')}</div>;
  }

  const skills = extensions?.skills ?? [];
  const mcpServers = extensions?.mcp_servers ?? [];

  // Tools belonging to a given MCP server (matched by server name) — only shown
  // inside the per-server "Advanced tool controls" drawer.
  const toolsForServer = (serverName: string) =>
    tools.filter((tool) => tool.type === 'mcp' && (tool.mcp_server_name || '') === serverName);

  const toggleAdvanced = (serverId: string) =>
    setOpenAdvanced((prev) => {
      const next = new Set(prev);
      if (next.has(serverId)) next.delete(serverId);
      else next.add(serverId);
      return next;
    });

  const Toggle = ({ checked, onChange, disabled }: { checked: boolean; onChange: (next: boolean) => void; disabled?: boolean }) => (
    <label style={{ position: 'relative', display: 'inline-block', width: '40px', height: '22px', cursor: disabled ? 'default' : 'pointer', flexShrink: 0, opacity: disabled ? 0.6 : 1 }}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        style={{ opacity: 0, width: 0, height: 0 }}
      />
      <span style={{ position: 'absolute', inset: 0, background: checked ? '#22c55e' : 'var(--bg-tertiary)', borderRadius: '11px', transition: 'background 0.2s' }}>
        <span style={{ position: 'absolute', left: checked ? '20px' : '2px', top: '2px', width: '18px', height: '18px', background: '#fff', borderRadius: '50%', transition: 'left 0.2s' }} />
      </span>
    </label>
  );

  const moduleTitle = (title: string, count: number) => (
    <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.5px', marginBottom: '10px' }}>
      {title} ({count})
    </div>
  );

  return (
    <>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
        {/* ── Skills module ── */}
        <section>
          {moduleTitle(t('agent.extensions.skills', 'Skills'), skills.length)}
          {skills.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              {skills.map((skill) => (
                <div key={skill.id} className="card" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 14px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px', minWidth: 0 }}>
                    <span style={{ fontSize: '16px' }}>📘</span>
                    <div style={{ minWidth: 0 }}>
                      <div style={{ fontWeight: 500, fontSize: '13px' }}>{skill.name}</div>
                      <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                        {t(`agent.extensions.source.${skill.source}`, skill.source)}
                      </div>
                    </div>
                  </div>
                  <span style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '11px', color: 'var(--text-tertiary)' }}>
                    <span style={{ width: '7px', height: '7px', borderRadius: '50%', background: statusColor(skill.status) }} />
                    {t(`agent.extensions.status.${skill.status}`, skill.status)}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div className="card" style={{ textAlign: 'center', padding: '24px', color: 'var(--text-tertiary)', fontSize: '13px' }}>
              {t('agent.extensions.noSkills', 'No skills yet')}
            </div>
          )}
        </section>

        {/* ── MCP Servers module ── */}
        <section>
          {moduleTitle(t('agent.extensions.mcpServers', 'MCP Servers'), mcpServers.length)}
          {mcpServers.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {mcpServers.map((server) => {
                const advancedOpen = openAdvanced.has(server.id);
                const serverTools = toolsForServer(server.name);
                return (
                  <div key={server.id} className="card" style={{ padding: 0, overflow: 'hidden' }}>
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '12px 14px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '10px', minWidth: 0 }}>
                        <span style={{ fontSize: '18px' }}>🔌</span>
                        <div style={{ minWidth: 0 }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                            <span style={{ fontWeight: 600, fontSize: '13px' }}>{server.name}</span>
                            <span style={{ fontSize: '10px', background: 'var(--primary)', color: '#fff', borderRadius: '4px', padding: '1px 5px' }}>MCP</span>
                          </div>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                            <span style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                              <span style={{ width: '7px', height: '7px', borderRadius: '50%', background: statusColor(server.status) }} />
                              {t(`agent.extensions.status.${server.status}`, server.status)}
                            </span>
                            <span>·</span>
                            <span>{t('agent.extensions.toolCount', { count: server.tool_count, defaultValue: '{{count}} tools' })}</span>
                          </div>
                        </div>
                      </div>
                      {canManage ? (
                        <Toggle checked={server.enabled} disabled={savingServerId === server.id} onChange={(next) => void toggleServer(server, next)} />
                      ) : (
                        <span style={{ fontSize: '11px', color: server.enabled ? '#22c55e' : 'var(--text-tertiary)', fontWeight: 500 }}>
                          {server.enabled ? t('common.enabled', 'On') : t('common.disabled', 'Off')}
                        </span>
                      )}
                    </div>

                    {serverTools.length > 0 && (
                      <div style={{ borderTop: '1px solid var(--border-subtle)' }}>
                        <button
                          onClick={() => toggleAdvanced(server.id)}
                          style={{ display: 'flex', alignItems: 'center', gap: '6px', width: '100%', background: 'none', border: 'none', padding: '8px 14px', cursor: 'pointer', color: 'var(--text-tertiary)', fontSize: '11px', textAlign: 'left' }}
                        >
                          <span style={{ transition: 'transform 0.15s', transform: advancedOpen ? 'rotate(90deg)' : 'rotate(0deg)' }}>▶</span>
                          {t('agent.extensions.advancedToolControls', 'Advanced tool controls')} ({serverTools.length})
                        </button>
                        {advancedOpen && (
                          <div style={{ padding: '0 14px 12px 14px', display: 'flex', flexDirection: 'column', gap: '4px', background: 'var(--bg-secondary)' }}>
                            {serverTools.map((tool) => {
                              const shortName = tool.mcp_server_name && tool.display_name?.startsWith(tool.mcp_server_name + ': ')
                                ? tool.display_name.slice(tool.mcp_server_name.length + 2)
                                : tool.display_name;
                              const hasConfig = tool.config_schema?.fields?.length > 0 || tool.type === 'mcp';
                              return (
                                <div key={tool.id} className="card" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '8px 12px' }}>
                                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
                                    <ToolIcon tool={tool} />
                                    <div style={{ minWidth: 0 }}>
                                      <div style={{ fontWeight: 500, fontSize: '12px' }}>{shortName}</div>
                                      <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                        {tool.description}
                                      </div>
                                    </div>
                                  </div>
                                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 }}>
                                    {canManage && hasConfig && (
                                      <button
                                        onClick={() => openConfig(tool)}
                                        style={{ background: 'none', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '3px 8px', fontSize: '11px', cursor: 'pointer', color: 'var(--text-secondary)' }}
                                        title={t('agent.extensions.configureTool', 'Configure tool')}
                                      >
                                        {t('enterprise.tools.configure', 'Configure')}
                                      </button>
                                    )}
                                    {canManage ? (
                                      <Toggle checked={tool.enabled} onChange={(next) => void toggleTool(tool.id, next)} />
                                    ) : (
                                      <span style={{ fontSize: '11px', color: tool.enabled ? '#22c55e' : 'var(--text-tertiary)', fontWeight: 500 }}>
                                        {tool.enabled ? t('common.enabled', 'On') : t('common.disabled', 'Off')}
                                      </span>
                                    )}
                                  </div>
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="card" style={{ textAlign: 'center', padding: '24px', color: 'var(--text-tertiary)', fontSize: '13px' }}>
              {t('agent.extensions.noMcpServers', 'No MCP servers connected')}
            </div>
          )}
        </section>
      </div>

      {configTool && (
        <div
          style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.55)', zIndex: 2000, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
          onClick={() => setConfigTool(null)}
        >
          <div
            onClick={(event) => event.stopPropagation()}
            style={{ background: 'var(--bg-primary)', borderRadius: '12px', padding: '24px', width: '480px', maxWidth: '95vw', maxHeight: '80vh', overflow: 'auto', boxShadow: '0 20px 60px rgba(0,0,0,0.4)' }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
              <div>
                <h3 style={{ margin: 0 }}>{configTool.display_name}</h3>
                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                  {t('agent.extensions.perAgentConfig', 'Per-agent configuration (overrides global defaults)')}
                </div>
              </div>
              <button onClick={() => setConfigTool(null)} style={{ background: 'none', border: 'none', fontSize: '18px', cursor: 'pointer', color: 'var(--text-secondary)' }}>✕</button>
            </div>

            {configTool.config_schema?.fields?.length > 0 ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                {(configTool.config_schema.fields as any[])
                  .filter((field: any) => {
                    if (!field.depends_on) return true;
                    return Object.entries(field.depends_on).every(([dependencyKey, dependencyValues]: [string, any]) =>
                      (dependencyValues as string[]).includes(configData[dependencyKey] ?? ''),
                    );
                  })
                  .map((field: any) => {
                    const userFromStore = useAuthStore.getState().user;
                    const currentUserRole = userFromStore?.role;
                    const isReadOnly = field.read_only_for_roles?.includes(currentUserRole);
                    return (
                      <div key={field.key}>
                        <label style={{ display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px' }}>
                          {field.label}
                          {isReadOnly && <span style={{ fontWeight: 400, color: 'var(--text-tertiary)', marginLeft: '4px' }}>(Admin only)</span>}
                        </label>
                        {field.type === 'password' ? (
                          <input
                            type="password"
                            autoComplete="new-password"
                            className="form-input"
                            value={configData[field.key] ?? ''}
                            placeholder={field.placeholder || 'Leave blank to use global default'}
                            onChange={(event) => setConfigData((previous) => ({ ...previous, [field.key]: event.target.value }))}
                          />
                        ) : field.type === 'select' ? (
                          <select className="form-input" value={configData[field.key] ?? field.default ?? ''} onChange={(event) => setConfigData((previous) => ({ ...previous, [field.key]: event.target.value }))}>
                            {(field.options || []).map((option: any) => (
                              <option key={option.value} value={option.value}>
                                {option.label}
                              </option>
                            ))}
                          </select>
                        ) : field.type === 'number' ? (
                          <input
                            type="number"
                            className="form-input"
                            value={configData[field.key] ?? field.default ?? ''}
                            placeholder={field.placeholder || ''}
                            min={field.min}
                            max={field.max}
                            onChange={(event) => setConfigData((previous) => ({ ...previous, [field.key]: event.target.value ? Number(event.target.value) : '' }))}
                          />
                        ) : (
                          <input
                            type="text"
                            className="form-input"
                            value={configData[field.key] ?? ''}
                            placeholder={field.placeholder || 'Leave blank to use global default'}
                            onChange={(event) => setConfigData((previous) => ({ ...previous, [field.key]: event.target.value }))}
                          />
                        )}
                      </div>
                    );
                  })}
              </div>
            ) : (
              <div>
                <label style={{ display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px' }}>Config JSON (Agent Override)</label>
                <textarea
                  className="form-input"
                  value={configJson}
                  onChange={(event) => setConfigJson(event.target.value)}
                  style={{ fontFamily: 'var(--font-mono)', fontSize: '12px', minHeight: '120px', resize: 'vertical' }}
                  placeholder="{}"
                />
              </div>
            )}

            <div style={{ display: 'flex', gap: '8px', marginTop: '16px', justifyContent: 'flex-end' }}>
              <button className="btn btn-secondary" onClick={() => setConfigTool(null)}>{t('common.cancel', 'Cancel')}</button>
              <button className="btn btn-primary" onClick={() => void saveConfig()} disabled={configSaving}>
                {configSaving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
