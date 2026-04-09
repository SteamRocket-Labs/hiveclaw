import { useEffect, useState } from 'react';

import { useTranslation } from 'react-i18next';

import { toolsApi } from '../../api/domains/tools';

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

export default function WorkspaceToolsSection({
  selectedTenantId,
}: WorkspaceToolsSectionProps) {
  const { t } = useTranslation();
  const categoryLabels: Record<string, string> = {
    file: t('agent.toolCategories.file', 'File'),
    task: t('agent.toolCategories.task', 'Task'),
    communication: t('agent.toolCategories.communication', 'Communication'),
    search: t('agent.toolCategories.search', 'Search'),
    aware: t('agent.toolCategories.aware', 'Aware & Triggers'),
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
  const [toolsView, setToolsView] = useState<'global' | 'agent-installed'>('global');
  const [agentInstalledTools, setAgentInstalledTools] = useState<any[]>([]);
  const [collapsedServers, setCollapsedServers] = useState<Set<string>>(new Set());

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
          ['agent-installed', t('enterprise.tools.agentInstalled', 'Agent Installed')],
        ] as const).map(([key, label]) => (
          <button
            key={key}
            onClick={() => {
              setToolsView(key);
              if (key === 'agent-installed') {
                loadAgentInstalledTools();
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
                      if (!confirm(t('enterprise.tools.removeFromAgent', { name: row.tool_display_name }))) return;
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
                                  alert(`${t('enterprise.tools.importFailed', 'Import failed')}: ${error.message}`);
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
                                alert(`Imported ${successCount}/${tools.length} tools.\nFailed:\n${errors.join('\n')}`);
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
                                    <span style={{ fontSize: '18px' }}>{tool.icon}</span>
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
                                          if (!confirm(`${t('common.delete', 'Delete')} ${tool.display_name}?`)) return;
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
                                          if (!confirm(`${t('common.delete', 'Delete')} ${serverName} (${serverTools.length} tools)?`)) return;
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
                    <div key={field.key}>
                      <label style={{ display: 'block', fontSize: '12px', fontWeight: 500, marginBottom: '4px' }}>{field.label}</label>
                      {field.type === 'password' ? (
                        <input type="password" autoComplete="new-password" className="form-input" value={editingConfig[field.key] ?? ''} placeholder={field.placeholder || ''} onChange={(event) => setEditingConfig((current) => ({ ...current, [field.key]: event.target.value }))} />
                      ) : (
                        <input type="text" className="form-input" value={editingConfig[field.key] ?? ''} placeholder={field.placeholder || ''} onChange={(event) => setEditingConfig((current) => ({ ...current, [field.key]: event.target.value }))} />
                      )}
                    </div>
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
