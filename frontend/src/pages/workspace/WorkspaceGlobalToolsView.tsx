import { useEffect, useState } from 'react';

import { useTranslation } from 'react-i18next';

import { enterpriseApi, type CapabilityDefinition, type CapabilityPolicy } from '../../api/domains/enterprise';
import { toolsApi } from '../../api/domains/tools';
import { requestAppConfirm, showAppToast } from '../../components/AppDialogs';
import ToolIcon from '../../components/ToolIcon';
import {
  GLOBAL_CATEGORY_CONFIG_SCHEMAS,
  ToolConfigSecretListField,
  getWorkspaceProviderAuthDisplay,
  getWorkspaceToolCategoryRank,
  getWorkspaceToolGovernanceState,
  isExtensionOrAddonTool,
  resolveWorkspaceToolCapability,
  sortWorkspaceToolsForDisplay,
  workspaceToolCategoryLabels,
  workspaceToolEffectiveStatusLabel,
  type WorkspaceToolExecutionMode,
  type WorkspaceToolRow,
  type WorkspaceToolsViewProps,
} from './workspaceToolsModel';

export default function WorkspaceGlobalToolsView({
  selectedTenantId,
}: WorkspaceToolsViewProps) {
  const { t } = useTranslation();
  const categoryLabels = workspaceToolCategoryLabels(t);

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
  const [collapsedServers, setCollapsedServers] = useState<Set<string>>(new Set());

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

  useEffect(() => {
    loadAllTools();
    loadCapabilityGovernance();
  }, [selectedTenantId]);

  const policyByCapability = new Map(
    capabilityPolicies
      .filter((policy) => !policy.agent_id)
      .map((policy) => [policy.capability, policy] as const),
  );

  const effectiveStatusLabel = (status: Parameters<typeof workspaceToolEffectiveStatusLabel>[0]) => (
    workspaceToolEffectiveStatusLabel(status, t)
  );

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
    </div>
  );
}
