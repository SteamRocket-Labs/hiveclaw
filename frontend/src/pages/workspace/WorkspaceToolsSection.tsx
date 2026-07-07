import { useEffect, useState } from 'react';

import { useTranslation } from 'react-i18next';

import { customApiConnectorsApi, type CustomApiConnector } from '../../api/domains/customApiConnectors';
import { enterpriseApi, type CapabilityDefinition, type CapabilityPolicy } from '../../api/domains/enterprise';
import { extensionsApi, type McpServerRecord } from '../../api/domains/extensions';
import { toolsApi } from '../../api/domains/tools';
import { requestAppConfirm, showAppToast } from '../../components/AppDialogs';
import ToolIcon from '../../components/ToolIcon';

import './WorkspaceToolsSection.css';

const MCP_STATUS_COLORS: Record<string, string> = {
  connected: 'var(--success)',
  needs_auth: 'var(--warning)',
  expired: 'var(--warning)',
  failed: 'var(--error)',
  error: 'var(--error)',
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

type ToolGovernanceTaxonomy = {
  l2_visible?: boolean;
  enterprise_toggleable?: boolean;
  layer?: string;
  source?: string;
};

export type WorkspaceProviderAuthMetadata = {
  mode?: 'no_key_default' | 'optional_key' | 'key_required' | string;
  keyless_supported?: boolean;
  credential_optional?: boolean;
  key_required?: boolean;
  label?: string;
  description?: string;
};

type WorkspaceToolRow = {
  id: string;
  name?: string;
  display_name?: string;
  description?: string;
  type?: string;
  category?: string;
  enabled?: boolean;
  is_default?: boolean;
  config?: Record<string, unknown>;
  config_schema?: { fields?: any[] };
  mcp_server_name?: string | null;
  mcp_server_url?: string | null;
  governance_taxonomy?: ToolGovernanceTaxonomy | null;
  provider_auth?: WorkspaceProviderAuthMetadata | null;
};

type WorkspaceToolCapabilityPolicy = Pick<CapabilityPolicy, 'allowed' | 'requires_approval'> | undefined;

export type WorkspaceToolExecutionMode = 'auto' | 'approval';
export type WorkspaceToolEffectiveStatus = 'disabled' | 'auto_allowed' | 'approval_required' | 'legacy_denied' | 'unmanaged';

export function isExtensionOrAddonTool(tool: Pick<WorkspaceToolRow, 'type' | 'name' | 'governance_taxonomy'>): boolean {
  const taxonomy = tool.governance_taxonomy;
  if (taxonomy?.l2_visible === true && taxonomy?.enterprise_toggleable === true) {
    return true;
  }
  if (taxonomy?.layer === 'agent_base') {
    return false;
  }
  return tool.type === 'mcp' || tool.type === 'custom_api' || String(tool.name || '').startsWith('custom_api__');
}

export function resolveWorkspaceToolCapability(
  tool: Pick<WorkspaceToolRow, 'name'>,
  definitions: Pick<CapabilityDefinition, 'capability' | 'tools'>[],
): string | null {
  const toolName = String(tool.name || '').trim();
  if (!toolName) return null;
  for (const definition of definitions) {
    if (definition.tools.includes(toolName)) {
      return definition.capability;
    }
  }
  for (const definition of definitions) {
    if (definition.tools.some((pattern) => pattern.endsWith('*') && toolName.startsWith(pattern.slice(0, -1)))) {
      return definition.capability;
    }
  }
  return null;
}

export function getWorkspaceToolGovernanceState({
  tool,
  capability,
  policy,
}: {
  tool: Pick<WorkspaceToolRow, 'enabled'>;
  capability?: string | null;
  policy?: WorkspaceToolCapabilityPolicy;
}): { executionMode: WorkspaceToolExecutionMode; effectiveStatus: WorkspaceToolEffectiveStatus } {
  const executionMode: WorkspaceToolExecutionMode = policy?.allowed === true && policy.requires_approval === true ? 'approval' : 'auto';
  if (tool.enabled === false) {
    return { executionMode, effectiveStatus: 'disabled' };
  }
  if (!capability) {
    return { executionMode, effectiveStatus: 'unmanaged' };
  }
  if (policy?.allowed === false) {
    return { executionMode, effectiveStatus: 'legacy_denied' };
  }
  if (executionMode === 'approval') {
    return { executionMode, effectiveStatus: 'approval_required' };
  }
  return { executionMode, effectiveStatus: 'auto_allowed' };
}

const WEB_PACK_TOOL_ORDER: Record<string, number> = {
  advanced_web_search: 0,
  advanced_web_fetch: 1,
  anysearch_get_sub_domains: 10,
  anysearch_search: 11,
  anysearch_batch_search: 12,
  anysearch_extract: 13,
  exa_search: 20,
  exa_fetch: 21,
  tavily_search: 30,
  tavily_extract: 31,
  firecrawl_search: 40,
  firecrawl_fetch: 41,
  xcrawl_scrape: 99,
};

function getWorkspaceToolCategoryRank(category?: string): number {
  if (category === 'web_pack') return 0;
  if (category === 'office_pack') return 20;
  if (category === 'mcp') return 90;
  return 50;
}

export function sortWorkspaceToolsForDisplay<T extends Pick<WorkspaceToolRow, 'name' | 'category' | 'display_name'>>(tools: T[]): T[] {
  return [...tools].sort((a, b) => {
    const categoryRank = getWorkspaceToolCategoryRank(a.category) - getWorkspaceToolCategoryRank(b.category);
    if (categoryRank !== 0) return categoryRank;
    const aRank = a.category === 'web_pack' ? WEB_PACK_TOOL_ORDER[String(a.name || '')] ?? 60 : 50;
    const bRank = b.category === 'web_pack' ? WEB_PACK_TOOL_ORDER[String(b.name || '')] ?? 60 : 50;
    if (aRank !== bRank) return aRank - bRank;
    return String(a.display_name || a.name || '').localeCompare(String(b.display_name || b.name || ''));
  });
}

export function getWorkspaceProviderAuthDisplay(
  providerAuth: WorkspaceProviderAuthMetadata | null | undefined,
  t: (key: string, fallback: string) => string,
): { label: string; description: string; className: string } | null {
  if (!providerAuth) return null;
  switch (providerAuth.mode) {
    case 'no_key_default':
      return {
        label: t('enterprise.tools.providerAuthNoKeyDefault', 'No key by default'),
        description: t(
          'enterprise.tools.providerAuthNoKeyDefaultDesc',
          'Runs without an API key by default; optional keys only raise limits or production control.',
        ),
        className: 'is-no-key',
      };
    case 'key_required':
      return {
        label: t('enterprise.tools.providerAuthKeyRequired', 'Key required'),
        description: t(
          'enterprise.tools.providerAuthKeyRequiredDesc',
          'Requires a configured provider key before agents can use it.',
        ),
        className: 'is-key-required',
      };
    case 'optional_key':
    default:
      return {
        label: t('enterprise.tools.providerAuthOptionalKey', 'Optional key'),
        description: t(
          'enterprise.tools.providerAuthOptionalKeyDesc',
          'Works without a key by default; add a key for higher limits or production control.',
        ),
        className: 'is-optional-key',
      };
  }
}

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
      <div className="ws-tools-field-head">
        <label className="ws-tools-secret-label">{label}</label>
        <span className="ws-tools-count-pill">
          {count} {count === 1 ? 'key' : 'keys'} configured
        </span>
      </div>
      <div className="ws-tools-col-8">
        {visibleRows.map((row, index) => (
          <div key={`${field.key}-${index}`} className="ws-tools-row-8">
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
      <div className="ws-tools-field-foot">
        <div className="ws-tools-note">
          Enter one API key per row. Calls rotate across saved keys in order.
          {field.description ? ` ${field.description}` : ''}
        </div>
        <button
          type="button"
          className="btn btn-secondary"
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
    web_pack: t('agent.toolCategories.web_pack', 'Web Research'),
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

  const [allTools, setAllTools] = useState<WorkspaceToolRow[]>([]);
  const [capabilityDefinitions, setCapabilityDefinitions] = useState<CapabilityDefinition[]>([]);
  const [capabilityPolicies, setCapabilityPolicies] = useState<CapabilityPolicy[]>([]);
  const [capabilityPolicyBusy, setCapabilityPolicyBusy] = useState<string | null>(null);
  const [showAddMCP, setShowAddMCP] = useState(false);
  const [mcpForm, setMcpForm] = useState({ server_url: '', server_name: '' });
  const [mcpRawInput, setMcpRawInput] = useState('');
  const [mcpTestResult, setMcpTestResult] = useState<any>(null);
  const [mcpTesting, setMcpTesting] = useState(false);
  const [editingToolId, setEditingToolId] = useState<string | null>(null);
  const [editingConfig, setEditingConfig] = useState<Record<string, any>>({});
  const [configCategory, setConfigCategory] = useState<string | null>(null);
  const [toolsView, setToolsView] = useState<'global' | 'mcp-servers' | 'custom-api' | 'agent-installed'>('global');
  const [agentInstalledTools, setAgentInstalledTools] = useState<any[]>([]);
  const [collapsedServers, setCollapsedServers] = useState<Set<string>>(new Set());
  const [mcpServers, setMcpServers] = useState<McpServerRecord[]>([]);
  const [mcpServersLoaded, setMcpServersLoaded] = useState(false);
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
    setAllTools(data as WorkspaceToolRow[]);
  };

  const loadCapabilityGovernance = async () => {
    try {
      const [definitions, policies] = await Promise.all([
        enterpriseApi.listCapabilityDefinitions(),
        enterpriseApi.listCapabilityPolicies({ tenantId: selectedTenantId || undefined }),
      ]);
      setCapabilityDefinitions(definitions);
      setCapabilityPolicies(policies);
    } catch {
      setCapabilityDefinitions([]);
      setCapabilityPolicies([]);
    }
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
    loadCapabilityGovernance();
    loadAgentInstalledTools();
  }, [selectedTenantId]);

  const policyByCapability = new Map(
    capabilityPolicies
      .filter((policy) => !policy.agent_id)
      .map((policy) => [policy.capability, policy] as const),
  );

  const effectiveStatusLabel = (status: WorkspaceToolEffectiveStatus) => {
    switch (status) {
      case 'disabled':
        return t('enterprise.tools.effectiveDisabled', 'Disabled');
      case 'approval_required':
        return t('enterprise.tools.effectiveApprovalRequired', 'Requires company approval');
      case 'legacy_denied':
        return t('enterprise.tools.effectiveLegacyDenied', 'Legacy deny policy');
      case 'unmanaged':
        return t('enterprise.tools.effectiveUnmanaged', 'Connector policy');
      case 'auto_allowed':
      default:
        return t('enterprise.tools.effectiveAutoAllowed', 'Auto allowed');
    }
  };

  const updateToolExecutionMode = async (capability: string, mode: WorkspaceToolExecutionMode) => {
    setCapabilityPolicyBusy(capability);
    try {
      await enterpriseApi.upsertCapabilityPolicy({
        capability,
        allowed: true,
        requires_approval: mode === 'approval',
        conditions: {},
      }, selectedTenantId || undefined);
      await loadCapabilityGovernance();
    } catch (error: any) {
      showAppToast(error?.message || t('enterprise.tools.updateFailed', 'Update failed'), 'error');
    } finally {
      setCapabilityPolicyBusy(null);
    }
  };

  return (
    <div>
      <div className="ws-tools-tabs">
        {([
          ['global', t('enterprise.tools.extensionsAddons', 'Extensions & Add-ons')],
          ['mcp-servers', t('agent.extensions.mcpServers', 'MCP Servers')],
          ['custom-api', t('enterprise.tools.customApis', 'Custom APIs')],
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
              } else if (key === 'custom-api') {
                loadCustomApis();
              }
            }}
            className={`ws-tools-tab ${toolsView === key ? 'active' : ''}`}
          >
            {label}
          </button>
        ))}
      </div>

      {toolsView === 'agent-installed' ? (
        <div>
          <p className="ws-tools-hint">
            {t('enterprise.tools.agentInstalledHint', 'These tools are installed directly by agents.')}
          </p>
          {agentInstalledTools.length === 0 ? (
            <div className="ws-tools-empty">
              {t('enterprise.tools.noAgentInstalledTools', 'No agent-installed tools')}
            </div>
          ) : (
            <div className="ws-tools-list-sm">
              {agentInstalledTools.map((row) => (
                <div key={row.agent_tool_id} className="card ws-tools-installed-card">
                  <div className="ws-tools-cell-grow">
                    <div className="ws-tools-row-8">
                      <span className="ws-tools-name">🔌 {row.tool_display_name}</span>
                      {row.mcp_server_name ? (
                        <span className="ws-tools-tag ws-tools-tag-mcp">MCP</span>
                      ) : null}
                    </div>
                    <div className="ws-tools-sub">
                      🤖 {row.installed_by_agent_name || 'Unknown Agent'}
                      {row.installed_at ? <span> · {new Date(row.installed_at).toLocaleString()}</span> : null}
                    </div>
                  </div>
                  <button
                    className="btn btn-ghost ws-tools-danger-text"
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
          <p className="ws-tools-hint">
            {t('enterprise.tools.mcpServersHint', 'External MCP integrations managed as server-level connectors. Each server may expose many tools internally.')}
          </p>
          {!mcpServersLoaded ? (
            <div className="ws-tools-empty">{t('common.loading', 'Loading...')}</div>
          ) : mcpServers.length === 0 ? (
            <div className="ws-tools-empty">
              {t('enterprise.tools.noMcpServers', 'No MCP servers yet. Add one from Extensions & Add-ons.')}
            </div>
          ) : (
            <div className="ws-tools-list">
              {mcpServers.map((server) => (
                <div key={server.id} className="card ws-tools-card-pad">
                  <div className="ws-tools-row-between">
                    <div className="ws-tools-row-10-min">
                      <span className="ws-tools-emoji">🔌</span>
                      <div className="ws-tools-min0">
                        <div className="ws-tools-row-6">
                          <span className="ws-tools-title-13">{server.name}</span>
                          <span className="ws-tools-tag ws-tools-tag-mcp">MCP</span>
                          <span className="ws-tools-tiny-muted">{server.transport}</span>
                        </div>
                        <div className="ws-tools-meta-row">
                          <span className="ws-tools-inline-4">
                            <span className="ws-tools-status-dot" style={{ background: MCP_STATUS_COLORS[server.status] || 'var(--text-tertiary)' }} />
                            {t(`agent.extensions.status.${server.status}`, server.status)}
                          </span>
                          <span>·</span>
                          <span>{t('enterprise.tools.authStatus', { status: t(`agent.extensions.authStatus.${server.auth_status}`, server.auth_status), defaultValue: 'auth: {{status}}' })}</span>
                          <span>·</span>
                          <span>{t('agent.extensions.toolCount', { count: server.tool_count, defaultValue: '{{count}} tools' })}</span>
                        </div>
                      </div>
                    </div>
                    <span className="ws-tools-meta-shrink">
                      {t('enterprise.tools.usedByAgents', { count: server.agent_count, defaultValue: '{{count}} agents' })}
                    </span>
                  </div>
                  {server.agents.length > 0 ? (
                    <div className="ws-tools-agent-wrap">
                      {server.agents.map((agent) => (
                        <span
                          key={agent.id}
                          className={`ws-tools-agent-chip ${agent.enabled ? 'enabled' : 'disabled'}`}
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

      {toolsView === 'custom-api' ? (
        <div>
          <p className="ws-tools-hint">
            {t('enterprise.tools.customApiHint', 'Tenant-governed HTTP API actions. Credentials are stored server-side and are never exposed to agents.')}
          </p>
          <div className="card ws-tools-connector-form">
            <div className="ws-tools-grid-2">
              <input className="form-input" value={customApiForm.connector_name} onChange={(event) => setCustomApiForm({ ...customApiForm, connector_name: event.target.value })} placeholder={t('enterprise.tools.connectorName', 'Connector name')} />
              <input className="form-input" value={customApiForm.action_name} onChange={(event) => setCustomApiForm({ ...customApiForm, action_name: event.target.value })} placeholder={t('enterprise.tools.actionName', 'Action name')} />
              <input className="form-input" value={customApiForm.base_url} onChange={(event) => setCustomApiForm({ ...customApiForm, base_url: event.target.value })} placeholder="https://api.example.com" />
              <div className="ws-tools-grid-100">
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
              <div className="ws-tools-grid-100">
                <select className="form-input" value={customApiForm.auth_location} onChange={(event) => setCustomApiForm({ ...customApiForm, auth_location: event.target.value })}>
                  <option value="header">{t('enterprise.tools.header', 'Header')}</option>
                  <option value="query">{t('enterprise.tools.query', 'Query')}</option>
                </select>
                <input className="form-input" value={customApiForm.auth_name} onChange={(event) => setCustomApiForm({ ...customApiForm, auth_name: event.target.value })} placeholder="X-API-Key" />
              </div>
              <input className="form-input" type="password" value={customApiForm.secret_value} onChange={(event) => setCustomApiForm({ ...customApiForm, secret_value: event.target.value })} placeholder={t('enterprise.tools.secretValue', 'Credential value')} />
              <input className="form-input" value={customApiForm.description} onChange={(event) => setCustomApiForm({ ...customApiForm, description: event.target.value })} placeholder={t('enterprise.tools.description', 'Description')} />
            </div>
            <div className="ws-tools-grid-2 ws-tools-mt-10">
              <textarea className="form-input" value={customApiForm.parameters_schema} onChange={(event) => setCustomApiForm({ ...customApiForm, parameters_schema: event.target.value })} rows={5} placeholder="parameters_schema JSON" />
              <textarea className="form-input" value={customApiForm.body_template} onChange={(event) => setCustomApiForm({ ...customApiForm, body_template: event.target.value })} rows={5} placeholder={t('enterprise.tools.bodyTemplateJson', 'Body template JSON, optional')} />
              <textarea className="form-input" value={customApiForm.headers} onChange={(event) => setCustomApiForm({ ...customApiForm, headers: event.target.value })} rows={3} placeholder={t('enterprise.tools.headersJson', 'Headers JSON')} />
              <textarea className="form-input" value={customApiForm.query} onChange={(event) => setCustomApiForm({ ...customApiForm, query: event.target.value })} rows={3} placeholder={t('enterprise.tools.queryJson', 'Query JSON')} />
            </div>
            <div className="ws-tools-row-between ws-tools-mt-10">
              <label className="ws-tools-check-label">
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
            <div className="ws-tools-empty">{t('common.loading', 'Loading...')}</div>
          ) : customApis.length === 0 ? (
            <div className="ws-tools-empty">{t('enterprise.tools.noCustomApis', 'No custom API connectors')}</div>
          ) : (
            <div className="ws-tools-list">
              {customApis.map((connector) => (
                <div key={connector.id} className="card ws-tools-card-pad">
                  <div className="ws-tools-split">
                    <div className="ws-tools-min0">
                      <div className="ws-tools-title-13">{connector.display_name}</div>
                      <div className="ws-tools-sub">{connector.name}</div>
                      {connector.description ? <div className="ws-tools-desc">{connector.description}</div> : null}
                    </div>
                    <div className="ws-tools-cell-shrink">
                      <span className={connector.enabled ? 'ws-tools-state-on' : 'ws-tools-state-off'}>{connector.enabled ? t('enterprise.tools.enabled', 'Enabled') : t('enterprise.tools.disabled', 'Disabled')}</span>
                      <button
                        className="btn btn-ghost ws-tools-danger-text"
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
                  <div className="ws-tools-row-8 ws-tools-mt-10">
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
                    <pre className="ws-tools-pre">{customApiTestResult[connector.id]}</pre>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </div>
      ) : null}

      {toolsView === 'global' ? (
        <>
          <div className="ws-tools-section-head">
            <h3>{t('enterprise.tools.extensionsAddons', 'Extensions & Add-ons')}</h3>
            <button className="btn btn-primary" onClick={() => setShowAddMCP(true)}>
              + {t('enterprise.tools.addMcpServer', 'Add MCP Server')}
            </button>
          </div>

          {showAddMCP ? (
            <div className="card ws-tools-addmcp-card">
              <h4 className="ws-tools-mb-12">{t('enterprise.tools.mcpServer', 'MCP Server')}</h4>
              <div className="ws-tools-col-10">
                <div>
                  <label className="ws-tools-form-label">
                    {t('enterprise.tools.jsonConfig', 'JSON Config')}
                  </label>
                  <textarea
                    className="form-input ws-tools-json-area"
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
                  />
                </div>
                {mcpForm.server_name ? (
                  <div className="ws-tools-parsed">
                    <span>Name: <strong>{mcpForm.server_name}</strong></span>
                    <span>URL: <strong>{mcpForm.server_url}</strong></span>
                  </div>
                ) : null}
                {!mcpForm.server_name ? (
                  <div>
                    <label className="ws-tools-form-label">
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
                <div className="ws-tools-row-8">
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
                  <div className={`card ws-tools-test-card ${mcpTestResult.ok ? 'ok' : 'fail'}`}>
                    {mcpTestResult.ok ? (
                      <div>
                        <div className="ws-tools-test-ok-title">
                          {t('enterprise.tools.connectionSuccess', { count: mcpTestResult.tools?.length || 0 })}
                        </div>
                        {(mcpTestResult.tools || []).map((tool: any, index: number) => (
                          <div key={index} className="ws-tools-test-row">
                            <div>
                              <span className="ws-tools-name">{tool.name}</span>
                              <div className="ws-tools-note">{tool.description?.slice(0, 80)}</div>
                            </div>
                            <button
                              className="btn btn-secondary"
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
                        <div className="ws-tools-actions-end">
                          <button
                            className="btn btn-primary"
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
                      <div className="ws-tools-danger-text">
                        {t('enterprise.tools.connectionFailed', 'Connection failed')}: {mcpTestResult.error}
                      </div>
                    )}
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}

          {(() => {
            const extensionTools = sortWorkspaceToolsForDisplay(allTools.filter(isExtensionOrAddonTool));
            const grouped = extensionTools.reduce((acc: Record<string, WorkspaceToolRow[]>, tool) => {
              const category = tool.category || 'general';
              (acc[category] = acc[category] || []).push(tool);
              return acc;
            }, {} as Record<string, WorkspaceToolRow[]>);

            if (extensionTools.length === 0) {
              return (
                <div className="ws-tools-empty">
                  {t('enterprise.tools.emptyExtensionsState', 'No extensions or add-ons configured')}
                </div>
              );
            }

            return (
              <div className="ws-tools-groups">
                {Object.entries(grouped)
                  .sort(([left], [right]) => getWorkspaceToolCategoryRank(left) - getWorkspaceToolCategoryRank(right))
                  .map(([category, categoryTools]) => {
                    const hasCategoryConfig = !!GLOBAL_CATEGORY_CONFIG_SCHEMAS[category];
                    const sortedCategoryTools = sortWorkspaceToolsForDisplay(categoryTools);

                    return (
                      <div key={category}>
                        <div className="ws-tools-group-head">
                          <div className="ws-tools-group-label">
                            {categoryLabels[category] || category}
                          </div>
                          {hasCategoryConfig ? (
                            <button
                              className="btn btn-secondary"
                              onClick={() => {
                                setConfigCategory(category);
                                setEditingConfig({});
                                const firstToolWithConfig = sortedCategoryTools.find((tool) => (tool.config_schema?.fields?.length ?? 0) > 0);
                                if (firstToolWithConfig?.config) {
                                  setEditingConfig({ ...firstToolWithConfig.config });
                                }
                              }}
                              title={`Configure ${category}`}
                            >
                              Configure
                            </button>
                          ) : null}
                        </div>
                        {category === 'web_pack' ? (
                          <div className="ws-tools-group-hint">
                            {t(
                              'enterprise.tools.webPackHint',
                              'No-key web research by default. Add provider keys only for higher limits or production control; XCrawl remains key-required.',
                            )}
                          </div>
                        ) : null}

                      <div className="ws-tools-list-sm">
                        {(() => {
                          // Split MCP tools into server groups; non-MCP tools render flat
                          const nonMcpTools = sortedCategoryTools.filter((tool) => tool.type !== 'mcp');
                          const mcpTools = sortedCategoryTools.filter((tool) => tool.type === 'mcp');
                          const mcpByServer: Record<string, any[]> = {};
                          for (const tool of mcpTools) {
                            const server = tool.mcp_server_name || 'MCP';
                            (mcpByServer[server] = mcpByServer[server] || []).push(tool);
                          }

                          const renderToolRow = (tool: WorkspaceToolRow) => {
                            const hasOwnConfig = (tool.config_schema?.fields?.length ?? 0) > 0 && !hasCategoryConfig;
                            const isEditing = editingToolId === tool.id;
                            const capability = resolveWorkspaceToolCapability(tool, capabilityDefinitions);
                            const capabilityPolicy = capability ? policyByCapability.get(capability) : undefined;
                            const governanceState = getWorkspaceToolGovernanceState({
                              tool,
                              capability,
                              policy: capabilityPolicy,
                            });
                            // Strip "ServerName: " prefix for MCP tools shown inside a server group
                            const displayName = tool.display_name || tool.name || tool.id;
                            const shortName = tool.type === 'mcp' && tool.mcp_server_name && displayName.startsWith(tool.mcp_server_name + ': ')
                              ? displayName.slice(tool.mcp_server_name.length + 2)
                              : displayName;
                            const providerAuth = getWorkspaceProviderAuthDisplay(tool.provider_auth, t);

                            return (
                              <div key={tool.id} className="card ws-tools-tool-card">
                                <div className="ws-tools-tool-row">
                                  <div className="ws-tools-tool-main">
                                    <ToolIcon tool={tool} />
                                    <div className="ws-tools-min0">
                                      <div className="ws-tools-row-6">
                                        <span className="ws-tools-name">{shortName}</span>
                                        {tool.type !== 'mcp' ? (
                                          <span className="ws-tools-tag ws-tools-tag-addon">
                                            {t('enterprise.tools.addOn', 'Add-on')}
                                          </span>
                                        ) : null}
                                        {tool.is_default ? (
                                          <span className="ws-tools-tag ws-tools-tag-default">Default</span>
                                        ) : null}
                                        {providerAuth ? (
                                          <span
                                            className={`ws-tools-tag ws-tools-tag-auth ${providerAuth.className}`}
                                            title={providerAuth.description}
                                          >
                                            {providerAuth.label}
                                          </span>
                                        ) : null}
                                      </div>
                                      <div className="ws-tools-desc-clip">
                                        {tool.description?.slice(0, 80)}
                                      </div>
                                    </div>
                                  </div>

                                  <div className="ws-tools-cell-shrink">
                                    {capability ? (
                                      <div className="ws-tools-row-6">
                                        <select
                                          className="form-input ws-tools-mode-select"
                                          value={governanceState.executionMode}
                                          disabled={tool.enabled === false || capabilityPolicyBusy === capability}
                                          onChange={async (event) => {
                                            await updateToolExecutionMode(capability, event.target.value as WorkspaceToolExecutionMode);
                                          }}
                                          title={t('enterprise.tools.executionMode', 'Execution mode')}
                                        >
                                          <option value="auto">{t('enterprise.tools.executionAuto', 'Auto allow')}</option>
                                          <option value="approval">{t('enterprise.tools.executionApproval', 'Require approval')}</option>
                                        </select>
                                        <span
                                          title={capability}
                                          className={`ws-tools-gov-status ${
                                            governanceState.effectiveStatus === 'legacy_denied'
                                              ? 'is-denied'
                                              : governanceState.effectiveStatus === 'approval_required'
                                                ? 'is-approval'
                                                : ''
                                          }`}
                                        >
                                          {effectiveStatusLabel(governanceState.effectiveStatus)}
                                        </span>
                                        {governanceState.effectiveStatus === 'legacy_denied' ? (
                                          <button
                                            className="btn btn-secondary"
                                            disabled={capabilityPolicyBusy === capability}
                                            onClick={async () => {
                                              await updateToolExecutionMode(capability, 'auto');
                                            }}
                                          >
                                            {t('enterprise.tools.restoreAutoAllow', 'Restore auto')}
                                          </button>
                                        ) : null}
                                      </div>
                                    ) : (
                                      <span className="ws-tools-gov-status">
                                        {effectiveStatusLabel('unmanaged')}
                                      </span>
                                    )}

                                    {hasOwnConfig ? (
                                      <button
                                        className="btn btn-secondary"
                                        onClick={() => {
                                          if (isEditing) {
                                            setEditingToolId(null);
                                          } else {
                                            setEditingToolId(tool.id);
                                            setEditingConfig({ ...(tool.config || {}) });
                                          }
                                        }}
                                      >
                                        {isEditing ? t('enterprise.tools.collapse', 'Collapse') : t('enterprise.tools.configure', 'Configure')}
                                      </button>
                                    ) : null}

                                    {tool.type !== 'builtin' ? (
                                      <button
                                        className="btn btn-danger"
                                        onClick={async () => {
                                          const confirmed = await requestAppConfirm({
                                            title: t('common.delete', 'Delete'),
                                            message: `${t('common.delete', 'Delete')} ${displayName}?`,
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

                                    <label className="ws-tools-toggle">
                                      <input
                                        type="checkbox"
                                        checked={tool.enabled !== false}
                                        onChange={async (event) => {
                                          try {
                                            await toolsApi.updateGlobalTool(tool.id, { enabled: event.target.checked });
                                            loadAllTools();
                                          } catch (error: any) {
                                            showAppToast(error?.message || t('enterprise.tools.updateFailed', 'Update failed'), 'error');
                                          }
                                        }}
                                        className="ws-tools-toggle-input"
                                      />
                                      <span className={`ws-tools-toggle-track ${tool.enabled !== false ? 'on' : ''}`}>
                                        <span className="ws-tools-toggle-knob" />
                                      </span>
                                    </label>
                                  </div>
                                </div>

                                {isEditing && hasOwnConfig ? (
                                  <div className="ws-tools-config-panel">
                                    <div className="ws-tools-col-12">
                                      {(tool.config_schema?.fields || []).map((field: any) => {
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
                                            <label className="ws-tools-field-label">{field.label}</label>
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
                                      <div className="ws-tools-row-8 ws-tools-mt-4">
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
                                  <div key={`mcp-server-${serverName}`} className="card ws-tools-tool-card">
                                    <div
                                      className="ws-tools-server-header"
                                      onClick={() => setCollapsedServers((prev) => {
                                        const next = new Set(prev);
                                        if (next.has(serverName)) next.delete(serverName);
                                        else next.add(serverName);
                                        return next;
                                      })}
                                    >
                                      <div className="ws-tools-row-10">
                                        <span className={`ws-tools-chevron ${isCollapsed ? '' : 'open'}`}>
                                          ▶
                                        </span>
                                        <span className="ws-tools-emoji">🔌</span>
                                        <div>
                                          <div className="ws-tools-row-6">
                                            <span className="ws-tools-title-13">{serverName}</span>
                                            <span className="ws-tools-tag ws-tools-tag-mcp">MCP</span>
                                            <span className="ws-tools-note">
                                              {enabledCount}/{serverTools.length} {t('enterprise.tools.toolsEnabled', 'enabled')}
                                            </span>
                                          </div>
                                          {serverTools[0]?.mcp_server_url ? (
                                            <div className="ws-tools-server-url">
                                              {serverTools[0].mcp_server_url.length > 60 ? serverTools[0].mcp_server_url.slice(0, 60) + '...' : serverTools[0].mcp_server_url}
                                            </div>
                                          ) : null}
                                        </div>
                                      </div>
                                      <button
                                        className="btn btn-danger"
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
                                      <div className="ws-tools-server-tools">
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
            <div className="ui-modal-overlay" onClick={() => setConfigCategory(null)}>
              <div onClick={(event) => event.stopPropagation()} className="ui-modal ws-tools-config-modal">
                <div className="ws-tools-section-head">
                  <div>
                    <h3 className="ws-tools-m0">{GLOBAL_CATEGORY_CONFIG_SCHEMAS[configCategory].title}</h3>
                    <div className="ws-tools-sub">Global configuration shared by all tools in this category</div>
                  </div>
                  <button onClick={() => setConfigCategory(null)} className="btn btn-ghost ws-tools-x">x</button>
                </div>
                <div className="ws-tools-col-12">
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
                        <label className="ws-tools-field-label">{field.label}</label>
                        {field.type === 'password' ? (
                          <input type="password" autoComplete="new-password" className="form-input" value={editingConfig[field.key] ?? ''} placeholder={field.placeholder || ''} onChange={(event) => setEditingConfig((current) => ({ ...current, [field.key]: event.target.value }))} />
                        ) : (
                          <input type="text" className="form-input" value={editingConfig[field.key] ?? ''} placeholder={field.placeholder || ''} onChange={(event) => setEditingConfig((current) => ({ ...current, [field.key]: event.target.value }))} />
                        )}
                      </div>
                    )
                  ))}
                  <div className="ws-tools-footer">
                    <button className="btn btn-secondary" onClick={() => setConfigCategory(null)}>
                      {t('common.cancel', 'Cancel')}
                    </button>
                    <button
                      className="btn btn-primary"
                      onClick={async () => {
                        const categoryTools = allTools.filter((tool) => isExtensionOrAddonTool(tool) && (tool.category || 'general') === configCategory && (tool.config_schema?.fields?.length ?? 0) > 0);
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
