import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import AgentApprovalsSection from './AgentApprovalsSection';
import AgentActivityLogSection from './AgentActivityLogSection';
import AgentAwareSection, {
  StaleWorkflowRefError,
  buildWakePolicyPayload,
  workflowDefinitionOptionKey,
} from './AgentAwareSection';
import AgentChatSection, {
  BranchComposePanel,
  BranchLineagePanel,
  StructuredToolResultBody,
  buildBranchLineageRows,
  extractPlanIdFromPlanModeMessage,
  getArtifactOpenMode,
  isClarificationCardAnsweredByLaterUserMessage,
} from './AgentChatSection';
import AgentMindSection from './AgentMindSection';
import AgentSettingsSection, { buildPatrolPlanRecommendationInput } from './AgentSettingsSection';
import AgentSkillsSection from './AgentSkillsSection';
import AgentStatusSection from './AgentStatusSection';
import AgentWorkspaceSection from './AgentWorkspaceSection';
import CopyMessageButton from './CopyMessageButton';
import LocalAgentLinkCard from './LocalAgentLinkCard';
import OpenClawSettings from '../OpenClawSettings';
import PlanCard, { confirmAndHandoffPlan } from './PlanCard';
import RelationshipEditor from './RelationshipEditor';
import ToolsManager from './ToolsManager';
import { AGENT_DETAIL_TABS } from '../AgentDetail';
import type { PlanRequest } from '../../api/domains/plans';

const queryKeyCalls = vi.hoisted(() => [] as unknown[][]);

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
  useQuery: ({ queryKey, enabled }: { queryKey: unknown[]; enabled?: boolean }) => {
    queryKeyCalls.push(queryKey);
    if (enabled === false) {
      return { data: undefined, isLoading: false, isError: false, error: null };
    }
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
    if (key === 'triggers') {
      return {
        data: [
          {
            id: 'trigger-settings-patrol',
            name: 'settings_patrol',
            type: 'interval',
            is_enabled: true,
            last_fired_at: '2026-03-27T09:00:00Z',
            config: {
              source: 'settings_patrol',
              minutes: 90,
              active_hours: '10:00-19:00',
              trigger_class: 'scheduled_job',
            },
          },
        ],
      };
    }
    if (key === 'workflow-definitions') {
      return {
        data: [
          {
            id: 'wf-1',
            name: 'daily-report',
            definition_version: 3,
            definition_hash: 'hash-v3',
            status: 'active',
            visibility_scope: 'tenant',
            owner_type: 'user',
            owner_id: null,
            call_policy: null,
            promoted_from_run_id: null,
          },
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
    if (key === 'local-bridge-connections') {
      return {
        data: {
          connections: [
            {
              id: 'bridge-1',
              tenant_id: 'tenant-1',
              agent_id: 'agent-1',
              user_id: 'user-1',
              device_name: 'Codex Desktop',
              client_kind: 'codex',
              status: 'active',
              scopes: ['gateway:poll', 'gateway:report', 'files:upload'],
              last_seen_at: new Date().toISOString(),
              created_at: new Date().toISOString(),
              revoked_at: null,
            },
          ],
        },
        isLoading: false,
        isError: false,
        refetch: vi.fn(),
      };
    }
    if (key === 'local-bridge-work-requests') {
      return {
        data: {
          work_requests: [
            {
              id: 'work-request-1',
              agent_id: 'agent-1',
              tenant_id: 'tenant-1',
              sender_user_id: 'user-1',
              conversation_id: 'session-local-bridge',
              content: 'Upload a markdown report to workspace',
              status: 'completed',
              result: 'done by command runtime',
              attachments: [{ path: 'workspace/local-bridge/report.md', direction: 'result' }],
              metadata: { kind: 'work_request', report: { runtime: 'command' } },
              created_at: '2026-06-22T06:20:00Z',
              delivered_at: '2026-06-22T06:20:03Z',
              completed_at: '2026-06-22T06:20:08Z',
            },
          ],
        },
        isLoading: false,
        isError: false,
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
    if (key === 'agent-plan-inline') {
      const planId = String(queryKey[2] || 'plan-inline-1');
      return {
        data: {
          id: planId,
          agent_id: 'agent-1',
          tenant_id: null,
          session_id: 'session-1',
          runtime_task_id: null,
          requested_by_user_id: 'user-1',
          source: 'web_chat',
          intent_type: 'autonomous_wake',
          original_request: '每天自动总结 Reddit 投资观点',
          status: 'awaiting_confirmation',
          plan_version: 1,
          plan_hash: 'sha256:inline-plan',
          plan_markdown_path: null,
          plan_json: {
            title: 'Daily Reddit investor monitoring plan',
            objective: 'Summarize Reddit investor opinions every afternoon.',
            steps: [{ order: 1, description: 'Collect relevant Reddit posts.', expected_output: 'Source list.' }],
            success_criteria: ['A concise Markdown summary is produced.'],
            wake_policy: { type: 'cron', timezone: 'Asia/Shanghai', expr: '0 13 * * *' },
            risk_assessment: { level: 'medium', reasons: ['recurring autonomous execution'] },
          },
          handoff_status: null,
          handoff_payload: null,
          confirmed_by_user_id: null,
          confirmed_at: null,
          rejected_by_user_id: null,
          rejected_at: null,
          superseded_by_plan_id: null,
          expires_at: null,
          created_at: null,
          updated_at: null,
          metadata: {},
        },
        refetch: vi.fn(),
      };
    }
    if (key === 'chat-session-work-ledger' || key === 'chat-work-ledger') {
      if (queryKey.includes('stale-session')) {
        return { data: undefined, isLoading: false, isError: true, error: new Error('missing') };
      }
      return {
        data: {
          schema: 'agent_work_ledger_view.v1',
          runtime_task_id: 'task-deep-1',
          session_id: 'session-1',
          source: 'deep_research',
          status: 'running',
          current_phase: 'collect_sources',
          todo_items: [
            { id: 'todo-1', title: 'Collect and grade sources', status: 'running', required: true },
            { id: 'todo-2', title: 'Write final report', status: 'pending', required: true },
          ],
          verification: [
            { id: 'verify-1', title: 'Verify citations', status: 'pending', required: true },
          ],
          progress: [
            { id: 'progress-1', status: 'running', delta: 'Started source collection.' },
          ],
          failures: [],
          findings: [],
          evidence_refs: ['runtime_artifacts/long_tasks/task-deep-1/deep_research/report.md'],
          counts: {
            todos_total: 2,
            todos_complete: 0,
            todos_open: 2,
            verification_pending: 1,
            progress_count: 1,
            failures_open: 0,
          },
          updated_at: '2026-06-01T10:00:00Z',
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
  default: ({
    title,
    rootPath,
    readOnly,
    singleFile,
  }: {
    title?: string;
    rootPath?: string;
    readOnly?: boolean;
    singleFile?: string;
  }) => (
    <div>
      {title || 'File Browser Mock'}
      {rootPath ? ` root=${rootPath}` : ''}
      {singleFile ? ` single=${singleFile}` : ''}
      {readOnly ? ' readOnly=true' : ''}
    </div>
  ),
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

  it('renders transcript-anchored conversation branch actions on messages', () => {
    const markup = renderToStaticMarkup(
      <AgentChatSection
        agentId="agent-1"
        agent={{ id: 'agent-1', name: 'Release Bot' }}
        currentUser={{ id: 'user-1' }}
        isAdmin={false}
        chatScope="mine"
        onSetChatScope={vi.fn()}
        onLoadAllSessions={vi.fn()}
        onCreateNewSession={vi.fn()}
        sessionsLoading={false}
        sessions={[]}
        activeSession={{
          id: 'session-1',
          user_id: 'user-1',
          title: 'Branchable run',
          created_at: '2026-06-01T09:00:00Z',
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
        historyMessagesSessionId={null}
        showHistoryScrollBtn={false}
        onScrollHistoryToBottom={vi.fn()}
        chatContainerRef={React.createRef<HTMLDivElement>()}
        onChatScroll={vi.fn()}
        chatMessages={[{ id: 'event-1', role: 'user', content: 'Use the Railway logs.' }]}
        chatMessagesSessionId="session-1"
        runtimeSummary={null}
        transportNotice={null}
        isWaiting={false}
        activeRunStatus={null}
        chatEndRef={React.createRef<HTMLDivElement>()}
        showScrollBtn={false}
        onScrollToBottom={vi.fn()}
        agentExpired={false}
        attachedFiles={[]}
        onRemoveAttachedFile={vi.fn()}
        fileInputRef={React.createRef<HTMLInputElement>()}
        onHandleChatFile={vi.fn()}
        uploading={false}
        uploadProgress={-1}
        uploadAbortRef={{ current: null }}
        chatInputRef={React.createRef<HTMLTextAreaElement>()}
        chatInput=""
        onSetChatInput={vi.fn()}
        onHandlePaste={vi.fn()}
        onSendChatMsg={vi.fn()}
        onBranchMessage={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('data-testid="message-action-fork"');
    expect(markup).toContain('data-testid="message-action-edit"');
    expect(markup).toContain('data-testid="message-action-insert-after"');
  });

  it('renders branch lineage as a selectable tree', () => {
    const rows = buildBranchLineageRows([
      { id: 'root', parent_session_id: null, title: 'Original', branch: {} },
      { id: 'edit', parent_session_id: 'root', title: 'Original (edit)', branch: { branch_mode: 'edit' } },
      { id: 'reply', parent_session_id: 'edit', title: 'Follow-up', branch: { branch_mode: 'reply' } },
    ]);

    expect(rows.map((row) => [row.id, row.depth])).toEqual([
      ['root', 0],
      ['edit', 1],
      ['reply', 2],
    ]);

    const markup = renderToStaticMarkup(
      <BranchLineagePanel
        activeSessionId="edit"
        lineage={[
          { id: 'root', parent_session_id: null, title: 'Original', branch: {} },
          { id: 'edit', parent_session_id: 'root', title: 'Original (edit)', branch: { branch_mode: 'edit' } },
        ]}
        onSelectSession={vi.fn()}
      />,
    );

    expect(markup).toContain('data-testid="branch-lineage-panel"');
    expect(markup).toContain('Original (edit)');
    expect(markup).toContain('edit');
  });

  it('renders an in-app branch compose panel instead of relying on browser prompts', () => {
    const markup = renderToStaticMarkup(
      <BranchComposePanel
        draft={{
          mode: 'edit',
          message: { id: 'event-1', role: 'user', content: 'Original request' },
          content: 'Edited request',
        }}
        busy={false}
        onChange={vi.fn()}
        onCancel={vi.fn()}
        onSubmit={vi.fn()}
      />,
    );

    expect(markup).toContain('data-testid="branch-compose-panel"');
    expect(markup).toContain('Edited request');
    expect(markup).toContain('Create branch');
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
    expect(markup).toContain('Skill Format:');
    expect(markup).not.toContain('root=skills');
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
        awareTriggers={[
          {
            id: 'trigger-1',
            name: 'release-check',
            type: 'cron',
            config: { expr: '0 9 * * *' },
            fire_count: 3,
            is_enabled: true,
            reason: 'Daily release check',
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
        expandedReflection="session-1"
        showAllTriggers={false}
        reflectionPage={0}
        onSetExpandedReflection={() => {}}
        onSetReflectionMessages={() => {}}
        onSetShowAllTriggers={() => {}}
        onSetReflectionPage={() => {}}
        onRefetchTriggers={async () => {}}
        onLoadReflectionMessages={async () => {}}
      />,
    );

    expect(markup).toContain('Every day at 09:00');
    expect(markup).toContain('Daily release check');
    expect(markup).toContain('All systems green.');
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
        awareTriggers={[]}
        reflectionSessions={[]}
        reflectionMessages={{}}
        autonomyOverview={{
          agent_id: 'agent-1',
          lookback_hours: 24,
          totals: { triggers: 2, recent_attempts: 1, findings: 1 },
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
              category: 'trigger_backoff_active',
              message: 'A trigger is waiting to retry after a recent failure.',
              recommendation: 'Review the failure and retry.',
            },
          ],
        }}
        expandedReflection={null}
        showAllTriggers={false}
        reflectionPage={0}
        onSetExpandedReflection={() => {}}
        onSetReflectionMessages={() => {}}
        onSetShowAllTriggers={() => {}}
        onSetReflectionPage={() => {}}
        onRefetchTriggers={async () => {}}
        onRefetchAutonomy={async () => {}}
      />,
    );

    expect(markup).toContain('Daily launch report');
    expect(markup).toContain('Waiting to retry after a recent failure.');
    expect(markup).toContain('Provider quota exceeded');
    expect(markup).not.toContain('trigger-internal-id');
    expect(markup).not.toContain('runtime-internal-id');
    expect(markup).not.toContain('runtime_artifacts/triggers');
  });

  it('builds trigger workflow_ref pins from the selected registered workflow', () => {
    const workflow = {
      id: 'wf-1',
      name: 'daily-report',
      description: '',
      definition_version: 3,
      definition_hash: 'hash-v3',
      status: 'active' as const,
      visibility_scope: 'tenant',
      owner_type: 'user',
      owner_id: null,
      call_policy: null,
      promoted_from_run_id: null,
    };
    const payload = buildWakePolicyPayload(
      {
        mode: 'scheduled_job',
        name: 'daily report trigger',
        reason: 'Run the pinned workflow',
        scheduleType: 'cron',
        cronExpr: '0 9 * * *',
        intervalMinutes: 60,
        onceAt: '',
        eventType: 'on_message',
        maxFires: 1,
        expiresAt: '',
        workflowDefinitionKey: workflowDefinitionOptionKey(workflow),
        workflowArgsText: '{"region":"apac"}',
      },
      workflow,
    );

    expect(payload).toMatchObject({
      name: 'daily report trigger',
      type: 'cron',
      reason: 'Run the pinned workflow',
      config: {
        trigger_class: 'scheduled_job',
        expr: '0 9 * * *',
        workflow_ref: {
          definition_name: 'daily-report',
          definition_version: 3,
          definition_hash: 'hash-v3',
          args: { region: 'apac' },
        },
      },
    });
  });

  it('rejects invalid workflow_ref args before trigger creation', () => {
    expect(() =>
      buildWakePolicyPayload(
        {
          mode: 'scheduled_job',
          name: 'bad args',
          reason: '',
          scheduleType: 'cron',
          cronExpr: '0 9 * * *',
          intervalMinutes: 60,
          onceAt: '',
          eventType: 'on_message',
          maxFires: 1,
          expiresAt: '',
          workflowDefinitionKey: 'daily-report::3::hash-v3',
          workflowArgsText: '{bad json',
        },
        {
          id: 'wf-1',
          name: 'daily-report',
          description: '',
          definition_version: 3,
          definition_hash: 'hash-v3',
          status: 'active',
          visibility_scope: 'tenant',
          owner_type: 'user',
          owner_id: null,
          call_policy: null,
          promoted_from_run_id: null,
        },
      ),
    ).toThrow();
  });

  it('rejects stale workflow_ref selections before trigger creation', () => {
    expect(() =>
      buildWakePolicyPayload(
        {
          mode: 'scheduled_job',
          name: 'stale ref',
          reason: '',
          scheduleType: 'cron',
          cronExpr: '0 9 * * *',
          intervalMinutes: 60,
          onceAt: '',
          eventType: 'on_message',
          maxFires: 1,
          expiresAt: '',
          workflowDefinitionKey: 'missing::1::hash',
          workflowArgsText: '{}',
        },
        undefined,
      ),
    ).toThrow();
  });

  it('distinguishes a stale workflow selection from invalid args JSON', () => {
    const base = {
      mode: 'scheduled_job',
      name: 'distinguish errors',
      reason: '',
      scheduleType: 'cron',
      cronExpr: '0 9 * * *',
      intervalMinutes: 60,
      onceAt: '',
      eventType: 'on_message',
      maxFires: 1,
      expiresAt: '',
      workflowDefinitionKey: 'missing::1::hash',
      workflowArgsText: '{}',
    };
    // Stale selection → typed error so the UI can say "pick another template"
    // instead of blaming the args JSON.
    expect(() => buildWakePolicyPayload(base, undefined)).toThrow(StaleWorkflowRefError);
    // Broken JSON keeps throwing a NON-stale error (the JSON message path).
    expect(() =>
      buildWakePolicyPayload(
        { ...base, workflowDefinitionKey: 'daily-report::3::hash-v3', workflowArgsText: '{bad json' },
        {
          id: 'wf-1',
          name: 'daily-report',
          description: '',
          definition_version: 3,
          definition_hash: 'hash-v3',
          status: 'active',
          visibility_scope: 'tenant',
          owner_type: 'user',
          owner_id: null,
          call_policy: null,
          promoted_from_run_id: null,
        },
      ),
    ).not.toThrow(StaleWorkflowRefError);
  });

  it('renders AgentMindSection as a standalone mind module', () => {
    const markup = renderToStaticMarkup(<AgentMindSection agentId="agent-1" canEdit />);

    expect(markup).toContain('Core identity, personality, and behavior boundaries.');
    expect(markup).toContain('Long-term knowledge curated from conversations. Feedback, strategies, blocked patterns, and project knowledge.');
    expect(markup).toContain('Curation history, performance scorecard, and blocked approaches.');
    expect(markup).toContain('soul.md is governed by Dream/Soul promotion.');
    expect(markup).not.toContain('single=soul.md');
    expect(markup).toContain('root=memory readOnly=true');
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
    expect(markup).toContain('Patrol &amp; Agent Circle');
    expect(markup).toContain('Enable patrol');
    expect(markup).toContain('Patrol interval');
    expect(markup).toContain('Active hours');
    expect(markup).toContain('value="90"');
    expect(markup).toContain('type="time"');
    expect(markup).toContain('value="10:00"');
    expect(markup).toContain('value="19:00"');
    expect(markup).not.toContain('value="10:00-19:00"');
    expect(markup).not.toContain('Memory Distillation');
    expect(markup).not.toContain('Always on');
    expect(markup).not.toContain('Enable Heartbeat');
    expect(markup).not.toContain('Agent will periodically check Agent Circle and work status');
    expect(markup).toContain('Runtime Safety Boundary');
    expect(markup).toContain('Loose (Default)');
    expect(markup).toContain('Approval Guard');
    expect(markup).toContain('Read-only Lockdown');
    expect(markup).not.toContain('Local Agent Link');
    expect(markup).not.toContain('Local Agent Workbench');
    expect(markup).toContain('Ordered from loose to strict');
    expect(markup.indexOf('Loose (Default)')).toBeLessThan(markup.indexOf('Approval Guard'));
    expect(markup.indexOf('Approval Guard')).toBeLessThan(markup.indexOf('Read-only Lockdown'));
    expect(markup).not.toContain('>Public<');
    expect(markup).not.toContain('>Restricted<');
    expect(markup).toContain('Run Shell Commands');
    expect(markup).toContain('Secret/Environment Reads');
    expect(markup).not.toContain('Manage Tasks');
    expect(markup).not.toContain('Create, update, or complete tasks');
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

  it('places Local Agent immediately after Sub-agents in the agent detail tabs', () => {
    const tabs = Array.from(AGENT_DETAIL_TABS);
    expect(tabs[tabs.indexOf('subagents') + 1]).toBe('localAgent');
  });

  it('renders Local Agent Link as its own agent detail work surface', () => {
    const markup = renderToStaticMarkup(<LocalAgentLinkCard agentId="agent-1" canManage />);

    expect(markup).toContain('Local Agent Link');
    expect(markup).toContain('hive-bridge login');
    expect(markup).toContain('Pairing code');
    expect(markup).toContain('Approve link');
    expect(markup).toContain('Local agent online');
    expect(markup).toContain('Codex Desktop');
    expect(markup).toContain('Cloud work request');
    expect(markup).toContain('Send work request');
    expect(markup).toContain('Local Agent Workbench');
    expect(markup).toContain('done by command runtime');
    expect(markup).toContain('workspace/local-bridge/report.md');
  });

  it('builds a bound Plan Mode opt-out recommendation for patrol saves', () => {
    expect(
      buildPatrolPlanRecommendationInput({
        agentId: 'agent-1',
        reason: 'Run scheduled patrols',
        actionKind: 'enable_autonomous_wake',
      }),
    ).toMatchObject({
      original_request: 'Run scheduled patrols',
      session_id: 'settings_patrol:agent-1',
      source: 'settings',
      intent_type: 'autonomous_wake',
      action_kind: 'enable_autonomous_wake',
      tool_name: 'trigger_rest',
      metadata: { surface: 'agent_settings_patrol' },
    });
  });

  it('lets admins edit access permissions for non-owned agents', () => {
    const markup = renderToStaticMarkup(
      <AgentSettingsSection
        agentId="agent-1"
        agent={{
          id: 'agent-1',
          agent_type: 'native',
          primary_model_id: 'model-1',
          fallback_model_id: '',
          max_triggers: 10,
          min_poll_interval_min: 5,
          webhook_rate_limit: 5,
          security_zone: 'standard',
        }}
        llmModels={[
          { id: 'model-1', label: 'GPT-5.4', provider: 'openai', model: 'gpt-5.4', enabled: true },
        ]}
        permData={{
          is_owner: false,
          scope_type: 'company',
          scope_ids: [],
          access_level: 'use',
          scope_names: [],
        }}
        canManage
        canManageCapabilityPolicies
        capabilityPolicies={[]}
        capabilityDefinitions={[{ capability: 'workspace.file.read', tools: ['read_file'] }]}
        capabilityPolicyLoading={false}
        capabilityPolicyError=""
        settingsForm={{
          primary_model_id: 'model-1',
          fallback_model_id: '',
          max_triggers: 10,
          min_poll_interval_min: 5,
          webhook_rate_limit: 5,
          smart_model_routing_enabled: false,
          security_zone: 'standard',
        }}
        onSettingsFormChange={vi.fn()}
        settingsSaving={false}
        settingsSaved={false}
        settingsError=""
        onSetSettingsSaving={vi.fn()}
        onSetSettingsSaved={vi.fn()}
        onSetSettingsError={vi.fn()}
        onResetSettingsInit={vi.fn()}
        wmDraft=""
        wmSaved={false}
        onSetWmDraft={vi.fn()}
        onSetWmSaved={vi.fn()}
        showDeleteConfirm={false}
        onSetShowDeleteConfirm={vi.fn()}
      />,
    );

    expect(markup).toContain('Default Access Level');
    expect(markup).not.toMatch(/name="perm_scope"[^>]*disabled/);
    expect(markup).not.toMatch(/Only the creator or admin can change permissions/);
  });

  it('lets admins edit OpenClaw access permissions for non-owned agents', () => {
    const markup = renderToStaticMarkup(
      <OpenClawSettings
        agent={{ id: 'agent-1', name: 'OpenClaw Agent', agent_type: 'openclaw', has_api_key: false }}
        agentId="agent-1"
        isAdmin
      />,
    );

    expect(markup).toContain('Default Access Level');
    expect(markup).not.toMatch(/name="perm_scope_oc"[^>]*disabled/);
    expect(markup).not.toMatch(/Only the creator or admin can change permissions/);
  });

  it('renders AgentChatSection as a standalone chat module', () => {
    const rawCompactionSummary = [
      '**Primary Request and Intent:** Do not render this internal summary in the default chat UI.',
      '**Tool Outcomes:** Search ran successfully.',
      '**Current Work:** Hide raw compaction details from the runtime chrome.',
      '**Recovery Context:** Internal restore details should stay behind an explicit disclosure.',
    ].join('\n');
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
        historyMessagesSessionId={null}
        showHistoryScrollBtn={false}
        onScrollHistoryToBottom={vi.fn()}
        chatContainerRef={React.createRef<HTMLDivElement>()}
        onChatScroll={vi.fn()}
        chatMessages={[
          { role: 'assistant', content: 'Ship it' },
        ]}
        chatMessagesSessionId="session-1"
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
          activated_tool_groups: ['web-research'],
          used_tools: ['search_query'],
          blocked_capabilities: [],
          compaction_count: 2,
          last_compaction: {
            summary: rawCompactionSummary,
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
    expect(markup).not.toContain('Primary Request and Intent');
    expect(markup).not.toContain('Recovery Context');
  });

  it('uses the route agent id for chat runtime queries when cached agent data is stale', () => {
    queryKeyCalls.length = 0;

    renderToStaticMarkup(
      <AgentChatSection
        agentId="route-agent"
        agent={{ id: 'stale-agent', name: 'Release Bot' }}
        currentUser={{ id: 'user-1' }}
        isAdmin={false}
        chatScope="mine"
        onSetChatScope={vi.fn()}
        onLoadAllSessions={vi.fn()}
        onCreateNewSession={vi.fn()}
        sessionsLoading={false}
        sessions={[]}
        activeSession={{
          id: 'session-1',
          agent_id: 'route-agent',
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
        historyMessagesSessionId={null}
        showHistoryScrollBtn={false}
        onScrollHistoryToBottom={vi.fn()}
        chatContainerRef={React.createRef<HTMLDivElement>()}
        onChatScroll={vi.fn()}
        chatMessages={[]}
        chatMessagesSessionId="session-1"
        runtimeSummary={null}
        transportNotice={null}
        isWaiting={false}
        chatEndRef={React.createRef<HTMLDivElement>()}
        showScrollBtn={false}
        onScrollToBottom={vi.fn()}
        agentExpired={false}
        attachedFiles={[]}
        onRemoveAttachedFile={vi.fn()}
        fileInputRef={React.createRef<HTMLInputElement>()}
        onHandleChatFile={vi.fn()}
        uploading={false}
        uploadProgress={-1}
        uploadAbortRef={{ current: null }}
        chatInputRef={React.createRef<HTMLTextAreaElement>()}
        chatInput=""
        onSetChatInput={vi.fn()}
        onHandlePaste={vi.fn()}
        onSendChatMsg={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(queryKeyCalls).toContainEqual(['chat-session-work-ledger', 'route-agent', 'session-1']);
    expect(queryKeyCalls).not.toContainEqual(['chat-session-work-ledger', 'stale-agent', 'session-1']);
  });

  it('renders assistant artifacts directly inside the chat transcript', () => {
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
        sessions={[]}
        activeSession={{
          id: 'session-1',
          user_id: 'user-1',
          title: 'Report delivery',
          created_at: '2026-06-20T09:00:00Z',
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
        historyMessagesSessionId={null}
        showHistoryScrollBtn={false}
        onScrollHistoryToBottom={vi.fn()}
        chatContainerRef={React.createRef<HTMLDivElement>()}
        onChatScroll={vi.fn()}
        chatMessages={[
          {
            role: 'assistant',
            content: '报告已经生成。',
            artifacts: [
              {
                id: 'artifact-1',
                name: 'market-report.md',
                path: 'workspace/market-report.md',
                previewKind: 'markdown',
                source: 'workspace_write',
              },
            ],
          },
        ]}
        chatMessagesSessionId="session-1"
        runtimeSummary={null}
        transportNotice={null}
        isWaiting={false}
        chatEndRef={React.createRef<HTMLDivElement>()}
        showScrollBtn={false}
        onScrollToBottom={vi.fn()}
        agentExpired={false}
        attachedFiles={[]}
        onRemoveAttachedFile={vi.fn()}
        fileInputRef={React.createRef<HTMLInputElement>()}
        onHandleChatFile={vi.fn()}
        uploading={false}
        uploadProgress={-1}
        uploadAbortRef={{ current: null }}
        chatInputRef={React.createRef<HTMLTextAreaElement>()}
        chatInput=""
        onSetChatInput={vi.fn()}
        onHandlePaste={vi.fn()}
        onSendChatMsg={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('报告已经生成。');
    expect(markup).toContain('market-report.md');
    expect(markup).toContain('Open');
    expect(markup).toContain('Download');
    expect(markup).toContain('/api/agents/agent-1/files/download?path=workspace%2Fmarket-report.md');
  });

  it('shows ordinary tool-call steps while keeping raw results collapsed by default', () => {
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
        sessions={[]}
        activeSession={{
          id: 'session-1',
          user_id: 'user-1',
          title: 'Tool trace',
          created_at: '2026-06-20T09:00:00Z',
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
        historyMessagesSessionId={null}
        showHistoryScrollBtn={false}
        onScrollHistoryToBottom={vi.fn()}
        chatContainerRef={React.createRef<HTMLDivElement>()}
        onChatScroll={vi.fn()}
        chatMessages={[
          {
            role: 'tool_call',
            content: '',
            toolName: 'read_file',
            toolArgs: { path: 'workspace/report.md' },
            toolStatus: 'done',
            toolResult: 'RAW FILE CONTENT SHOULD NOT BE INLINE',
          },
        ]}
        chatMessagesSessionId="session-1"
        runtimeSummary={null}
        transportNotice={null}
        isWaiting={false}
        chatEndRef={React.createRef<HTMLDivElement>()}
        showScrollBtn={false}
        onScrollToBottom={vi.fn()}
        agentExpired={false}
        attachedFiles={[]}
        onRemoveAttachedFile={vi.fn()}
        fileInputRef={React.createRef<HTMLInputElement>()}
        onHandleChatFile={vi.fn()}
        uploading={false}
        uploadProgress={-1}
        uploadAbortRef={{ current: null }}
        chatInputRef={React.createRef<HTMLTextAreaElement>()}
        chatInput=""
        onSetChatInput={vi.fn()}
        onHandlePaste={vi.fn()}
        onSendChatMsg={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('Processed');
    expect(markup).toContain('read_file');
    expect(markup).toContain('workspace/report.md');
    expect(markup).not.toContain('RAW FILE CONTENT SHOULD NOT BE INLINE');
  });

  it('groups consecutive runtime steps into one turn-level disclosure block before the final answer', () => {
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
        sessions={[]}
        activeSession={{
          id: 'session-1',
          user_id: 'user-1',
          title: 'Grouped turn',
          created_at: '2026-06-22T09:00:00Z',
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
        historyMessagesSessionId={null}
        showHistoryScrollBtn={false}
        onScrollHistoryToBottom={vi.fn()}
        chatContainerRef={React.createRef<HTMLDivElement>()}
        onChatScroll={vi.fn()}
        chatMessages={[
          {
            role: 'assistant',
            content: '',
            thinking: 'Inspect code before answering.',
            timestamp: '2026-06-22T10:00:00Z',
          },
          {
            role: 'tool_call',
            content: '',
            toolName: 'read_file',
            toolArgs: { path: 'frontend/src/pages/agent-detail/AgentChatSection.tsx' },
            toolStatus: 'done',
            toolResult: 'RAW READ FILE CONTENT',
            timestamp: '2026-06-22T10:00:01Z',
          },
          {
            role: 'event',
            content: 'Compacted prior context.',
            eventType: 'session_compact',
            eventTitle: 'Context Compacted',
            timestamp: '2026-06-22T10:00:02Z',
          },
          {
            role: 'tool_call',
            content: '',
            toolName: 'execute_code',
            toolArgs: { cmd: 'npm test -- --run chatDisclosureReducer.test.ts' },
            toolStatus: 'done',
            toolResult: 'RAW COMMAND OUTPUT',
            timestamp: '2026-06-22T10:00:03Z',
          },
          {
            role: 'assistant',
            content: '最终答案已经完成。',
            timestamp: '2026-06-22T10:00:04Z',
          },
        ]}
        chatMessagesSessionId="session-1"
        runtimeSummary={null}
        transportNotice={null}
        isWaiting={false}
        chatEndRef={React.createRef<HTMLDivElement>()}
        showScrollBtn={false}
        onScrollToBottom={vi.fn()}
        agentExpired={false}
        attachedFiles={[]}
        onRemoveAttachedFile={vi.fn()}
        fileInputRef={React.createRef<HTMLInputElement>()}
        onHandleChatFile={vi.fn()}
        uploading={false}
        uploadProgress={-1}
        uploadAbortRef={{ current: null }}
        chatInputRef={React.createRef<HTMLTextAreaElement>()}
        chatInput=""
        onSetChatInput={vi.fn()}
        onHandlePaste={vi.fn()}
        onSendChatMsg={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup.match(/data-testid="run-disclosure-block"/g)?.length).toBe(1);
    expect(markup).toContain('Thinking');
    expect(markup).toContain('read_file');
    expect(markup).toContain('Context Compacted');
    expect(markup).toContain('execute_code');
    expect(markup).toContain('最终答案已经完成。');
    expect(markup).not.toContain('RAW READ FILE CONTENT');
    expect(markup).not.toContain('RAW COMMAND OUTPUT');
  });

  it('extracts Plan Mode plan ids from assistant replies', () => {
    expect(
      extractPlanIdFromPlanModeMessage(
        '已进入计划模式，并生成一份待确认计划（plan_id=a7cdfa75-cec5-4062-8bda-b18b2d2821a3）。请在计划卡片中确认。',
      ),
    ).toBe('a7cdfa75-cec5-4062-8bda-b18b2d2821a3');
    expect(extractPlanIdFromPlanModeMessage('普通回复，没有计划 ID')).toBeNull();
  });

  it('routes chat artifacts to inline preview only when the file type is previewable', () => {
    expect(getArtifactOpenMode({ name: 'report.md', path: 'workspace/report.md', previewKind: 'markdown' })).toBe('inline_preview');
    expect(getArtifactOpenMode({ name: 'notes.txt', path: 'workspace/notes.txt', previewKind: 'text' })).toBe('inline_preview');
    expect(getArtifactOpenMode({ name: 'chart.png', path: 'workspace/chart.png', previewKind: 'image' })).toBe('inline_preview');
    expect(getArtifactOpenMode({ name: 'slides.pdf', path: 'workspace/slides.pdf', previewKind: 'pdf' })).toBe('inline_preview');
    expect(getArtifactOpenMode({ name: 'deck.pptx', path: 'workspace/deck.pptx', previewKind: 'office' })).toBe('download');
    expect(getArtifactOpenMode({ name: 'archive.zip', path: 'workspace/archive.zip', previewKind: 'download' })).toBe('download');
  });

  it('renders PlanCard inline in the chatbox when an assistant reply contains a plan_id', () => {
    const planId = 'a7cdfa75-cec5-4062-8bda-b18b2d2821a3';
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
        sessions={[]}
        activeSession={{
          id: 'session-1',
          user_id: 'user-1',
          title: 'Plan Mode run',
          created_at: '2026-06-01T09:00:00Z',
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
        historyMessagesSessionId={null}
        showHistoryScrollBtn={false}
        onScrollHistoryToBottom={vi.fn()}
        chatContainerRef={React.createRef<HTMLDivElement>()}
        onChatScroll={vi.fn()}
        chatMessages={[
          {
            role: 'assistant',
            content: `已进入计划模式，并生成一份待确认计划（plan_id=${planId}）。请在计划卡片中确认、修改或拒绝；确认后我再开始执行。`,
          },
        ]}
        chatMessagesSessionId="session-1"
        runtimeSummary={null}
        transportNotice={null}
        isWaiting={false}
        chatEndRef={React.createRef<HTMLDivElement>()}
        showScrollBtn={false}
        onScrollToBottom={vi.fn()}
        agentExpired={false}
        attachedFiles={[]}
        onRemoveAttachedFile={vi.fn()}
        fileInputRef={React.createRef<HTMLInputElement>()}
        onHandleChatFile={vi.fn()}
        uploading={false}
        uploadProgress={-1}
        uploadAbortRef={{ current: null }}
        chatInputRef={React.createRef<HTMLTextAreaElement>()}
        chatInput=""
        onSetChatInput={vi.fn()}
        onHandlePaste={vi.fn()}
        onSendChatMsg={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('Daily Reddit investor monitoring plan');
    expect(markup).toContain('Confirm and start');
  });

  it('keeps Plan Mode tool-result cards visible when internal trace is hidden', () => {
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
        sessions={[]}
        activeSession={{
          id: 'session-1',
          user_id: 'user-1',
          title: 'Plan Mode run',
          created_at: '2026-06-01T09:00:00Z',
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
        historyMessagesSessionId={null}
        showHistoryScrollBtn={false}
        onScrollHistoryToBottom={vi.fn()}
        chatContainerRef={React.createRef<HTMLDivElement>()}
        onChatScroll={vi.fn()}
        chatMessages={[
          {
            role: 'tool_call',
            content: '',
            toolName: 'set_trigger',
            toolStatus: 'done',
            toolResult: 'Plan created',
            toolMeta: {
              kind: 'plan_needs_confirmation',
              planId: 'a7cdfa75-cec5-4062-8bda-b18b2d2821a3',
              planVersion: 1,
              planHash: 'sha256:inline-tool',
              status: 'needs_plan',
              summary: 'Plan created',
              nextAction: 'Confirm before creating the trigger.',
              planJson: {
                title: 'Inline tool plan',
                objective: 'Confirm a recurring trigger before execution.',
                wake_policy: { type: 'cron', timezone: 'Asia/Shanghai', expr: '0 13 * * *' },
              },
            },
          },
        ]}
        chatMessagesSessionId="session-1"
        runtimeSummary={null}
        transportNotice={null}
        isWaiting={false}
        chatEndRef={React.createRef<HTMLDivElement>()}
        showScrollBtn={false}
        onScrollToBottom={vi.fn()}
        agentExpired={false}
        attachedFiles={[]}
        onRemoveAttachedFile={vi.fn()}
        fileInputRef={React.createRef<HTMLInputElement>()}
        onHandleChatFile={vi.fn()}
        uploading={false}
        uploadProgress={-1}
        uploadAbortRef={{ current: null }}
        chatInputRef={React.createRef<HTMLTextAreaElement>()}
        chatInput=""
        onSetChatInput={vi.fn()}
        onHandlePaste={vi.fn()}
        onSendChatMsg={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    // CC-align §4.5: plan_needs_confirmation now renders InlinePlanCard (the REAL
    // plan fetched by id), NOT a synthetic card built from toolMeta — so the
    // fetched plan title shows, the stale toolMeta title does not, and the card
    // reflects live status instead of a hardcoded awaiting_confirmation.
    expect(markup).toContain('Daily Reddit investor monitoring plan');
    expect(markup).not.toContain('Inline tool plan');
    expect(markup).toContain('Confirm and start');
    expect(markup).toContain('Confirm before creating the trigger.');
  });

  it('renders completed create-digital-employee tool cards in the chat transcript', () => {
    const markup = renderToStaticMarkup(
      <AgentChatSection
        agent={{ id: 'agent-1', name: 'HR Bot' }}
        currentUser={{ id: 'user-1' }}
        isAdmin={false}
        chatScope="mine"
        onSetChatScope={vi.fn()}
        onLoadAllSessions={vi.fn()}
        onCreateNewSession={vi.fn()}
        sessionsLoading={false}
        sessions={[]}
        activeSession={{
          id: 'session-1',
          user_id: 'user-1',
          title: 'Create employee',
          created_at: '2026-06-20T09:00:00Z',
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
        historyMessagesSessionId={null}
        showHistoryScrollBtn={false}
        onScrollHistoryToBottom={vi.fn()}
        chatContainerRef={React.createRef<HTMLDivElement>()}
        onChatScroll={vi.fn()}
        chatMessages={[
          {
            role: 'tool_call',
            content: '',
            toolName: 'create_digital_employee',
            toolStatus: 'done',
            toolResult: 'Created',
            toolMeta: {
              kind: 'create_employee_success',
              agentId: 'bef8b286-b923-4e29-84c9-022f995ae6b3',
              agentName: 'RWA项目与营销专员',
              message: '数字员工已创建完成。',
              warnings: [],
              manualSteps: [],
            },
          },
        ]}
        chatMessagesSessionId="session-1"
        runtimeSummary={null}
        transportNotice={null}
        isWaiting={false}
        chatEndRef={React.createRef<HTMLDivElement>()}
        showScrollBtn={false}
        onScrollToBottom={vi.fn()}
        agentExpired={false}
        attachedFiles={[]}
        onRemoveAttachedFile={vi.fn()}
        fileInputRef={React.createRef<HTMLInputElement>()}
        onHandleChatFile={vi.fn()}
        uploading={false}
        uploadProgress={-1}
        uploadAbortRef={{ current: null }}
        chatInputRef={React.createRef<HTMLTextAreaElement>()}
        chatInput=""
        onSetChatInput={vi.fn()}
        onHandlePaste={vi.fn()}
        onSendChatMsg={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('Digital Employee Created');
    expect(markup).toContain('RWA项目与营销专员');
    expect(markup).toContain('数字员工已创建完成。');
  });

  it('renders the running task todo dock above the chat composer', () => {
    const markup = renderToStaticMarkup(
      <AgentChatSection
        agent={{ id: 'agent-1', name: 'Research Bot' }}
        currentUser={{ id: 'user-1' }}
        isAdmin={false}
        chatScope="mine"
        onSetChatScope={vi.fn()}
        onLoadAllSessions={vi.fn()}
        onCreateNewSession={vi.fn()}
        sessionsLoading={false}
        sessions={[]}
        activeSession={{
          id: 'session-1',
          user_id: 'user-1',
          title: 'Deep Research run',
          created_at: '2026-06-01T09:00:00Z',
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
        historyMessagesSessionId={null}
        showHistoryScrollBtn={false}
        onScrollHistoryToBottom={vi.fn()}
        chatContainerRef={React.createRef<HTMLDivElement>()}
        onChatScroll={vi.fn()}
        chatMessages={[
          {
            role: 'tool_call',
            content: '',
            toolName: 'deep_research_start',
            toolStatus: 'done',
            toolResult: 'Deep research running',
            toolMeta: {
              kind: 'deep_research',
              taskId: 'task-deep-1',
              status: 'running',
              summary: 'Research has started.',
              reportPath: null,
              artifactDir: 'runtime_artifacts/long_tasks/task-deep-1/deep_research',
              sourceCount: null,
              claimCount: null,
              qualityGates: {},
              gaps: [],
            },
          },
        ]}
        chatMessagesSessionId="session-1"
        runtimeSummary={null}
        transportNotice={null}
        isWaiting={false}
        chatEndRef={React.createRef<HTMLDivElement>()}
        showScrollBtn={false}
        onScrollToBottom={vi.fn()}
        agentExpired={false}
        attachedFiles={[]}
        onRemoveAttachedFile={vi.fn()}
        fileInputRef={React.createRef<HTMLInputElement>()}
        onHandleChatFile={vi.fn()}
        uploading={false}
        uploadProgress={-1}
        uploadAbortRef={{ current: null }}
        chatInputRef={React.createRef<HTMLTextAreaElement>()}
        chatInput=""
        onSetChatInput={vi.fn()}
        onHandlePaste={vi.fn()}
        onSendChatMsg={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('data-testid="chat-work-ledger-dock"');
    expect(markup).toContain('Collect and grade sources');
    expect(markup).toContain('Write final report');
    expect(markup).not.toContain('Deep Research');
    expect(markup.indexOf('data-testid="chat-work-ledger-dock"')).toBeLessThan(markup.indexOf('chat-input'));
  });

  it('does not keep polling a stale historical Deep Research start result', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-01T17:18:00Z'));
    try {
      const markup = renderToStaticMarkup(
        <AgentChatSection
          agent={{ id: 'agent-1', name: 'Research Bot' }}
          currentUser={{ id: 'user-1' }}
          isAdmin={false}
          chatScope="mine"
          onSetChatScope={vi.fn()}
          onLoadAllSessions={vi.fn()}
          onCreateNewSession={vi.fn()}
          sessionsLoading={false}
          sessions={[]}
          activeSession={{
            id: 'stale-session',
            user_id: 'user-1',
            title: 'Old Deep Research run',
            created_at: '2026-05-29T07:18:00Z',
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
          historyMessagesSessionId={null}
          showHistoryScrollBtn={false}
          onScrollHistoryToBottom={vi.fn()}
          chatContainerRef={React.createRef<HTMLDivElement>()}
          onChatScroll={vi.fn()}
          chatMessages={[
            {
              role: 'tool_call',
              content: '',
              toolName: 'deep_research_start',
              toolStatus: 'done',
              toolResult: 'Deep research running',
              timestamp: '2026-05-29T07:18:00Z',
              toolMeta: {
                kind: 'deep_research',
                taskId: 'old-deep-task',
                status: 'running',
                summary: 'Research has started.',
                reportPath: null,
                artifactDir: 'runtime_artifacts/long_tasks/old-deep-task/deep_research',
                sourceCount: null,
                claimCount: null,
                qualityGates: {},
                gaps: [],
              },
            },
          ]}
          chatMessagesSessionId="stale-session"
          runtimeSummary={null}
          transportNotice={null}
          isWaiting={false}
          chatEndRef={React.createRef<HTMLDivElement>()}
          showScrollBtn={false}
          onScrollToBottom={vi.fn()}
          agentExpired={false}
          attachedFiles={[]}
          onRemoveAttachedFile={vi.fn()}
          fileInputRef={React.createRef<HTMLInputElement>()}
          onHandleChatFile={vi.fn()}
          uploading={false}
          uploadProgress={-1}
          uploadAbortRef={{ current: null }}
          chatInputRef={React.createRef<HTMLTextAreaElement>()}
          chatInput=""
          onSetChatInput={vi.fn()}
          onHandlePaste={vi.fn()}
          onSendChatMsg={vi.fn()}
          isStreaming={false}
          onAbortGeneration={vi.fn()}
        />,
      );

      expect(markup).not.toContain('data-testid="chat-work-ledger-dock"');
      expect(markup).not.toContain('old-deep-task');
    } finally {
      vi.useRealTimers();
    }
  });

  it('renders the persistent session work ledger dock without Deep Research tool metadata', () => {
    const markup = renderToStaticMarkup(
      <AgentChatSection
        agent={{ id: 'agent-1', name: 'Builder Bot' }}
        currentUser={{ id: 'user-1' }}
        isAdmin={false}
        chatScope="mine"
        onSetChatScope={vi.fn()}
        onLoadAllSessions={vi.fn()}
        onCreateNewSession={vi.fn()}
        sessionsLoading={false}
        sessions={[]}
        activeSession={{
          id: 'session-1',
          user_id: 'user-1',
          title: 'Todo run',
          created_at: '2026-06-01T09:00:00Z',
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
        historyMessagesSessionId={null}
        showHistoryScrollBtn={false}
        onScrollHistoryToBottom={vi.fn()}
        chatContainerRef={React.createRef<HTMLDivElement>()}
        onChatScroll={vi.fn()}
        chatMessages={[
          {
            role: 'assistant',
            content: 'I am working through the implementation todos.',
          },
        ]}
        chatMessagesSessionId="session-1"
        runtimeSummary={null}
        transportNotice={null}
        isWaiting={false}
        activeRunStatus="running"
        chatEndRef={React.createRef<HTMLDivElement>()}
        showScrollBtn={false}
        onScrollToBottom={vi.fn()}
        agentExpired={false}
        attachedFiles={[]}
        onRemoveAttachedFile={vi.fn()}
        fileInputRef={React.createRef<HTMLInputElement>()}
        onHandleChatFile={vi.fn()}
        uploading={false}
        uploadProgress={-1}
        uploadAbortRef={{ current: null }}
        chatInputRef={React.createRef<HTMLTextAreaElement>()}
        chatInput=""
        onSetChatInput={vi.fn()}
        onHandlePaste={vi.fn()}
        onSendChatMsg={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('data-testid="chat-work-ledger-dock"');
    expect(markup).toContain('Collect and grade sources');
    expect(markup.indexOf('data-testid="chat-work-ledger-dock"')).toBeLessThan(markup.indexOf('chat-input'));
  });

  it('shows the durable run continuation state while a session run is active', () => {
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
        sessions={[]}
        activeSession={{
          id: 'session-1',
          user_id: 'user-1',
          title: 'Long planning run',
          created_at: '2026-05-21T09:00:00Z',
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
        historyMessagesSessionId={null}
        showHistoryScrollBtn={false}
        onScrollHistoryToBottom={vi.fn()}
        chatContainerRef={React.createRef<HTMLDivElement>()}
        onChatScroll={vi.fn()}
        chatMessages={[]}
        chatMessagesSessionId="session-1"
        runtimeSummary={null}
        transportNotice={null}
        isWaiting
        activeRunStatus="running"
        chatEndRef={React.createRef<HTMLDivElement>()}
        showScrollBtn={false}
        onScrollToBottom={vi.fn()}
        agentExpired={false}
        attachedFiles={[]}
        onRemoveAttachedFile={vi.fn()}
        fileInputRef={React.createRef<HTMLInputElement>()}
        onHandleChatFile={vi.fn()}
        uploading={false}
        uploadProgress={-1}
        uploadAbortRef={{ current: null }}
        chatInputRef={React.createRef<HTMLTextAreaElement>()}
        chatInput=""
        onSetChatInput={vi.fn()}
        onHandlePaste={vi.fn()}
        onSendChatMsg={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('Agent is continuing this run...');
    expect(markup).toContain('btn-stop-generation');
  });

  it('keeps the send action available while a session run is active', () => {
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
        sessions={[]}
        activeSession={{
          id: 'session-1',
          user_id: 'user-1',
          title: 'Long planning run',
          created_at: '2026-05-21T09:00:00Z',
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
        historyMessagesSessionId={null}
        showHistoryScrollBtn={false}
        onScrollHistoryToBottom={vi.fn()}
        chatContainerRef={React.createRef<HTMLDivElement>()}
        onChatScroll={vi.fn()}
        chatMessages={[]}
        chatMessagesSessionId="session-1"
        runtimeSummary={null}
        transportNotice={null}
        isWaiting
        activeRunStatus="running"
        chatEndRef={React.createRef<HTMLDivElement>()}
        showScrollBtn={false}
        onScrollToBottom={vi.fn()}
        agentExpired={false}
        attachedFiles={[]}
        onRemoveAttachedFile={vi.fn()}
        fileInputRef={React.createRef<HTMLInputElement>()}
        onHandleChatFile={vi.fn()}
        uploading={false}
        uploadProgress={-1}
        uploadAbortRef={{ current: null }}
        chatInputRef={React.createRef<HTMLTextAreaElement>()}
        chatInput="Add this while it is still running"
        onSetChatInput={vi.fn()}
        onHandlePaste={vi.fn()}
        onSendChatMsg={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('btn-stop-generation');
    expect(markup).toContain('btn-primary');
    expect(markup).toContain('send');
  });

  it('does not render stale chat messages from a different active session', () => {
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
        sessions={[]}
        activeSession={{
          id: 'session-2',
          user_id: 'user-1',
          title: 'New RWA run',
          created_at: '2026-05-12T02:11:00Z',
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
        historyMessagesSessionId={null}
        showHistoryScrollBtn={false}
        onScrollHistoryToBottom={vi.fn()}
        chatContainerRef={React.createRef<HTMLDivElement>()}
        onChatScroll={vi.fn()}
        chatMessages={[
          { role: 'assistant', content: 'Task ID: `old-task-id`' },
        ]}
        chatMessagesSessionId="session-1"
        runtimeSummary={null}
        transportNotice={null}
        isWaiting={false}
        chatEndRef={React.createRef<HTMLDivElement>()}
        showScrollBtn={false}
        onScrollToBottom={vi.fn()}
        agentExpired={false}
        attachedFiles={[]}
        onRemoveAttachedFile={vi.fn()}
        fileInputRef={React.createRef<HTMLInputElement>()}
        onHandleChatFile={vi.fn()}
        uploading={false}
        uploadProgress={-1}
        uploadAbortRef={{ current: null }}
        chatInputRef={React.createRef<HTMLTextAreaElement>()}
        chatInput=""
        onSetChatInput={vi.fn()}
        onHandlePaste={vi.fn()}
        onSendChatMsg={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('New RWA run');
    expect(markup).not.toContain('old-task-id');
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

  it('treats a persisted clarification card as answered when a later user message exists', () => {
    const messages = [
      {
        role: 'tool_call' as const,
        content: '',
        toolName: 'ask_user_question',
        toolStatus: 'done' as const,
        toolMeta: {
          kind: 'user_clarification' as const,
          questions: [{ question: 'Scope?', header: 'Scope', options: [{ label: 'Mine', description: '' }], multiSelect: false }],
          blocking: true,
          nextAction: null,
        },
      },
      {
        role: 'user' as const,
        content: 'Scope: Mine',
      },
    ];

    expect(isClarificationCardAnsweredByLaterUserMessage(messages, 0)).toBe(true);
    expect(isClarificationCardAnsweredByLaterUserMessage(messages, 1)).toBe(false);
  });

  it('treats durable clarification answer metadata as answered after refresh', () => {
    const messages = [
      {
        role: 'tool_call' as const,
        content: '',
        toolName: 'ask_user_question',
        toolStatus: 'done' as const,
        toolMeta: {
          kind: 'user_clarification' as const,
          answered: true,
          answeredByEventId: 'evt-user-answer',
          answerText: 'Scope: Mine',
          questions: [{ question: 'Scope?', header: 'Scope', options: [{ label: 'Mine', description: '' }], multiSelect: false }],
          blocking: true,
          nextAction: null,
        },
      },
    ];

    expect(isClarificationCardAnsweredByLaterUserMessage(messages, 0)).toBe(true);
  });

  it('renders PlanCard with plan_json fields and confirmation actions while awaiting', () => {
    const plan = {
      id: 'plan-1',
      agent_id: 'agent-1',
      tenant_id: null,
      session_id: null,
      runtime_task_id: null,
      requested_by_user_id: null,
      source: 'web_chat',
      intent_type: 'autonomous_wake',
      original_request: 'Send me a daily industry brief',
      status: 'awaiting_confirmation',
      plan_version: 1,
      plan_hash: 'sha256:abc123',
      plan_markdown_path: null,
      plan_json: {
        title: 'Daily industry news brief',
        objective: 'Produce a useful daily industry brief for the user.',
        motivation: 'User asked for a recurring morning industry news summary.',
        steps: [{ order: 1, description: 'Collect high-signal news sources.', expected_output: 'Source list.' }],
        success_criteria: ['Brief includes 5-10 material updates with source links.'],
        wake_policy: { type: 'cron', timezone: 'Asia/Shanghai', expr: '0 9 * * 1-5' },
        required_capabilities: ['web_search', 'send_feishu_message'],
        external_side_effects: [{ kind: 'message', channel: 'feishu', audience: 'requesting user', requires_confirmation: true }],
        risk_assessment: { level: 'medium', reasons: ['recurring autonomous wake'] },
        estimated_cost: { tokens_per_run: 'medium', expected_duration: '1-3 minutes' },
        stop_conditions: ['User cancels the plan.'],
        assumptions: ['User wants Asia-market focus by default.'],
        open_questions: ['Which sectors should the brief prioritise?'],
      },
      handoff_status: null,
      handoff_payload: null,
      confirmed_by_user_id: null,
      confirmed_at: null,
      rejected_by_user_id: null,
      rejected_at: null,
      superseded_by_plan_id: null,
      expires_at: null,
      created_at: null,
      updated_at: null,
      metadata: {
        planner_work_ledger: {
          schema: 'agent_work_ledger.v1',
          path: 'plans/plan-1.work_ledger.json',
          updated_at: '2026-06-01T00:00:00Z',
        },
      },
    } as PlanRequest;

    const markup = renderToStaticMarkup(<PlanCard agentId="agent-1" plan={plan} />);

    expect(markup).toContain('Daily industry news brief');
    expect(markup).toContain('Produce a useful daily industry brief for the user.');
    expect(markup).toContain('Collect high-signal news sources.');
    expect(markup).toContain('Brief includes 5-10 material updates with source links.');
    expect(markup).toContain('0 9 * * 1-5');
    expect(markup).not.toContain('Capabilities');
    expect(markup).not.toContain('web_search');
    expect(markup).not.toContain('send_feishu_message');
    expect(markup).toContain('1-3 minutes');
    expect(markup).toContain('feishu');
    expect(markup).not.toContain('Work ledger');
    expect(markup).not.toContain('plans/plan-1.work_ledger.json');
    // Risk level renders via its raw value fallback (i18n mock returns the fallback string).
    expect(markup).toContain('medium');
    expect(markup).toContain('User cancels the plan.');
    expect(markup).toContain('User wants Asia-market focus by default.');
    expect(markup).toContain('Which sectors should the brief prioritise?');
    // Actionable while awaiting confirmation; confirmation should clearly start handoff.
    expect(markup).toContain('Confirm and start');
    expect(markup).toContain('Request changes');
    expect(markup).toContain('Reject');
  });

  it('does not expose internal ledger paths or empty side-effect placeholders in PlanCard', () => {
    const plan = {
      id: 'plan-1',
      agent_id: 'agent-1',
      tenant_id: null,
      session_id: null,
      runtime_task_id: null,
      requested_by_user_id: null,
      source: 'web_chat',
      intent_type: 'long_task',
      original_request: '使用 deepresearch做一个web3的全景报告',
      status: 'awaiting_confirmation',
      plan_version: 1,
      plan_hash: 'sha256:abc123',
      plan_markdown_path: null,
      plan_json: {
        title: 'Web3 全景深度研究报告',
        objective: '生成面向投资决策的中文研究报告。',
        external_side_effects: [{}, { kind: '', channel: '', audience: '' }],
      },
      handoff_status: null,
      handoff_payload: null,
      confirmed_by_user_id: null,
      confirmed_at: null,
      rejected_by_user_id: null,
      rejected_at: null,
      superseded_by_plan_id: null,
      expires_at: null,
      created_at: null,
      updated_at: null,
      metadata: {
        planner_work_ledger: {
          schema: 'agent_work_ledger.v1',
          path: 'runtime_artifacts/long_tasks/7063ee2e/work_ledger.json',
        },
      },
    } as PlanRequest;

    const markup = renderToStaticMarkup(<PlanCard agentId="agent-1" plan={plan} />);

    expect(markup).toContain('Web3 全景深度研究报告');
    expect(markup).not.toContain('External side effects');
    expect(markup).not.toContain('External action');
    expect(markup).not.toContain('Work ledger');
    expect(markup).not.toContain('runtime_artifacts/long_tasks/7063ee2e/work_ledger.json');
  });

  it('renders PlanCard recovery actions and failure reasons when planning failed', () => {
    const plan = {
      id: 'plan-failed',
      agent_id: 'agent-1',
      tenant_id: null,
      session_id: null,
      runtime_task_id: null,
      requested_by_user_id: null,
      source: 'web_chat',
      intent_type: 'long_task',
      original_request: '使用 deepresearch做一个RWA pre-ipo的全景报告',
      status: 'planning_failed',
      plan_version: 1,
      plan_hash: null,
      plan_markdown_path: null,
      plan_json: {
        title: '使用 deepresearch做一个RWA pre-ipo的全景报告',
      },
      handoff_status: null,
      handoff_payload: null,
      confirmed_by_user_id: null,
      confirmed_at: null,
      rejected_by_user_id: null,
      rejected_at: null,
      superseded_by_plan_id: null,
      expires_at: null,
      created_at: null,
      updated_at: null,
      metadata: {
        planning_errors: ['missing required field: objective'],
        planner_work_ledger: {
          schema: 'agent_work_ledger.v1',
          path: 'runtime_artifacts/long_tasks/735ae31e/work_ledger.json',
        },
      },
    } as PlanRequest;

    const markup = renderToStaticMarkup(<PlanCard agentId="agent-1" plan={plan} />);

    expect(markup).toContain('使用 deepresearch做一个RWA pre-ipo的全景报告');
    expect(markup).toContain('Planning failed');
    expect(markup).toContain('missing required field: objective');
    expect(markup).toContain('Retry plan generation');
    expect(markup).toContain('Revise and retry');
    expect(markup).toContain('Reject');
    expect(markup).not.toContain('Confirm and start');
    expect(markup).not.toContain('No actions available for this plan.');
  });

  it('renders PlanCard planning state without terminal or confirmation actions', () => {
    const plan = {
      id: 'plan-planning',
      agent_id: 'agent-1',
      tenant_id: null,
      session_id: null,
      runtime_task_id: null,
      requested_by_user_id: null,
      source: 'web_chat',
      intent_type: 'long_task',
      original_request: 'Plan a market research report',
      status: 'planning',
      plan_version: 1,
      plan_hash: null,
      plan_markdown_path: null,
      plan_json: {
        title: 'Market research report',
      },
      handoff_status: null,
      handoff_payload: null,
      confirmed_by_user_id: null,
      confirmed_at: null,
      rejected_by_user_id: null,
      rejected_at: null,
      superseded_by_plan_id: null,
      expires_at: null,
      created_at: null,
      updated_at: null,
      metadata: {},
    } as PlanRequest;

    const markup = renderToStaticMarkup(<PlanCard agentId="agent-1" plan={plan} />);

    expect(markup).toContain('Market research report');
    expect(markup).toContain('Planning in progress');
    expect(markup).toContain('The agent is drafting a confirmable plan.');
    expect(markup).not.toContain('Confirm and start');
    expect(markup).not.toContain('Retry plan generation');
    expect(markup).not.toContain('No actions available for this plan.');
  });

  it('confirms a plan and immediately hands it off to execution', async () => {
    const plan = {
      id: 'plan-1',
      agent_id: 'agent-1',
      tenant_id: null,
      session_id: null,
      runtime_task_id: null,
      requested_by_user_id: null,
      source: 'web_chat',
      intent_type: 'autonomous_wake',
      original_request: 'Send me a daily industry brief',
      status: 'awaiting_confirmation',
      plan_version: 3,
      plan_hash: 'sha256:abc123',
      plan_markdown_path: null,
      plan_json: {},
      handoff_status: null,
      handoff_payload: null,
      confirmed_by_user_id: null,
      confirmed_at: null,
      rejected_by_user_id: null,
      rejected_at: null,
      superseded_by_plan_id: null,
      expires_at: null,
      created_at: null,
      updated_at: null,
      metadata: {},
    } as PlanRequest;
    const api = {
      confirmAndHandoff: vi.fn(async () => {
        return { ok: true, status: 'confirmed', plan_id: 'plan-1', handoff_status: 'completed', handoff_payload: {} };
      }),
    };

    await confirmAndHandoffPlan('agent-1', plan, api);

    expect(api.confirmAndHandoff).toHaveBeenCalledWith('agent-1', 'plan-1', {
      plan_version: 3,
      plan_hash: 'sha256:abc123',
    });
  });

  it('renders PlanCard without actions once a plan is confirmed', () => {
    const plan = {
      id: 'plan-2',
      agent_id: 'agent-1',
      tenant_id: null,
      session_id: null,
      runtime_task_id: null,
      requested_by_user_id: null,
      source: 'web_chat',
      intent_type: 'autonomous_wake',
      original_request: 'Send me a daily industry brief',
      status: 'confirmed',
      plan_version: 1,
      plan_hash: 'sha256:abc123',
      plan_markdown_path: null,
      plan_json: { title: 'Daily industry news brief' },
      handoff_status: 'completed',
      handoff_payload: null,
      confirmed_by_user_id: 'user-1',
      confirmed_at: '2026-05-29T09:00:00Z',
      rejected_by_user_id: null,
      rejected_at: null,
      superseded_by_plan_id: null,
      expires_at: null,
      created_at: null,
      updated_at: null,
      metadata: {},
    } as PlanRequest;

    const markup = renderToStaticMarkup(<PlanCard agentId="agent-1" plan={plan} />);

    expect(markup).toContain('Daily industry news brief');
    // CC-align §4.5/§4.6: a confirmed plan shows its real execution state via the
    // handoff banner (here: started, executing in this conversation) — never a
    // stale confirm/revise button.
    expect(markup).toContain('Started — executing in this conversation');
    expect(markup).not.toContain('Request changes');
    expect(markup).not.toContain('Confirm and start');
  });

  it('renders PlanCard handoff states: queued, skipped reason, and the markdown body', () => {
    const base = {
      id: 'plan-3',
      agent_id: 'agent-1',
      tenant_id: null,
      session_id: 'sess-1',
      runtime_task_id: null,
      requested_by_user_id: 'user-1',
      source: 'web_chat',
      intent_type: 'long_task',
      original_request: 'RWA weekly',
      plan_version: 1,
      plan_hash: 'sha256:abc',
      plan_markdown_path: null,
      confirmed_by_user_id: 'user-1',
      confirmed_at: '2026-06-08T00:00:00Z',
      rejected_by_user_id: null,
      rejected_at: null,
      superseded_by_plan_id: null,
      expires_at: null,
      created_at: null,
      updated_at: null,
      metadata: {},
    };

    // queued: confirmed but waiting for the active run.
    const queued = {
      ...base,
      status: 'confirmed',
      plan_json: { title: 'RWA 周报', plan_markdown: '## 思路\n聚焦三条赛道，给出投资视角。' },
      handoff_status: 'queued',
      handoff_payload: { reason: 'active_run_exists' },
    } as PlanRequest;
    const queuedMarkup = renderToStaticMarkup(<PlanCard agentId="agent-1" plan={queued} />);
    // Markdown body is the primary surface (CC-align §4.1).
    expect(queuedMarkup).toContain('聚焦三条赛道，给出投资视角。');
    expect(queuedMarkup).toContain('Confirmed — waiting for the current run to finish');
    expect(queuedMarkup).not.toContain('Confirm and start');

    // skipped: visible reason, not a silent state.
    const skipped = {
      ...base,
      status: 'confirmed',
      plan_json: { title: 'RWA 周报' },
      handoff_status: 'skipped',
      handoff_payload: { reason: 'no_handler_registered' },
    } as PlanRequest;
    const skippedMarkup = renderToStaticMarkup(<PlanCard agentId="agent-1" plan={skipped} />);
    expect(skippedMarkup).toContain('Confirmed, but execution did not start');
    expect(skippedMarkup).toContain('no_handler_registered');
  });
});
