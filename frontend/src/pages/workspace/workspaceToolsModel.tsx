import type { CapabilityDefinition, CapabilityPolicy } from '../../api/domains/enterprise';

export interface WorkspaceToolsViewProps {
  selectedTenantId: string;
}

export type ToolConfigField = {
  key: string;
  label?: string;
  placeholder?: string;
  description?: string;
  [key: string]: unknown;
};

export type ToolGovernanceTaxonomy = {
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

export type WorkspaceToolRow = {
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

export const GLOBAL_CATEGORY_CONFIG_SCHEMAS: Record<string, { title: string; fields: any[] }> = {
  agentbay: {
    title: 'AgentBay Settings',
    fields: [
      { key: 'api_key', label: 'API Key (from AgentBay)', type: 'password', placeholder: 'Enter your AgentBay API key' },
    ],
  },
};

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

export function getWorkspaceToolCategoryRank(category?: string): number {
  if (category === 'web_pack') return 0;
  if (category === 'office_pack') return 20;
  if (category === 'mcp') return 90;
  return 50;
}

export function isExtensionOrAddonTool(tool: Pick<WorkspaceToolRow, 'type' | 'name' | 'governance_taxonomy'>): boolean {
  const taxonomy = tool.governance_taxonomy;
  if (taxonomy?.l2_visible === true && taxonomy?.enterprise_toggleable === true) return true;
  if (taxonomy?.layer === 'agent_base') return false;
  return tool.type === 'mcp' || tool.type === 'custom_api' || String(tool.name || '').startsWith('custom_api__');
}

export function resolveWorkspaceToolCapability(
  tool: Pick<WorkspaceToolRow, 'name'>,
  definitions: Pick<CapabilityDefinition, 'capability' | 'tools'>[],
): string | null {
  const toolName = String(tool.name || '').trim();
  if (!toolName) return null;
  for (const definition of definitions) {
    if (definition.tools.includes(toolName)) return definition.capability;
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
  if (tool.enabled === false) return { executionMode, effectiveStatus: 'disabled' };
  if (!capability) return { executionMode, effectiveStatus: 'unmanaged' };
  if (policy?.allowed === false) return { executionMode, effectiveStatus: 'legacy_denied' };
  if (executionMode === 'approval') return { executionMode, effectiveStatus: 'approval_required' };
  return { executionMode, effectiveStatus: 'auto_allowed' };
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
        description: t('enterprise.tools.providerAuthNoKeyDefaultDesc', 'Runs without an API key by default; optional keys only raise limits or production control.'),
        className: 'is-no-key',
      };
    case 'key_required':
      return {
        label: t('enterprise.tools.providerAuthKeyRequired', 'Key required'),
        description: t('enterprise.tools.providerAuthKeyRequiredDesc', 'Requires a configured provider key before agents can use it.'),
        className: 'is-key-required',
      };
    case 'optional_key':
    default:
      return {
        label: t('enterprise.tools.providerAuthOptionalKey', 'Optional key'),
        description: t('enterprise.tools.providerAuthOptionalKeyDesc', 'Works without a key by default; add a key for higher limits or production control.'),
        className: 'is-optional-key',
      };
  }
}

export function normalizeToolConfigListValue(value: unknown, options: { preserveEmpty?: boolean } = {}): string[] {
  const parts = Array.isArray(value)
    ? value.map((item) => String(item).trim())
    : typeof value === 'string'
      ? value.split(/[\n,]+/).map((item) => item.trim())
      : [];
  return options.preserveEmpty ? parts : parts.filter(Boolean);
}

export function countToolConfigListValues(value: unknown): number {
  return normalizeToolConfigListValue(value).length;
}

export function workspaceToolCategoryLabels(t: (key: string, fallback: string) => string): Record<string, string> {
  return Object.fromEntries([
    ['file', 'File'], ['filesystem', 'Filesystem'], ['task', 'Task'], ['tasks', 'Tasks'],
    ['communication', 'Communication'], ['search', 'Search'], ['aware', 'Aware & Triggers'],
    ['triggers', 'Triggers'], ['skills', 'Skills'], ['memory', 'Memory'], ['hr', 'HR'],
    ['mcp', 'MCP'], ['plaza', 'Agent Circle'], ['web_pack', 'Web Research'], ['office_pack', 'Office'],
    ['social', 'Social'], ['code', 'Code & Execution'], ['discovery', 'Discovery'], ['email', 'Email'],
    ['feishu', 'Feishu / Lark'], ['custom', 'Custom'], ['general', 'General'], ['agentbay', 'AgentBay'],
  ].map(([category, fallback]) => [category, t(`agent.toolCategories.${category}`, fallback)]));
}

export function workspaceToolEffectiveStatusLabel(
  status: WorkspaceToolEffectiveStatus,
  t: (key: string, fallback: string) => string,
): string {
  const labels: Record<WorkspaceToolEffectiveStatus, [string, string]> = {
    disabled: ['enterprise.tools.effectiveDisabled', 'Disabled'],
    approval_required: ['enterprise.tools.effectiveApprovalRequired', 'Requires company approval'],
    legacy_denied: ['enterprise.tools.effectiveLegacyDenied', 'Legacy deny policy'],
    unmanaged: ['enterprise.tools.effectiveUnmanaged', 'Connector policy'],
    auto_allowed: ['enterprise.tools.effectiveAutoAllowed', 'Auto allowed'],
  };
  return t(...labels[status]);
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
  const updateRows = (nextRows: string[]) => onChange(nextRows.join('\n'));

  return (
    <div>
      <div className="ws-tools-field-head">
        <label className="ws-tools-secret-label">{label}</label>
        <span className="ws-tools-count-pill">{count} {count === 1 ? 'key' : 'keys'} configured</span>
      </div>
      <div className="ws-tools-col-8">
        {visibleRows.map((row, index) => (
          <div key={`${field.key}-${index}`} className="ws-tools-row-8">
            <input
              type="password"
              autoComplete="new-password"
              className="form-input"
              value={row}
              placeholder={index === 0 ? field.placeholder || 'Enter one key per row' : `API key #${index + 1}`}
              aria-label={`${label} #${index + 1}`}
              onChange={(event) => {
                const nextRows = [...visibleRows];
                nextRows[index] = event.target.value;
                updateRows(nextRows);
              }}
            />
            <button type="button" className="btn btn-secondary" onClick={() => {
              const nextRows = visibleRows.filter((_, rowIndex) => rowIndex !== index);
              updateRows(nextRows.length > 0 ? nextRows : ['']);
            }}>Remove</button>
          </div>
        ))}
      </div>
      <div className="ws-tools-field-foot">
        <div className="ws-tools-note">
          Enter one API key per row. Calls rotate across saved keys in order.
          {field.description ? ` ${field.description}` : ''}
        </div>
        <button type="button" className="btn btn-secondary" onClick={() => updateRows([...visibleRows, ''])}>Add key</button>
      </div>
    </div>
  );
}
