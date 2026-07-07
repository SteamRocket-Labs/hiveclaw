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
import { showAppToast } from '../../components/AppDialogs';
import ToolIcon from '../../components/ToolIcon';
import { useAuthStore } from '../../stores';
import './ToolsManager.css';

type ToolsManagerProps = {
  agentId: string;
  canManage?: boolean;
};

const STATUS_COLORS: Record<string, string> = {
  connected: 'var(--success)',
  available: 'var(--success)',
  needs_auth: 'var(--warning)',
  expired: 'var(--warning)',
  failed: 'var(--error)',
  error: 'var(--error)',
  disabled: 'var(--text-tertiary)',
};

const statusColor = (status?: string) => STATUS_COLORS[status || ''] || 'var(--text-tertiary)';

export function externalActivationComponentSummary(componentTypes?: Record<string, unknown> | null): string {
  return Object.entries(componentTypes ?? {})
    .filter(([, count]) => Number(count) > 0)
    .map(([componentType, count]) => `${componentType} ${Number(count)}`)
    .join(' · ');
}

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
      showAppToast(`Save failed: ${error}`, 'error');
    }
    setConfigSaving(false);
  };

  if (loading) {
    return <div className="tools-manager-loading">{t('common.loading')}</div>;
  }

  const mcpServers = extensions?.mcp_servers ?? [];
  const plugins = extensions?.plugins ?? [];
  const externalActivations = extensions?.external_activations ?? [];

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
    <label className={`tools-manager-toggle${disabled ? ' disabled' : ''}`}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        className="tools-manager-toggle-input"
      />
      <span className={`tools-manager-toggle-track${checked ? ' checked' : ''}`}>
        <span className="tools-manager-toggle-knob" />
      </span>
    </label>
  );

  const moduleTitle = (title: string, count: number) => (
    <div className="tools-manager-module-title">
      {title} ({count})
    </div>
  );

  return (
    <>
      <div className="tools-manager-sections">
        <section>
          {moduleTitle(t('agent.extensions.plugins', 'Plugins'), plugins.length)}
          {plugins.length > 0 ? (
            <div className="tools-manager-list">
              {plugins.map((plugin) => (
                <div key={plugin.plugin_key} className="card tools-manager-row">
                  <div className="tools-manager-min0">
                    <div className="tools-manager-row-head">
                      <span className="tools-manager-name">{plugin.plugin_key}</span>
                      <span className="tools-manager-tag">
                        {plugin.source_kind}
                      </span>
                    </div>
                    <div className="tools-manager-sub">
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
                    <span className={`tools-manager-onoff${plugin.enabled ? ' on' : ''}`}>
                      {plugin.enabled ? t('common.enabled', 'On') : t('common.disabled', 'Off')}
                    </span>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <div className="card tools-manager-empty">
              {t('agent.extensions.noPlugins', 'No plugins assigned')}
            </div>
          )}
        </section>

        <section>
          {moduleTitle(t('agent.extensions.externalSnapshots', 'Approved External Snapshots'), externalActivations.length)}
          {externalActivations.length > 0 ? (
            <div className="tools-manager-list">
              {externalActivations.map((activation) => {
                const componentSummary = externalActivationComponentSummary(activation.component_types);
                return (
                  <div key={activation.id} className="card tools-manager-row">
                    <div className="tools-manager-min0">
                      <div className="tools-manager-row-head">
                        <span className="tools-manager-name">
                          {activation.normalized_name || activation.snapshot_id}
                        </span>
                        <span className="tools-manager-tag">
                          {activation.source_format || 'external'}
                        </span>
                      </div>
                      <div className="tools-manager-sub">
                        {activation.status}
                        {componentSummary ? ` · ${componentSummary}` : ''}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="card tools-manager-empty">
              {t('agent.extensions.noExternalSnapshots', 'No approved external snapshots activated')}
            </div>
          )}
        </section>

        {/* ── MCP Servers module (Skills 已移至独立「技能」tab，工具 tab 管 MCP + plugins) ── */}
        <section>
          {moduleTitle(t('agent.extensions.mcpServers', 'MCP Servers'), mcpServers.length)}
          {mcpServers.length > 0 ? (
            <div className="tools-manager-list">
              {mcpServers.map((server) => {
                const advancedOpen = openAdvanced.has(server.id);
                const serverTools = serverToolsById[server.id] ?? [];
                const isLoadingServerTools = loadingServerTools.has(server.id);
                return (
                  <div key={server.id} className="card card-pad-none tools-manager-server-card">
                    <div className="tools-manager-server-head">
                      <div className="tools-manager-server-head-main">
                        <span className="tools-manager-plug-icon">🔌</span>
                        <div className="tools-manager-min0">
                          <div className="tools-manager-server-title-row">
                            <span className="tools-manager-name">{server.name}</span>
                            <span className="tools-manager-tag">MCP</span>
                          </div>
                          <div className="tools-manager-server-meta">
                            <span className="tools-manager-status-inline">
                              <span className="tools-manager-status-dot" style={{ background: statusColor(server.status) }} />
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
                        <span className={`tools-manager-onoff${server.enabled ? ' on' : ''}`}>
                          {server.enabled ? t('common.enabled', 'On') : t('common.disabled', 'Off')}
                        </span>
                      )}
                    </div>

                    {canManage && (
                      <div className="tools-manager-always-row">
                        <div className="tools-manager-min0">
                          <div className="tools-manager-always-title">
                            {t('agent.extensions.alwaysLoad', 'Load at start')}
                          </div>
                          <div className="tools-manager-sub">
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
                      <div className="tools-manager-advanced">
                        <button
                          onClick={() => toggleAdvanced(server.id)}
                          className="tools-manager-advanced-btn"
                        >
                          <span className={`tools-manager-chevron${advancedOpen ? ' open' : ''}`}>▶</span>
                          {t('agent.extensions.advancedToolControls', 'Advanced tool controls')} ({server.tool_count})
                        </button>
                        {advancedOpen && (
                          <div className="tools-manager-advanced-body">
                            {isLoadingServerTools ? (
                              <div className="tools-manager-advanced-note">
                                {t('common.loading', 'Loading...')}
                              </div>
                            ) : serverTools.length > 0 ? (
                              serverTools.map((serverTool) => {
                                const configTool = configToolForServerTool(server, serverTool);
                                const hasConfig = Boolean(configTool?.config_schema?.fields?.length) || Boolean(configTool);
                                const savingKey = `${server.id}:${serverTool.tool_name}`;
                                return (
                                  <div key={serverTool.tool_name} className="card tools-manager-tool-row">
                                    <div className="tools-manager-tool-main">
                                      <ToolIcon tool={configTool || { name: serverTool.tool_name, category: 'mcp' }} />
                                      <div className="tools-manager-min0">
                                        <div className="tools-manager-tool-name">{serverTool.display_name}</div>
                                        <div className="tools-manager-tool-desc">
                                          {configTool?.description || serverTool.tool_name}
                                        </div>
                                      </div>
                                    </div>
                                    <div className="tools-manager-tool-actions">
                                      {canManage && configTool && hasConfig && (
                                        <button
                                          onClick={() => openConfig(configTool)}
                                          className="tools-manager-config-btn"
                                          title={t('agent.extensions.configureTool', 'Configure tool')}
                                        >
                                          {t('enterprise.tools.configure', 'Configure')}
                                        </button>
                                      )}
                                      {canManage ? (
                                        <select
                                          className="form-input tools-manager-mode-select"
                                          value={serverTool.mode}
                                          disabled={!server.enabled || savingToolKey === savingKey}
                                          onChange={(event) => void setToolMode(server, serverTool, event.target.value as McpToolMode)}
                                          title={t('agent.extensions.toolPolicyTitle', 'Tool policy')}
                                        >
                                          <option value="auto">{t('agent.extensions.toolPolicy.auto', 'Auto')}</option>
                                          <option value="approval">{t('agent.extensions.toolPolicy.approval', 'Approval')}</option>
                                          <option value="deny">{t('agent.extensions.toolPolicy.deny', 'Deny')}</option>
                                        </select>
                                      ) : (
                                        <span className="tools-manager-mode-text">
                                          {t(`agent.extensions.toolPolicy.${serverTool.effective_mode}`, serverTool.effective_mode)}
                                        </span>
                                      )}
                                    </div>
                                  </div>
                                );
                              })
                            ) : (
                              <div className="tools-manager-advanced-note">
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
            <div className="card tools-manager-empty">
              {t('agent.extensions.noMcpServers', 'No MCP servers connected')}
            </div>
          )}
        </section>
      </div>

      {configTool && (
        <div
          className="ui-modal-overlay"
          onClick={() => setConfigTool(null)}
        >
          <div
            onClick={(event) => event.stopPropagation()}
            className="tools-manager-modal"
          >
            <div className="tools-manager-modal-head">
              <div>
                <h3 className="tools-manager-modal-title">{configTool.display_name}</h3>
                <div className="tools-manager-sub">
                  {t('agent.extensions.perAgentConfig', 'Per-agent configuration (overrides global defaults)')}
                </div>
              </div>
              <button onClick={() => setConfigTool(null)} className="tools-manager-modal-close">✕</button>
            </div>

            {configTool.config_schema?.fields?.length > 0 ? (
              <div className="tools-manager-fields">
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
                        <label className="tools-manager-field-label">
                          {field.label}
                          {isReadOnly && <span className="tools-manager-field-note">(Admin only)</span>}
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
                <label className="tools-manager-field-label">Config JSON (Agent Override)</label>
                <textarea
                  className="form-input tools-manager-json-input"
                  value={configJson}
                  onChange={(event) => setConfigJson(event.target.value)}
                  placeholder="{}"
                />
              </div>
            )}

            <div className="tools-manager-modal-footer">
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
