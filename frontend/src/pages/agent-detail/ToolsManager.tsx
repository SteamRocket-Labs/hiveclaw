import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { toolsApi } from '../../api/domains/tools';
import { useAuthStore } from '../../stores';

type ToolsManagerProps = {
  agentId: string;
  canManage?: boolean;
};

const getCategoryLabels = (t: any): Record<string, string> => ({
  file: t('agent.toolCategories.file'),
  task: t('agent.toolCategories.task'),
  communication: t('agent.toolCategories.communication'),
  search: t('agent.toolCategories.search'),
  aware: t('agent.toolCategories.aware', 'Aware & Triggers'),
  social: t('agent.toolCategories.social', 'Social'),
  code: t('agent.toolCategories.code', 'Code & Execution'),
  discovery: t('agent.toolCategories.discovery', 'Discovery'),
  email: t('agent.toolCategories.email', 'Email'),
  feishu: t('agent.toolCategories.feishu', 'Feishu / Lark'),
  custom: t('agent.toolCategories.custom'),
  general: t('agent.toolCategories.general'),
  agentbay: t('agent.toolCategories.agentbay', 'AgentBay'),
});

export default function ToolsManager({ agentId, canManage = false }: ToolsManagerProps) {
  const { t } = useTranslation();
  const [tools, setTools] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [configTool, setConfigTool] = useState<any | null>(null);
  const [configData, setConfigData] = useState<Record<string, any>>({});
  const [configJson, setConfigJson] = useState('');
  const [configSaving, setConfigSaving] = useState(false);
  const [toolTab, setToolTab] = useState<'platform' | 'installed'>('platform');
  const [deletingToolId, setDeletingToolId] = useState<string | null>(null);
  const [configCategory, setConfigCategory] = useState<string | null>(null);

  const CATEGORY_CONFIG_SCHEMAS: Record<string, any> = {
    agentbay: {
      title: 'AgentBay Settings',
      fields: [
        { key: 'api_key', label: 'API Key (from AgentBay)', type: 'password', placeholder: 'Enter your AgentBay API key' },
      ],
    },
    atlassian: {
      title: 'Atlassian Connectivity Settings',
      fields: [
        { key: 'api_key', label: 'API Key (Atlassian API Token)', type: 'password', placeholder: 'Enter your Atlassian API key' },
        { key: 'cloud_id', label: 'Cloud ID (Optional)', type: 'text', placeholder: 'e.g. bcc01-abc-123' },
      ],
    },
  };

  const loadTools = async () => {
    try {
      const data = await toolsApi.listWithConfig(agentId).catch(() => toolsApi.list(agentId));
      setTools(data);
    } catch (error) {
      console.error(error);
    }
    setLoading(false);
  };

  useEffect(() => {
    void loadTools();
  }, [agentId]);

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

  const openCategoryConfig = async (category: string) => {
    setConfigCategory(category);
    setConfigData({});
    setConfigSaving(true);
    try {
      const data = await toolsApi.getCategoryConfig(agentId, category);
      setConfigData((data as any).config || {});
    } catch (error) {
      console.error(error);
    }
    setConfigSaving(false);
  };

  const saveConfig = async () => {
    if (!configTool && !configCategory) return;
    setConfigSaving(true);
    try {
      if (configCategory) {
        await toolsApi.updateCategoryConfig(agentId, configCategory, { config: configData });
        setConfigCategory(null);
      } else {
        const hasSchema = configTool.config_schema?.fields?.length > 0;
        const payload = hasSchema ? configData : JSON.parse(configJson || '{}');
        await toolsApi.updateToolConfig(agentId, configTool.id, payload);
        setConfigTool(null);
      }
      await loadTools();
    } catch (error) {
      alert(`Save failed: ${error}`);
    }
    setConfigSaving(false);
  };

  if (loading) {
    return <div style={{ color: 'var(--text-tertiary)', padding: '20px' }}>{t('common.loading')}</div>;
  }

  const systemTools = tools.filter((tool) => tool.source !== 'user_installed');
  const agentInstalledTools = tools.filter((tool) => tool.source === 'user_installed');

  const groupByCategory = (toolList: any[]) =>
    toolList.reduce((acc: Record<string, any[]>, tool) => {
      const category = tool.category || 'general';
      (acc[category] = acc[category] || []).push(tool);
      return acc;
    }, {});

  const renderToolGroup = (groupedTools: Record<string, any[]>) =>
    Object.entries(groupedTools).map(([category, categoryTools]) => (
      <div key={category}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
          <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            {getCategoryLabels(t)[category] || category}
          </div>
          {CATEGORY_CONFIG_SCHEMAS[category] && canManage && (
            <button
              onClick={() => void openCategoryConfig(category)}
              style={{ background: 'none', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '3px 8px', fontSize: '11px', cursor: 'pointer', color: 'var(--text-secondary)' }}
              title={`Configure ${category}`}
            >
              ⚙️ Config
            </button>
          )}
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
          {categoryTools.map((tool: any) => {
            const hasConfig = tool.config_schema?.fields?.length > 0 || tool.type === 'mcp';
            const hasAgentOverride = tool.agent_config && Object.keys(tool.agent_config).length > 0;
            const isGlobalCategoryConfig = category === 'agentbay' && tool.name === 'agentbay_browser_navigate';
            return (
              <div key={tool.id} className="card" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 14px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flex: 1, minWidth: 0 }}>
                  <span style={{ fontSize: '18px' }}>{tool.icon}</span>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                      <span style={{ fontWeight: 500, fontSize: '13px' }}>{tool.display_name}</span>
                      {tool.type === 'mcp' && (
                        <span style={{ fontSize: '10px', background: 'var(--primary)', color: '#fff', borderRadius: '4px', padding: '1px 5px' }}>MCP</span>
                      )}
                      {tool.type === 'builtin' && (
                        <span style={{ fontSize: '10px', background: 'var(--bg-tertiary)', color: 'var(--text-secondary)', borderRadius: '4px', padding: '1px 5px' }}>Built-in</span>
                      )}
                      {hasAgentOverride && (
                        <span style={{ fontSize: '10px', background: 'rgba(99,102,241,0.15)', color: 'var(--accent-color)', borderRadius: '4px', padding: '1px 5px' }}>Configured</span>
                      )}
                    </div>
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {tool.description}
                      {tool.mcp_server_name && <span> · {tool.mcp_server_name}</span>}
                    </div>
                  </div>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 }}>
                  {canManage && hasConfig && !isGlobalCategoryConfig && (
                    <button
                      onClick={() => openConfig(tool)}
                      style={{ background: 'none', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '3px 8px', fontSize: '11px', cursor: 'pointer', color: 'var(--text-secondary)' }}
                      title="Configure per-agent settings"
                    >
                      ⚙️ Config
                    </button>
                  )}
                  {canManage && tool.source === 'user_installed' && tool.agent_tool_id && (
                    <button
                      onClick={async () => {
                        if (!confirm(t('agent.tools.confirmDelete', `Remove "${tool.display_name}" from this agent?`))) return;
                        setDeletingToolId(tool.id);
                        try {
                          await toolsApi.removeAgentTool(tool.agent_tool_id);
                          await loadTools();
                        } catch (error) {
                          alert(`Delete failed: ${error}`);
                        }
                        setDeletingToolId(null);
                      }}
                      disabled={deletingToolId === tool.id}
                      style={{ background: 'none', border: '1px solid var(--border-subtle)', borderRadius: '6px', padding: '3px 8px', fontSize: '11px', cursor: 'pointer', color: 'var(--text-tertiary)', opacity: deletingToolId === tool.id ? 0.5 : 1 }}
                      title={t('agent.tools.removeTool', 'Remove from agent')}
                    >
                      {deletingToolId === tool.id ? '...' : '✕'}
                    </button>
                  )}
                  {canManage ? (
                    <label style={{ position: 'relative', display: 'inline-block', width: '40px', height: '22px', cursor: 'pointer', flexShrink: 0 }}>
                      <input
                        type="checkbox"
                        checked={tool.enabled}
                        onChange={(event) => void toggleTool(tool.id, event.target.checked)}
                        style={{ opacity: 0, width: 0, height: 0 }}
                      />
                      <span
                        style={{
                          position: 'absolute',
                          inset: 0,
                          background: tool.enabled ? '#22c55e' : 'var(--bg-tertiary)',
                          borderRadius: '11px',
                          transition: 'background 0.2s',
                        }}
                      >
                        <span
                          style={{
                            position: 'absolute',
                            left: tool.enabled ? '20px' : '2px',
                            top: '2px',
                            width: '18px',
                            height: '18px',
                            background: '#fff',
                            borderRadius: '50%',
                            transition: 'left 0.2s',
                          }}
                        />
                      </span>
                    </label>
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
      </div>
    ));

  const activeTools = toolTab === 'platform' ? systemTools : agentInstalledTools;

  return (
    <>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        <div style={{ display: 'flex', gap: '2px', background: 'var(--bg-tertiary)', borderRadius: '8px', padding: '3px' }}>
          <button
            onClick={() => setToolTab('platform')}
            style={{
              flex: 1,
              padding: '7px 12px',
              border: 'none',
              borderRadius: '6px',
              cursor: 'pointer',
              fontSize: '12px',
              fontWeight: 600,
              transition: 'all 0.2s',
              background: toolTab === 'platform' ? 'var(--bg-primary)' : 'transparent',
              color: toolTab === 'platform' ? 'var(--text-primary)' : 'var(--text-tertiary)',
              boxShadow: toolTab === 'platform' ? '0 1px 3px rgba(0,0,0,0.1)' : 'none',
            }}
          >
            🔧 {t('agent.tools.platformTools', 'Platform Tools')} ({systemTools.length})
          </button>
          <button
            onClick={() => setToolTab('installed')}
            style={{
              flex: 1,
              padding: '7px 12px',
              border: 'none',
              borderRadius: '6px',
              cursor: 'pointer',
              fontSize: '12px',
              fontWeight: 600,
              transition: 'all 0.2s',
              background: toolTab === 'installed' ? 'var(--bg-primary)' : 'transparent',
              color: toolTab === 'installed' ? 'var(--text-primary)' : 'var(--text-tertiary)',
              boxShadow: toolTab === 'installed' ? '0 1px 3px rgba(0,0,0,0.1)' : 'none',
            }}
          >
            🤖 {t('agent.tools.agentInstalled', 'Agent-Installed Tools')} ({agentInstalledTools.length})
          </button>
        </div>

        {activeTools.length > 0 ? (
          renderToolGroup(groupByCategory(activeTools))
        ) : (
          <div className="card" style={{ textAlign: 'center', padding: '30px', color: 'var(--text-tertiary)' }}>
            {toolTab === 'installed' ? t('agent.tools.noInstalled', 'No agent-installed tools yet') : t('common.noData')}
          </div>
        )}
      </div>
      {tools.length === 0 && (
        <div className="card" style={{ textAlign: 'center', padding: '30px', color: 'var(--text-tertiary)' }}>
          {t('common.noData')}
        </div>
      )}

      {(configTool || configCategory) &&
        (() => {
          const target = configTool || CATEGORY_CONFIG_SCHEMAS[configCategory!];
          const fields = configTool ? (configTool.config_schema?.fields || []) : target.fields || [];
          const title = configTool ? configTool.display_name : target.title;
          const isCategoryConfig = !!configCategory;
          return (
            <div
              style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(0,0,0,0.55)', zIndex: 2000, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
              onClick={() => {
                setConfigTool(null);
                setConfigCategory(null);
              }}
            >
              <div
                onClick={(event) => event.stopPropagation()}
                style={{ background: 'var(--bg-primary)', borderRadius: '12px', padding: '24px', width: '480px', maxWidth: '95vw', maxHeight: '80vh', overflow: 'auto', boxShadow: '0 20px 60px rgba(0,0,0,0.4)' }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                  <div>
                    <h3 style={{ margin: 0 }}>⚙️ {title}</h3>
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                      {isCategoryConfig ? 'Shared category configuration (affects all tools in this category)' : 'Per-agent configuration (overrides global defaults)'}
                    </div>
                  </div>
                  <button onClick={() => { setConfigTool(null); setConfigCategory(null); }} style={{ background: 'none', border: 'none', fontSize: '18px', cursor: 'pointer', color: 'var(--text-secondary)' }}>✕</button>
                </div>

                {fields.length > 0 ? (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                    {fields
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
                              {configTool?.global_config?.[field.key] && (
                                <span style={{ fontWeight: 400, color: 'var(--text-tertiary)', marginLeft: '4px' }}>
                                  (global: {String(configTool.global_config[field.key]).slice(0, 20)}
                                  {String(configTool.global_config[field.key]).length > 20 ? '…' : ''})
                                </span>
                              )}
                            </label>
                            {field.type === 'checkbox' ? (
                              <label style={{ position: 'relative', display: 'inline-block', width: '40px', height: '22px', cursor: isReadOnly ? 'not-allowed' : 'pointer' }}>
                                <input
                                  type="checkbox"
                                  checked={configData[field.key] ?? field.default ?? false}
                                  disabled={isReadOnly}
                                  onChange={(event) => setConfigData((previous) => ({ ...previous, [field.key]: event.target.checked }))}
                                  style={{ opacity: 0, width: 0, height: 0 }}
                                />
                                <span
                                  style={{
                                    position: 'absolute',
                                    inset: 0,
                                    background: configData[field.key] ?? field.default ? '#22c55e' : 'var(--bg-tertiary)',
                                    borderRadius: '11px',
                                    transition: 'background 0.2s',
                                    opacity: isReadOnly ? 0.6 : 1,
                                  }}
                                >
                                  <span
                                    style={{
                                      position: 'absolute',
                                      left: configData[field.key] ?? field.default ? '20px' : '2px',
                                      top: '2px',
                                      width: '18px',
                                      height: '18px',
                                      background: '#fff',
                                      borderRadius: '50%',
                                      transition: 'left 0.2s',
                                    }}
                                  />
                                </span>
                              </label>
                            ) : field.type === 'password' ? (
                              <>
                                <input
                                  type="password"
                                  autoComplete="new-password"
                                  className="form-input"
                                  value={configData[field.key] ?? ''}
                                  placeholder={field.placeholder || 'Leave blank to use global default'}
                                  onChange={(event) => setConfigData((previous) => ({ ...previous, [field.key]: event.target.value }))}
                                />
                                {field.key === 'auth_code' &&
                                  (() => {
                                    const providerField = configTool?.config_schema?.fields?.find((schemaField: any) => schemaField.key === 'email_provider');
                                    const selectedProvider = configData.email_provider || providerField?.default || '';
                                    const providerOption = providerField?.options?.find((option: any) => option.value === selectedProvider);
                                    if (!providerOption?.help_text) return null;
                                    return (
                                      <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px', lineHeight: '1.5' }}>
                                        {providerOption.help_text}
                                        {providerOption.help_url && (
                                          <>
                                            {' '}
                                            &middot;{' '}
                                            <a href={providerOption.help_url} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--accent-primary)', textDecoration: 'none' }}>
                                              Setup guide
                                            </a>
                                          </>
                                        )}
                                      </div>
                                    );
                                  })()}
                              </>
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
                                onChange={(event) =>
                                  setConfigData((previous) => ({
                                    ...previous,
                                    [field.key]: event.target.value ? Number(event.target.value) : '',
                                  }))
                                }
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
                    {configTool?.category === 'email' && (
                      <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: '12px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
                        <button
                          className="btn btn-secondary"
                          style={{ alignSelf: 'flex-start' }}
                          onClick={async () => {
                            const button = document.getElementById('email-test-btn');
                            const status = document.getElementById('email-test-status');
                            if (button) button.textContent = 'Testing...';
                            if (button) (button as HTMLButtonElement).disabled = true;
                            try {
                              const data = (await toolsApi.testEmail({ config: configData })) as any;
                              if (status) {
                                status.textContent = data.ok ? `${data.imap}\n${data.smtp}` : `${data.imap || ''}\n${data.smtp || ''}\n${data.error || ''}`;
                                status.style.color = data.ok ? 'var(--success)' : 'var(--error)';
                              }
                            } catch (error: any) {
                              if (status) {
                                status.textContent = `Error: ${error.message}`;
                                status.style.color = 'var(--error)';
                              }
                            } finally {
                              if (button) {
                                button.textContent = 'Test Connection';
                                (button as HTMLButtonElement).disabled = false;
                              }
                            }
                          }}
                          id="email-test-btn"
                        >
                          Test Connection
                        </button>
                        <div id="email-test-status" style={{ fontSize: '11px', whiteSpace: 'pre-line', minHeight: '16px' }} />
                      </div>
                    )}
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
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                      Global default: <code style={{ fontSize: '10px' }}>{JSON.stringify(configTool?.global_config || {}).slice(0, 80)}</code>
                    </div>
                  </div>
                )}

                <div style={{ display: 'flex', gap: '8px', marginTop: '16px', justifyContent: 'flex-end' }}>
                  {configTool && configTool.agent_config && Object.keys(configTool.agent_config || {}).length > 0 && (
                    <button
                      className="btn btn-ghost"
                      style={{ color: 'var(--error)', marginRight: 'auto' }}
                      onClick={async () => {
                        await toolsApi.updateToolConfig(agentId, configTool.id, {});
                        setConfigTool(null);
                        await loadTools();
                      }}
                    >
                      Reset to Global
                    </button>
                  )}
                  {isCategoryConfig && (
                    <button
                      className="btn btn-secondary"
                      style={{ marginRight: 'auto' }}
                      onClick={async () => {
                        const button = document.getElementById('cat-test-btn');
                        if (button) button.textContent = 'Testing...';
                        try {
                          const data = (await toolsApi.testCategory(agentId, configCategory!)) as any;
                          alert(data.message || (data.ok ? '✅ Test successful' : `❌ Test failed: ${data.error}`));
                        } catch (error: any) {
                          alert(`Test failed: ${error.message}`);
                        } finally {
                          if (button) button.textContent = 'Test Connection';
                        }
                      }}
                      id="cat-test-btn"
                    >
                      Test Connection
                    </button>
                  )}
                  <button className="btn btn-secondary" onClick={() => { setConfigTool(null); setConfigCategory(null); }}>Cancel</button>
                  <button className="btn btn-primary" onClick={() => void saveConfig()} disabled={configSaving}>
                    {configSaving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
                  </button>
                </div>
              </div>
            </div>
          );
        })()}
    </>
  );
}
