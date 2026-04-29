import React from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';

import ChannelConfig from '../../components/ChannelConfig';
import { agentApi } from '../../api/domains/agents';
import { enterpriseApi, type CapabilityDefinition, type CapabilityPolicy } from '../../api/domains/enterprise';

type AgentSettingsForm = {
  primary_model_id: string;
  fallback_model_id: string;
  max_triggers: number;
  min_poll_interval_min: number;
  webhook_rate_limit: number;
  smart_model_routing_enabled: boolean;
  security_zone: string;
};

type CapabilityPolicyMode = 'auto' | 'approval' | 'deny';

const policyToMode = (policy?: CapabilityPolicy): CapabilityPolicyMode => {
  if (!policy) return 'auto';
  if (!policy.allowed) return 'deny';
  return policy.requires_approval ? 'approval' : 'auto';
};

const modeToPolicy = (mode: CapabilityPolicyMode) => {
  if (mode === 'deny') return { allowed: false, requires_approval: false };
  if (mode === 'approval') return { allowed: true, requires_approval: true };
  return { allowed: true, requires_approval: false };
};

type CapabilityActionMeta = {
  key: string;
  capability: string;
  labelKey: string;
  descKey: string;
  fallbackLabel: string;
  fallbackDesc: string;
};

const KNOWN_CAPABILITY_ACTIONS: CapabilityActionMeta[] = [
  {
    key: 'read_files',
    capability: 'workspace.file.read',
    labelKey: 'readFiles',
    descKey: 'readFilesDesc',
    fallbackLabel: 'Read Files',
    fallbackDesc: 'View files in the workspace and knowledge base',
  },
  {
    key: 'write_workspace_files',
    capability: 'workspace.file.write',
    labelKey: 'writeFiles',
    descKey: 'writeFilesDesc',
    fallbackLabel: 'Write Files',
    fallbackDesc: 'Create or edit files in the workspace',
  },
  {
    key: 'delete_files',
    capability: 'workspace.file.delete',
    labelKey: 'deleteFiles',
    descKey: 'deleteFilesDesc',
    fallbackLabel: 'Delete Files',
    fallbackDesc: 'Remove files from the workspace',
  },
  {
    key: 'execute_code',
    capability: 'workspace.code.execute',
    labelKey: 'executeCode',
    descKey: 'executeCodeDesc',
    fallbackLabel: 'Run Code',
    fallbackDesc: 'Execute scripts in a secure sandbox',
  },
  {
    key: 'run_command',
    capability: 'workspace.command.execute',
    labelKey: 'runCommand',
    descKey: 'runCommandDesc',
    fallbackLabel: 'Run Shell Commands',
    fallbackDesc: 'Run shell commands in the agent workspace',
  },
  {
    key: 'dangerous_commands',
    capability: 'workspace.command.dangerous',
    labelKey: 'dangerousCommands',
    descKey: 'dangerousCommandsDesc',
    fallbackLabel: 'Dangerous Commands',
    fallbackDesc: 'Recursive deletes, SQL destructive commands, sudo, or permission changes',
  },
  {
    key: 'secret_reads',
    capability: 'workspace.command.secret_exfiltration',
    labelKey: 'secretReads',
    descKey: 'secretReadsDesc',
    fallbackLabel: 'Secret/Environment Reads',
    fallbackDesc: 'Commands that inspect environment variables, secrets, or tokens',
  },
  {
    key: 'read_tasks',
    capability: 'agent.task.read',
    labelKey: 'readTasks',
    descKey: 'readTasksDesc',
    fallbackLabel: 'Read Tasks',
    fallbackDesc: 'View task records',
  },
  {
    key: 'manage_tasks',
    capability: 'agent.task.modify',
    labelKey: 'manageTasks',
    descKey: 'manageTasksDesc',
    fallbackLabel: 'Manage Tasks',
    fallbackDesc: 'Create, update, or complete tasks',
  },
  {
    key: 'read_objectives',
    capability: 'agent.objective.read',
    labelKey: 'readObjectives',
    descKey: 'readObjectivesDesc',
    fallbackLabel: 'Read Objectives',
    fallbackDesc: 'View durable objective records',
  },
  {
    key: 'manage_objectives',
    capability: 'agent.objective.modify',
    labelKey: 'manageObjectives',
    descKey: 'manageObjectivesDesc',
    fallbackLabel: 'Objectives',
    fallbackDesc: 'Create, update, or complete durable objectives',
  },
  {
    key: 'read_memory',
    capability: 'agent.memory.read',
    labelKey: 'readMemory',
    descKey: 'readMemoryDesc',
    fallbackLabel: 'Read Memory',
    fallbackDesc: 'Search long-term memory and past sessions',
  },
  {
    key: 'write_memory',
    capability: 'agent.memory.write',
    labelKey: 'writeMemory',
    descKey: 'writeMemoryDesc',
    fallbackLabel: 'Write Memory',
    fallbackDesc: 'Save facts into long-term memory',
  },
  {
    key: 'read_skills',
    capability: 'agent.skill.read',
    labelKey: 'readSkills',
    descKey: 'readSkillsDesc',
    fallbackLabel: 'Read Skills',
    fallbackDesc: 'Load reusable skill instructions',
  },
  {
    key: 'write_skills',
    capability: 'agent.skill.write',
    labelKey: 'writeSkills',
    descKey: 'writeSkillsDesc',
    fallbackLabel: 'Write Skills',
    fallbackDesc: 'Create or update reusable skills',
  },
  {
    key: 'discover_tools',
    capability: 'agent.tool.discover',
    labelKey: 'discoverTools',
    descKey: 'discoverToolsDesc',
    fallbackLabel: 'Discover Tools',
    fallbackDesc: 'Search available tools, skills, and capability packs',
  },
  {
    key: 'install_mcp_server',
    capability: 'agent.tool.install',
    labelKey: 'installMcp',
    descKey: 'installMcpDesc',
    fallbackLabel: 'Install Extensions',
    fallbackDesc: 'Add third-party tool extensions',
  },
  {
    key: 'read_mcp_resources',
    capability: 'agent.mcp.read',
    labelKey: 'readMcp',
    descKey: 'readMcpDesc',
    fallbackLabel: 'Read MCP Resources',
    fallbackDesc: 'List or read resources from connected MCP servers',
  },
  {
    key: 'read_triggers',
    capability: 'agent.trigger.read',
    labelKey: 'readTriggers',
    descKey: 'readTriggersDesc',
    fallbackLabel: 'Read Triggers',
    fallbackDesc: 'View automation triggers',
  },
  {
    key: 'manage_triggers',
    capability: 'agent.trigger.modify',
    labelKey: 'manageTriggers',
    descKey: 'manageTriggersDesc',
    fallbackLabel: 'Manage Triggers',
    fallbackDesc: 'Create, update, or cancel automation triggers',
  },
  {
    key: 'send_agent_message',
    capability: 'agent.message.send',
    labelKey: 'sendAgentMessage',
    descKey: 'sendAgentMessageDesc',
    fallbackLabel: 'Agent Messaging',
    fallbackDesc: 'Message or delegate work to other digital employees',
  },
  {
    key: 'read_async_tasks',
    capability: 'agent.async_task.read',
    labelKey: 'readAsyncTasks',
    descKey: 'readAsyncTasksDesc',
    fallbackLabel: 'Read Async Tasks',
    fallbackDesc: 'Check delegated task status',
  },
  {
    key: 'manage_async_tasks',
    capability: 'agent.async_task.modify',
    labelKey: 'manageAsyncTasks',
    descKey: 'manageAsyncTasksDesc',
    fallbackLabel: 'Manage Async Tasks',
    fallbackDesc: 'Cancel delegated background tasks',
  },
  {
    key: 'create_employee',
    capability: 'agent.employee.create',
    labelKey: 'createEmployee',
    descKey: 'createEmployeeDesc',
    fallbackLabel: 'Create Employees',
    fallbackDesc: 'Preview or create digital employee colleagues',
  },
  {
    key: 'send_email',
    capability: 'channel.email.send',
    labelKey: 'sendEmail',
    descKey: 'sendEmailDesc',
    fallbackLabel: 'Send Email',
    fallbackDesc: 'Send or reply to emails',
  },
  {
    key: 'read_email',
    capability: 'channel.email.read',
    labelKey: 'readEmail',
    descKey: 'readEmailDesc',
    fallbackLabel: 'Read Email',
    fallbackDesc: 'Read mailbox messages',
  },
  {
    key: 'send_channel_message',
    capability: 'channel.message.send',
    labelKey: 'sendChannelMessage',
    descKey: 'sendChannelMessageDesc',
    fallbackLabel: 'Send Channel Messages',
    fallbackDesc: 'Reply through the active web, Feishu, Telegram, WeCom, Slack, Discord, or WeChat channel',
  },
  {
    key: 'send_channel_file',
    capability: 'channel.file.send',
    labelKey: 'sendChannelFile',
    descKey: 'sendChannelFileDesc',
    fallbackLabel: 'Send Channel Files',
    fallbackDesc: 'Send files or uploaded images through a communication channel',
  },
  {
    key: 'send_feishu_message',
    capability: 'channel.feishu.message',
    labelKey: 'sendFeishu',
    descKey: 'sendFeishuDesc',
    fallbackLabel: 'Send Feishu Messages',
    fallbackDesc: 'Send Feishu messages directly to people',
  },
  {
    key: 'feishu_documents',
    capability: 'channel.feishu.document',
    labelKey: 'feishuDocs',
    descKey: 'feishuDocsDesc',
    fallbackLabel: 'Feishu Documents',
    fallbackDesc: 'Create, update, share, read, or delete Feishu documents',
  },
  {
    key: 'feishu_base',
    capability: 'channel.feishu.base',
    labelKey: 'feishuBase',
    descKey: 'feishuBaseDesc',
    fallbackLabel: 'Feishu Base',
    fallbackDesc: 'Create, read, update, or delete Base records and fields',
  },
  {
    key: 'feishu_spreadsheet',
    capability: 'channel.feishu.spreadsheet',
    labelKey: 'feishuSpreadsheet',
    descKey: 'feishuSpreadsheetDesc',
    fallbackLabel: 'Feishu Spreadsheets',
    fallbackDesc: 'Read Feishu spreadsheet information and values',
  },
  {
    key: 'feishu_tasks',
    capability: 'channel.feishu.task',
    labelKey: 'feishuTasks',
    descKey: 'feishuTasksDesc',
    fallbackLabel: 'Feishu Tasks',
    fallbackDesc: 'Create, complete, list, or comment on Feishu tasks',
  },
  {
    key: 'feishu_calendar',
    capability: 'channel.feishu.calendar',
    labelKey: 'feishuCalendar',
    descKey: 'feishuCalendarDesc',
    fallbackLabel: 'Feishu Calendar',
    fallbackDesc: 'List, create, update, or delete Feishu calendar events',
  },
  {
    key: 'feishu_approval',
    capability: 'channel.feishu.approval',
    labelKey: 'feishuApproval',
    descKey: 'feishuApprovalDesc',
    fallbackLabel: 'Feishu Approval',
    fallbackDesc: 'Create or inspect Feishu approval instances',
  },
  {
    key: 'feishu_directory',
    capability: 'channel.feishu.directory',
    labelKey: 'feishuDirectory',
    descKey: 'feishuDirectoryDesc',
    fallbackLabel: 'Feishu Directory',
    fallbackDesc: 'Search Feishu users and directory information',
  },
  {
    key: 'web_search',
    capability: 'external.web.search',
    labelKey: 'webSearch',
    descKey: 'webSearchDesc',
    fallbackLabel: 'Search the Web',
    fallbackDesc: 'Look up information on the internet',
  },
  {
    key: 'web_read',
    capability: 'external.web.read',
    labelKey: 'webRead',
    descKey: 'webReadDesc',
    fallbackLabel: 'Read Web Pages',
    fallbackDesc: 'Fetch or scrape web page content',
  },
  {
    key: 'plaza_read',
    capability: 'plaza.post.read',
    labelKey: 'plazaRead',
    descKey: 'plazaReadDesc',
    fallbackLabel: 'Read Plaza Posts',
    fallbackDesc: 'Read posts from the agent plaza',
  },
  {
    key: 'plaza_write',
    capability: 'plaza.post.write',
    labelKey: 'plazaWrite',
    descKey: 'plazaWriteDesc',
    fallbackLabel: 'Write Plaza Posts',
    fallbackDesc: 'Create posts or comments in the agent plaza',
  },
];

interface AgentSettingsSectionProps {
  agentId: string;
  agent: any;
  llmModels: any[];
  permData: any;
  canManage: boolean;
  canManageCapabilityPolicies: boolean;
  capabilityDefinitions?: CapabilityDefinition[];
  capabilityPolicies: CapabilityPolicy[];
  capabilityPolicyLoading: boolean;
  capabilityPolicyError: string;
  settingsForm: AgentSettingsForm;
  onSettingsFormChange: React.Dispatch<React.SetStateAction<AgentSettingsForm>>;
  settingsSaving: boolean;
  settingsSaved: boolean;
  settingsError: string;
  onSetSettingsSaving: (value: boolean) => void;
  onSetSettingsSaved: (value: boolean) => void;
  onSetSettingsError: (value: string) => void;
  onResetSettingsInit: () => void;
  wmDraft: string;
  wmSaved: boolean;
  onSetWmDraft: (value: string) => void;
  onSetWmSaved: (value: boolean) => void;
  showDeleteConfirm: boolean;
  onSetShowDeleteConfirm: (value: boolean) => void;
}

const formatTokens = (n: number) => {
  if (!n) return '0';
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return String(n);
};

export default function AgentSettingsSection({
  agentId,
  agent,
  llmModels,
  permData,
  canManage,
  canManageCapabilityPolicies,
  capabilityDefinitions = [],
  capabilityPolicies,
  capabilityPolicyLoading,
  capabilityPolicyError,
  settingsForm,
  onSettingsFormChange,
  settingsSaving,
  settingsSaved,
  settingsError,
  onSetSettingsSaving,
  onSetSettingsSaved,
  onSetSettingsError,
  onResetSettingsInit,
  wmDraft,
  wmSaved,
  onSetWmDraft,
  onSetWmSaved,
  showDeleteConfirm,
  onSetShowDeleteConfirm,
}: AgentSettingsSectionProps) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const hasChanges =
    settingsForm.primary_model_id !== (agent?.primary_model_id || '') ||
    settingsForm.fallback_model_id !== (agent?.fallback_model_id || '') ||
    settingsForm.max_triggers !== ((agent as any)?.max_triggers ?? 20) ||
    settingsForm.min_poll_interval_min !== ((agent as any)?.min_poll_interval_min ?? 5) ||
    settingsForm.webhook_rate_limit !== ((agent as any)?.webhook_rate_limit ?? 5) ||
    settingsForm.smart_model_routing_enabled !== !!((agent as any)?.smart_model_routing?.enabled) ||
    settingsForm.security_zone !== ((agent as any)?.security_zone || 'standard');

  const capabilityDefinitionSet = React.useMemo(
    () => new Set(capabilityDefinitions.map((item) => item.capability)),
    [capabilityDefinitions],
  );
  const capabilityPolicyByCapability = React.useMemo(
    () => new Map(capabilityPolicies.map((policy) => [policy.capability, policy])),
    [capabilityPolicies],
  );
  const capabilityActions = React.useMemo(() => {
    const knownCapabilities = new Set(KNOWN_CAPABILITY_ACTIONS.map((item) => item.capability));
    const knownActions = KNOWN_CAPABILITY_ACTIONS.map((item) => ({
      key: item.key,
      capability: item.capability,
      label: t(`agent.settings.autonomy.${item.labelKey}`, item.fallbackLabel),
      desc: t(`agent.settings.autonomy.${item.descKey}`, item.fallbackDesc),
    }));
    const dynamicActions = capabilityDefinitions
      .filter((item) => !knownCapabilities.has(item.capability))
      .map((item) => ({
        key: item.capability,
        capability: item.capability,
        label: item.capability,
        desc:
          item.tools.length > 0
            ? t('agent.settings.autonomy.dynamicTools', 'Backend tools: {{tools}}', { tools: item.tools.join(', ') })
            : t('agent.settings.autonomy.dynamicNoTools', 'No mapped tools reported by backend'),
      }));
    return [...knownActions, ...dynamicActions];
  }, [capabilityDefinitions, t]);

  const handleCapabilityPolicyChange = async (capability: string, mode: CapabilityPolicyMode) => {
    if (!canManageCapabilityPolicies) return;
    const nextPolicy = modeToPolicy(mode);
    try {
      await enterpriseApi.upsertCapabilityPolicy({
        capability,
        agent_id: agentId,
        ...nextPolicy,
        conditions: capabilityPolicyByCapability.get(capability)?.conditions || {},
      });
      queryClient.invalidateQueries({ queryKey: ['capability-policies', agentId] });
    } catch (e: any) {
      onSetSettingsError(e?.message || 'Failed to save capability policy');
    }
  };

  const handleSaveSettings = async () => {
    onSetSettingsSaving(true);
    onSetSettingsError('');
    try {
      const result: any = await agentApi.update(agentId, {
        primary_model_id: settingsForm.primary_model_id || null,
        fallback_model_id: settingsForm.fallback_model_id || null,
        max_triggers: settingsForm.max_triggers,
        min_poll_interval_min: settingsForm.min_poll_interval_min,
        webhook_rate_limit: settingsForm.webhook_rate_limit,
        security_zone: settingsForm.security_zone,
        smart_model_routing: settingsForm.smart_model_routing_enabled
          ? { enabled: true, max_simple_chars: 160, max_simple_words: 28 }
          : null,
      } as any);
      queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
      onResetSettingsInit();

      const clamped = result?._clamped_fields;
      if (clamped && clamped.length > 0) {
        const fieldNames: Record<string, string> = {
          min_poll_interval_min: t('agent.settings.clampedField.minPollInterval'),
          webhook_rate_limit: t('agent.settings.clampedField.webhookRateLimit'),
          heartbeat_interval_minutes: t('agent.settings.clampedField.heartbeatInterval'),
        };
        const msgs = clamped.map((c: any) => {
          const name = fieldNames[c.field] || c.field;
          return t('agent.settings.clampedMessage', { name, requested: c.requested, applied: c.applied });
        });
        onSetSettingsError(`Some values were adjusted:\n${msgs.join('\n')}`);
      }

      onSetSettingsSaved(true);
      setTimeout(() => onSetSettingsSaved(false), 2000);
    } catch (e: any) {
      onSetSettingsError(e?.message || 'Failed to save');
    } finally {
      onSetSettingsSaving(false);
    }
  };

  const saveWelcomeMessage = async () => {
    try {
      await agentApi.update(agentId, { welcome_message: wmDraft } as any);
      queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
      onSetWmSaved(true);
      setTimeout(() => onSetWmSaved(false), 2000);
    } catch {}
  };

  const isOwner = permData?.is_owner ?? false;
  const canManageAccessPermissions = isOwner || canManageCapabilityPolicies;

  const handleScopeChange = async (newScope: string) => {
    if (!canManageAccessPermissions) return;
    try {
      await agentApi.updatePermissions(agentId, {
        scope_type: newScope,
        scope_ids: [],
        access_level: permData?.access_level || 'use',
      });
      queryClient.invalidateQueries({ queryKey: ['agent-permissions', agentId] });
      queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
    } catch (e) {
      console.error('Failed to update permissions', e);
    }
  };

  const handleAccessLevelChange = async (newLevel: string) => {
    if (!canManageAccessPermissions) return;
    try {
      await agentApi.updatePermissions(agentId, {
        scope_type: permData?.scope_type || 'company',
        scope_ids: permData?.scope_ids || [],
        access_level: newLevel,
      });
      queryClient.invalidateQueries({ queryKey: ['agent-permissions', agentId] });
      queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
    } catch (e) {
      console.error('Failed to update access level', e);
    }
  };

  const currentScope = permData?.scope_type || 'company';
  const currentAccessLevel = permData?.access_level || 'use';
  const scopeNames = permData?.scope_names || [];
  const scopeLabels: Record<string, string> = {
    company: '🏢 ' + t('agent.settings.perm.companyWide', 'Company-wide'),
    user: '👤 ' + t('agent.settings.perm.onlyMe', 'Only Me'),
  };
  return (
    <div>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: '16px',
          position: 'sticky',
          top: 0,
          zIndex: 10,
          background: 'var(--bg-primary)',
          paddingTop: '4px',
          paddingBottom: '12px',
          borderBottom: '1px solid var(--border-subtle)',
        }}
      >
        <h3 style={{ margin: 0 }}>{t('agent.settings.title')}</h3>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          {settingsSaved && <span style={{ fontSize: '12px', color: 'var(--success)' }}>{t('agent.settings.saved', 'Saved')}</span>}
          {settingsError && (
            <span
              style={{
                fontSize: '12px',
                color: settingsError.includes('adjusted') ? 'var(--warning)' : 'var(--error)',
                whiteSpace: 'pre-line',
              }}
            >
              {settingsError}
            </span>
          )}
          <button
            className="btn btn-primary"
            disabled={!hasChanges || settingsSaving}
            onClick={handleSaveSettings}
            style={{
              opacity: hasChanges ? 1 : 0.5,
              cursor: hasChanges ? 'pointer' : 'default',
              padding: '6px 20px',
              fontSize: '13px',
            }}
          >
            {settingsSaving ? t('agent.settings.saving', 'Saving...') : t('agent.settings.save', 'Save')}
          </button>
        </div>
      </div>

      <div className="card" style={{ marginBottom: '12px' }}>
        <h4 style={{ marginBottom: '12px' }}>{t('agent.settings.modelConfig')}</h4>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          <div>
            <label style={{ display: 'block', fontSize: '13px', fontWeight: 500, marginBottom: '6px' }}>{t('agent.settings.primaryModel')}</label>
            <select
              className="input"
              value={settingsForm.primary_model_id}
              onChange={(e) => onSettingsFormChange((f) => ({ ...f, primary_model_id: e.target.value }))}
            >
              <option value="">--</option>
              {llmModels.filter((m: any) => m.enabled || m.id === settingsForm.primary_model_id).map((m: any) => (
                <option key={m.id} value={m.id}>
                  {m.label} ({m.provider}/{m.model}){!m.enabled ? ` [${t('enterprise.llm.disabled', 'Disabled')}]` : ''}
                </option>
              ))}
            </select>
            {settingsForm.primary_model_id && llmModels.some((m: any) => m.id === settingsForm.primary_model_id && !m.enabled) && (
              <div style={{ fontSize: '11px', color: 'var(--error)', marginTop: '4px' }}>
                {t('agent.settings.modelDisabledWarning', 'This model has been disabled by admin. The agent will automatically use the fallback model.')}
              </div>
            )}
            {!settingsForm.primary_model_id && settingsForm.fallback_model_id && (() => {
              const fb = llmModels.find((m: any) => m.id === settingsForm.fallback_model_id);
              return fb ? (
                <div style={{ fontSize: '11px', color: 'var(--accent)', marginTop: '4px' }}>
                  {t('agent.settings.usingFallback', { model: fb.label })}
                </div>
              ) : null;
            })()}
            {!settingsForm.primary_model_id && !settingsForm.fallback_model_id && llmModels.length > 0 && (
              <div style={{ fontSize: '11px', color: 'var(--warning)', marginTop: '4px' }}>
                {t('agent.settings.noModelWarning')}
              </div>
            )}
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>{t('agent.settings.primaryModel')}</div>
          </div>
          <div>
            <label style={{ display: 'block', fontSize: '13px', fontWeight: 500, marginBottom: '6px' }}>{t('agent.settings.fallbackModel')}</label>
            <select
              className="input"
              value={settingsForm.fallback_model_id}
              onChange={(e) => onSettingsFormChange((f) => ({ ...f, fallback_model_id: e.target.value }))}
            >
              <option value="">--</option>
              {llmModels.filter((m: any) => m.enabled || m.id === settingsForm.fallback_model_id).map((m: any) => (
                <option key={m.id} value={m.id}>
                  {m.label} ({m.provider}/{m.model}){!m.enabled ? ` [${t('enterprise.llm.disabled', 'Disabled')}]` : ''}
                </option>
              ))}
            </select>
            {settingsForm.fallback_model_id && llmModels.some((m: any) => m.id === settingsForm.fallback_model_id && !m.enabled) && (
              <div style={{ fontSize: '11px', color: 'var(--error)', marginTop: '4px' }}>
                {t('agent.settings.modelDisabledWarning', 'This model has been disabled by admin. The agent will automatically use the fallback model.')}
              </div>
            )}
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>{t('agent.settings.fallbackModel')}</div>
          </div>
          {settingsForm.fallback_model_id && (
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                padding: '10px 14px',
                background: 'var(--bg-elevated)',
                borderRadius: '8px',
                border: '1px solid var(--border-subtle)',
              }}
            >
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 500, fontSize: '13px' }}>{t('agent.settings.smartRouting', 'Smart Model Routing')}</div>
                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                  {t('agent.settings.smartRoutingDesc', 'Automatically use the fallback model for simple conversational turns to save costs. Complex tasks always use the primary model.')}
                </div>
              </div>
              <label style={{ position: 'relative', display: 'inline-block', width: '36px', height: '20px', flexShrink: 0, marginLeft: '12px' }}>
                <input
                  type="checkbox"
                  checked={settingsForm.smart_model_routing_enabled}
                  onChange={(e) => onSettingsFormChange((f) => ({ ...f, smart_model_routing_enabled: e.target.checked }))}
                  style={{ opacity: 0, width: 0, height: 0 }}
                />
                <span
                  style={{
                    position: 'absolute',
                    cursor: 'pointer',
                    inset: 0,
                    background: settingsForm.smart_model_routing_enabled ? 'var(--accent-primary)' : 'var(--bg-tertiary)',
                    borderRadius: '10px',
                    transition: 'background 0.2s',
                  }}
                >
                  <span
                    style={{
                      position: 'absolute',
                      height: '14px',
                      width: '14px',
                      left: settingsForm.smart_model_routing_enabled ? '19px' : '3px',
                      bottom: '3px',
                      background: 'white',
                      borderRadius: '50%',
                      transition: 'left 0.2s',
                    }}
                  />
                </span>
              </label>
            </div>
          )}
        </div>
      </div>


      <div className="card" style={{ marginBottom: '12px' }}>
        <h4 style={{ marginBottom: '12px' }}>{t('agent.settings.tokenStats')}</h4>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '12px' }}>
          <div>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '4px' }}>{t('agent.settings.tokenToday')}</div>
            <div style={{ fontSize: '18px', fontWeight: 600 }}>{formatTokens(agent?.tokens_used_today || 0)}</div>
          </div>
          <div>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '4px' }}>{t('agent.settings.tokenMonth')}</div>
            <div style={{ fontSize: '18px', fontWeight: 600 }}>{formatTokens(agent?.tokens_used_month || 0)}</div>
          </div>
          <div>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '4px' }}>{t('agent.settings.tokenTotal')}</div>
            <div style={{ fontSize: '18px', fontWeight: 600 }}>{formatTokens(agent?.tokens_used_total || 0)}</div>
          </div>
        </div>
        <div style={{ fontSize: '11px', color: 'var(--text-quaternary)', marginTop: '8px' }}>
          {t('agent.settings.tokenQuotaHint')}
        </div>
      </div>

      <div className="card" style={{ marginBottom: '12px' }}>
        <h4 style={{ marginBottom: '4px' }}>{t('agent.settings.triggerLimits')}</h4>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
          {t('agent.settings.triggerLimitsDesc')}
        </p>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '12px' }}>
          <div>
            <label style={{ display: 'block', fontSize: '13px', fontWeight: 500, marginBottom: '6px' }}>
              {t('agent.settings.maxTriggers')}
            </label>
            <input
              className="input"
              type="number"
              min={1}
              max={100}
              value={settingsForm.max_triggers}
              onChange={(e) =>
                onSettingsFormChange((f) => ({ ...f, max_triggers: Math.max(1, Math.min(100, parseInt(e.target.value, 10) || 20)) }))
              }
              style={{ width: '100%' }}
            />
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
              {t('agent.settings.maxTriggersDesc')}
            </div>
          </div>
          <div>
            <label style={{ display: 'block', fontSize: '13px', fontWeight: 500, marginBottom: '6px' }}>
              {t('agent.settings.minPollInterval')}
            </label>
            <input
              className="input"
              type="number"
              min={1}
              max={60}
              value={settingsForm.min_poll_interval_min}
              onChange={(e) =>
                onSettingsFormChange((f) => ({ ...f, min_poll_interval_min: Math.max(1, Math.min(60, parseInt(e.target.value, 10) || 5)) }))
              }
              style={{ width: '100%' }}
            />
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
              {t('agent.settings.minPollIntervalDesc')}
            </div>
          </div>
          <div>
            <label style={{ display: 'block', fontSize: '13px', fontWeight: 500, marginBottom: '6px' }}>
              {t('agent.settings.webhookRateLimit')}
            </label>
            <input
              className="input"
              type="number"
              min={1}
              max={60}
              value={settingsForm.webhook_rate_limit}
              onChange={(e) =>
                onSettingsFormChange((f) => ({ ...f, webhook_rate_limit: Math.max(1, Math.min(60, parseInt(e.target.value, 10) || 5)) }))
              }
              style={{ width: '100%' }}
            />
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
              {t('agent.settings.webhookRateLimitDesc')}
            </div>
          </div>
        </div>
      </div>

      <div className="card" style={{ marginBottom: '12px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '4px' }}>
          <h4 style={{ margin: 0 }}>{t('agent.settings.welcomeMessage')}</h4>
          {wmSaved && <span style={{ fontSize: '12px', color: 'var(--success)' }}>✓ {t('agent.settings.saved')}</span>}
        </div>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
          {t('agent.settings.welcomeMessageDesc')}
        </p>
        <textarea
          className="input"
          rows={4}
          value={wmDraft}
          onChange={(e) => onSetWmDraft(e.target.value)}
          onBlur={saveWelcomeMessage}
          placeholder={t('agent.settings.welcomeMessagePlaceholder')}
          style={{
            width: '100%',
            minHeight: '80px',
            resize: 'vertical',
            fontFamily: 'inherit',
            fontSize: '13px',
          }}
        />
      </div>

      <div className="card" style={{ marginBottom: '12px' }}>
        <h4 style={{ marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '8px' }}>
          {t('agent.settings.securityZone.title', 'Runtime Safety Boundary')}
        </h4>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '16px' }}>
          {t('agent.settings.securityZone.description', 'Choose how strict the coarse runtime guard should be. This is evaluated before capability policies.')}
        </p>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '10px 14px',
            background: 'var(--bg-elevated)',
            borderRadius: '8px',
            border: '1px solid var(--border-subtle)',
          }}
        >
          <div>
            <div style={{ fontWeight: 500, fontSize: '13px' }}>{t('agent.settings.securityZone.current', 'Selected Boundary')}</div>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
              {settingsForm.security_zone === 'standard' && t('agent.settings.securityZone.standardDesc', 'No extra zone approval; capability policies below decide whether actions run automatically, need approval, or are denied.')}
              {settingsForm.security_zone === 'restricted' && t('agent.settings.securityZone.restrictedDesc', 'Sensitive actions require approval even when capability policies allow auto execution.')}
              {settingsForm.security_zone === 'public' && t('agent.settings.securityZone.publicDesc', 'Only safe read-only tools can run. Write, send, delete, and execute actions are blocked.')}
            </div>
          </div>
          <select
            className="input"
            value={settingsForm.security_zone}
            onChange={(e) => onSettingsFormChange((prev) => ({ ...prev, security_zone: e.target.value }))}
            disabled={!canManage}
            style={{ width: '220px', fontSize: '12px', opacity: canManage ? 1 : 0.6 }}
          >
            <option value="standard">{t('agent.settings.securityZone.standard', 'Loose (Default)')}</option>
            <option value="restricted">{t('agent.settings.securityZone.restricted', 'Approval Guard')}</option>
            <option value="public">{t('agent.settings.securityZone.public', 'Read-only Lockdown')}</option>
          </select>
        </div>
        <div
          style={{
            marginTop: '10px',
            padding: '9px 12px',
            borderRadius: '8px',
            border: '1px solid var(--border-subtle)',
            background: 'var(--bg-elevated)',
            color: 'var(--text-secondary)',
            fontSize: '12px',
            lineHeight: 1.5,
          }}
        >
          {t(
            'agent.settings.securityZone.precedenceHint',
            'Ordered from loose to strict. The boundary is checked before capability policies, so stricter modes can override Auto below.',
          )}
        </div>
      </div>

      <div className="card" style={{ marginBottom: '12px' }}>
        <h4 style={{ marginBottom: '4px' }}>{t('agent.settings.autonomy.title')}</h4>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '16px' }}>{t('agent.settings.autonomy.description')}</p>
        {capabilityPolicyLoading && (
          <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
            {t('agent.settings.autonomy.policyLoading')}
          </p>
        )}
        {capabilityPolicyError && (
          <p style={{ fontSize: '12px', color: 'var(--error)', marginBottom: '12px' }}>
            {t('agent.settings.autonomy.policyError', { message: capabilityPolicyError })}
          </p>
        )}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
          {capabilityActions.map((action) => {
            const currentMode = policyToMode(capabilityPolicyByCapability.get(action.capability));
            const unsupported = capabilityDefinitionSet.size > 0 && !capabilityDefinitionSet.has(action.capability);
            const disabled = !canManageCapabilityPolicies || capabilityPolicyLoading || !!capabilityPolicyError || unsupported;
            return (
              <div
                key={action.key}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  padding: '10px 14px',
                  background: 'var(--bg-elevated)',
                  borderRadius: '8px',
                  border: '1px solid var(--border-subtle)',
                }}
              >
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 500, fontSize: '13px' }}>{action.label}</div>
                  <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{action.desc}</div>
                </div>
                <select
                  className="input"
                  value={currentMode}
                  disabled={disabled}
                  onChange={async (e) => {
                    await handleCapabilityPolicyChange(action.capability, e.target.value as CapabilityPolicyMode);
                  }}
                  style={{
                    width: '140px',
                    fontSize: '12px',
                    color: currentMode === 'auto' ? 'var(--success)' : currentMode === 'approval' ? 'var(--warning)' : 'var(--error)',
                    fontWeight: 600,
                    opacity: disabled ? 0.6 : 1,
                  }}
                >
                  <option value="auto">{t('agent.settings.autonomy.l1Auto')}</option>
                  <option value="approval">{t('agent.settings.autonomy.l3Approve')}</option>
                  <option value="deny">{t('agent.settings.autonomy.deny')}</option>
                </select>
              </div>
            );
          })}
        </div>
      </div>

      <div className="card" style={{ marginBottom: '12px' }}>
        <h4 style={{ marginBottom: '12px' }}>🔒 {t('agent.settings.perm.title', 'Access Permissions')}</h4>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '16px' }}>
          {t('agent.settings.perm.description', 'Control who can see and interact with this agent. Only the creator or admin can change this.')}
        </p>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginBottom: '16px' }}>
          {(['company', 'user'] as const).map((scope) => (
            <label
              key={scope}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '10px',
                padding: '12px 14px',
                borderRadius: '8px',
                cursor: canManageAccessPermissions ? 'pointer' : 'default',
                border: currentScope === scope ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
                background: currentScope === scope ? 'rgba(99,102,241,0.06)' : 'transparent',
                opacity: canManageAccessPermissions ? 1 : 0.7,
                transition: 'all 0.15s',
              }}
            >
              <input
                type="radio"
                name="perm_scope"
                checked={currentScope === scope}
                disabled={!canManageAccessPermissions}
                onChange={() => handleScopeChange(scope)}
                style={{ accentColor: 'var(--accent-primary)' }}
              />
              <div>
                <div style={{ fontWeight: 500, fontSize: '13px' }}>{scopeLabels[scope]}</div>
                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                  {scope === 'company' && t('agent.settings.perm.companyWideDesc', 'All users in the organization can use this agent')}
                  {scope === 'user' && t('agent.settings.perm.onlyMeDesc', 'Only the creator can use this agent')}
                </div>
              </div>
            </label>
          ))}
        </div>

        {currentScope === 'company' && canManageAccessPermissions && (
          <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: '12px' }}>
            <label style={{ display: 'block', fontSize: '13px', fontWeight: 500, marginBottom: '8px' }}>
              {t('agent.settings.perm.defaultAccess', 'Default Access Level')}
            </label>
            <div style={{ display: 'flex', gap: '8px' }}>
              {[
                { val: 'use', label: '👁️ ' + t('agent.settings.perm.useAccess', 'Use'), desc: t('agent.settings.perm.useAccessDesc', 'Task, Chat, Tools, Skills, Workspace') },
                { val: 'manage', label: '⚙️ ' + t('agent.settings.perm.manageAccess', 'Manage'), desc: t('agent.settings.perm.manageAccessDesc', 'Full access including Settings, Mind, Relationships') },
              ].map((opt) => (
                <label
                  key={opt.val}
                  style={{
                    flex: 1,
                    padding: '10px 12px',
                    borderRadius: '8px',
                    cursor: 'pointer',
                    border: currentAccessLevel === opt.val ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
                    background: currentAccessLevel === opt.val ? 'rgba(99,102,241,0.06)' : 'transparent',
                    transition: 'all 0.15s',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <input
                      type="radio"
                      name="access_level"
                      checked={currentAccessLevel === opt.val}
                      onChange={() => handleAccessLevelChange(opt.val)}
                      style={{ accentColor: 'var(--accent-primary)' }}
                    />
                    <span style={{ fontWeight: 500, fontSize: '13px' }}>{opt.label}</span>
                  </div>
                  <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px', marginLeft: '20px' }}>{opt.desc}</div>
                </label>
              ))}
            </div>
          </div>
        )}

        {currentScope !== 'company' && scopeNames.length > 0 && (
          <div style={{ marginTop: '12px', fontSize: '12px', color: 'var(--text-secondary)' }}>
            <span style={{ fontWeight: 500 }}>{t('agent.settings.perm.currentAccess', 'Current access')}:</span> {scopeNames.map((s: any) => s.name).join(', ')}
          </div>
        )}

        {!canManageAccessPermissions && (
          <div style={{ marginTop: '12px', fontSize: '11px', color: 'var(--text-tertiary)', fontStyle: 'italic' }}>
            {t('agent.settings.perm.readOnly', 'Only the creator or admin can change permissions')}
          </div>
        )}
      </div>

      <div className="card" style={{ marginBottom: '12px' }}>
        <h4 style={{ marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '8px' }}>{t('agent.settings.timezone.title', '🌐 Timezone')}</h4>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '16px' }}>
          {t('agent.settings.timezone.description', "The timezone used for this agent's scheduling, active hours, and time awareness. Defaults to the company timezone if not set.")}
        </p>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '10px 14px',
            background: 'var(--bg-elevated)',
            borderRadius: '8px',
            border: '1px solid var(--border-subtle)',
          }}
        >
          <div>
            <div style={{ fontWeight: 500, fontSize: '13px' }}>{t('agent.settings.timezone.current', 'Agent Timezone')}</div>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
              {agent?.timezone
                ? t('agent.settings.timezone.override', 'Custom timezone for this agent')
                : t('agent.settings.timezone.inherited', 'Using company default timezone')}
            </div>
          </div>
          <select
            className="input"
            disabled={!canManage}
            value={agent?.timezone || ''}
            onChange={async (e) => {
              if (!canManage) return;
              const val = e.target.value || null;
              await agentApi.update(agentId, { timezone: val } as any);
              queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
            }}
            style={{ width: '200px', fontSize: '12px', opacity: canManage ? 1 : 0.6 }}
          >
            <option value="">{t('agent.settings.timezone.default', '↩ Company default')}</option>
            {[
              'UTC',
              'Asia/Shanghai',
              'Asia/Tokyo',
              'Asia/Seoul',
              'Asia/Singapore',
              'Asia/Kolkata',
              'Asia/Dubai',
              'Europe/London',
              'Europe/Paris',
              'Europe/Berlin',
              'Europe/Moscow',
              'America/New_York',
              'America/Chicago',
              'America/Denver',
              'America/Los_Angeles',
              'America/Sao_Paulo',
              'Australia/Sydney',
              'Pacific/Auckland',
            ].map((tz) => (
              <option key={tz} value={tz}>
                {tz}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="card" style={{ marginBottom: '12px' }}>
        <h4 style={{ marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '8px' }}>
          {t('agent.settings.executionMode.title', 'Execution Mode')}
        </h4>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '16px' }}>
          {t('agent.settings.executionMode.description', 'Choose whether this agent runs as a normal worker or as a coordinator that primarily delegates to other agents.')}
        </p>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '10px 14px',
            background: 'var(--bg-elevated)',
            borderRadius: '8px',
            border: '1px solid var(--border-subtle)',
          }}
        >
          <div>
            <div style={{ fontWeight: 500, fontSize: '13px' }}>
              {t('agent.settings.executionMode.current', 'Current Mode')}
            </div>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
              {(agent?.execution_mode || 'standard') === 'coordinator'
                ? t('agent.settings.executionMode.coordinatorDesc', 'Delegates and synthesizes work across worker agents')
                : t('agent.settings.executionMode.standardDesc', 'Uses the normal single-agent runtime')}
            </div>
          </div>
          <select
            className="input"
            disabled={!canManage}
            value={agent?.execution_mode || 'standard'}
            onChange={async (e) => {
              if (!canManage) return;
              await agentApi.update(agentId, { execution_mode: e.target.value as 'standard' | 'coordinator' });
              queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
            }}
            style={{ width: '220px', fontSize: '12px', opacity: canManage ? 1 : 0.6 }}
          >
            <option value="standard">{t('agent.settings.executionMode.standard', 'Standard')}</option>
            <option value="coordinator">{t('agent.settings.executionMode.coordinator', 'Coordinator')}</option>
          </select>
        </div>
      </div>

      <div className="card" style={{ marginBottom: '12px' }}>
        <h4 style={{ marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '8px' }}>{t('agent.settings.heartbeat.title', 'Heartbeat')}</h4>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '16px' }}>
          {t('agent.settings.heartbeat.description', 'Periodic awareness check — agent proactively monitors the plaza and work environment.')}
        </p>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: '10px 14px',
              background: 'var(--bg-elevated)',
              borderRadius: '8px',
              border: '1px solid var(--border-subtle)',
            }}
          >
            <div>
              <div style={{ fontWeight: 500, fontSize: '13px' }}>{t('agent.settings.heartbeat.enabled', 'Enable Heartbeat')}</div>
              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{t('agent.settings.heartbeat.enabledDesc', 'Agent will periodically check plaza and work status')}</div>
            </div>
            <label style={{ position: 'relative', display: 'inline-block', width: '44px', height: '24px', cursor: canManage ? 'pointer' : 'default' }}>
              <input
                type="checkbox"
                checked={agent?.heartbeat_enabled ?? true}
                disabled={!canManage}
                onChange={async (e) => {
                  if (!canManage) return;
                  await agentApi.update(agentId, { heartbeat_enabled: e.target.checked } as any);
                  queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
                }}
                style={{ opacity: 0, width: 0, height: 0 }}
              />
              <span
                style={{
                  position: 'absolute',
                  top: 0,
                  left: 0,
                  right: 0,
                  bottom: 0,
                  background: (agent?.heartbeat_enabled ?? true) ? 'var(--accent-primary)' : 'var(--bg-tertiary)',
                  borderRadius: '12px',
                  transition: 'background 0.2s',
                  opacity: canManage ? 1 : 0.6,
                }}
              >
                <span
                  style={{
                    position: 'absolute',
                    top: '3px',
                    left: (agent?.heartbeat_enabled ?? true) ? '23px' : '3px',
                    width: '18px',
                    height: '18px',
                    background: 'white',
                    borderRadius: '50%',
                    transition: 'left 0.2s',
                  }}
                />
              </span>
            </label>
          </div>

          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: '10px 14px',
              background: 'var(--bg-elevated)',
              borderRadius: '8px',
              border: '1px solid var(--border-subtle)',
            }}
          >
            <div>
              <div style={{ fontWeight: 500, fontSize: '13px' }}>{t('agent.settings.heartbeat.interval', 'Check Interval')}</div>
              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{t('agent.settings.heartbeat.intervalDesc', 'How often the agent checks for updates')}</div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <input
                type="number"
                className="input"
                disabled={!canManage}
                min={1}
                defaultValue={agent?.heartbeat_interval_minutes ?? 120}
                key={agent?.heartbeat_interval_minutes}
                onBlur={async (e) => {
                  if (!canManage) return;
                  const val = Math.max(1, Number(e.target.value) || 120);
                  e.target.value = String(val);
                  await agentApi.update(agentId, { heartbeat_interval_minutes: val } as any);
                  queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
                }}
                style={{ width: '80px', fontSize: '12px', opacity: canManage ? 1 : 0.6 }}
              />
              <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{t('common.minutes', 'min')}</span>
            </div>
          </div>

          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: '10px 14px',
              background: 'var(--bg-elevated)',
              borderRadius: '8px',
              border: '1px solid var(--border-subtle)',
            }}
          >
            <div>
              <div style={{ fontWeight: 500, fontSize: '13px' }}>{t('agent.settings.heartbeat.activeHours', 'Active Hours')}</div>
              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{t('agent.settings.heartbeat.activeHoursDesc', 'Only trigger heartbeat during these hours (HH:MM-HH:MM)')}</div>
            </div>
            <input
              className="input"
              disabled={!canManage}
              value={agent?.heartbeat_active_hours ?? '09:00-18:00'}
              onChange={async (e) => {
                if (!canManage) return;
                await agentApi.update(agentId, { heartbeat_active_hours: e.target.value } as any);
                queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
              }}
              style={{ width: '140px', fontSize: '12px', textAlign: 'center', opacity: canManage ? 1 : 0.6 }}
              placeholder="09:00-18:00"
            />
          </div>

          {agent?.last_heartbeat_at && (
            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', paddingLeft: '4px' }}>
              {t('agent.settings.heartbeat.lastRun', 'Last heartbeat')}: {new Date(agent.last_heartbeat_at).toLocaleString()}
            </div>
          )}
        </div>
      </div>

      <div style={{ marginBottom: '12px' }}>
        <ChannelConfig mode="edit" agentId={agentId} />
      </div>

      <div className="card" style={{ borderColor: 'var(--error)' }}>
        <h4 style={{ color: 'var(--error)', marginBottom: '12px' }}>{t('agent.settings.danger.title')}</h4>
        <p style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '12px' }}>{t('agent.settings.danger.deleteWarning')}</p>
        {!showDeleteConfirm ? (
          <button className="btn btn-danger" onClick={() => onSetShowDeleteConfirm(true)}>
            × {t('agent.settings.danger.deleteAgent')}
          </button>
        ) : (
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
            <span style={{ fontSize: '13px', color: 'var(--error)', fontWeight: 600 }}>{t('agent.settings.danger.deleteWarning')}</span>
            <button
              className="btn btn-danger"
              onClick={async () => {
                try {
                  await agentApi.remove(agentId);
                  queryClient.invalidateQueries({ queryKey: ['agents'] });
                  navigate('/');
                } catch (err: any) {
                  alert(err?.message || 'Failed to delete agent');
                }
              }}
            >
              {t('agent.settings.danger.confirmDelete')}
            </button>
            <button className="btn btn-secondary" onClick={() => onSetShowDeleteConfirm(false)}>
              {t('common.cancel')}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
