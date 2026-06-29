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
  SessionCommandControlPanel,
  StructuredToolResultBody,
  buildBranchLineageRows,
  extractPlanIdFromPlanModeMessage,
  getArtifactOpenMode,
  isPendingEmptyArtifactPreview,
  isClarificationCardAnsweredByLaterUserMessage,
} from './AgentChatSection';
import AgentGovernanceSection from './AgentGovernanceSection';
import AgentMindSection from './AgentMindSection';
import AgentSettingsSection, { buildPatrolPlanRecommendationInput } from './AgentSettingsSection';
import AgentSkillsSection from './AgentSkillsSection';
import AgentStatusSection from './AgentStatusSection';
import AgentWorkspaceSection from './AgentWorkspaceSection';
import CopyMessageButton from './CopyMessageButton';
import PlanCard, { confirmAndHandoffPlan } from './PlanCard';
import AgentA2ASection from './AgentA2ASection';
import ToolsManager from './ToolsManager';
import {
  AGENT_DETAIL_TABS,
  buildAgentDetailTabNavigation,
  getAgentDetailHashTab,
  getVisibleAgentDetailTabs,
  isLocalAgentRuntimeType,
  isSessionWorkbenchRoute,
} from '../AgentDetail';
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
    if (key === 'a2a-collaborators') {
      return {
        data: {
          same_owner_agents: [
            {
              id: 'agent-3',
              name: 'Same Owner Bot',
              role_description: 'Can collaborate directly',
              status: 'running',
              policy_reason: 'same_owner',
            },
          ],
          public_agents: [
            {
              id: 'agent-5',
              name: 'Public Bot',
              role_description: 'Visible to everyone in the tenant',
              status: 'running',
              relation: 'public',
            },
          ],
          collaboration_groups: [
            {
              group_id: 'group-1',
              group_name: 'Launch room',
              status: 'active',
              members: [
                {
                  agent_id: 'agent-4',
                  name: 'Partner Bot',
                  role_description: 'Approved cross-owner member',
                  role: 'member',
                  status: 'active',
                },
              ],
            },
          ],
        },
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
    if (key === 'slash-command-menu') {
      return {
        data: [
          {
            name: 'goal',
            canonical_name: 'goal_start',
            aliases: ['goal'],
            description: 'Start a session goal',
            category: 'goal',
            source: 'builtin',
            execution_mode: 'runtime',
            permission_mode: 'default',
            bridge_safe: true,
            remote_safe: true,
          },
          {
            name: 'team',
            canonical_name: 'team_create',
            aliases: ['team'],
            description: 'Create an enterable agent team',
            category: 'team',
            source: 'builtin',
            execution_mode: 'runtime',
            permission_mode: 'default',
            bridge_safe: true,
            remote_safe: true,
          },
        ],
        isLoading: false,
        isError: false,
        error: null,
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
    if (key === 'capability-definitions') {
      return {
        data: [
          {
            capability: 'workspace.file.write',
            tools: ['write_file', 'edit_file'],
          },
          {
            capability: 'channel.email.send',
            tools: ['send_email', 'reply_email'],
          },
        ],
      };
    }
    if (key === 'capability-policies') {
      return {
        data: [
          {
            id: 'policy-1',
            capability: 'workspace.file.write',
            agent_id: 'agent-1',
            allowed: true,
            requires_approval: true,
            conditions: {},
          },
        ],
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
              scopes: ['local_agent:receive', 'local_agent:report', 'files:upload'],
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
          runtime_task_id: 'workflow-run-1',
          session_id: 'session-1',
          source: 'workflow',
          status: 'running',
          current_phase: 'execute_steps',
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
          evidence_refs: ['runtime_artifacts/workflow_runs/workflow-run-1/report.md'],
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
  it('uses session-only workbench mode for chat routes but not detail management routes', () => {
    expect(isSessionWorkbenchRoute('chat', '?session_id=session-1')).toBe(true);
    expect(isSessionWorkbenchRoute('chat', '')).toBe(false);
    expect(isSessionWorkbenchRoute('chat', '?manage=true')).toBe(false);
    expect(isSessionWorkbenchRoute('chat', '?manage=true&session_id=session-1')).toBe(false);
    expect(isSessionWorkbenchRoute('status', '?session_id=session-1')).toBe(false);
  });

  it('builds detail tab navigation through React Router instead of stale history state', () => {
    expect(buildAgentDetailTabNavigation('/agents/agent-1', '', 'chat', { detailChat: true })).toEqual({
      pathname: '/agents/agent-1',
      search: '?manage=true',
      hash: '#chat',
    });
    expect(buildAgentDetailTabNavigation('/agents/agent-1', '?manage=true&session_id=session-1', 'chat', { detailChat: true })).toEqual({
      pathname: '/agents/agent-1',
      search: '?manage=true&session_id=session-1',
      hash: '#chat',
    });
    expect(getAgentDetailHashTab('#mind', AGENT_DETAIL_TABS)).toBe('knowledge');
    expect(getAgentDetailHashTab('#unknown', AGENT_DETAIL_TABS)).toBeNull();
  });

  it('renders the detail chat conversation browser only outside session-only mode', () => {
    const markup = renderToStaticMarkup(
      <AgentChatSection
        agent={{ id: 'agent-1', name: 'Release Bot' }}
        currentUser={{ id: 'user-1' }}
        isAdmin
        chatScope="all"
        onSetChatScope={vi.fn()}
        onLoadAllSessions={vi.fn()}
        onCreateNewSession={vi.fn()}
        sessionsLoading={false}
        sessions={[
          {
            id: 'session-1',
            user_id: 'user-1',
            title: 'My launch sync',
            created_at: '2026-03-27T09:00:00Z',
          },
        ]}
        activeSession={{
          id: 'session-2',
          user_id: 'user-2',
          title: 'Customer IM thread',
          source_channel: 'feishu',
          username: 'Customer',
          created_at: '2026-03-27T10:00:00Z',
        }}
        wsConnected={false}
        allSessions={[
          {
            id: 'session-2',
            user_id: 'user-2',
            title: 'Customer IM thread',
            source_channel: 'feishu',
            username: 'Customer',
            created_at: '2026-03-27T10:00:00Z',
          },
        ]}
        allSessionsLoading={false}
        allUserFilter=""
        onSetAllUserFilter={vi.fn()}
        onSelectSession={vi.fn()}
        onDeleteSession={vi.fn()}
        historyContainerRef={React.createRef<HTMLDivElement>()}
        onHistoryScroll={vi.fn()}
        historyMsgs={[]}
        historyMessagesSessionId="session-2"
        showHistoryScrollBtn={false}
        onScrollHistoryToBottom={vi.fn()}
        chatContainerRef={React.createRef<HTMLDivElement>()}
        onChatScroll={vi.fn()}
        chatMessages={[]}
        chatMessagesSessionId={null}
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

    expect(markup).toContain('data-testid="detail-session-browser"');
    expect(markup).toContain('My Conversations');
    expect(markup).toContain('All Users');
    expect(markup).toContain('Customer IM thread');
    expect(markup).toContain('class="detail-session-row active"');
    expect(markup).not.toContain('session-only');
  });

  it('renders the Session TUI shell without the legacy hard-coded chat height or composer gap', () => {
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
          title: 'Shell density',
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
        chatMessages={[{ id: 'msg-1', role: 'assistant', content: 'Shell ready.' }]}
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

    expect(markup).toContain('session-tui-shell');
    expect(markup).toContain('session-tui-center');
    expect(markup).toContain('session-tui-history');
    expect(markup).toContain('session-tui-composer');
    expect(markup).not.toContain('height:calc(100vh - 206px)');
    expect(markup).not.toContain('padding:14px 16px 16px');
  });

  it('renders ToolsManager as a standalone module with loading placeholder', () => {
    const markup = renderToStaticMarkup(<ToolsManager agentId="agent-1" canManage />);

    expect(markup).toContain('loading');
  });

  it('renders AgentA2ASection from governed A2A collaborators instead of all tenant agents', () => {
    const markup = renderToStaticMarkup(<AgentA2ASection agentId="agent-1" />);

    expect(markup).toContain('Same-owner agents');
    expect(markup).toContain('Same Owner Bot');
    expect(markup).toContain('Public agents');
    expect(markup).toContain('Public Bot');
    expect(markup).toContain('Launch room');
    expect(markup).toContain('Partner Bot');
    expect(markup).not.toContain('Reviewer Bot');
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

  it('renders chat messages through Session TUI density classes instead of legacy colored bubbles', () => {
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
          title: 'Codex density run',
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
          { id: 'user-msg-1', role: 'user', content: 'Use the checkpoint trail.', timestamp: '2026-06-01T09:00:00Z' },
          {
            id: 'assistant-msg-1',
            role: 'assistant',
            content: 'Checkpoint trail updated.',
            thinking: 'I checked the current branch state.',
            timestamp: '2026-06-01T09:00:02Z',
          },
        ]}
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
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('session-tui-message-row session-tui-message-row-user');
    expect(markup).toContain('session-tui-message-row session-tui-message-row-assistant');
    expect(markup).toContain('session-tui-message-bubble');
    expect(markup).toContain('session-tui-thinking');
    expect(markup).not.toContain('rgba(16,185,129');
    expect(markup).not.toContain('147, 130, 220');
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

  it('renders a session command checkpoint selector as an in-session control panel', () => {
    const markup = renderToStaticMarkup(
      <SessionCommandControlPanel
        control={{
          type: 'checkpoint_selector',
          title: '选择回溯位置',
          message: '选择一个 checkpoint 继续。',
          checkpoints: [
            {
              checkpoint_event_id: 'evt-1',
              sequence: 1,
              role: 'user',
              content: '第一次输入',
            },
            {
              checkpoint_event_id: 'evt-2',
              sequence: 2,
              role: 'user',
              content: '第二次输入',
            },
          ],
        }}
        onDismiss={vi.fn()}
        onRunCommand={vi.fn()}
      />,
    );

    expect(markup).toContain('data-testid="session-command-control-panel"');
    expect(markup).toContain('data-testid="session-checkpoint-row"');
    expect(markup).toContain('第一次输入');
    expect(markup).toContain('第二次输入');
  });

  it('renders compact and rewind command outcomes inside the session instead of toast-only feedback', () => {
    const markup = renderToStaticMarkup(
      <SessionCommandControlPanel
        control={{
          type: 'projection_status',
          title: '上下文已自动压缩',
          message: '后续请求将使用压缩后的上下文。',
          command: 'compact',
        }}
        onDismiss={vi.fn()}
        onRunCommand={vi.fn()}
      />,
    );

    expect(markup).toContain('data-testid="session-command-control-panel"');
    expect(markup).toContain('上下文已自动压缩');
    expect(markup).toContain('后续请求将使用压缩后的上下文');
  });

  it('renders workspace rewind restore outcomes inside the session control panel', () => {
    const markup = renderToStaticMarkup(
      <SessionCommandControlPanel
        control={{
          type: 'projection_status',
          title: 'Workspace restored',
          message: 'Workspace files were restored from the selected checkpoint.',
          command: 'rewind',
          payload: {
            action: 'workspace_rewind_applied',
            restored_count: 1,
            deleted_count: 1,
          },
        }}
        onDismiss={vi.fn()}
        onRunCommand={vi.fn()}
      />,
    );

    expect(markup).toContain('data-testid="session-command-control-panel"');
    expect(markup).toContain('Workspace restored');
    expect(markup).toContain('workspace_rewind_applied');
    expect(markup).toContain('restored_count');
  });

  it('renders resume command status inside the session control panel', () => {
    const markup = renderToStaticMarkup(
      <SessionCommandControlPanel
        control={{
          type: 'resume_picker',
          title: 'Resume session',
          message: 'Session resume status is ready.',
          command: 'resume',
          payload: {
            interrupted: true,
            repair_strategy: 'transcript_replay_chain_repair',
          },
        }}
        onDismiss={vi.fn()}
        onRunCommand={vi.fn()}
      />,
    );

    expect(markup).toContain('data-testid="session-command-control-panel"');
    expect(markup).toContain('Resume session');
    expect(markup).toContain('transcript_replay_chain_repair');
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

  it('renders AgentGovernanceSection as a standalone capability policy module', () => {
    const markup = renderToStaticMarkup(<AgentGovernanceSection agentId="agent-1" canManage />);

    expect(markup).toContain('capability-policies');
    expect(markup).toContain('Workspace File Write');
    expect(markup).toContain('write_file, edit_file');
    expect(markup).toContain('Require approval');
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

  it('renders manual automation creation with Agent vocabulary and without Codex-only controls', () => {
    const markup = renderToStaticMarkup(
      <AgentAwareSection
        agentId="agent-1"
        awareTriggers={[]}
        reflectionSessions={[]}
        reflectionMessages={{}}
        autonomyOverview={{
          agent_id: 'agent-1',
          lookback_hours: 24,
          totals: { triggers: 0, recent_attempts: 0, findings: 0 },
          triggers: [],
          recent_attempts: [],
          findings: [],
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
        initialShowCreateWake
      />,
    );

    expect(markup).toContain('Manual create');
    expect(markup).toContain('Automation title');
    expect(markup).toContain('Select agent');
    expect(markup).toContain('Primary Bot');
    expect(markup).toContain('No workflow');
    expect(markup).toContain('Every hour');
    expect(markup).toContain('Every day');
    expect(markup).toContain('Every week');
    expect(markup).toContain('Custom');
    expect(markup).not.toContain('Work Number');
    expect(markup).not.toContain('Reasoning strength');
    expect(markup).not.toContain('Mode');
  });

  it('builds automation schedule presets into cron wake policies', () => {
    const dailyPayload = buildWakePolicyPayload({
      mode: 'scheduled_job',
      name: 'daily report trigger',
      reason: 'Run daily report',
      scheduleType: 'cron',
      schedulePreset: 'daily',
      dailyTime: '09:30',
      weeklyDay: '1',
      weeklyTime: '09:00',
      cronExpr: '0 9 * * *',
      intervalMinutes: 60,
      onceAt: '',
      eventType: 'on_message',
      maxFires: 1,
      expiresAt: '',
      workflowDefinitionKey: '',
      workflowArgsText: '{}',
    });

    expect(dailyPayload).toMatchObject({
      type: 'cron',
      config: { trigger_class: 'scheduled_job', expr: '30 9 * * *' },
    });

    const weeklyPayload = buildWakePolicyPayload({
      mode: 'scheduled_job',
      name: 'weekly report trigger',
      reason: 'Run weekly report',
      scheduleType: 'cron',
      schedulePreset: 'weekly',
      dailyTime: '09:00',
      weeklyDay: '5',
      weeklyTime: '18:15',
      cronExpr: '0 9 * * *',
      intervalMinutes: 60,
      onceAt: '',
      eventType: 'on_message',
      maxFires: 1,
      expiresAt: '',
      workflowDefinitionKey: '',
      workflowArgsText: '{}',
    });

    expect(weeklyPayload).toMatchObject({
      type: 'cron',
      config: { trigger_class: 'scheduled_job', expr: '15 18 * * 5' },
    });
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
        canManage
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
      />,
    );

    expect(markup).toContain('modelConfig');
    expect(markup).not.toContain('Execution Mode');
    expect(markup).not.toContain('Coordinator');
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
    expect(markup).not.toContain('Runtime Safety Boundary');
    expect(markup).not.toContain('Loose (Default)');
    expect(markup).not.toContain('Approval Guard');
    expect(markup).not.toContain('Read-only Lockdown');
    expect(markup).not.toContain('Local Agent Link');
    expect(markup).not.toContain('Local Agent Workbench');
    expect(markup).not.toContain('Ordered from loose to strict');
    expect(markup).not.toContain('>Public<');
    expect(markup).not.toContain('>Restricted<');
    expect(markup).not.toContain('Run Shell Commands');
    expect(markup).not.toContain('Secret/Environment Reads');
    expect(markup).not.toContain('Manage Tasks');
    expect(markup).not.toContain('Create, update, or complete tasks');
    expect(markup).not.toContain('unknown.future.capability');
    expect(markup).not.toContain('future_tool');
    expect(markup).not.toContain('value="approval" selected=""');
    expect(markup).toContain('welcomeMessage');
    expect(markup).not.toContain('value="deny" selected=""');
    expect(markup).not.toContain('value="L2"');
    expect(markup).not.toContain('Access Permissions');
    expect(markup).toContain('Channel Config Mock');
    expect(markup).not.toContain('deleteAgent');
  });

  it('wires L1 capability policy management into a dedicated governance surface', async () => {
    const fsModuleId = 'node:fs';
    const { readFileSync } = (await import(/* @vite-ignore */ fsModuleId)) as {
      readFileSync: (path: URL, encoding: string) => string;
    };
    const settingsSource = readFileSync(new URL('./AgentSettingsSection.tsx', import.meta.url), 'utf8');
    const governanceSource = readFileSync(new URL('./AgentGovernanceSection.tsx', import.meta.url), 'utf8');
    const detailSource = readFileSync(new URL('../AgentDetail.tsx', import.meta.url), 'utf8');

    expect(settingsSource).not.toContain('renderCapabilityPolicyRow');
    expect(settingsSource).not.toContain('handleScopeChange');
    expect(settingsSource).not.toContain('handleAccessLevelChange');
    expect(settingsSource).not.toContain('showDeleteConfirm');
    expect(Array.from(AGENT_DETAIL_TABS)).toContain('governance');
    expect(detailSource).toContain('capability-policies');
    expect(detailSource).toContain('AgentGovernanceSection');
    expect(governanceSource).toContain('listCapabilityPolicies');
    expect(governanceSource).toContain('upsertCapabilityPolicy');
  });

  it('treats Local Agent as a real agent with a local runtime label and focused detail tabs', () => {
    expect(isLocalAgentRuntimeType({ agent_type: 'local_agent' })).toBe(true);
    expect(isLocalAgentRuntimeType({ agent_type: 'native' })).toBe(false);
    expect(getVisibleAgentDetailTabs({ agent_type: 'local_agent' })).toEqual(['chat', 'workspace']);
    expect(Array.from(AGENT_DETAIL_TABS)).toEqual(expect.arrayContaining(['chat', 'workspace']));
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

  it('does not render Agent access permissions inside detail settings', () => {
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
        canManage
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
      />,
    );

    expect(markup).not.toContain('Access Permissions');
    expect(markup).not.toContain('Default Access Level');
    expect(markup).not.toMatch(/name="perm_scope"/);
    expect(markup).not.toContain('Delete Agent');
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
        sessionOnly
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('Launch sync');
    expect(markup).toContain('data-testid="session-workbench"');
    expect(markup).toContain('session-only');
    expect(markup).toContain('session-tui-shell-session-only');
    expect(markup).not.toContain('calc(100vh - 64px)');
    expect(markup).toContain('data-testid="session-workbench-header"');
    expect(markup).not.toContain('data-testid="session-workbench-sidebar"');
    expect(markup).not.toContain('data-testid="session-workbench-inspector"');
    expect(markup).not.toContain('data-testid="session-native-controls"');
    expect(markup).not.toContain('Start goal');
    expect(markup).not.toContain('Create team');
    expect(markup).not.toContain('My Conversations');
    expect(markup).not.toContain('All Users');
    expect(markup).toContain('Ship it');
    expect(markup).toContain('notes.md');
    expect(markup).toContain('chat-input');
    expect(markup).toContain('send');
    expect(markup).not.toContain('agent.chat.commands.title');
    expect(markup).not.toContain('checkpoints · session');
    expect(markup).not.toContain('Primary Request and Intent');
    expect(markup).not.toContain('Recovery Context');
  });

  it('shows slash command suggestions only when the composer starts with slash', () => {
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
        chatInput="/team"
        onSetChatInput={vi.fn()}
        onHandlePaste={vi.fn()}
        onSendChatMsg={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('data-testid="slash-command-menu"');
    expect(markup).toContain('/team');
    expect(markup).not.toContain('team_create');
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

    expect(queryKeyCalls).toContainEqual(['chat-session-index', 'route-agent', 'session-1']);
    expect(queryKeyCalls).not.toContainEqual(['chat-session-index', 'stale-agent', 'session-1']);
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
    expect(markup).toContain('data-testid="chat-artifact-row-open"');
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
    expect(markup).toContain('Read file');
    expect(markup).toContain('report.md');
    expect(markup).not.toContain('path:');
    expect(markup).not.toContain('RAW FILE CONTENT SHOULD NOT BE INLINE');
  });

  it('renders tool-produced artifacts inside the session timeline', () => {
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
          title: 'Tool artifact',
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
            toolName: 'office_document_apply',
            toolArgs: { path: 'workspace/proposal.docx' },
            toolStatus: 'done',
            toolResult: '{"ok": true}',
            artifacts: [
              {
                id: 'artifact-doc',
                name: 'proposal.docx',
                path: 'workspace/proposal.docx',
                previewKind: 'office',
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

    expect(markup).toContain('office_document_apply');
    expect(markup).toContain('proposal.docx');
    expect(markup).toContain('Open');
    expect(markup).toContain('Download');
    expect(markup).toContain('data-testid="chat-artifact-row-open"');
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
    expect(markup).toContain('Read 1 file');
    expect(markup).toContain('Ran 1 command');
    expect(markup).toContain('Read file');
    expect(markup).toContain('Context Compacted');
    expect(markup).toContain('Run command');
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

  it('renders in-session permission requests with resolver actions', () => {
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
          title: 'Permission request',
          created_at: '2026-06-25T09:00:00Z',
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
            role: 'event',
            content: "Tool 'send_email' requires session permission",
            eventType: 'permission',
            eventTitle: 'Permission Gate',
            eventStatus: 'session_permission_required',
            eventToolName: 'send_email',
            eventCapability: 'communication.email.send',
            sessionPermissionRequest: {
              permission_request_id: '11111111-1111-4111-8111-111111111111',
              session_id: 'session-1',
              tool_name: 'send_email',
              arguments: { to: 'a@example.com' },
              permission_mode: 'default',
            },
          },
        ]}
        chatMessagesSessionId="session-1"
        runtimeSummary={null}
        agentPermissions={{ scope_type: 'company', scope_ids: [], access_level: 'manage' }}
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
        onResolveSessionPermission={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('Permission Gate');
    expect(markup).toContain('send_email');
    expect(markup).toContain('The agent needs permission to use send_email.');
    expect(markup).not.toContain('Tool &#x27;send_email&#x27; requires session permission');
    expect(markup).not.toContain('communication.email.send');
    expect(markup).toContain('Allow once');
    expect(markup).toContain('Allow for this session');
    expect(markup).toContain('Deny');
  });

  it('does not render session-wide approval for destructive permission gates', () => {
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
          title: 'Permission request',
          created_at: '2026-06-25T09:00:00Z',
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
            role: 'event',
            content: "Tool 'run_command' requires session permission",
            eventType: 'permission',
            eventTitle: 'Permission Gate',
            eventStatus: 'session_permission_required',
            eventToolName: 'run_command',
            eventCapability: 'workspace.command.destructive_delete',
            sessionPermissionRequest: {
              permission_request_id: '11111111-1111-4111-8111-111111111111',
              session_id: 'session-1',
              tool_name: 'run_command',
              arguments: { command: 'rm workspace/report.md' },
              permission_mode: 'bypassPermissions',
              risk_class: 'destructive_delete',
              confirmation_kind: 'destructive_once',
              allow_session_allowed: false,
            },
          },
        ]}
        chatMessagesSessionId="session-1"
        runtimeSummary={null}
        agentPermissions={{ scope_type: 'company', scope_ids: [], access_level: 'manage' }}
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
        onResolveSessionPermission={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('Permission Gate');
    expect(markup).toContain('run_command');
    expect(markup).toContain('Allow once');
    expect(markup).not.toContain('Allow for this session');
    expect(markup).toContain('Delete actions can only be allowed once.');
    expect(markup).toContain('Deny');
  });

  it('renders child session runtime events with continuation metadata', () => {
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
          title: 'Parent session',
          created_at: '2026-06-25T09:00:00Z',
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
            role: 'event',
            content: 'Research worker completed.',
            eventType: 'child_session',
            eventTitle: 'Child Session',
            eventStatus: 'completed',
            eventRuntimeTaskId: 'run-1',
            eventChildSessionId: 'child-session-1',
            eventParentSessionId: 'session-1',
          },
        ]}
        chatMessagesSessionId="session-1"
        runtimeSummary={null}
        agentPermissions={{ scope_type: 'company', scope_ids: [], access_level: 'manage' }}
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
        onResolveSessionPermission={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('Child Session');
    expect(markup).toContain('Research worker completed.');
    expect(markup).toContain('child:child-session-1');
    expect(markup).toContain('run:run-1');
  });

  it('routes chat artifacts to the session inspector only when the file type is previewable', () => {
    expect(getArtifactOpenMode({ name: 'report.md', path: 'workspace/report.md', previewKind: 'markdown' })).toBe('inspector_preview');
    expect(getArtifactOpenMode({ name: 'notes.txt', path: 'workspace/notes.txt', previewKind: 'text' })).toBe('inspector_preview');
    expect(getArtifactOpenMode({ name: 'chart.png', path: 'workspace/chart.png', previewKind: 'image' })).toBe('inspector_preview');
    expect(getArtifactOpenMode({ name: 'slides.pdf', path: 'workspace/slides.pdf', previewKind: 'pdf' })).toBe('inspector_preview');
    expect(getArtifactOpenMode({ name: 'deck.pptx', path: 'workspace/deck.pptx', previewKind: 'office' })).toBe('download');
    expect(getArtifactOpenMode({ name: 'archive.zip', path: 'workspace/archive.zip', previewKind: 'download' })).toBe('download');
  });

  it('shows a pending empty-state for zero-byte artifact previews instead of a blank panel', () => {
    expect(
      isPendingEmptyArtifactPreview(
        { name: 'session.plan.md', path: 'workspace/plans/session.plan.md', previewKind: 'markdown', size: 0 },
        '',
      ),
    ).toBe(true);
    expect(
      isPendingEmptyArtifactPreview(
        { name: 'session.plan.md', path: 'workspace/plans/session.plan.md', previewKind: 'markdown', size: 0 },
        '# Plan',
      ),
    ).toBe(false);
    expect(
      isPendingEmptyArtifactPreview(
        { name: 'session.plan.md', path: 'workspace/plans/session.plan.md', previewKind: 'markdown', size: 12 },
        '',
      ),
    ).toBe(false);
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
    expect(markup).toContain('Implement this plan');
    expect(markup).toContain('Adjust plan');
    expect(markup).toContain('Ignore / exit plan');
    expect(markup).toContain('Tell the agent what to adjust');
    expect(markup).toContain('Reason for leaving Plan Mode');
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
              kind: 'plan_proposal',
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

    // CC-align §4.5: plan_proposal now renders InlinePlanCard (the REAL
    // plan fetched by id), NOT a synthetic card built from toolMeta — so the
    // fetched plan title shows, the stale toolMeta title does not, and the card
    // reflects live status instead of a hardcoded awaiting_confirmation.
    expect(markup).toContain('Daily Reddit investor monitoring plan');
    expect(markup).not.toContain('Inline tool plan');
    expect(markup).toContain('Implement this plan');
    expect(markup).not.toContain('Confirm before creating the trigger.');
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

  it('keeps running task todo controls out of the default chat chrome', () => {
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
          title: 'Research run',
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
            toolName: 'start_workflow',
            toolStatus: 'done',
            toolResult: 'Workflow running',
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

    expect(markup).toContain('data-testid="session-workbench"');
    expect(markup).not.toContain('data-testid="session-workbench-sidebar"');
    expect(markup).toContain('data-testid="session-workbench-inspector"');
    expect(markup).toContain('data-testid="session-native-controls"');
    expect(markup).not.toContain('data-testid="chat-work-ledger-dock"');
    expect(markup).toContain('Start goal');
    expect(markup).toContain('Create team');
  });

  it('does not mount the work ledger dock for a stale historical running tool result', () => {
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
            title: 'Old workflow run',
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
              toolName: 'start_workflow',
              toolStatus: 'done',
              toolResult: 'Workflow running',
              timestamp: '2026-05-29T07:18:00Z',
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
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not mount the persistent work ledger dock as a permanent chat-side column', () => {
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

    expect(markup).toContain('data-testid="session-workbench"');
    expect(markup).not.toContain('data-testid="session-workbench-sidebar"');
    expect(markup).toContain('data-testid="session-workbench-inspector"');
    expect(markup).toContain('data-testid="session-native-controls"');
    expect(markup).not.toContain('data-testid="chat-work-ledger-dock"');
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

    expect(markup).toContain('data-testid="active-run-cell"');
    expect(markup).toContain('Waiting for model');
    expect(markup).not.toContain('thinking-indicator');
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

  it('keeps slash commands and attachments inside the single session composer', () => {
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
          title: 'Composer contract',
          created_at: '2026-06-23T09:00:00Z',
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
        activeRunStatus={null}
        chatEndRef={React.createRef<HTMLDivElement>()}
        showScrollBtn={false}
        onScrollToBottom={vi.fn()}
        agentExpired={false}
        attachedFiles={[{ name: 'brief.pdf', text: '', path: 'workspace/brief.pdf' }]}
        onRemoveAttachedFile={vi.fn()}
        fileInputRef={React.createRef<HTMLInputElement>()}
        onHandleChatFile={vi.fn()}
        uploading={false}
        uploadProgress={-1}
        uploadAbortRef={{ current: null }}
        chatInputRef={React.createRef<HTMLTextAreaElement>()}
        chatInput="/team"
        onSetChatInput={vi.fn()}
        onHandlePaste={vi.fn()}
        onSendChatMsg={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    const composerIndex = markup.indexOf('data-testid="session-composer"');
    const slashIndex = markup.indexOf('data-testid="slash-command-menu"');
    const attachmentsIndex = markup.indexOf('data-testid="session-composer-attachments"');
    expect(composerIndex).toBeGreaterThanOrEqual(0);
    expect(slashIndex).toBeGreaterThan(composerIndex);
    expect(attachmentsIndex).toBeGreaterThan(composerIndex);
    expect(markup).toContain('brief.pdf');
    expect(markup).not.toContain('data-testid="chat-artifact-preview"');
  });

  it('renders the session composer as a Codex-style control surface with scoped actions and passive badges', () => {
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
          title: 'Composer contract',
          created_at: '2026-06-23T09:00:00Z',
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
        runtimeSummary={{
          model: {
            label: 'GPT-5.4',
            provider: 'openai',
            name: 'gpt-5.4',
            context_window_tokens: 128000,
          },
          runtime: {
            connected: true,
            estimated_input_tokens: 32000,
            remaining_tokens_estimate: 96000,
          },
          activated_tool_groups: [],
          used_tools: [],
          blocked_capabilities: [],
          compaction_count: 0,
        }}
        agentPermissions={{
          scope_type: 'company',
          scope_ids: [],
          access_level: 'manage',
          is_owner: true,
        }}
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
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    const composerIndex = markup.indexOf('data-testid="session-composer"');
    const shellIndex = markup.indexOf('data-testid="session-composer-shell"');
    const menuIndex = markup.indexOf('data-testid="session-composer-plus-menu"');
    expect(composerIndex).toBeGreaterThanOrEqual(0);
    expect(shellIndex).toBeGreaterThan(composerIndex);
    expect(menuIndex).toBeGreaterThan(shellIndex);
    expect(markup).toContain('Plan Mode');
    expect(markup).toContain('Goal mode');
    expect(markup).toContain('Scheduled task');
    expect(markup).toContain('Upload file');
    expect(markup).toContain('data-testid="session-composer-action-plan-switch"');
    expect(markup).not.toContain('data-testid="session-composer-action-goal-switch"');
    expect(markup).not.toContain('data-testid="session-composer-action-schedule-switch"');
    expect(markup).toContain('role="switch"');
    expect(markup).toContain('aria-checked="false"');
    expect(markup).toContain('Approve for me');
    expect(markup).toContain('Ask first');
    expect(markup).toContain('Full access');
    expect(markup).toContain('data-testid="session-composer-permission-mode-auto"');
    expect(markup).toContain('data-testid="session-composer-permission-mode-default"');
    expect(markup).toContain('data-testid="session-composer-permission-mode-bypassPermissions"');
    expect(markup).not.toContain('Manage access');
    expect(markup).not.toContain('acceptEdits');
    expect(markup).toContain('GPT-5.4');
    expect(markup).toContain('25% used');
    expect(markup).not.toMatch(/microphone|voice|语音/i);
  });

  it('shows Plan Mode as an active switch when the next turn is already in Plan Mode', () => {
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
          title: 'Composer contract',
          created_at: '2026-06-23T09:00:00Z',
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
        agentPermissions={{
          scope_type: 'company',
          scope_ids: [],
          access_level: 'manage',
        }}
        transportNotice={null}
        isWaiting={false}
        activeRunStatus={null}
        planModeRequested
        onTogglePlanMode={vi.fn()}
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

    expect(markup).toContain('data-testid="session-composer-action-plan-switch"');
    expect(markup).toContain('aria-checked="true"');
  });

  it('shows a hydrating state instead of stale or empty chat content during session switches', () => {
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

    expect(markup).toContain('data-testid="session-loading-state"');
    expect(markup).not.toContain('old-task-id');
    expect(markup).not.toContain('startConversation');
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

  it('renders Dynamic Workflow proposal cards with candidate preview instructions', () => {
    const markup = renderToStaticMarkup(
      <StructuredToolResultBody
        toolName="propose_dynamic_workflow"
        toolResult='{"status":"dynamic_workflow_proposed"}'
        toolMeta={{
          kind: 'dynamic_workflow_proposal',
          proposalId: 'proposal-1',
          goal: 'Audit repository slices.',
          whyWorkflow: 'Needs fanout plus critic verification.',
          successCriteria: ['Each slice cites evidence.', 'Critic passes.'],
          recommendedCandidateId: 'fanout-critic',
          nextAction: 'Call preview_workflow with the selected candidate.',
          candidates: [
            {
              candidateId: 'fanout-critic',
              name: 'Fanout then critic',
              patternMix: ['fanout_synthesize', 'adversarial_verify'],
              riskLevel: 'medium',
              plannedLeafCalls: 3,
              budgetTokens: 12000,
              confirmationRequired: false,
              definitionHash: 'hash-1',
              argsHash: 'args-1',
            },
          ],
        }}
      />,
    );

    expect(markup).toContain('Dynamic Workflow Proposal');
    expect(markup).toContain('Audit repository slices.');
    expect(markup).toContain('Fanout then critic');
    expect(markup).toContain('Recommended');
    expect(markup).toContain('Call preview_workflow with the selected candidate.');
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
    expect(markup).toContain('Implement this plan');
    expect(markup).toContain('Adjust plan');
    expect(markup).toContain('Ignore / exit plan');
    expect(markup).toContain('data-testid="plan-revision-composer"');
    expect(markup).toContain('data-testid="plan-reject-composer"');
    expect(markup).toContain('Tell the agent what to adjust');
    expect(markup).toContain('Reason for leaving Plan Mode');
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
      original_request: 'Plan a source-grounded market report',
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
      original_request: 'Plan a source-grounded market report',
      status: 'planning_failed',
      plan_version: 1,
      plan_hash: null,
      plan_markdown_path: null,
      plan_json: {
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

    expect(markup).toContain('Planning failed');
    expect(markup).toContain('missing required field: objective');
    expect(markup).toContain('Retry plan generation');
    expect(markup).toContain('Adjust and retry');
    expect(markup).toContain('Ignore / exit plan');
    expect(markup).not.toContain('Implement this plan');
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
    expect(markup).not.toContain('Implement this plan');
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
    expect(markup).not.toContain('Adjust plan');
    expect(markup).not.toContain('Implement this plan');
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
    expect(queuedMarkup).not.toContain('Implement this plan');

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
