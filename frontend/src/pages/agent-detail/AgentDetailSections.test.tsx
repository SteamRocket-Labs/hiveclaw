import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import AgentApprovalsSection from './AgentApprovalsSection';
import AgentActivityLogSection from './AgentActivityLogSection';
import AgentAwareSection from './AgentAwareSection';
import AgentChatSection, { StructuredToolResultBody } from './AgentChatSection';
import AgentMindSection from './AgentMindSection';
import AgentSettingsSection from './AgentSettingsSection';
import AgentSkillsSection from './AgentSkillsSection';
import AgentStatusSection from './AgentStatusSection';
import AgentWorkspaceSection from './AgentWorkspaceSection';
import CopyMessageButton from './CopyMessageButton';
import RelationshipEditor from './RelationshipEditor';
import ToolsManager from './ToolsManager';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallbackOrOptions?: string | Record<string, unknown>, options?: Record<string, unknown>) => {
      if (typeof fallbackOrOptions === 'string') {
        return fallbackOrOptions.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, name) => String(options?.[name] ?? ''));
      }
      const values = (fallbackOrOptions as Record<string, unknown> | undefined) ?? options ?? {};
      if ('count' in values) {
        return `${key.split('.').pop() ?? key}:${String(values.count)}`;
      }
      if ('name' in values) {
        return `${key.split('.').pop() ?? key}:${String(values.name)}`;
      }
      return key.split('.').pop() ?? key;
    },
    i18n: {
      language: 'en',
    },
  }),
}));

vi.stubGlobal('localStorage', {
  getItem: vi.fn(() => ''),
  setItem: vi.fn(),
  removeItem: vi.fn(),
  clear: vi.fn(),
  key: vi.fn(),
  length: 0,
} as unknown as Storage);

vi.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey }: { queryKey: unknown[] }) => {
    const key = String(queryKey[0]);
    if (key === 'relationships') {
      return {
        data: [
          {
            id: 'rel-1',
            member_id: 'member-1',
            relation: 'collaborator',
            relation_label: 'Collaborator',
            description: 'Works with the agent daily.',
            member: {
              name: 'Alice',
              title: 'Engineer',
              department_path: 'Engineering',
            },
          },
        ],
      };
    }
    if (key === 'agent-relationships') {
      return {
        data: [
          {
            id: 'arel-1',
            target_agent_id: 'agent-2',
            relation: 'peer',
            relation_label: 'Peer',
            description: 'Peer reviewer.',
            target_agent: {
              name: 'Reviewer Bot',
              role_description: 'Quality reviewer',
            },
          },
        ],
      };
    }
    if (key === 'agents') {
      return {
        data: [
          { id: 'agent-1', name: 'Primary Bot', role_description: 'Main agent' },
          { id: 'agent-2', name: 'Reviewer Bot', role_description: 'Quality reviewer' },
        ],
      };
    }
    if (key === 'agents-for-rel') {
      return {
        data: [
          { id: 'agent-1', name: 'Primary Bot', role_description: 'Main agent' },
          { id: 'agent-2', name: 'Reviewer Bot', role_description: 'Quality reviewer' },
        ],
      };
    }
    if (key === 'agent-approvals') {
      return {
        data: [
          {
            id: 'approval-1',
            action_type: 'deploy_run',
            status: 'pending',
            created_at: '2026-03-27T09:00:00Z',
            details: { environment: 'prod' },
          },
          {
            id: 'approval-2',
            action_type: 'publish_post',
            status: 'approved',
            resolved_at: '2026-03-27T09:30:00Z',
          },
        ],
        refetch: vi.fn(),
      };
    }
    if (key === 'team-memory') {
      return {
        data: [
          {
            key: 'deploy-playbook',
            title: 'Deploy Playbook',
            workspace_key: 'workspace',
            updated_at: '2026-03-27T09:00:00Z',
            snippet: 'Use a canary rollout and verify logs before promoting globally.',
            path: 'shared_memory/tenant/workspace/deploy-playbook.md',
            absolute_path: '/tmp/shared_memory/tenant/workspace/deploy-playbook.md',
          },
        ],
      };
    }
    if (key === 'team-memory-entry') {
      return {
        data: {
          key: 'deploy-playbook',
          title: 'Deploy Playbook',
          workspace_key: 'workspace',
          updated_at: '2026-03-27T09:00:00Z',
          snippet: 'Use a canary rollout and verify logs before promoting globally.',
          content: 'Use a canary rollout and verify logs before promoting globally.\nDocument rollback before the final promotion.',
          path: 'shared_memory/tenant/workspace/deploy-playbook.md',
          absolute_path: '/tmp/shared_memory/tenant/workspace/deploy-playbook.md',
        },
      };
    }
    return { data: [] };
  },
  useMutation: () => ({
    mutate: vi.fn(),
    isPending: false,
  }),
  useQueryClient: () => ({
    invalidateQueries: vi.fn(),
  }),
}));

vi.mock('../../stores', () => {
  const useAuthStore = Object.assign(vi.fn(), {
    getState: () => ({
      user: null,
      token: null,
    }),
  });
  return { useAuthStore };
});

vi.mock('../../components/FileBrowser', () => ({
  default: ({ title }: { title?: string }) => <div>{title || 'File Browser Mock'}</div>,
}));

vi.mock('../../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}));

vi.mock('../../components/ChannelConfig', () => ({
  default: () => <div>Channel Config Mock</div>,
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
}));

describe('AgentDetail extracted sections', () => {
  it('renders ToolsManager as a standalone module with loading placeholder', () => {
    const markup = renderToStaticMarkup(<ToolsManager agentId="agent-1" canManage />);

    expect(markup).toContain('loading');
  });

  it('renders RelationshipEditor as a standalone module with human and agent sections', () => {
    const markup = renderToStaticMarkup(<RelationshipEditor agentId="agent-1" />);

    expect(markup).toContain('owner');
    expect(markup).toContain('bindEmployee');
    expect(markup).toContain('peers');
    expect(markup).toContain('Reviewer Bot');
  });

  it('renders CopyMessageButton as a standalone message action', () => {
    const markup = renderToStaticMarkup(<CopyMessageButton text="Hello world" />);

    expect(markup).toContain('title="Copy"');
    expect(markup).toContain('<button');
  });

  it('renders AgentStatusSection as a standalone overview module', () => {
    const markup = renderToStaticMarkup(
      <AgentStatusSection
        agent={{
          id: 'agent-1',
          agent_type: 'native',
          tokens_used_today: 1234,
          // max_tokens_per_day: 5000,
          tokens_used_month: 6789,
          // max_tokens_per_month: 20000,
          tokens_used_total: 98765,
          role_description: 'Handles release coordination.',
          created_at: '2026-03-20T10:00:00Z',
          creator_username: 'rocky',
          last_active_at: '2026-03-27T09:00:00Z',
          effective_timezone: 'Asia/Shanghai',
          primary_model_id: 'model-1',
        }}
        llmModels={[{ id: 'model-1', label: 'GPT-5.4', model: 'gpt-5.4', provider: 'openai' }]}
        metrics={{
          tasks: { done: 3, total: 5, completion_rate: 60 },
          approvals: { pending: 2 },
          activity: { actions_last_24h: 14 },
        }}
        activityLogs={[
          { id: 'log-1', created_at: '2026-03-27T09:15:00Z', summary: 'Sent release reminder', action_type: 'chat_reply' },
        ]}
        capabilityInstalls={[
          {
            id: 'install-1',
            kind: 'mcp_server',
            display_name: 'smithery/github',
            status: 'failed',
            error_message: 'OAuth required',
          },
          {
            id: 'install-2',
            kind: 'platform_skill',
            display_name: 'feishu-integration',
            status: 'installed',
          },
        ]}
        channelCapabilities={[
          {
            channel: 'telegram',
            connected: true,
            official_api: true,
            capabilities: {
              live_text: true,
              inbound_file: true,
              outbound_file: true,
              deferred_text: true,
              deferred_file: true,
              on_message_current_sender: true,
              on_message_by_name: false,
            },
            limitations: [],
          },
        ]}
        statusKey="active"
        onSelectTab={() => {}}
      />,
    );

    expect(markup).toContain('Recent Activity');
    expect(markup).toContain('GPT-5.4');
    expect(markup).toContain('Handles release coordination.');
    expect(markup).toContain('Sent release reminder');
    expect(markup).toContain('Capability Install Status');
    expect(markup).toContain('smithery/github');
    expect(markup).toContain('OAuth required');
    expect(markup).toContain('telegram');
    expect(markup).toContain('Channel');
  });

  it('renders AgentActivityLogSection as a standalone activity module', () => {
    const markup = renderToStaticMarkup(
      <AgentActivityLogSection
        agentType="native"
        activityLogs={[
          {
            id: 'log-1',
            created_at: '2026-03-27T09:15:00Z',
            summary: 'Heartbeat completed',
            action_type: 'heartbeat',
            detail: { cycle: 'morning' },
          },
        ]}
        toolFailureSummary={{
          total_errors: 3,
          by_tool: [{ tool_name: 'firecrawl_fetch', count: 2 }],
          by_provider: [{ provider: 'firecrawl', count: 2 }],
          by_error_class: [{ error_class: 'quota_or_billing', count: 2 }],
          by_http_status: [{ http_status: 402, count: 2 }],
          recent_errors: [
            {
              summary: 'Firecrawl billing issue',
              tool_name: 'firecrawl_fetch',
              provider: 'firecrawl',
              error_class: 'quota_or_billing',
              http_status: 402,
              created_at: '2026-03-27T09:16:00Z',
            },
          ],
        }}
        logFilter="heartbeat"
        expandedLogId="log-1"
        onFilterChange={() => {}}
        onToggleExpandedLog={() => {}}
      />,
    );

    expect(markup).toContain('User Actions');
    expect(markup).toContain('Heartbeat completed');
    expect(markup).toContain('cycle');
    expect(markup).toContain('Tool Failure Summary');
    expect(markup).toContain('firecrawl_fetch');
    expect(markup).toContain('quota_or_billing');
  });

  it('renders AgentApprovalsSection as a standalone approvals module', () => {
    const markup = renderToStaticMarkup(<AgentApprovalsSection agentId="agent-1" />);

    expect(markup).toContain('deploy_run');
    expect(markup).toContain('publish_post');
    expect(markup).toContain('prod');
  });

  it('renders AgentSkillsSection as a standalone skills module', () => {
    const markup = renderToStaticMarkup(<AgentSkillsSection agentId="agent-1" />);

    expect(markup).toContain('Import from URL');
    expect(markup).toContain('Browse ClawHub');
    expect(markup).toContain('skillFiles');
  });

  it('renders AgentWorkspaceSection as a standalone workspace module', () => {
    const markup = renderToStaticMarkup(<AgentWorkspaceSection agentId="agent-1" />);

    expect(markup).toContain('File Browser Mock');
    expect(markup).toContain('Deploy Playbook');
    expect(markup).toContain('canary rollout');
    expect(markup).toContain('Search shared memory');
    expect(markup).toContain('Save to shared memory');
    expect(markup).toContain('Delete entry');
    expect(markup).toContain('Document rollback before the final promotion.');
  });

  it('renders AgentAwareSection as a standalone aware module', () => {
    const markup = renderToStaticMarkup(
      <AgentAwareSection
        agentId="agent-1"
        focusContent={'- [ ] release: monitor deploy health\n- [x] archive: wrap up old incidents'}
        awareTriggers={[
          {
            id: 'trigger-1',
            name: 'release-check',
            type: 'cron',
            config: { expr: '0 9 * * *' },
            focus_ref: 'release',
            fire_count: 3,
            is_enabled: true,
            reason: 'Daily release check',
          },
        ]}
        activityLogs={[
          {
            id: 'log-1',
            action_type: 'trigger_fired',
            created_at: '2026-03-27T09:00:00Z',
            summary: 'release-check trigger fired successfully',
          },
        ]}
        reflectionSessions={[
          {
            id: 'session-1',
            title: 'Morning release reflection',
            created_at: '2026-03-27T09:00:00Z',
            message_count: 1,
          },
        ]}
        reflectionMessages={{
          'session-1': [
            {
              role: 'tool_result',
              toolName: 'preview_agent_blueprint',
              toolResult: JSON.stringify({
                status: 'preview',
                blueprint: {
                  name: 'Release Planner',
                  deferred_capabilities: ['github-research'],
                },
                summary: {
                  mission: 'Coordinate launch readiness checks.',
                  first_mission: 'Draft the first launch checklist.',
                },
                ready_now: ['Builtin tools + default skills + memory loop'],
                will_install: ['mcp: github'],
                warnings: ['primary_users is empty — the agent may be less clear about who it serves.'],
                manual_steps: ['Validate the first deliverable before expanding capabilities.'],
              }),
            },
            { role: 'assistant', content: 'All systems green.' },
          ],
        }}
        expandedFocus="release"
        expandedReflection="session-1"
        showAllFocus={false}
        showCompletedFocus={true}
        showAllTriggers={false}
        reflectionPage={0}
        onSetExpandedFocus={() => {}}
        onSetExpandedReflection={() => {}}
        onSetReflectionMessages={() => {}}
        onSetShowAllFocus={() => {}}
        onSetShowCompletedFocus={() => {}}
        onSetShowAllTriggers={() => {}}
        onSetReflectionPage={() => {}}
        onRefetchTriggers={async () => {}}
        onLoadReflectionMessages={async () => {}}
      />,
    );

    expect(markup).toContain('monitor deploy health');
    expect(markup).toContain('Every day at 09:00');
    expect(markup).toContain('All systems green.');
    expect(markup).toContain('archive');
    expect(markup).toContain('Deferred Capabilities');
    expect(markup).toContain('github-research');
    expect(markup).toContain('Deploy Playbook');
    expect(markup).toContain('canary rollout');
    expect(markup).toContain('Search shared memory');
    expect(markup).toContain('Document rollback before the final promotion.');
  });

  it('renders autonomy overview as the primary aware surface without raw internals', () => {
    const markup = renderToStaticMarkup(
      <AgentAwareSection
        agentId="agent-1"
        focusContent={'- [ ] legacy: this raw projection should be secondary'}
        awareTriggers={[]}
        activityLogs={[]}
        reflectionSessions={[]}
        reflectionMessages={{}}
        autonomyOverview={{
          agent_id: 'agent-1',
          lookback_hours: 24,
          totals: { objectives: 2, triggers: 2, recent_attempts: 1, findings: 1 },
          objectives: [
            {
              id: 'objective-internal-id',
              description: 'Send investor update',
              status: 'proposed',
              wake_state: 'no_wake_policy',
              requires_approval: true,
              success_criteria: 'Sent with confirmation',
            },
            {
              id: 'objective-active-id',
              description: 'Monitor launch health',
              status: 'active',
              wake_state: 'has_wake_policy',
              requires_approval: false,
              completion_evidence: 'workspace/report.md',
            },
          ],
          triggers: [
            {
              id: 'trigger-internal-id',
              display_kind: 'scheduled_job',
              display_title: 'Daily launch report',
              display_schedule: '0 9 * * *',
              attention_state: 'backoff_active',
              attention_reason: 'Waiting to retry after a recent failure.',
              next_action: 'request_retry',
              linked_objective: null,
              last_attempt: {
                task_id: 'runtime-internal-id',
                status: 'failed',
                display_summary: 'Provider quota exceeded',
                attention_reason: 'Provider quota exceeded',
              },
              last_artifact: { path: 'runtime_artifacts/triggers/runtime-internal-id.json' },
            },
          ],
          recent_attempts: [
            {
              task_id: 'runtime-internal-id',
              task_type: 'trigger',
              status: 'failed',
              display_summary: 'Provider quota exceeded',
              attention_reason: 'Provider quota exceeded',
            },
          ],
          findings: [
            {
              severity: 'warning',
              category: 'objective_waiting_approval',
              message: 'Objective is waiting for approval.',
              recommendation: 'Approve or reject it.',
            },
          ],
        }}
        expandedFocus={null}
        expandedReflection={null}
        showAllFocus={false}
        showCompletedFocus={false}
        showAllTriggers={false}
        reflectionPage={0}
        onSetExpandedFocus={() => {}}
        onSetExpandedReflection={() => {}}
        onSetReflectionMessages={() => {}}
        onSetShowAllFocus={() => {}}
        onSetShowCompletedFocus={() => {}}
        onSetShowAllTriggers={() => {}}
        onSetReflectionPage={() => {}}
        onRefetchTriggers={async () => {}}
        onRefetchAutonomy={async () => {}}
      />,
    );

    expect(markup).toContain('Send investor update');
    expect(markup).toContain('proposed');
    expect(markup).toContain('Approve');
    expect(markup).toContain('Daily launch report');
    expect(markup).toContain('Waiting to retry after a recent failure.');
    expect(markup).toContain('Provider quota exceeded');
    expect(markup).not.toContain('trigger-internal-id');
    expect(markup).not.toContain('runtime-internal-id');
    expect(markup).not.toContain('objective-internal-id');
    expect(markup).not.toContain('runtime_artifacts/triggers');
    expect(markup).not.toContain('legacy: this raw projection should be secondary');
  });

  it('renders AgentMindSection as a standalone mind module', () => {
    const markup = renderToStaticMarkup(<AgentMindSection agentId="agent-1" canEdit />);

    expect(markup).toContain('Core identity, personality, and behavior boundaries.');
    expect(markup).toContain('Long-term knowledge curated from conversations. Feedback, strategies, blocked patterns, and project knowledge.');
    expect(markup).toContain('Curation history, performance scorecard, and blocked approaches.');
    expect(markup).toContain('File Browser Mock');
  });

  it('renders AgentSettingsSection as a standalone settings module', () => {
    const markup = renderToStaticMarkup(
      <AgentSettingsSection
        agentId="agent-1"
        agent={{
          id: 'agent-1',
          agent_type: 'native',
          execution_mode: 'coordinator',
          primary_model_id: 'model-1',
          fallback_model_id: '',
          // max_tokens_per_day: 10000,
          // max_tokens_per_month: 200000,
          max_triggers: 10,
          min_poll_interval_min: 5,
          webhook_rate_limit: 5,
          tokens_used_today: 1234,
          tokens_used_month: 5678,
          welcome_message: 'Hello there',
          security_zone: 'restricted',
          timezone: 'Asia/Shanghai',
          heartbeat_enabled: true,
          heartbeat_interval_minutes: 120,
          heartbeat_active_hours: '09:00-18:00',
          last_heartbeat_at: '2026-03-27T09:00:00Z',
        }}
        llmModels={[
          { id: 'model-1', label: 'GPT-5.4', provider: 'openai', model: 'gpt-5.4', enabled: true },
        ]}
        permData={{
          is_owner: true,
          scope_type: 'company',
          scope_ids: [],
          access_level: 'manage',
          scope_names: [],
        }}
        canManage
        canManageCapabilityPolicies
        capabilityPolicies={[
          {
            id: 'policy-1',
            capability: 'workspace.file.delete',
            agent_id: 'agent-1',
            allowed: false,
            requires_approval: false,
            conditions: {},
          },
          {
            id: 'policy-2',
            capability: 'workspace.command.secret_exfiltration',
            agent_id: 'agent-1',
            allowed: true,
            requires_approval: true,
            conditions: {},
          },
        ]}
        capabilityDefinitions={[
          { capability: 'workspace.file.read', tools: ['list_files', 'read_file'] },
          { capability: 'workspace.file.write', tools: ['write_file', 'edit_file'] },
          { capability: 'workspace.file.delete', tools: ['delete_file'] },
          { capability: 'workspace.command.execute', tools: ['run_command'] },
          { capability: 'workspace.command.secret_exfiltration', tools: ['run_command'] },
          { capability: 'agent.objective.modify', tools: ['propose_objective', 'update_objective', 'complete_objective'] },
          { capability: 'unknown.future.capability', tools: ['future_tool'] },
        ]}
        capabilityPolicyLoading={false}
        capabilityPolicyError=""
        settingsForm={{
          primary_model_id: 'model-1',
          fallback_model_id: '',
          // max_tokens_per_day: 10000,
          // max_tokens_per_month: 200000,
          max_triggers: 10,
          min_poll_interval_min: 5,
          webhook_rate_limit: 5,
          smart_model_routing_enabled: false,
          security_zone: 'restricted',
        }}
        onSettingsFormChange={vi.fn()}
        settingsSaving={false}
        settingsSaved={false}
        settingsError=""
        onSetSettingsSaving={vi.fn()}
        onSetSettingsSaved={vi.fn()}
        onSetSettingsError={vi.fn()}
        onResetSettingsInit={vi.fn()}
        wmDraft="Hello there"
        wmSaved={false}
        onSetWmDraft={vi.fn()}
        onSetWmSaved={vi.fn()}
        showDeleteConfirm={false}
        onSetShowDeleteConfirm={vi.fn()}
      />,
    );

    expect(markup).toContain('modelConfig');
    expect(markup).toContain('Execution Mode');
    expect(markup).toContain('Coordinator');
    expect(markup).toContain('Runtime Safety Boundary');
    expect(markup).toContain('Loose (Default)');
    expect(markup).toContain('Approval Guard');
    expect(markup).toContain('Read-only Lockdown');
    expect(markup).toContain('Ordered from loose to strict');
    expect(markup.indexOf('Loose (Default)')).toBeLessThan(markup.indexOf('Approval Guard'));
    expect(markup.indexOf('Approval Guard')).toBeLessThan(markup.indexOf('Read-only Lockdown'));
    expect(markup).not.toContain('>Public<');
    expect(markup).not.toContain('>Restricted<');
    expect(markup).toContain('Run Shell Commands');
    expect(markup).toContain('Secret/Environment Reads');
    expect(markup).toContain('Objectives');
    expect(markup).toContain('unknown.future.capability');
    expect(markup).toContain('future_tool');
    expect(markup).toContain('value="approval" selected=""');
    expect(markup).toContain('welcomeMessage');
    expect(markup).toContain('value="deny" selected=""');
    expect(markup).not.toContain('value="L2"');
    expect(markup).toContain('Access Permissions');
    expect(markup).toContain('Channel Config Mock');
    expect(markup).toContain('deleteAgent');
  });

  it('renders AgentChatSection as a standalone chat module', () => {
    const markup = renderToStaticMarkup(
      <AgentChatSection
        agent={{ id: 'agent-1', name: 'Release Bot' }}
        currentUser={{ id: 'user-1' }}
        isAdmin={false}
        chatScope="mine"
        onSetChatScope={vi.fn()}
        onLoadAllSessions={vi.fn()}
        onCreateNewSession={vi.fn()}
        sessionsLoading={false}
        sessions={[
          {
            id: 'session-1',
            user_id: 'user-1',
            title: 'Launch sync',
            created_at: '2026-03-27T09:00:00Z',
            last_message_at: '2026-03-27T09:30:00Z',
            message_count: 3,
          },
        ]}
        activeSession={{
          id: 'session-1',
          user_id: 'user-1',
          title: 'Launch sync',
          created_at: '2026-03-27T09:00:00Z',
        }}
        wsConnected
        allSessions={[]}
        allSessionsLoading={false}
        allUserFilter=""
        onSetAllUserFilter={vi.fn()}
        onSelectSession={vi.fn()}
        onDeleteSession={vi.fn()}
        historyContainerRef={React.createRef<HTMLDivElement>()}
        onHistoryScroll={vi.fn()}
        historyMsgs={[]}
        showHistoryScrollBtn={false}
        onScrollHistoryToBottom={vi.fn()}
        chatContainerRef={React.createRef<HTMLDivElement>()}
        onChatScroll={vi.fn()}
        chatMessages={[
          { role: 'assistant', content: 'Ship it' },
        ]}
        runtimeSummary={{
          model: {
            label: 'GPT-5.4',
            provider: 'openai',
            name: 'gpt-5.4',
            context_window_tokens: 128000,
          },
          runtime: {
            connected: true,
            estimated_input_tokens: 18400,
            remaining_tokens_estimate: 109600,
          },
          activated_packs: ['web-research'],
          used_tools: ['search_query'],
          blocked_capabilities: [],
          compaction_count: 2,
          last_compaction: {
            summary: 'Compacted older turns and kept the active work context.',
            original_message_count: 26,
            kept_message_count: 8,
          },
        }}
        transportNotice={null}
        isWaiting={false}
        chatEndRef={React.createRef<HTMLDivElement>()}
        showScrollBtn={false}
        onScrollToBottom={vi.fn()}
        agentExpired={false}
        attachedFiles={[
          { name: 'notes.md', text: '# notes' },
        ]}
        onRemoveAttachedFile={vi.fn()}
        fileInputRef={React.createRef<HTMLInputElement>()}
        onHandleChatFile={vi.fn()}
        uploading={false}
        uploadProgress={-1}
        uploadAbortRef={{ current: null }}
        chatInputRef={React.createRef<HTMLTextAreaElement>()}
        chatInput="Can you summarize?"
        onSetChatInput={vi.fn()}
        onHandlePaste={vi.fn()}
        onSendChatMsg={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('Launch sync');
    expect(markup).toContain('Ship it');
    expect(markup).toContain('notes.md');
    expect(markup).toContain('chat-input');
    expect(markup).toContain('send');
  });

  it('renders structured HR preview details for tool results', () => {
    const markup = renderToStaticMarkup(
      <StructuredToolResultBody
        toolName="preview_agent_blueprint"
        toolResult='{"status":"preview"}'
        toolMeta={{
          kind: 'hr_preview',
          name: 'Research Bot',
          mission: 'Research competitors and write briefs.',
          firstMission: 'Create the first competitor landscape brief.',
          readyNow: ['Builtin tools + default skills + memory loop'],
          willInstall: ['mcp: github'],
          deferredCapabilities: ['github-research'],
          warnings: ['primary_users is empty — the agent may be less clear about who it serves.'],
          manualSteps: ['Validate the first deliverable before expanding capabilities.'],
        }}
      />,
    );

    expect(markup).toContain('Research Bot');
    expect(markup).toContain('Ready Now');
    expect(markup).toContain('Will Install');
    expect(markup).toContain('Deferred Capabilities');
    expect(markup).toContain('Warnings');
    expect(markup).toContain('Manual Steps');
    expect(markup).toContain('Builtin tools + default skills + memory loop');
    expect(markup).toContain('mcp: github');
    expect(markup).toContain('github-research');
  });
});
