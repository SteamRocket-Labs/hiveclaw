import { useEffect, useState } from 'react';

import { useTranslation } from 'react-i18next';

import { customApiConnectorsApi, type CustomApiConnector } from '../../api/domains/customApiConnectors';
import { extensionsApi, type InstalledPlugin, type McpServerRecord } from '../../api/domains/extensions';
import { toolsApi } from '../../api/domains/tools';
import { requestAppConfirm, showAppToast } from '../../components/AppDialogs';
import ToolIcon from '../../components/ToolIcon';

const MCP_STATUS_COLORS: Record<string, string> = {
  connected: '#22c55e',
  needs_auth: '#f59e0b',
  expired: '#f59e0b',
  failed: '#ef4444',
  error: '#ef4444',
  disabled: 'var(--text-tertiary)',
};

interface WorkspaceToolsSectionProps {
  selectedTenantId: string;
}

const GLOBAL_CATEGORY_CONFIG_SCHEMAS: Record<string, { title: string; fields: any[] }> = {
  agentbay: {
    title: 'AgentBay Settings',
    fields: [
      { key: 'api_key', label: 'API Key (from AgentBay)', type: 'password', placeholder: 'Enter your AgentBay API key' },
    ],
  },
};

type ToolConfigField = {
  key: string;
  label?: string;
  placeholder?: string;
  description?: string;
  [key: string]: unknown;
};

export function normalizeToolConfigListValue(value: unknown, options: { preserveEmpty?: boolean } = {}): string[] {
  let parts: string[];
  if (Array.isArray(value)) {
    parts = value.map((item) => String(item).trim());
  } else if (typeof value === 'string') {
    parts = value.split(/[\n,]+/).map((item) => item.trim());
  } else {
    parts = [];
  }
  return options.preserveEmpty ? parts : parts.filter(Boolean);
}

export function countToolConfigListValues(value: unknown): number {
  return normalizeToolConfigListValue(value).length;
}

function joinToolConfigListRows(rows: string[]): string {
  return rows.join('\n');
}

export function ToolConfigSecretListField({
  field,
  value,
  onChange,
}: {
  field: ToolConfigField;
  value: unknown;
  onChange: (nextValue: string) => void;
}) {
  const rows = normalizeToolConfigListValue(value, { preserveEmpty: true });
  const visibleRows = rows.length > 0 ? rows : [''];
  const count = countToolConfigListValues(visibleRows);
  const label = field.label || field.key;
  const placeholder = field.placeholder || 'Enter one key per row';

  const updateRows = (nextRows: string[]) => {
    onChange(joinToolConfigListRows(nextRows));
  };

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px', marginBottom: '6px' }}>
        <label style={{ display: 'block', fontSize: '12px', fontWeight: 500 }}>{label}</label>
        <span style={{ fontSize: '11px', color: 'var(--text-secondary)', background: 'var(--bg-primary)', border: '1px solid var(--border-color)', borderRadius: '999px', padding: '2px 8px', whiteSpace: 'nowrap' }}>
          {count} {count === 1 ? 'key' : 'keys'} configured
        </span>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        {visibleRows.map((row, index) => (
          <div key={`${field.key}-${index}`} style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <input
              type="password"
              autoComplete="new-password"
              className="form-input"
              value={row}
              placeholder={index === 0 ? placeholder : `API key #${index + 1}`}
              aria-label={`${label} #${index + 1}`}
              onChange={(event) => {
                const nextRows = [...visibleRows];
                nextRows[index] = event.target.value;
                updateRows(nextRows);
              }}
            />
            <button
              type="button"
              className="btn btn-secondary"
              style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}
              onClick={() => {
                const nextRows = visibleRows.filter((_, rowIndex) => rowIndex !== index);
                updateRows(nextRows.length > 0 ? nextRows : ['']);
              }}
            >
              Remove
            </button>
          </div>
        ))}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px', marginTop: '8px' }}>
        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
          Enter one API key per row. Calls rotate across saved keys in order.
          {field.description ? ` ${field.description}` : ''}
        </div>
        <button
          type="button"
          className="btn btn-secondary"
          style={{ padding: '6px 10px', whiteSpace: 'nowrap' }}
          onClick={() => updateRows([...visibleRows, ''])}
        >
          Add key
        </button>
      </div>
    </div>
  );
}

export default function WorkspaceToolsSection({
  selectedTenantId,
}: WorkspaceToolsSectionProps) {
  const { t } = useTranslation();
  const categoryLabels: Record<string, string> = {
    file: t('agent.toolCategories.file', 'File'),
    filesystem: t('agent.toolCategories.filesystem', 'Filesystem'),
    task: t('agent.toolCategories.task', 'Task'),
    tasks: t('agent.toolCategories.tasks', 'Tasks'),
    communication: t('agent.toolCategories.communication', 'Communication'),
    search: t('agent.toolCategories.search', 'Search'),
    aware: t('agent.toolCategories.aware', 'Aware & Triggers'),
    triggers: t('agent.toolCategories.triggers', 'Triggers'),
    skills: t('agent.toolCategories.skills', 'Skills'),
    memory: t('agent.toolCategories.memory', 'Memory'),
    hr: t('agent.toolCategories.hr', 'HR'),
    mcp: t('agent.toolCategories.mcp', 'MCP'),
    plaza: t('agent.toolCategories.plaza', 'Agent Circle'),
    office_pack: t('agent.toolCategories.office_pack', 'Office'),
    social: t('agent.toolCategories.social', 'Social'),
    code: t('agent.toolCategories.code', 'Code & Execution'),
    discovery: t('agent.toolCategories.discovery', 'Discovery'),
    email: t('agent.toolCategories.email', 'Email'),
    feishu: t('agent.toolCategories.feishu', 'Feishu / Lark'),
    custom: t('agent.toolCategories.custom', 'Custom'),
    general: t('agent.toolCategories.general', 'General'),
    agentbay: t('agent.toolCategories.agentbay', 'AgentBay'),
  };

  const [allTools, setAllTools] = useState<any[]>([]);
  const [showAddMCP, setShowAddMCP] = useState(false);
  const [mcpForm, setMcpForm] = useState({ server_url: '', server_name: '' });
  const [mcpRawInput, setMcpRawInput] = useState('');
  const [mcpTestResult, setMcpTestResult] = useState<any>(null);
  const [mcpTesting, setMcpTesting] = useState(false);
  const [editingToolId, setEditingToolId] = useState<string | null>(null);
  const [editingConfig, setEditingConfig] = useState<Record<string, any>>({});
  const [configCategory, setConfigCategory] = useState<string | null>(null);
  const [toolsView, setToolsView] = useState<'global' | 'mcp-servers' | 'custom-api' | 'plugins' | 'agent-installed'>('global');
  const [agentInstalledTools, setAgentInstalledTools] = useState<any[]>([]);
  const [collapsedServers, setCollapsedServers] = useState<Set<string>>(new Set());
  const [mcpServers, setMcpServers] = useState<McpServerRecord[]>([]);
  const [mcpServersLoaded, setMcpServersLoaded] = useState(false);
  const [plugins, setPlugins] = useState<InstalledPlugin[]>([]);
  const [pluginsLoaded, setPluginsLoaded] = useState(false);
  const [pluginKeyInput, setPluginKeyInput] = useState('');
  const [pluginBusy, setPluginBusy] = useState<string | null>(null);
  const [customApis, setCustomApis] = useState<CustomApiConnector[]>([]);
  const [customApisLoaded, setCustomApisLoaded] = useState(false);
  const [customApiBusy, setCustomApiBusy] = useState<string | null>(null);
  const [customApiTestResult, setCustomApiTestResult] = useState<Record<string, string>>({});
  const [customApiForm, setCustomApiForm] = useState({
    connector_name: '',
    action_name: '',
    description: '',
    base_url: '',
    method: 'GET',
    path: '/',
    auth_scheme: 'api_key',
    auth_location: 'header',
    auth_name: 'X-API-Key',
    secret_value: '',
    parameters_schema: '{\n  "type": "object",\n  "properties": {}\n}',
    headers: '{}',
    query: '{}',
    body_template: '',
    test_arguments: '{}',
    is_default: false,
  });

  const loadMcpServers = async () => {
    try {
      const data = await extensionsApi.listEnterpriseMcpServers();
      setMcpServers(data);
    } catch {
      setMcpServers([]);
    }
    setMcpServersLoaded(true);
  };

  const loadPlugins = async () => {
    try {
      const data = await extensionsApi.listEnterprisePlugins();
      setPlugins(data);
    } catch {
      setPlugins([]);
    }
    setPluginsLoaded(true);
  };

  const loadCustomApis = async () => {
    try {
      const data = await customApiConnectorsApi.list();
      setCustomApis(data);
    } catch {
      setCustomApis([]);
    }
    setCustomApisLoaded(true);
  };

  const parseJsonField = (value: string, fallback: unknown) => {
    const trimmed = value.trim();
    if (!trimmed) return fallback;
    return JSON.parse(trimmed);
  };

  const loadAllTools = async () => {
    const data = await toolsApi.listCatalog(selectedTenantId || undefined);
    setAllTools(data);
  };

  const loadAgentInstalledTools = async () => {
    try {
      const data = await toolsApi.listAgentInstalled(selectedTenantId || undefined);
      setAgentInstalledTools(data);
    } catch {
      setAgentInstalledTools([]);
    }
  };

  useEffect(() => {
    loadAllTools();
    loadAgentInstalledTools();
  }, [selectedTenantId]);

  return (
    <div>
      <div style={{ display: 'flex', gap: '8px', marginBottom: '16px', borderBottom: '1px solid var(--border-subtle)', paddingBottom: '8px' }}>
        {([
          ['global', t('enterprise.tools.globalTools', 'Global Tools')],
          ['mcp-servers', t('agent.extensions.mcpServers', 'MCP Servers')],
          ['custom-api', t('enterprise.tools.customApis', 'Custom APIs')],
          ['plugins', t('agent.extensions.plugins', 'Plugins')],
          ['agent-installed', t('enterprise.tools.agentInstalled', 'Agent Installed')],
        ] as const).map(([key, label]) => (
          <button
            key={key}
            onClick={() => {
              setToolsView(key);
              if (key === 'agent-installed') {
                loadAgentInstalledTools();
              } else if (key === 'mcp-servers') {
                loadMcpServers();
              } else if (key === 'plugins') {
                loadPlugins();
              } else if (key === 'custom-api') {
                loadCustomApis();
              }
            }}
            style={{
              padding: '4px 14px',
              borderRadius: '12px',
              fontSize: '12px',
              fontWeight: 500,
              cursor: 'pointer',
              border: 'none',
              background: toolsView === key ? 'var(--accent-primary)' : 'var(--bg-tertiary)',
              color: toolsView === key ? '#fff' : 'var(--text-secondary)',
              transition: 'all 0.15s',
            }}
          >
            {label}
          </button>
        ))}
      </div>

      {toolsView === 'agent-installed' ? (
        <div>
          <p style={{ fontSize: '13px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
            {t('enterprise.tools.agentInstalledHint', 'These tools are installed directly by agents.')}
          </p>
          {agentInstalledTools.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>
              {t('enterprise.tools.noAgentInstalledTools', 'No agent-installed tools')}
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              {agentInstalledTools.map((row) => (
                <div key={row.agent_tool_id} className="card" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 16px' }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <span style={{ fontWeight: 500, fontSize: '13px' }}>🔌 {row.tool_display_name}</span>
                      {row.mcp_server_name ? (
                        <span style={{ fontSize: '10px', background: 'var(--primary)', color: '#fff', borderRadius: '4px', padding: '1px 5px' }}>MCP</span>
                      ) : null}
                    </div>
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                      🤖 {row.installed_by_agent_name || 'Unknown Agent'}
                      {row.installed_at ? <span> · {new Date(row.installed_at).toLocaleString()}</span> : null}
                    </div>
                  </div>
                  <button
                    className="btn btn-ghost"
                    style={{ color: 'var(--error)', fontSize: '12px' }}
                    onClick={async () => {
                      const confirmed = await requestAppConfirm({
                        title: t('common.delete', 'Delete'),
                        message: t('enterprise.tools.removeFromAgent', { name: row.tool_display_name }),
                        confirmLabel: t('common.delete', 'Delete'),
                        danger: true,
                      });
                      if (!confirmed) return;
                      try {
                        await toolsApi.removeAgentTool(row.agent_tool_id);
                      } catch {
                        // Ignore already removed tools and just refresh.
                      }
                      loadAgentInstalledTools();
                    }}
                  >
                    🗑️ {t('enterprise.tools.delete', 'Delete')}
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      ) : null}

      {toolsView === 'mcp-servers' ? (
        <div>
          <p style={{ fontSize: '13px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
            {t('enterprise.tools.mcpServersHint', 'External MCP integrations managed as server-level connectors. Each server may expose many tools internally.')}
          </p>
          {!mcpServersLoaded ? (
            <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>{t('common.loading', 'Loading...')}</div>
          ) : mcpServers.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>
              {t('enterprise.tools.noMcpServers', 'No MCP servers yet. Add one from Global Tools.')}
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {mcpServers.map((server) => (
                <div key={server.id} className="card" style={{ padding: '12px 16px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px', minWidth: 0 }}>
                      <span style={{ fontSize: '18px' }}>🔌</span>
                      <div style={{ minWidth: 0 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                          <span style={{ fontWeight: 600, fontSize: '13px' }}>{server.name}</span>
                          <span style={{ fontSize: '10px', background: 'var(--primary)', color: '#fff', borderRadius: '4px', padding: '1px 5px' }}>MCP</span>
                          <span style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>{server.transport}</span>
                        </div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                          <span style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                            <span style={{ width: '7px', height: '7px', borderRadius: '50%', background: MCP_STATUS_COLORS[server.status] || 'var(--text-tertiary)' }} />
                            {t(`agent.extensions.status.${server.status}`, server.status)}
                          </span>
                          <span>·</span>
                          <span>{t('enterprise.tools.authStatus', { status: t(`agent.extensions.authStatus.${server.auth_status}`, server.auth_status), defaultValue: 'auth: {{status}}' })}</span>
                          <span>·</span>
                          <span>{t('agent.extensions.toolCount', { count: server.tool_count, defaultValue: '{{count}} tools' })}</span>
                        </div>
                      </div>
                    </div>
                    <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', flexShrink: 0 }}>
                      {t('enterprise.tools.usedByAgents', { count: server.agent_count, defaultValue: '{{count}} agents' })}
                    </span>
                  </div>
                  {server.agents.length > 0 ? (
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginTop: '10px', paddingTop: '10px', borderTop: '1px solid var(--border-subtle)' }}>
                      {server.agents.map((agent) => (
                        <span
                          key={agent.id}
                          style={{
                            fontSize: '11px',
                            padding: '2px 8px',
                            borderRadius: '10px',
                            background: agent.enabled ? 'rgba(34,197,94,0.12)' : 'var(--bg-tertiary)',
                            color: agent.enabled ? 'var(--success)' : 'var(--text-tertiary)',
                          }}
                        >
                          {agent.name}
                        </span>
                      ))}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </div>
      ) : null}

      {toolsView === 'plugins' ? (
        <div>
          <p style={{ fontSize: '13px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
            {t('enterprise.tools.pluginsHint', 'Tenant-installed plugins compose tools, skills, MCP servers, agents, hooks, and dependencies. Agent visibility is controlled per agent.')}
          </p>
          <div className="card" style={{ display: 'flex', gap: '8px', alignItems: 'center', padding: '12px 16px', marginBottom: '12px' }}>
            <input
              className="form-input"
              value={pluginKeyInput}
              onChange={(event) => setPluginKeyInput(event.target.value)}
              placeholder={t('enterprise.tools.pluginKeyPlaceholder', 'plugin key, e.g. web_pack')}
              style={{ flex: 1, minWidth: 0 }}
            />
            <button
              className="btn btn-primary"
              disabled={!pluginKeyInput.trim() || pluginBusy === 'install'}
              onClick={async () => {
                const key = pluginKeyInput.trim();
                if (!key) return;
                setPluginBusy('install');
                try {
                  await extensionsApi.installEnterprisePlugin({ plugin_key: key });
                  setPluginKeyInput('');
                  await loadPlugins();
                } finally {
                  setPluginBusy(null);
                }
              }}
            >
              {t('enterprise.tools.installPlugin', 'Install')}
            </button>
            <button
              className="btn btn-ghost"
              disabled={pluginBusy === 'backfill'}
              onClick={async () => {
                setPluginBusy('backfill');
                try {
                  await extensionsApi.backfillEnterprisePlugins();
                  await loadPlugins();
                } finally {
                  setPluginBusy(null);
                }
              }}
            >
              {t('enterprise.tools.backfillPlugins', 'Backfill')}
            </button>
          </div>
          {!pluginsLoaded ? (
            <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>{t('common.loading', 'Loading...')}</div>
          ) : plugins.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>
              {t('enterprise.tools.noPlugins', 'No plugins installed')}
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {plugins.map((plugin) => (
                <div key={plugin.id} className="card" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '12px 16px' }}>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <span style={{ fontWeight: 600, fontSize: '13px' }}>{plugin.plugin_key}</span>
                      <span style={{ fontSize: '10px', background: 'var(--primary)', color: '#fff', borderRadius: '4px', padding: '1px 5px' }}>
                        {plugin.source_kind}
                      </span>
                      <span style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>{plugin.status}</span>
                    </div>
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                      {plugin.version}
                    </div>
                  </div>
                  <button
                    className="btn btn-ghost"
                    style={{ color: 'var(--error)', fontSize: '12px' }}
                    disabled={pluginBusy === plugin.plugin_key}
                    onClick={async () => {
                      const confirmed = await requestAppConfirm({
                        title: t('enterprise.tools.uninstallPlugin', 'Uninstall plugin'),
                        message: t('enterprise.tools.uninstallPluginConfirm', { name: plugin.plugin_key, defaultValue: `Uninstall ${plugin.plugin_key}?` }),
                        confirmLabel: t('common.delete', 'Delete'),
                        danger: true,
                      });
                      if (!confirmed) return;
                      setPluginBusy(plugin.plugin_key);
                      try {
                        await extensionsApi.uninstallEnterprisePlugin({ plugin_key: plugin.plugin_key });
                        await loadPlugins();
                      } finally {
                        setPluginBusy(null);
                      }
                    }}
                  >
                    {t('enterprise.tools.uninstallPlugin', 'Uninstall')}
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      ) : null}

      {toolsView === 'custom-api' ? (
        <div>
          <p style={{ fontSize: '13px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
            {t('enterprise.tools.customApiHint', 'Tenant-governed HTTP API actions. Credentials are stored server-side and are never exposed to agents.')}
          </p>
          <div className="card" style={{ padding: '14px 16px', marginBottom: '12px' }}>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: '10px' }}>
              <input className="form-input" value={customApiForm.connector_name} onChange={(event) => setCustomApiForm({ ...customApiForm, connector_name: event.target.value })} placeholder={t('enterprise.tools.connectorName', 'Connector name')} />
              <input className="form-input" value={customApiForm.action_name} onChange={(event) => setCustomApiForm({ ...customApiForm, action_name: event.target.value })} placeholder={t('enterprise.tools.actionName', 'Action name')} />
              <input className="form-input" value={customApiForm.base_url} onChange={(event) => setCustomApiForm({ ...customApiForm, base_url: event.target.value })} placeholder="https://api.example.com" />
              <div style={{ display: 'grid', gridTemplateColumns: '100px 1fr', gap: '8px' }}>
                <select className="form-input" value={customApiForm.method} onChange={(event) => setCustomApiForm({ ...customApiForm, method: event.target.value })}>
                  {['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((method) => <option key={method} value={method}>{method}</option>)}
                </select>
                <input className="form-input" value={customApiForm.path} onChange={(event) => setCustomApiForm({ ...customApiForm, path: event.target.value })} placeholder="/v1/action/{id}" />
              </div>
              <select className="form-input" value={customApiForm.auth_scheme} onChange={(event) => setCustomApiForm({ ...customApiForm, auth_scheme: event.target.value })}>
                <option value="none">{t('enterprise.tools.authNone', 'No auth')}</option>
                <option value="api_key">{t('enterprise.tools.authApiKey', 'API key')}</option>
                <option value="bearer">{t('enterprise.tools.authBearer', 'Bearer token')}</option>
                <option value="basic">{t('enterprise.tools.authBasic', 'Basic auth')}</option>
              </select>
              <div style={{ display: 'grid', gridTemplateColumns: '100px 1fr', gap: '8px' }}>
                <select className="form-input" value={customApiForm.auth_location} onChange={(event) => setCustomApiForm({ ...customApiForm, auth_location: event.target.value })}>
                  <option value="header">{t('enterprise.tools.header', 'Header')}</option>
                  <option value="query">{t('enterprise.tools.query', 'Query')}</option>
                </select>
                <input className="form-input" value={customApiForm.auth_name} onChange={(event) => setCustomApiForm({ ...customApiForm, auth_name: event.target.value })} placeholder="X-API-Key" />
              </div>
              <input className="form-input" type="password" value={customApiForm.secret_value} onChange={(event) => setCustomApiForm({ ...customApiForm, secret_value: event.target.value })} placeholder={t('enterprise.tools.secretValue', 'Credential value')} />
              <input className="form-input" value={customApiForm.description} onChange={(event) => setCustomApiForm({ ...customApiForm, description: event.target.value })} placeholder={t('enterprise.tools.description', 'Description')} />
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: '10px', marginTop: '10px' }}>
              <textarea className="form-input" value={customApiForm.parameters_schema} onChange={(event) => setCustomApiForm({ ...customApiForm, parameters_schema: event.target.value })} rows={5} placeholder="parameters_schema JSON" />
              <textarea className="form-input" value={customApiForm.body_template} onChange={(event) => setCustomApiForm({ ...customApiForm, body_template: event.target.value })} rows={5} placeholder={t('enterprise.tools.bodyTemplateJson', 'Body template JSON, optional')} />
              <textarea className="form-input" value={customApiForm.headers} onChange={(event) => setCustomApiForm({ ...customApiForm, headers: event.target.value })} rows={3} placeholder={t('enterprise.tools.headersJson', 'Headers JSON')} />
              <textarea className="form-input" value={customApiForm.query} onChange={(event) => setCustomApiForm({ ...customApiForm, query: event.target.value })} rows={3} placeholder={t('enterprise.tools.queryJson', 'Query JSON')} />
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '10px' }}>
              <label style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '12px', color: 'var(--text-secondary)' }}>
                <input type="checkbox" checked={customApiForm.is_default} onChange={(event) => setCustomApiForm({ ...customApiForm, is_default: event.target.checked })} />
                {t('enterprise.tools.enableForAllAgents', 'Enable for all agents')}
              </label>
              <button
                className="btn btn-primary"
                disabled={!customApiForm.connector_name.trim() || !customApiForm.action_name.trim() || !customApiForm.base_url.trim() || customApiBusy === 'create'}
                onClick={async () => {
                  setCustomApiBusy('create');
                  try {
                    await customApiConnectorsApi.create({
                      connector_name: customApiForm.connector_name,
                      action_name: customApiForm.action_name,
                      description: customApiForm.description,
                      base_url: customApiForm.base_url,
                      method: customApiForm.method,
                      path: customApiForm.path,
                      auth_scheme: customApiForm.auth_scheme,
                      auth_location: customApiForm.auth_location,
                      auth_name: customApiForm.auth_name || null,
                      secret_value: customApiForm.secret_value || null,
                      parameters_schema: parseJsonField(customApiForm.parameters_schema, { type: 'object', properties: {} }) as Record<string, unknown>,
                      headers: parseJsonField(customApiForm.headers, {}) as Record<string, unknown>,
                      query: parseJsonField(customApiForm.query, {}) as Record<string, unknown>,
                      body_template: parseJsonField(customApiForm.body_template, null),
                      is_default: customApiForm.is_default,
                      enabled: true,
                    });
                    setCustomApiForm({ ...customApiForm, action_name: '', description: '', path: '/', secret_value: '', body_template: '', test_arguments: '{}' });
                    await loadCustomApis();
                    await loadAllTools();
                  } finally {
                    setCustomApiBusy(null);
                  }
                }}
              >
                {t('enterprise.tools.createConnector', 'Create Connector')}
              </button>
            </div>
          </div>
          {!customApisLoaded ? (
            <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>{t('common.loading', 'Loading...')}</div>
          ) : customApis.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>{t('enterprise.tools.noCustomApis', 'No custom API connectors')}</div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {customApis.map((connector) => (
                <div key={connector.id} className="card" style={{ padding: '12px 16px' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px' }}>
                    <div style={{ minWidth: 0 }}>
                      <div style={{ fontWeight: 600, fontSize: '13px' }}>{connector.display_name}</div>
                      <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{connector.name}</div>
                      {connector.description ? <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '6px' }}>{connector.description}</div> : null}
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 }}>
                      <span style={{ fontSize: '11px', color: connector.enabled ? 'var(--success)' : 'var(--text-tertiary)' }}>{connector.enabled ? t('enterprise.tools.enabled', 'Enabled') : t('enterprise.tools.disabled', 'Disabled')}</span>
                      <button
                        className="btn btn-ghost"
                        style={{ color: 'var(--error)', fontSize: '12px' }}
                        disabled={customApiBusy === connector.id}
                        onClick={async () => {
                          const confirmed = await requestAppConfirm({
                            title: t('enterprise.tools.deleteConnector', 'Delete connector'),
                            message: t('enterprise.tools.deleteConnectorConfirm', { name: connector.display_name, defaultValue: `Delete ${connector.display_name}?` }),
                            confirmLabel: t('common.delete', 'Delete'),
                            danger: true,
                          });
                          if (!confirmed) return;
                          setCustomApiBusy(connector.id);
                          try {
                            await customApiConnectorsApi.delete(connector.id);
                            await loadCustomApis();
                            await loadAllTools();
                          } finally {
                            setCustomApiBusy(null);
                          }
                        }}
                      >
                        {t('enterprise.tools.delete', 'Delete')}
                      </button>
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: '8px', marginTop: '10px' }}>
                    <input className="form-input" value={customApiForm.test_arguments} onChange={(event) => setCustomApiForm({ ...customApiForm, test_arguments: event.target.value })} placeholder={t('enterprise.tools.testArgumentsJson', 'Test arguments JSON')} />
                    <button
                      className="btn btn-ghost"
                      disabled={customApiBusy === `test:${connector.id}`}
                      onClick={async () => {
                        setCustomApiBusy(`test:${connector.id}`);
                        try {
                          const result = await customApiConnectorsApi.test(connector.id, parseJsonField(customApiForm.test_arguments, {}) as Record<string, unknown>);
                          setCustomApiTestResult({ ...customApiTestResult, [connector.id]: result.result });
                        } finally {
                          setCustomApiBusy(null);
                        }
                      }}
                    >
                      {t('enterprise.tools.testConnector', 'Test')}
                    </button>
                  </div>
                  {customApiTestResult[connector.id] ? (
                    <pre style={{ marginTop: '8px', padding: '10px', maxHeight: '220px', overflow: 'auto', fontSize: '11px', background: 'var(--bg-tertiary)', borderRadius: '6px' }}>{customApiTestResult[connector.id]}</pre>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </div>
      ) : null}

      {toolsView === 'global' ? (
        <>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
            <h3>{t('enterprise.tools.title', 'Global Tools')}</h3>
            <button className="btn btn-primary" onClick={() => setShowAddMCP(true)}>
              + {t('enterprise.tools.addMcpServer', 'Add MCP Server')}
            </button>
          </div>

          {showAddMCP ? (
            <div className="card" style={{ padding: '16px', marginBottom: '16px' }}>
              <h4 style={{ marginBottom: '12px' }}>{t('enterprise.tools.mcpServer', 'MCP Server')}</h4>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                <div>
                  <label style={{ display: 'block', fontSize: '12px', marginBottom: '4px' }}>
                    {t('enterprise.tools.jsonConfig', 'JSON Config')}
                  </label>
                  <textarea
                    className="form-input"
                    value={mcpRawInput}
                    onChange={(event) => {
                      const value = event.target.value;
                      setMcpRawInput(value);
                      try {
                        const parsed = JSON.parse(value);
                        const servers = parsed.mcpServers || parsed;
                        const names = Object.keys(servers);
                        if (names.length > 0) {
                          const name = names[0];
                          const cfg = servers[name];
                          const url = cfg.url || cfg.uri || '';
                          setMcpForm({ server_name: name, server_url: url });
                        }
                      } catch {
                        setMcpForm((current) => ({ ...current, server_url: value }));
                      }
                    }}
                    placeholder={"{\n  \"mcpServers\": {\n    \"server-name\": {\n      \"type\": \"sse\",\n      \"url\": \"https://mcp.example.com/sse\"\n    }\n  }\n}\n\nor paste a URL directly"}
                    style={{ minHeight: '120px', fontFamily: 'var(--font-mono)', fontSize: '12px', resize: 'vertical' }}
                  />
                </div>
                {mcpForm.server_name ? (
                  <div style={{ display: 'flex', gap: '12px', fontSize: '12px', color: 'var(--text-secondary)', padding: '8px 12px', background: 'var(--bg-tertiary)', borderRadius: '6px' }}>
                    <span>Name: <strong>{mcpForm.server_name}</strong></span>
                    <span>URL: <strong>{mcpForm.server_url}</strong></span>
                  </div>
                ) : null}
                {!mcpForm.server_name ? (
                  <div>
                    <label style={{ display: 'block', fontSize: '12px', marginBottom: '4px' }}>
                      {t('enterprise.tools.mcpServerName', 'MCP Server Name')}
                    </label>
                    <input
                      className="form-input"
                      value={mcpForm.server_name}
                      onChange={(event) => setMcpForm((current) => ({ ...current, server_name: event.target.value }))}
                      placeholder="My MCP Server"
                    />
                  </div>
                ) : null}
                <div style={{ display: 'flex', gap: '8px' }}>
                  <button
                    className="btn btn-secondary"
                    disabled={mcpTesting || !mcpForm.server_url}
                    onClick={async () => {
                      setMcpTesting(true);
                      setMcpTestResult(null);
                      try {
                        const result = await toolsApi.testMcp({ server_url: mcpForm.server_url });
                        setMcpTestResult(result);
                      } catch (error: any) {
                        setMcpTestResult({ ok: false, error: error.message });
                      }
                      setMcpTesting(false);
                    }}
                  >
                    {mcpTesting ? t('enterprise.tools.testing', 'Testing...') : t('enterprise.tools.testConnection', 'Test Connection')}
                  </button>
                  <button
                    className="btn btn-secondary"
                    onClick={() => {
                      setShowAddMCP(false);
                      setMcpTestResult(null);
                      setMcpForm({ server_url: '', server_name: '' });
                      setMcpRawInput('');
                    }}
                  >
                    {t('common.cancel', 'Cancel')}
                  </button>
                </div>
                {mcpTestResult ? (
                  <div className="card" style={{ padding: '12px', background: mcpTestResult.ok ? 'rgba(0,200,100,0.1)' : 'rgba(255,0,0,0.1)' }}>
                    {mcpTestResult.ok ? (
                      <div>
                        <div style={{ color: 'var(--success)', fontWeight: 600, marginBottom: '8px' }}>
                          {t('enterprise.tools.connectionSuccess', { count: mcpTestResult.tools?.length || 0 })}
                        </div>
                        {(mcpTestResult.tools || []).map((tool: any, index: number) => (
                          <div key={index} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '6px 0', borderBottom: '1px solid var(--border-color)' }}>
                            <div>
                              <span style={{ fontWeight: 500, fontSize: '13px' }}>{tool.name}</span>
                              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{tool.description?.slice(0, 80)}</div>
                            </div>
                            <button
                              className="btn btn-secondary"
                              style={{ padding: '4px 10px', fontSize: '11px' }}
                              onClick={async () => {
                                try {
                                  await toolsApi.createTool({
                                    name: `mcp_${tool.name}`,
                                    display_name: tool.name,
                                    description: tool.description || '',
                                    type: 'mcp',
                                    category: 'custom',
                                    icon: '·',
                                    mcp_server_url: mcpForm.server_url,
                                    mcp_server_name: mcpForm.server_name || mcpForm.server_url,
                                    mcp_tool_name: tool.name,
                                    parameters_schema: tool.inputSchema || {},
                                    is_default: false,
                                  });
                                  loadAllTools();
                                } catch (error: any) {
                                  showAppToast(`${t('enterprise.tools.importFailed', 'Import failed')}: ${error.message}`, 'error');
                                }
                              }}
                            >
                              {t('enterprise.tools.import', 'Import')}
                            </button>
                          </div>
                        ))}
                        <div style={{ marginTop: '10px', display: 'flex', justifyContent: 'flex-end' }}>
                          <button
                            className="btn btn-primary"
                            style={{ padding: '6px 14px', fontSize: '12px' }}
                            onClick={async () => {
                              const tools = mcpTestResult.tools || [];
                              let successCount = 0;
                              const errors: string[] = [];
                              for (const tool of tools) {
                                try {
                                  await toolsApi.createTool({
                                    name: `mcp_${tool.name}`,
                                    display_name: tool.name,
                                    description: tool.description || '',
                                    type: 'mcp',
                                    category: 'custom',
                                    icon: '·',
                                    mcp_server_url: mcpForm.server_url,
                                    mcp_server_name: mcpForm.server_name || mcpForm.server_url,
                                    mcp_tool_name: tool.name,
                                    parameters_schema: tool.inputSchema || {},
                                    is_default: false,
                                  });
                                  successCount++;
                                } catch (error: any) {
                                  errors.push(`${tool.name}: ${error.message}`);
                                }
                              }
                              loadAllTools();
                              setShowAddMCP(false);
                              setMcpTestResult(null);
                              setMcpForm({ server_url: '', server_name: '' });
                              setMcpRawInput('');
                              if (errors.length > 0) {
                                showAppToast(`Imported ${successCount}/${tools.length} tools. Failed: ${errors.join('; ')}`, 'error');
                              } else {
                                showAppToast(`Imported ${successCount}/${tools.length} tools`, 'success');
                              }
                            }}
                          >
                            {t('enterprise.tools.importAll', 'Import All')}
                          </button>
                        </div>
                      </div>
                    ) : (
                      <div style={{ color: 'var(--danger)' }}>
                        {t('enterprise.tools.connectionFailed', 'Connection failed')}: {mcpTestResult.error}
                      </div>
                    )}
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}

          {(() => {
            const grouped = allTools.reduce((acc: Record<string, any[]>, tool: any) => {
              const category = tool.category || 'general';
              (acc[category] = acc[category] || []).push(tool);
              return acc;
            }, {} as Record<string, any[]>);

            if (allTools.length === 0) {
              return (
                <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>
                  {t('enterprise.tools.emptyState', 'No global tools configured')}
                </div>
              );
            }

            return (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
                {Object.entries(grouped).map(([category, categoryTools]) => {
                  const hasCategoryConfig = !!GLOBAL_CATEGORY_CONFIG_SCHEMAS[category];

                  return (
                    <div key={category}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                        <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
                          {categoryLabels[category] || category}
                        </div>
                        {hasCategoryConfig ? (
                          <button
                            onClick={() => {
                              setConfigCategory(category);
                              setEditingConfig({});
                              const firstToolWithConfig = categoryTools.find((tool) => tool.config_schema?.fields?.length > 0);
                              if (firstToolWithConfig?.config) {
                                setEditingConfig({ ...firstToolWithConfig.config });
                              }
                            }}
                            style={{ background: 'none', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '3px 8px', fontSize: '11px', cursor: 'pointer', color: 'var(--text-secondary)' }}
                            title={`Configure ${category}`}
                          >
                            Configure
                          </button>
                        ) : null}
                      </div>

                      <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                        {(() => {
                          // Split MCP tools into server groups; non-MCP tools render flat
                          const nonMcpTools = categoryTools.filter((tool) => tool.type !== 'mcp');
                          const mcpTools = categoryTools.filter((tool) => tool.type === 'mcp');
                          const mcpByServer: Record<string, any[]> = {};
                          for (const tool of mcpTools) {
                            const server = tool.mcp_server_name || 'MCP';
                            (mcpByServer[server] = mcpByServer[server] || []).push(tool);
                          }

                          const renderToolRow = (tool: any) => {
                            const hasOwnConfig = tool.config_schema?.fields?.length > 0 && !hasCategoryConfig;
                            const isEditing = editingToolId === tool.id;
                            // Strip "ServerName: " prefix for MCP tools shown inside a server group
                            const shortName = tool.type === 'mcp' && tool.mcp_server_name && tool.display_name.startsWith(tool.mcp_server_name + ': ')
                              ? tool.display_name.slice(tool.mcp_server_name.length + 2)
                              : tool.display_name;

                            return (
                              <div key={tool.id} className="card" style={{ padding: '0', overflow: 'hidden' }}>
                                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 14px' }}>
                                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flex: 1, minWidth: 0 }}>
                                    <ToolIcon tool={tool} />
                                    <div style={{ minWidth: 0 }}>
                                      <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                                        <span style={{ fontWeight: 500, fontSize: '13px' }}>{shortName}</span>
                                        {tool.type !== 'mcp' ? (
                                          <span style={{ fontSize: '10px', background: 'var(--bg-tertiary)', color: 'var(--text-secondary)', borderRadius: '4px', padding: '1px 5px' }}>
                                            Built-in
                                          </span>
                                        ) : null}
                                        {tool.is_default ? (
                                          <span style={{ fontSize: '10px', background: 'rgba(0,200,100,0.15)', color: 'var(--success)', borderRadius: '4px', padding: '1px 5px' }}>Default</span>
                                        ) : null}
                                      </div>
                                      <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                        {tool.description?.slice(0, 80)}
                                      </div>
                                    </div>
                                  </div>

                                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 }}>
                                    {hasOwnConfig ? (
                                      <button
                                        className="btn btn-secondary"
                                        style={{ padding: '4px 8px', fontSize: '11px' }}
                                        onClick={() => {
                                          if (isEditing) {
                                            setEditingToolId(null);
                                          } else {
                                            setEditingToolId(tool.id);
                                            setEditingConfig({ ...tool.config });
                                          }
                                        }}
                                      >
                                        {isEditing ? t('enterprise.tools.collapse', 'Collapse') : t('enterprise.tools.configure', 'Configure')}
                                      </button>
                                    ) : null}

                                    {tool.type !== 'builtin' ? (
                                      <button
                                        className="btn btn-danger"
                                        style={{ padding: '4px 8px', fontSize: '11px' }}
                                        onClick={async () => {
                                          const confirmed = await requestAppConfirm({
                                            title: t('common.delete', 'Delete'),
                                            message: `${t('common.delete', 'Delete')} ${tool.display_name}?`,
                                            confirmLabel: t('common.delete', 'Delete'),
                                            danger: true,
                                          });
                                          if (!confirmed) return;
                                          await toolsApi.deleteGlobalTool(tool.id);
                                          loadAllTools();
                                          loadAgentInstalledTools();
                                        }}
                                      >
                                        {t('common.delete', 'Delete')}
                                      </button>
                                    ) : null}

                                    <label style={{ position: 'relative', display: 'inline-block', width: '40px', height: '22px', cursor: 'pointer', flexShrink: 0 }}>
                                      <input
                                        type="checkbox"
                                        checked={tool.enabled}
                                        onChange={async (event) => {
                                          await toolsApi.updateGlobalTool(tool.id, { enabled: event.target.checked });
                                          loadAllTools();
                                        }}
                                        style={{ opacity: 0, width: 0, height: 0 }}
                                      />
                                      <span style={{ position: 'absolute', inset: 0, background: tool.enabled ? '#22c55e' : 'var(--bg-tertiary)', borderRadius: '11px', transition: 'background 0.2s' }}>
                                        <span style={{ position: 'absolute', left: tool.enabled ? '20px' : '2px', top: '2px', width: '18px', height: '18px', background: '#fff', borderRadius: '50%', transition: 'left 0.2s' }} />
                                      </span>
                                    </label>
                                  </div>
                                </div>

                                {isEditing && hasOwnConfig ? (
                                  <div style={{ borderTop: '1px solid var(--border-color)', padding: '16px', background: 'var(--bg-secondary)' }}>
                                    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                                      {(tool.config_schema.fields || []).map((field: any) => {
                                        if (field.depends_on) {
                                          const visible = Object.entries(field.depends_on).every(([key, values]: [string, any]) =>
                                            values.includes(editingConfig[key]),
                                          );
                                          if (!visible) {
                                            return null;
                                          }
                                        }
                                        if (field.type === 'password' && field.multiline) {
                                          return (
                                            <ToolConfigSecretListField
                                              key={field.key}
                                              field={field}
                                              value={editingConfig[field.key] ?? ''}
                                              onChange={(value) => setEditingConfig((current) => ({ ...current, [field.key]: value }))}
                                            />
                                          );
                                        }
                                        return (
                                          <div key={field.key}>
                                            <label style={{ display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px' }}>{field.label}</label>
                                            {field.type === 'select' ? (
                                              <select className="form-input" value={editingConfig[field.key] ?? field.default ?? ''} onChange={(event) => setEditingConfig((current) => ({ ...current, [field.key]: event.target.value }))}>
                                                {(field.options || []).map((option: any) => (
                                                  <option key={option.value} value={option.value}>{option.label}</option>
                                                ))}
                                              </select>
                                            ) : field.type === 'number' ? (
                                              <input type="number" className="form-input" value={editingConfig[field.key] ?? field.default ?? ''} min={field.min} max={field.max} onChange={(event) => setEditingConfig((current) => ({ ...current, [field.key]: Number(event.target.value) }))} />
                                            ) : field.type === 'password' ? (
                                              <input type="password" autoComplete="new-password" className="form-input" value={editingConfig[field.key] ?? ''} placeholder={field.placeholder || ''} onChange={(event) => setEditingConfig((current) => ({ ...current, [field.key]: event.target.value }))} />
                                            ) : (
                                              <input type="text" className="form-input" value={editingConfig[field.key] ?? field.default ?? ''} placeholder={field.placeholder || ''} onChange={(event) => setEditingConfig((current) => ({ ...current, [field.key]: event.target.value }))} />
                                            )}
                                          </div>
                                        );
                                      })}
                                      <div style={{ display: 'flex', gap: '8px', marginTop: '4px' }}>
                                        <button
                                          className="btn btn-primary"
                                          onClick={async () => {
                                            await toolsApi.updateGlobalTool(tool.id, { config: editingConfig });
                                            setEditingToolId(null);
                                            loadAllTools();
                                          }}
                                        >
                                          {t('enterprise.tools.saveConfig', 'Save Config')}
                                        </button>
                                        <button className="btn btn-secondary" onClick={() => setEditingToolId(null)}>
                                          {t('common.cancel', 'Cancel')}
                                        </button>
                                      </div>
                                    </div>
                                  </div>
                                ) : null}
                              </div>
                            );
                          };

                          return (
                            <>
                              {/* Non-MCP tools: flat rendering */}
                              {nonMcpTools.map(renderToolRow)}

                              {/* MCP tools: grouped by server, collapsible */}
                              {Object.entries(mcpByServer).map(([serverName, serverTools]) => {
                                const isCollapsed = !collapsedServers.has(serverName);
                                const enabledCount = serverTools.filter((t) => t.enabled).length;
                                return (
                                  <div key={`mcp-server-${serverName}`} className="card" style={{ padding: '0', overflow: 'hidden' }}>
                                    <div
                                      style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 14px', cursor: 'pointer', userSelect: 'none' }}
                                      onClick={() => setCollapsedServers((prev) => {
                                        const next = new Set(prev);
                                        if (next.has(serverName)) next.delete(serverName);
                                        else next.add(serverName);
                                        return next;
                                      })}
                                    >
                                      <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                                        <span style={{ fontSize: '12px', color: 'var(--text-tertiary)', transition: 'transform 0.15s', transform: isCollapsed ? 'rotate(0deg)' : 'rotate(90deg)' }}>
                                          ▶
                                        </span>
                                        <span style={{ fontSize: '18px' }}>🔌</span>
                                        <div>
                                          <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                                            <span style={{ fontWeight: 600, fontSize: '13px' }}>{serverName}</span>
                                            <span style={{ fontSize: '10px', background: 'var(--primary)', color: '#fff', borderRadius: '4px', padding: '1px 5px' }}>MCP</span>
                                            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                              {enabledCount}/{serverTools.length} {t('enterprise.tools.toolsEnabled', 'enabled')}
                                            </span>
                                          </div>
                                          {serverTools[0]?.mcp_server_url ? (
                                            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '1px' }}>
                                              {serverTools[0].mcp_server_url.length > 60 ? serverTools[0].mcp_server_url.slice(0, 60) + '...' : serverTools[0].mcp_server_url}
                                            </div>
                                          ) : null}
                                        </div>
                                      </div>
                                      <button
                                        className="btn btn-danger"
                                        style={{ padding: '4px 8px', fontSize: '11px' }}
                                        onClick={async (event) => {
                                          event.stopPropagation();
                                          const confirmed = await requestAppConfirm({
                                            title: t('common.delete', 'Delete'),
                                            message: `${t('common.delete', 'Delete')} ${serverName} (${serverTools.length} tools)?`,
                                            confirmLabel: t('common.delete', 'Delete'),
                                            danger: true,
                                          });
                                          if (!confirmed) return;
                                          for (const tool of serverTools) {
                                            await toolsApi.deleteGlobalTool(tool.id);
                                          }
                                          loadAllTools();
                                          loadAgentInstalledTools();
                                        }}
                                      >
                                        {t('common.delete', 'Delete')}
                                      </button>
                                    </div>
                                    {!isCollapsed ? (
                                      <div style={{ borderTop: '1px solid var(--border-color)', padding: '4px 8px 8px 42px', background: 'var(--bg-secondary)', display: 'flex', flexDirection: 'column', gap: '4px' }}>
                                        {serverTools.map(renderToolRow)}
                                      </div>
                                    ) : null}
                                  </div>
                                );
                              })}
                            </>
                          );
                        })()}
                      </div>
                    </div>
                  );
                })}
              </div>
            );
          })()}

          {configCategory && GLOBAL_CATEGORY_CONFIG_SCHEMAS[configCategory] ? (
            <div style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.55)', zIndex: 2000, display: 'flex', alignItems: 'center', justifyContent: 'center' }} onClick={() => setConfigCategory(null)}>
              <div onClick={(event) => event.stopPropagation()} style={{ background: 'var(--bg-primary)', borderRadius: '12px', padding: '24px', width: '480px', maxWidth: '95vw', maxHeight: '80vh', overflow: 'auto', boxShadow: '0 20px 60px rgba(0,0,0,0.4)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                  <div>
                    <h3 style={{ margin: 0 }}>{GLOBAL_CATEGORY_CONFIG_SCHEMAS[configCategory].title}</h3>
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>Global configuration shared by all tools in this category</div>
                  </div>
                  <button onClick={() => setConfigCategory(null)} style={{ background: 'none', border: 'none', fontSize: '18px', cursor: 'pointer', color: 'var(--text-secondary)' }}>x</button>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                  {GLOBAL_CATEGORY_CONFIG_SCHEMAS[configCategory].fields.map((field: any) => (
                    field.type === 'password' && field.multiline ? (
                      <ToolConfigSecretListField
                        key={field.key}
                        field={field}
                        value={editingConfig[field.key] ?? ''}
                        onChange={(value) => setEditingConfig((current) => ({ ...current, [field.key]: value }))}
                      />
                    ) : (
                      <div key={field.key}>
                        <label style={{ display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px' }}>{field.label}</label>
                        {field.type === 'password' ? (
                          <input type="password" autoComplete="new-password" className="form-input" value={editingConfig[field.key] ?? ''} placeholder={field.placeholder || ''} onChange={(event) => setEditingConfig((current) => ({ ...current, [field.key]: event.target.value }))} />
                        ) : (
                          <input type="text" className="form-input" value={editingConfig[field.key] ?? ''} placeholder={field.placeholder || ''} onChange={(event) => setEditingConfig((current) => ({ ...current, [field.key]: event.target.value }))} />
                        )}
                      </div>
                    )
                  ))}
                  <div style={{ display: 'flex', gap: '8px', marginTop: '8px', justifyContent: 'flex-end' }}>
                    <button className="btn btn-secondary" onClick={() => setConfigCategory(null)}>
                      {t('common.cancel', 'Cancel')}
                    </button>
                    <button
                      className="btn btn-primary"
                      onClick={async () => {
                        const categoryTools = allTools.filter((tool) => (tool.category || 'general') === configCategory && tool.config_schema?.fields?.length > 0);
                        for (const tool of categoryTools) {
                          await toolsApi.updateGlobalTool(tool.id, { config: editingConfig });
                        }
                        setConfigCategory(null);
                        loadAllTools();
                      }}
                    >
                      {t('common.save', 'Save')}
                    </button>
                  </div>
                </div>
              </div>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
