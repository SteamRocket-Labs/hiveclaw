import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  extensionsApi,
  type AgentExtensions,
  type AgentMcpServer,
  type AgentMcpServerTool,
  type McpToolMode,
} from '../../api/domains/extensions';
import { toolsApi, type AgentTool } from '../../api/domains/tools';
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
  const [tools, setTools] = useState<AgentTool[]>([]);
  const [extensions, setExtensions] = useState<AgentExtensions | null>(null);
  const [loading, setLoading] = useState(true);
  const [configTool, setConfigTool] = useState<any | null>(null);
  const [configData, setConfigData] = useState<Record<string, any>>({});
  const [configJson, setConfigJson] = useState('');
  const [configSaving, setConfigSaving] = useState(false);
  const [savingServerId, setSavingServerId] = useState<string | null>(null);
  const [savingPluginKey, setSavingPluginKey] = useState<string | null>(null);
  // MCP servers whose "Advanced tool controls" drawer is expanded (hidden by default).
  const [openAdvanced, setOpenAdvanced] = useState<Set<string>>(new Set());
  const [serverToolsById, setServerToolsById] = useState<Record<string, AgentMcpServerTool[]>>({});
  const [loadingServerTools, setLoadingServerTools] = useState<Set<string>>(new Set());
  const [savingToolKey, setSavingToolKey] = useState<string | null>(null);

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
    setOpenAdvanced(new Set());
    setServerToolsById({});
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
        always_load: server.always_load,
      });
    } catch (error) {
      console.error(error);
      await loadExtensions();
    }
    setSavingServerId(null);
  };

  const setServerAlwaysLoad = async (server: AgentMcpServer, alwaysLoad: boolean) => {
    setExtensions((prev) =>
      prev
        ? { ...prev, mcp_servers: prev.mcp_servers.map((s) => (s.id === server.id ? { ...s, always_load: alwaysLoad } : s)) }
        : prev,
    );
    setSavingServerId(server.id);
    try {
      await extensionsApi.setAgentMcpAssignment(agentId, server.id, {
        enabled: server.enabled,
        default_tool_mode: server.default_tool_mode,
        always_load: alwaysLoad,
      });
    } catch (error) {
      console.error(error);
      await loadExtensions();
    } finally {
      setSavingServerId(null);
    }
  };

  const loadServerTools = async (serverId: string) => {
    setLoadingServerTools((prev) => new Set(prev).add(serverId));
    try {
      const data = await extensionsApi.getAgentMcpServerTools(agentId, serverId);
      setServerToolsById((prev) => ({ ...prev, [serverId]: data }));
    } catch (error) {
      console.error(error);
      setServerToolsById((prev) => ({ ...prev, [serverId]: [] }));
    } finally {
      setLoadingServerTools((prev) => {
        const next = new Set(prev);
        next.delete(serverId);
        return next;
      });
    }
  };

  const setToolMode = async (server: AgentMcpServer, tool: AgentMcpServerTool, mode: McpToolMode) => {
    const key = `${server.id}:${tool.tool_name}`;
    setSavingToolKey(key);
    setServerToolsById((prev) => ({
      ...prev,
      [server.id]: (prev[server.id] ?? []).map((item) =>
        item.tool_name === tool.tool_name ? { ...item, mode, effective_mode: mode } : item,
      ),
    }));
    try {
      const updated = await extensionsApi.setAgentMcpToolPolicy(agentId, server.id, tool.tool_name, { mode });
      setServerToolsById((prev) => ({
        ...prev,
        [server.id]: (prev[server.id] ?? []).map((item) =>
          item.tool_name === updated.tool_name ? updated : item,
        ),
      }));
    } catch (error) {
      console.error(error);
      await loadServerTools(server.id);
    } finally {
      setSavingToolKey(null);
    }
  };

  const togglePlugin = async (pluginKey: string, enabled: boolean) => {
    setExtensions((prev) =>
      prev
        ? {
            ...prev,
            plugins: (prev.plugins ?? []).map((plugin) =>
              plugin.plugin_key === pluginKey ? { ...plugin, enabled } : plugin,
            ),
          }
        : prev,
    );
    setSavingPluginKey(pluginKey);
    try {
      await extensionsApi.setAgentPluginAssignment(agentId, pluginKey, { enabled });
    } catch (error) {
      console.error(error);
      await loadExtensions();
    } finally {
      setSavingPluginKey(null);
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

  const mcpServers = extensions?.mcp_servers ?? [];
  const plugins = extensions?.plugins ?? [];

  const configToolForServerTool = (server: AgentMcpServer, serverTool: AgentMcpServerTool) =>
    tools.find((tool) => {
      if (serverTool.tool_id && (tool.id === serverTool.tool_id || tool.tool_id === serverTool.tool_id)) return true;
      if (tool.type !== 'mcp') return false;
      return (
        (tool.mcp_server_name || '') === server.name &&
        (tool.mcp_tool_name || tool.name) === serverTool.tool_name
      );
    });

  const toggleAdvanced = (serverId: string) => {
    const shouldOpen = !openAdvanced.has(serverId);
    setOpenAdvanced((prev) => {
      const next = new Set(prev);
      if (next.has(serverId)) next.delete(serverId);
      else next.add(serverId);
      return next;
    });
    if (shouldOpen && !serverToolsById[serverId]) {
      void loadServerTools(serverId);
    }
  };

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
        <section>
          {moduleTitle(t('agent.extensions.plugins', 'Plugins'), plugins.length)}
          {plugins.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {plugins.map((plugin) => (
                <div key={plugin.plugin_key} className="card" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '12px 14px' }}>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <span style={{ fontWeight: 600, fontSize: '13px' }}>{plugin.plugin_key}</span>
                      <span style={{ fontSize: '10px', background: 'var(--primary)', color: '#fff', borderRadius: '4px', padding: '1px 5px' }}>
                        {plugin.source_kind}
                      </span>
                    </div>
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                      {plugin.version} · {plugin.status}
                    </div>
                  </div>
                  {canManage ? (
                    <Toggle
                      checked={Boolean(plugin.enabled)}
                      disabled={savingPluginKey === plugin.plugin_key}
                      onChange={(next) => void togglePlugin(plugin.plugin_key, next)}
                    />
                  ) : (
                    <span style={{ fontSize: '11px', color: plugin.enabled ? '#22c55e' : 'var(--text-tertiary)', fontWeight: 500 }}>
                      {plugin.enabled ? t('common.enabled', 'On') : t('common.disabled', 'Off')}
                    </span>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <div className="card" style={{ textAlign: 'center', padding: '24px', color: 'var(--text-tertiary)', fontSize: '13px' }}>
              {t('agent.extensions.noPlugins', 'No plugins assigned')}
            </div>
          )}
        </section>

        {/* ── MCP Servers module (Skills 已移至独立「技能」tab，工具 tab 管 MCP + plugins) ── */}
        <section>
          {moduleTitle(t('agent.extensions.mcpServers', 'MCP Servers'), mcpServers.length)}
          {mcpServers.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {mcpServers.map((server) => {
                const advancedOpen = openAdvanced.has(server.id);
                const serverTools = serverToolsById[server.id] ?? [];
                const isLoadingServerTools = loadingServerTools.has(server.id);
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

                    {canManage && (
                      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px', alignItems: 'center', borderTop: '1px solid var(--border-subtle)', padding: '10px 14px' }}>
                        <div style={{ minWidth: 0 }}>
                          <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)' }}>
                            {t('agent.extensions.alwaysLoad', 'Load at start')}
                          </div>
                          <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                            {t('agent.extensions.alwaysLoadHint', 'Keep this server’s approved tools in the first tool surface.')}
                          </div>
                        </div>
                        <Toggle
                          checked={Boolean(server.always_load)}
                          disabled={!server.enabled || savingServerId === server.id}
                          onChange={(next) => void setServerAlwaysLoad(server, next)}
                        />
                      </div>
                    )}

                    {server.tool_count > 0 && (
                      <div style={{ borderTop: '1px solid var(--border-subtle)' }}>
                        <button
                          onClick={() => toggleAdvanced(server.id)}
                          style={{ display: 'flex', alignItems: 'center', gap: '6px', width: '100%', background: 'none', border: 'none', padding: '8px 14px', cursor: 'pointer', color: 'var(--text-tertiary)', fontSize: '11px', textAlign: 'left' }}
                        >
                          <span style={{ transition: 'transform 0.15s', transform: advancedOpen ? 'rotate(90deg)' : 'rotate(0deg)' }}>▶</span>
                          {t('agent.extensions.advancedToolControls', 'Advanced tool controls')} ({server.tool_count})
                        </button>
                        {advancedOpen && (
                          <div style={{ padding: '0 14px 12px 14px', display: 'flex', flexDirection: 'column', gap: '4px', background: 'var(--bg-secondary)' }}>
                            {isLoadingServerTools ? (
                              <div style={{ padding: '10px 12px', color: 'var(--text-tertiary)', fontSize: '12px' }}>
                                {t('common.loading', 'Loading...')}
                              </div>
                            ) : serverTools.length > 0 ? (
                              serverTools.map((serverTool) => {
                                const configTool = configToolForServerTool(server, serverTool);
                                const hasConfig = Boolean(configTool?.config_schema?.fields?.length) || Boolean(configTool);
                                const savingKey = `${server.id}:${serverTool.tool_name}`;
                                return (
                                  <div key={serverTool.tool_name} className="card" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '8px 12px' }}>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
                                      <ToolIcon tool={configTool || { name: serverTool.tool_name, category: 'mcp' }} />
                                      <div style={{ minWidth: 0 }}>
                                        <div style={{ fontWeight: 500, fontSize: '12px' }}>{serverTool.display_name}</div>
                                        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                          {configTool?.description || serverTool.tool_name}
                                        </div>
                                      </div>
                                    </div>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 }}>
                                      {canManage && configTool && hasConfig && (
                                        <button
                                          onClick={() => openConfig(configTool)}
                                          style={{ background: 'none', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '3px 8px', fontSize: '11px', cursor: 'pointer', color: 'var(--text-secondary)' }}
                                          title={t('agent.extensions.configureTool', 'Configure tool')}
                                        >
                                          {t('enterprise.tools.configure', 'Configure')}
                                        </button>
                                      )}
                                      {canManage ? (
                                        <select
                                          className="form-input"
                                          value={serverTool.mode}
                                          disabled={!server.enabled || savingToolKey === savingKey}
                                          onChange={(event) => void setToolMode(server, serverTool, event.target.value as McpToolMode)}
                                          style={{ width: '128px', minHeight: '28px', padding: '3px 8px', fontSize: '11px' }}
                                          title={t('agent.extensions.toolPolicyTitle', 'Tool policy')}
                                        >
                                          <option value="auto">{t('agent.extensions.toolPolicy.auto', 'Auto')}</option>
                                          <option value="approval">{t('agent.extensions.toolPolicy.approval', 'Approval')}</option>
                                          <option value="deny">{t('agent.extensions.toolPolicy.deny', 'Deny')}</option>
                                        </select>
                                      ) : (
                                        <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', fontWeight: 500 }}>
                                          {t(`agent.extensions.toolPolicy.${serverTool.effective_mode}`, serverTool.effective_mode)}
                                        </span>
                                      )}
                                    </div>
                                  </div>
                                );
                              })
                            ) : (
                              <div style={{ padding: '10px 12px', color: 'var(--text-tertiary)', fontSize: '12px' }}>
                                {t('agent.extensions.noMcpServerTools', 'No server tools found')}
                              </div>
                            )}
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
