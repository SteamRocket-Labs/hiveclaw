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
  SessionCommandControlPanel,
  buildSessionRewindCommandArgs,
  extractPlanIdFromPlanModeMessage,
  findRetryAnchorMessage,
  isClarificationCardAnsweredByLaterUserMessage,
  isInlineToolCardMessage,
  permissionOnceOnlyMessageKey,
  sessionAuthorizedInlineImageUrl,
  sessionPermissionModeOptions,
} from './AgentChatSection';
import { buildMessageFeedbackInput } from './messageFeedback';
import {
  AssistantMessageBody,
  shouldCollapseAssistantSupplement,
} from './CanonicalCardAssistantSupplement';
import {
  BranchLineagePanel,
  branchModeLabel,
  buildBranchLineageRows,
  getSessionGitLineDensity,
  pickFocusedCheckpointIdForScroll,
  sessionCheckpointPreview,
} from './SessionLineageSurface';
import {
  getArtifactOpenMode,
  isPendingEmptyArtifactPreview,
  isUserFacingDeliveryArtifact,
} from './ArtifactSurface';
import {
  ActiveTailStatusLine,
  runtimeStatusLabel,
  SessionRuntimePanel,
  subagentWorkerRecoveryModel,
  userFacingRuntimeStatus,
  WorkflowRunFocusPanel,
} from './SessionRuntimePanel';
import { SessionDecisionHistory } from './SessionDecisionHistory';
import { StructuredToolResultBody } from './StructuredToolResult';
import AgentMindSection from './AgentMindSection';
import AgentSettingsSection, {
  buildPatrolPlanRecommendationInput,
  buildPatrolPlanReviewRequest,
  patrolEnabledUpdateValue,
  patrolSaveDisposition,
} from './AgentSettingsSection';
import AgentSkillsSection, { invalidateAgentSkillQueries } from './AgentSkillsSection';
import AgentStatusSection from './AgentStatusSection';
import AgentWorkspaceSection from './AgentWorkspaceSection';
import CopyMessageButton from './CopyMessageButton';
import PlanCard, { confirmAndHandoffPlan } from './PlanCard';
import AgentA2ASection from './AgentA2ASection';
import ToolsManager, { externalActivationComponentSummary } from './ToolsManager';
import {
  AGENT_DETAIL_TABS,
  applySessionActiveProjection,
  buildAgentDetailTabNavigation,
  buildSessionWorkbenchNavigation,
  buildSessionCommandPanelNavigation,
  getAgentDetailHashTab,
  getVisibleAgentDetailTabs,
  isLocalAgentRuntimeType,
  isSessionWorkbenchRoute,
  readSessionCommandPanel,
} from '../AgentDetail';
import type { PlanRequest } from '../../api/domains/plans';
import { AGENT_WORKBENCH_AREAS } from './agentDetailPolicy';
import { buildSessionCommandStatusControl } from './sessionCommandPanelPresentation';
import zh from '../../i18n/zh.json';

async function renderWithLazyHrPreview(element: React.ReactElement): Promise<string> {
  renderToStaticMarkup(element);
  await import('./HrBlueprintPreviewCard');
  await Promise.resolve();
  return renderToStaticMarkup(element);
}

describe('buildSessionCommandStatusControl', () => {
  const t = ((_key: string, fallback: string) => fallback) as unknown as Parameters<typeof buildSessionCommandStatusControl>[0];

  it.each([
    ['open_resume_picker', true, undefined, 'resume_picker', 'Continue interrupted work'],
    ['open_resume_picker', false, undefined, 'resume_picker', 'Session is ready'],
    ['open_resume_picker', false, 'needs_reconciliation', 'resume_picker', 'Review required before continuing'],
    ['open_resume_picker', false, 'active', 'resume_picker', 'Work is already in progress'],
    ['confirm_workspace_restore', false, undefined, 'workspace_restore_confirmation', 'Restore workspace files?'],
    ['install_compacted_context', false, undefined, 'projection_status', 'Context compacted'],
    ['install_workspace_snapshot', false, undefined, 'projection_status', 'Workspace restored'],
    ['install_active_projection', false, undefined, 'projection_status', 'Rewind complete'],
  ] as const)('maps %s to its user-facing control', (action, interrupted, resumeState, type, title) => {
    expect(buildSessionCommandStatusControl(t, action, { payload: {}, interrupted, resumeState })).toMatchObject({ type, title });
  });
});

describe('runtimeStatusLabel', () => {
  it('renders through the translator so non-English locales get localized labels', () => {
    const t = ((key: string, fallback?: string) => `zh:${fallback ?? key}`) as unknown as Parameters<typeof runtimeStatusLabel>[1];
    expect(runtimeStatusLabel('idle', t)).toBe('zh:Ready');
    expect(runtimeStatusLabel('waiting_budget_approval', t)).toBe('zh:Waiting for approval');
    expect(runtimeStatusLabel('needs_reconciliation', t)).toBe('zh:Needs admin review');
  });
});

describe('userFacingRuntimeStatus', () => {
  it('maps known machine codes exactly and never falls back to raw runtime values', () => {
    expect(userFacingRuntimeStatus('waiting_budget_approval')).toBe('Waiting for approval');
    expect(userFacingRuntimeStatus('blocked')).toBe('Needs attention');
    expect(userFacingRuntimeStatus('not_admitted')).toBe('Skipped');
    expect(userFacingRuntimeStatus('ready')).toBe('Ready');
    expect(userFacingRuntimeStatus('quota_denied')).toBe('Needs attention');
    expect(userFacingRuntimeStatus('quota_unavailable')).toBe('Needs attention');
  });

  it('renders unknown states as a neutral unavailable label without substring inference', () => {
    // Model-agency boundary: an unrecognized machine state must not be
    // guessed into a semantic status (old behavior: any code containing
    // 'fail'/'done'/'stop'... was coerced, and the default masqueraded as
    // Working). Benign/unknown codes render one honest neutral label.
    expect(userFacingRuntimeStatus('provider_stream_half_closed_internal')).toBe('Status unavailable');
    expect(userFacingRuntimeStatus('narrative_with_the_word_done_inside')).toBe('Status unavailable');
    expect(userFacingRuntimeStatus('detailing_a_failure_story')).toBe('Status unavailable');
    expect(userFacingRuntimeStatus('')).toBe('Status unavailable');

    const t = ((key: string, fallback?: string) => fallback ?? key) as unknown as Parameters<typeof runtimeStatusLabel>[1];
    const unknownLabel = runtimeStatusLabel('provider_stream_half_closed_internal', t);
    expect(unknownLabel).toBe('Status unavailable');
    expect(unknownLabel).not.toContain('provider_stream');
  });
});

describe('SessionDecisionHistory', () => {
  it('shows understandable action decisions without exposing internal ids or reason codes', () => {
    const decisionId = 'decision-3a620b2c4f844d7f9b52753ab4ef9338';
    const markup = renderToStaticMarkup(
      <SessionDecisionHistory
        decisions={[
          {
            id: decisionId,
            action: 'send_feishu_message',
            tool_name: 'send_feishu_message',
            outcome: 'ask',
            reason_codes: ['charter_confirm_first', 'high_risk_axis:visibility'],
            created_at: '2026-07-24T01:00:00Z',
            feedback_count: 0,
          },
        ]}
        onFeedback={vi.fn()}
      />,
    );

    expect(markup).toContain('Action decisions');
    expect(markup).toContain('Send Feishu Message');
    expect(markup).toContain('Approval needed');
    expect(markup).toContain('Your settings require approval');
    expect(markup).toContain('Externally visible action');
    expect(markup).toContain('Helpful');
    expect(markup).toContain('Misleading');
    expect(markup).not.toContain(decisionId);
    expect(markup).not.toContain('charter_confirm_first');
    expect(markup).not.toContain('high_risk_axis:visibility');
  });

  it('has employee-facing Chinese copy for decisions instead of raw tool or policy identifiers', () => {
    expect(zh.sessionWorkbench.rightPanel.actionDecisions).toBe('操作决策');
    expect(zh.sessionWorkbench.rightPanel.decisionActions.send_feishu_message).toBe('发送飞书消息');
    expect(zh.sessionWorkbench.rightPanel.decisionOutcomes.ask).toBe('需要你批准');
    expect(zh.sessionWorkbench.rightPanel.decisionReasons.charter_confirm_first).toBe('你的设置要求先确认');
  });
});

describe('SessionRuntimePanel read-only controls', () => {
  it('keeps operator evidence visible without goal, decision, team, or retry mutations', () => {
    const markup = renderToStaticMarkup(
      <SessionRuntimePanel
        messages={[]}
        sessionWorkbench={{
          schema: 'session_workbench.v1',
          agent_id: 'agent-1',
          session: { id: 'session-operator', title: 'Operator evidence' },
          goals: [{
            id: 'goal-1',
            objective: 'Preserve the evidence trail',
            status: 'active',
            controls: { can_pause: true, can_resume: true, can_stop: true },
          }],
          runtime_sections: {
            agent_teams: [{
              id: 'team-1',
              runtime_kind: 'agent_team',
              label: 'Review Team',
              status: 'active',
              members: [{
                id: 'member-1',
                runtime_kind: 'team_member',
                label: 'Policy reviewer',
                status: 'failed',
                child_session_id: 'member-session-1',
                enterable: true,
              }],
            }],
            subagents: [{
              id: 'worker-1',
              runtime_kind: 'subagent',
              label: 'Evidence critic',
              status: 'failed',
              child_session_id: 'worker-session-1',
              enterable: true,
            }],
          },
        } as any}
        activeSession={{ id: 'session-operator', title: 'Operator evidence' }}
        agent={{ id: 'agent-1', name: 'Release Bot' }}
        agentId="agent-1"
        sessionId="session-operator"
        readOnly
        onSelectSession={vi.fn()}
        onGoalChanged={vi.fn()}
        onTeamChanged={vi.fn()}
        onRetrySubagent={vi.fn()}
        sessionDecisions={[{
          id: 'decision-1',
          action: 'send_email',
          tool_name: 'send_email',
          outcome: 'ask',
          reason_codes: ['charter_confirm_first'],
          created_at: '2026-08-31T00:00:00Z',
          feedback_count: 0,
        }]}
        onDecisionFeedback={vi.fn()}
      />,
    );

    expect(markup).toContain('Preserve the evidence trail');
    expect(markup).not.toContain('session-goal-actions');
    expect(markup).toContain('Policy reviewer');
    expect(markup).toContain('>Enter<');
    expect(markup).not.toContain('>Send<');
    expect(markup).not.toContain('>Resume<');
    expect(markup).not.toContain('>Close team<');
    expect(markup).toContain('Send an email');
    expect(markup).not.toContain('Helpful');
    expect(markup).not.toContain('Misleading');

    const workerMarkup = renderToStaticMarkup(
      <SessionRuntimePanel
        messages={[]}
        sessionWorkbench={{
          schema: 'session_workbench.v1',
          agent_id: 'agent-1',
          session: { id: 'session-operator', title: 'Operator evidence' },
          runtime_sections: {
            subagents: [{
              id: 'worker-1',
              runtime_kind: 'subagent',
              label: 'Evidence critic',
              status: 'failed',
              child_session_id: 'worker-session-1',
              enterable: true,
            }],
          },
        } as any}
        activeSession={{ id: 'session-operator', title: 'Operator evidence' }}
        agent={{ id: 'agent-1', name: 'Release Bot' }}
        readOnly
        onSelectSession={vi.fn()}
        onRetrySubagent={vi.fn()}
      />,
    );
    expect(workerMarkup).toContain('Evidence critic');
    expect(workerMarkup).toContain('data-runtime-action="subagent-worker-inspect"');
    expect(workerMarkup).not.toContain('data-runtime-action="subagent-worker-retry"');
  });
});

describe('buildMessageFeedbackInput', () => {
  it('links only durable UUID messages and never invents a decision reference', () => {
    expect(buildMessageFeedbackInput('assistant-stream-42', 'useful')).toEqual({
      label: 'useful',
    });
    expect(buildMessageFeedbackInput('11111111-1111-4111-8111-111111111111', 'misleading')).toEqual({
      label: 'misleading',
      message_id: '11111111-1111-4111-8111-111111111111',
    });
  });
});

describe('canonical card assistant supplements', () => {
  const messages = [
    { role: 'user' as const, content: 'Preview this employee.' },
    {
      role: 'tool_call' as const,
      content: '',
      toolName: 'preview_agent_blueprint',
      toolMeta: {
        kind: 'hr_preview' as const,
        blueprintId: 'draft-1',
        blueprintVersion: 1,
        blueprintHash: 'sha256:canonical',
        status: 'awaiting_confirmation',
        name: 'Release coordinator',
        mission: 'Prepare release checks.',
        firstMission: 'Prepare three checks.',
        primaryUsers: ['Owner'],
        coreOutputs: ['Checklist'],
        boundaries: 'Read-only.',
        permissionScope: 'company',
        sourceAttributions: [],
        riskClass: 'standard',
        missingGates: [],
        knowledgeDebt: [],
        confirmationRequirements: [],
        readyNow: [],
        willInstall: [],
        deferredCapabilities: [],
        warnings: [],
        manualSteps: [],
      },
    },
    {
      role: 'assistant' as const,
      content: 'blueprint_id: draft-1; permission_scope=company',
    },
  ];

  it('collapses only assistant prose in the same turn as a canonical HR preview', () => {
    expect(shouldCollapseAssistantSupplement(messages, 2)).toBe(true);
    expect(shouldCollapseAssistantSupplement([
      ...messages,
      { role: 'user', content: 'What does this employee do?' },
      { role: 'assistant', content: 'It prepares release checks.' },
    ], 4)).toBe(false);
  });

  it('collapses an earlier assistant preamble once the same turn receives a canonical HR preview', () => {
    const liveMessages = [
      messages[0],
      { role: 'assistant' as const, content: 'I will call preview_agent_blueprint now.' },
      messages[1],
      messages[2],
    ];

    expect(shouldCollapseAssistantSupplement(liveMessages, 1)).toBe(true);
    expect(shouldCollapseAssistantSupplement(liveMessages, 3)).toBe(true);
  });

  it('keeps the exact model-authored bytes in a closed supplemental disclosure', () => {
    const content = 'blueprint_id: draft-1; permission_scope=company';
    const markup = renderToStaticMarkup(
      <AssistantMessageBody
        content={content}
        streaming={false}
        supplemental
        supplementalLabel="Agent supplemental notes"
      />,
    );

    expect(markup).toContain('data-testid="assistant-canonical-card-supplement"');
    expect(markup).toContain('<summary>Agent supplemental notes</summary>');
    expect(markup).not.toContain('<details open=""');
    expect(markup).toContain('blueprint_id: draft-1; permission_scope=company');
  });
});

const queryKeyCalls = vi.hoisted(() => [] as unknown[][]);
const queryOptionCalls = vi.hoisted(() => [] as Array<{ queryKey: unknown[]; enabled?: boolean }>);

function findElementByTestId(node: React.ReactNode, testId: string): React.ReactElement<Record<string, any>> {
  if (!React.isValidElement(node)) {
    throw new Error(`Unable to find ${testId}`);
  }
  const element = node as React.ReactElement<Record<string, any>>;
  if (element.props?.['data-testid'] === testId) {
    return element;
  }
  const children = React.Children.toArray(element.props?.children);
  for (const child of children) {
    try {
      return findElementByTestId(child, testId);
    } catch {
      continue;
    }
  }
  throw new Error(`Unable to find ${testId}`);
}

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
    queryOptionCalls.push({ queryKey, enabled });
    if (enabled === false) {
      return { data: undefined, isLoading: false, isError: false, error: null };
    }
    const key = String(queryKey[0]);
    if (key === 'workflow-preview') {
      return { data: { preview_status: 'ready' }, refetch: vi.fn(), isLoading: false, isError: false };
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
            action_type: 'workspace.command.escalation',
            status: 'pending',
            created_at: '2026-03-27T09:00:00Z',
            details: {
              reason: 'Release the verified frontend build.',
              command: 'railway up --service frontend',
              requested_by: 'user-internal-123',
              args: { command: 'railway up --service frontend' },
              execution_envelope: { bearer_token: 'secret-token' },
            },
          },
          {
            id: 'approval-2',
            action_type: 'local_agent.execute',
            status: 'approved',
            tool_name: 'run_command',
            execution_status: 'succeeded',
            execution_receipt: { continuation_status: 'queued' },
            details: {
              reason: 'Send the approved brief to the connected local employee.',
              local_agent_message_id: 'message-internal-456',
            },
            resolved_at: '2026-03-27T09:30:00Z',
          },
        ],
        refetch: vi.fn(),
      };
    }
    if (key === 'agent-soul') {
      return {
        data: {
          path: 'soul.md',
          content: 'schema: hive.soul.v2\n\nOwn the verified outcome.',
        },
        isLoading: false,
        isError: false,
        refetch: vi.fn(),
      };
    }
    if (key === 'agent-permissions') {
      return {
        data: {
          scope_type: 'user',
          scope_ids: [],
          access_level: 'manage',
          is_owner: true,
        },
        isLoading: false,
        isError: false,
        refetch: vi.fn(),
      };
    }
    if (key === 'owner-action-policy') {
      return {
        data: {
          schema: 'hive.owner_action_policy.v1',
          actions: {
            'tool.external_effect': 'confirm_first',
            'tool.local_read': 'full_authority',
            'tool.local_write': 'full_authority',
          },
          version: 1,
          revision_id: 'policy-revision-1',
          content_hash: 'policy-hash-1',
          source: 'migration',
          valid: true,
          error_code: null,
          can_manage: true,
        },
        isLoading: false,
        error: null,
      };
    }
    if (key === 'owner-action-policy-history') {
      return {
        data: {
          items: [
            {
              version: 1,
              is_active: true,
              change_source: 'migration',
              created_at: '2026-07-24T00:00:00Z',
            },
          ],
        },
        isLoading: false,
        error: null,
      };
    }
    if (key === 'agent-runtime-health') {
      if (String(queryKey[1]) === 'agent-active-budget-envelope') {
        return {
          data: {
            schema: 'hive.agent.runtime_health.v1',
            agent_id: 'agent-active-budget-envelope',
            status: 'healthy',
            interrupted_turns: 0,
            observed_issues: 0,
            retry_available: false,
            last_issue_at: null,
          },
          isLoading: false,
          isError: false,
          error: null,
        };
      }
      return {
        data: {
          schema: 'hive.agent.runtime_health.v1',
          agent_id: 'agent-1',
          status: 'needs_attention',
          interrupted_turns: 1,
          observed_issues: 2,
          retry_available: true,
          last_issue_at: '2026-07-24T04:00:00Z',
        },
        isLoading: false,
        isError: false,
        error: null,
      };
    }
    if (key === 'agent-runtime-budget-runs' && String(queryKey[1]) === 'agent-active-budget-envelope') {
      return {
        data: [
          {
            id: 'budget-run-active-after-root-completed',
            status: 'active',
            user_status: 'Running',
            user_reason: 'System safeguard intervened',
            user_next_action: 'Wait for the current run',
          },
        ],
        isLoading: false,
        isError: false,
        error: null,
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
    if (key === 'agent-extensions') {
      return {
        data: {
          skills: [
            { id: 'skill-internal-1', name: 'web-research', source: 'internal', status: 'installed' },
            { id: 'skill-agent-1', name: 'market-research', source: 'agent', status: 'installed' },
            { id: 'skill-url-1', name: 'github-briefing', source: 'url', status: 'installed' },
          ],
          mcp_servers: [
            {
              id: 'mcp-1',
              name: 'filesystem-mcp',
              status: 'connected',
              enabled: true,
              tool_count: 4,
              default_tool_mode: 'approval',
              always_load: false,
            },
          ],
          plugins: [
            {
              id: 'plugin-1',
              plugin_key: 'paperclip',
              version: '1.0.0',
              status: 'installed',
              source_kind: 'plugin',
              enabled: true,
            },
          ],
        },
        isLoading: false,
        isError: false,
        error: null,
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
    if (key === 'chat-session-index') {
      if (!queryKey.includes('runtime-panel-session')) {
        return { data: undefined, isLoading: false, isError: false, error: null };
      }
      return {
        data: {
          schema: 'session_index.v1',
          thread_id: 'runtime-panel-session',
          session_id: 'runtime-panel-session',
          agent_id: 'agent-1',
          dynamic_tools: [],
          checkpoints: [
            {
              checkpoint_event_id: 'checkpoint-user-1',
              sequence: 1,
              role: 'user',
              content: 'Create a runtime report.',
            },
            {
              checkpoint_event_id: 'checkpoint-assistant-2',
              sequence: 2,
              role: 'assistant',
              content: 'Generated a report.',
            },
          ],
          event_count: 2,
          t0_segments: [],
          resume_health: { status: 'ok' },
        },
        isLoading: false,
        isError: false,
        error: null,
      };
    }
    if (key === 'chat-session-workbench') {
      if (queryKey.includes('session-subagent-worker')) {
        return {
          data: {
            schema: 'session_workbench.v1',
            agent_id: 'agent-1',
            session: { id: 'session-subagent-worker', title: 'Sub-agent worker session' },
            runtime_sections: {
              subagents: [
                {
                  id: 'subagent-worker-1',
                  runtime_kind: 'subagent',
                  label: 'One-shot critic',
                  status: 'completed',
                  child_session_id: 'child-subagent-session',
                  enterable: false,
                },
              ],
            },
          },
          isLoading: false,
          isError: false,
          error: null,
        };
      }
      if (!queryKey.includes('runtime-panel-session')) {
        return { data: undefined, isLoading: false, isError: false, error: null };
      }
      return {
        data: {
          schema: 'session_workbench.v1',
          agent_id: 'agent-1',
          session: { id: 'runtime-panel-session', title: 'Runtime panel session' },
          turn: {
            truth_source: 't0_events_jsonl',
            event_count: 14,
            checkpoint_count: 2,
          },
          controls: {},
          tool_calls: [
            { id: 'tool-1', tool_name: 'web_search', status: 'completed' },
            { id: 'tool-2', tool_name: 'spawn_subagent', status: 'running' },
          ],
          approvals: [
            { id: 'approval-1', tool_name: 'web_fetch', status: 'pending' },
          ],
          hooks: [
            { event: 'pre_tool_use', status: 'enabled' },
          ],
          goals: [
            { id: 'goal-1', objective: 'Validate runtime panel', status: 'active' },
          ],
          runtime_tasks: [
            {
              id: 'workflow-run-1',
              task_type: 'workflow',
              title: 'ccplus-closure-audit',
              status: 'running',
              progress: { completed: 21, total: 24 },
            },
            {
              id: 'background-run-1',
              task_type: 'background_command',
              title: 'backend verification',
              status: 'completed',
            },
          ],
          active_run: {
            id: 'workflow-run-1',
            task_type: 'workflow',
            status: 'running',
          },
          teams: [
            {
              id: 'team-1',
              name: 'Research Team',
              status: 'running',
              transcript_truth: 'team_member_chat_session',
              lead_agent_id: 'agent-1',
              parent_session_id: 'runtime-panel-session',
              member_count: 1,
              members: [
                {
                  id: 'member-1',
                  member_name: 'Reviewer',
                  member_role: 'audit',
                  chat_session_id: 'member-session-1',
                  runtime_task_id: 'member-task-1',
                  runtime_task_type: 'web_chat_turn',
                  status: 'awaiting_approval',
                  summary: 'Checking runtime panel evidence.',
                },
              ],
            },
          ],
          completion_wake_summary: {
            pending: 1,
            completed: 0,
          },
          completion_wakes: [
            { id: 'wake-1', status: 'pending', reason: 'notify user when run completes' },
          ],
          runtime_sections: {
            agent_teams: [
              {
                id: 'team-1',
                runtime_kind: 'agent_team',
                label: 'Research Team',
                status: 'running',
                chat_session_id: 'team-session-1',
                enterable: true,
                members: [
                  {
                    id: 'member-1',
                    runtime_kind: 'team_member',
                    label: 'Reviewer',
                    elapsed_seconds: 95,
                    total_tokens: 3600,
                    tool_use_count: 4,
                    child_session_id: 'member-session-1',
                    enterable: true,
                    summary: 'Checking runtime panel evidence.',
                    status: 'awaiting_approval',
                  },
                ],
              },
            ],
            subagents: [
              {
                id: 'subagent-1',
                runtime_kind: 'subagent',
                label: 'One-shot critic',
                status: 'awaiting_user_clarification',
                child_session_id: 'subagent-session-1',
                enterable: true,
              },
            ],
            peer_a2a: {
              schema: 'hive.ccplus.runtime_section.v1',
              key: 'peer_a2a',
              count: 1,
              items: [
                {
                  id: 'a2a-task-1',
                  runtime_kind: 'peer_a2a',
                  label: 'Finance digital employee',
                  status: 'blocked',
                  child_session_id: 'a2a-session-1',
                  enterable: true,
                  summary: 'The target model provider rejected the request.',
                },
              ],
            },
            workflows: [
              {
                id: 'workflow-run-1',
                runtime_kind: 'workflow',
                label: 'ccplus-closure-audit',
                status: 'running',
                elapsed_seconds: 125,
                token_count: 4200,
                tool_count: 3,
                steps: [{ id: 'workflow-step-1', label: 'Review plan', status: 'gate_waiting' }],
                leaf_calls: [{ id: 'workflow-leaf-1', label: 'Leaf check', status: 'completed', enterable: false }],
              },
            ],
            background: [
              {
                id: 'background-run-1',
                runtime_kind: 'background_agent',
                label: 'backend verification',
                status: 'completed',
              },
            ],
            notifications: [
              {
                id: 'wake-1',
                runtime_kind: 'notification',
                label: 'notify user when run completes',
                status: 'pending',
              },
            ],
            runs: [
              {
                id: 'run-1',
                runtime_kind: 'runtime_task',
                label: 'web chat turn',
                status: 'running',
                elapsed_seconds: 25,
                token_count: 900,
                tool_use_count: 1,
              },
            ],
            raw: [
              {
                id: 'raw-1',
                runtime_kind: 'raw_event',
                label: 'runtime_action_completed',
                status: 'completed',
              },
            ],
          },
          context_window: {
            schema: 'context_window.v1',
            decision_count: 1,
            latest_status: { status: 'ok', utilization_pct: 62 },
            decisions: [{ id: 'context-1', status: 'ok' }],
          },
        },
        isLoading: false,
        isError: false,
        error: null,
      };
    }
    return { data: [] };
  },
  useMutation: () => ({
    mutate: vi.fn(),
    isPending: false,
    isError: false,
    error: null,
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
  it('adds the normalized operator authority to inline image requests and leaves owner URLs unchanged', () => {
    const ownerUrl = '/api/agents/agent-1/files/download?path=workspace/uploads/evidence.png';

    expect(sessionAuthorizedInlineImageUrl(ownerUrl, false, 'ignored')).toBe(ownerUrl);
    expect(sessionAuthorizedInlineImageUrl(ownerUrl, true, '  Incident evidence review  ')).toBe(
      '/api/agents/agent-1/files/download?path=workspace%2Fuploads%2Fevidence.png&operator_view=true&operator_reason=Incident+evidence+review',
    );
    expect(sessionAuthorizedInlineImageUrl(ownerUrl, true, '   ')).toBeUndefined();
  });

  it('hides the previous Session transcript and composer while a new conversation takes authority', () => {
    const oldMarker = 'OLD-SESSION-MARKER-MUST-NOT-LEAK';
    const oldModel = 'OLD-RUNTIME-MODEL-MUST-NOT-LEAK';
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
        activeSession={{ id: 'session-old', user_id: 'user-1', title: 'Old session' }}
        sessionTransitionPending
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
        chatMessages={[{ id: 'old-answer', role: 'assistant', content: oldMarker }]}
        chatMessagesSessionId="session-old"
        runtimeSummary={{
          model: { label: oldModel },
          activated_tool_groups: [],
          used_tools: [],
          blocked_capabilities: [],
          compaction_count: 0,
        }}
        transportNotice={null}
        isWaiting
        runtimePhase="responding"
        activeRunStatus="running"
        activeRunId="old-run"
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
        isStreaming
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('Starting a new conversation');
    expect(markup).not.toContain(oldMarker);
    expect(markup).not.toContain(oldModel);
    expect(markup).not.toContain('Old session');
    expect(markup).not.toContain('data-testid="session-composer"');
    expect(markup).not.toContain('data-testid="session-runtime-panel"');
    expect(markup).toContain('role="status"');
  });

  it('distinguishes destructive approval copy from ordinary one-shot scope', () => {
    expect(permissionOnceOnlyMessageKey({
      permission_request_id: 'controlled-write',
      risk_class: 'controlled_write',
      allow_session_allowed: false,
    })).toBe('agent.chat.permission.onceOnly');
    expect(permissionOnceOnlyMessageKey({
      permission_request_id: 'destructive-delete',
      risk_class: 'destructive_delete',
      allow_session_allowed: false,
    })).toBe('agent.chat.permission.deleteOnceOnly');
  });

  it('anchors a retryable runtime error to the nearest preceding user turn', () => {
    const messages = [
      {
        id: 'user-1',
        transcriptEventId: 'event-user-1',
        role: 'user' as const,
        content: 'Run the analysis.',
      },
      { id: 'agent-1', role: 'assistant' as const, content: 'Starting.' },
      {
        id: 'user-2',
        transcriptEventId: 'event-user-2',
        role: 'user' as const,
        content: 'Use the latest file.',
      },
      { id: 'error-1', role: 'event' as const, content: 'Provider timed out.' },
    ];

    expect(findRetryAnchorMessage(messages, 3)).toBe(messages[2]);
    expect(findRetryAnchorMessage([
      messages[0],
      messages[1],
      { ...messages[2], transcriptEventId: null },
      messages[3],
    ], 3)).toBeNull();
    expect(findRetryAnchorMessage(messages, 0)).toBeNull();
  });
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
    // §8.4: session navigation now lands on the canonical session route,
    // never on the query-string disguise.
    expect(buildSessionWorkbenchNavigation('/agents/agent-1', '?manage=true&session_id=session-1', 'session-2')).toEqual({
      pathname: '/agents/agent-1/sessions/session-2',
      search: '',
      hash: '',
    });
    expect(buildSessionCommandPanelNavigation('/agents/agent-1/sessions/session-2', '?keep=yes', 'context')).toEqual({
      pathname: '/agents/agent-1/sessions/session-2',
      search: '?keep=yes&command_panel=context',
      hash: '',
    });
    expect(readSessionCommandPanel('?command_panel=usage')).toBe('usage');
    expect(readSessionCommandPanel('?command_panel=unknown')).toBeNull();
    expect(getAgentDetailHashTab('#mind', AGENT_DETAIL_TABS)).toBe('knowledge');
    expect(getAgentDetailHashTab('#tools', AGENT_DETAIL_TABS)).toBe('extensions');
    expect(getAgentDetailHashTab('#skills', AGENT_DETAIL_TABS)).toBe('extensions');
    expect(getAgentDetailHashTab('#subagents', AGENT_DETAIL_TABS)).toBe('extensions');
    expect(getAgentDetailHashTab('#unknown', AGENT_DETAIL_TABS)).toBeNull();
  });

  it('applies active rewind projection before the selected user checkpoint and returns that prompt as draft', () => {
    const result = applySessionActiveProjection(
      {
        transcript_metadata_json: {
          active_projection: {
            projection_reason: 'rewind',
            checkpoint_event_id: 'evt-user-2',
            draft_content: 'Selected prompt returns to composer.',
          },
        },
      },
      [
        { id: 'msg-user-1', transcriptEventId: 'evt-user-1', role: 'user', content: 'Earlier prompt.' },
        { id: 'msg-assistant-1', transcriptEventId: 'evt-assistant-1', role: 'assistant', content: 'Earlier answer.' },
        { id: 'msg-user-2', transcriptEventId: 'evt-user-2', role: 'user', content: 'Selected prompt returns to composer.' },
        { id: 'msg-assistant-2', transcriptEventId: 'evt-assistant-2', role: 'assistant', content: 'Tail answer.' },
      ],
    );

    expect(result.messages.map((message) => message.transcriptEventId)).toEqual(['evt-user-1', 'evt-assistant-1']);
    expect(result.draftContent).toBe('Selected prompt returns to composer.');
    expect(result.checkpointEventId).toBe('evt-user-2');
    expect(result.shouldScrollToProjectionTail).toBe(true);
  });

  it('restores the active rewind projection from the reload-safe Session read model', () => {
    const result = applySessionActiveProjection(
      {
        active_projection: {
          projection_reason: 'rewind',
          checkpoint_event_id: 'evt-user-2',
          draft_content: 'Retry the second request.',
        },
      },
      [
        { id: 'msg-user-1', transcriptEventId: 'evt-user-1', role: 'user', content: 'Earlier prompt.' },
        { id: 'msg-assistant-1', transcriptEventId: 'evt-assistant-1', role: 'assistant', content: 'Earlier answer.' },
        { id: 'msg-user-2', transcriptEventId: 'evt-user-2', role: 'user', content: 'Retry the second request.' },
        { id: 'msg-assistant-2', transcriptEventId: 'evt-assistant-2', role: 'assistant', content: 'Stale tail.' },
      ],
    );

    expect(result.messages.map((message) => message.transcriptEventId)).toEqual(['evt-user-1', 'evt-assistant-1']);
    expect(result.draftContent).toBe('Retry the second request.');
    expect(result.checkpointEventId).toBe('evt-user-2');
  });

  it('keeps operator inspection reason-scoped and removes every writable session control', () => {
    const renderOperatorAudit = (reason: string) => renderToStaticMarkup(
      <AgentChatSection
        agentId="agent-1"
        agent={{ id: 'agent-1', name: 'Release Bot' }}
        currentUser={{ id: 'user-1' }}
        isAdmin
        operatorReason={reason}
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
          id: 'runtime-panel-session',
          user_id: 'user-2',
          title: 'Customer IM thread',
          source_channel: 'feishu',
          username: 'Customer',
          operator_view: true,
          root_session_id: 'operator-root-session',
          created_at: '2026-03-27T10:00:00Z',
        }}
        branchLineage={[
          { id: 'operator-root-session', parent_session_id: null, title: 'Audit root', branch: {} },
          {
            id: 'runtime-panel-session',
            parent_session_id: 'operator-root-session',
            root_session_id: 'operator-root-session',
            title: 'Customer IM thread',
            branch: { root_session_id: 'operator-root-session' },
          },
        ]}
        wsConnected={false}
        allSessions={[
          {
            id: 'runtime-panel-session',
            user_id: 'user-2',
            title: 'Customer IM thread',
            source_channel: 'feishu',
            username: 'Customer',
            operator_view: true,
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
        historyMsgs={[
          {
            id: 'operator-user-message',
            transcriptEventId: 'operator-user-event',
            role: 'user',
            content: 'Review the incident evidence.',
          },
          {
            id: 'operator-assistant-message',
            transcriptEventId: 'operator-assistant-event',
            role: 'assistant',
            content: 'Evidence ready. plan_id=a7cdfa75-cec5-4062-8bda-b18b2d2821a3',
          },
          {
            id: 'operator-permission-event',
            role: 'event',
            content: "Tool 'send_email' requires session permission",
            eventType: 'permission',
            eventStatus: 'session_permission_required',
            sessionPermissionRequest: {
              permission_request_id: '11111111-1111-4111-8111-111111111111',
              session_id: 'runtime-panel-session',
              tool_name: 'send_email',
              arguments: { to: 'a@example.com' },
              permission_mode: 'default',
            },
          },
          {
            id: 'operator-retryable-event',
            role: 'event',
            content: 'The service was temporarily unavailable.',
            eventType: 'runtime_error',
            eventStatus: 'failed',
            threadItem: {
              id: 'operator-retryable-thread-item',
              item_type: 'error',
              item_status: 'failed',
              item_data: { retryable: true },
              audience: 'operator',
              user_summary: 'This turn can be retried safely.',
            } as any,
          },
        ]}
        historyMessagesSessionId="runtime-panel-session"
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
        onBranchMessage={vi.fn()}
        onSendMessage={vi.fn()}
        onEnterPlanMode={vi.fn()}
        onRunSessionCommand={vi.fn()}
        onResolveSessionPermission={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />
    );

    queryKeyCalls.length = 0;
    queryOptionCalls.length = 0;
    const markup = renderOperatorAudit('  Incident evidence review  ');

    expect(markup).toContain('data-testid="detail-session-browser"');
    expect(markup).toContain('All Users');
    expect(markup).not.toContain('My Conversations');
    expect(markup).not.toContain('New Conversation');
    expect(markup).not.toContain('My launch sync');
    expect(markup).toContain('Customer IM thread');
    expect(markup).toContain('class="detail-session-row active"');
    expect(markup).toContain('data-testid="session-operator-view"');
    expect(markup).toContain('Operator View');
    expect(markup).not.toContain('aria-label="Delete session Customer IM thread"');
    expect(markup).not.toContain('session-only');
    expect(markup).not.toContain('data-testid="session-tui-composer"');
    expect(markup).not.toContain('data-testid="message-action-like"');
    expect(markup).not.toContain('data-testid="message-action-dislike"');
    expect(markup).not.toContain('data-testid="message-action-branch"');
    expect(markup).not.toContain('data-testid="message-action-rewind"');
    expect(markup).not.toContain('data-testid="thread-item-retry-turn"');
    expect(markup).not.toContain('chat-inline-plan-card');
    expect(markup).not.toContain('Allow once');
    expect(markup).not.toContain('Allow for this session');
    expect(markup).not.toContain('>Deny<');
    expect(markup).toContain('Validate runtime panel');
    expect(markup).not.toContain('session-goal-actions');
    expect(markup).not.toContain('>Send<');
    expect(markup).not.toContain('>Resume<');
    expect(markup).not.toContain('>Close team<');
    expect(markup).not.toContain('data-runtime-action="subagent-worker-retry"');

    expect(queryKeyCalls).toContainEqual([
      'chat-session-index', 'agent-1', 'runtime-panel-session', 'operator', 'Incident evidence review',
    ]);
    expect(queryKeyCalls).toContainEqual([
      'chat-session-decisions', 'agent-1', 'runtime-panel-session', 'operator', 'Incident evidence review',
    ]);
    expect(queryKeyCalls).toContainEqual([
      'chat-session-context-usage', 'agent-1', 'runtime-panel-session', 'operator', 'Incident evidence review',
    ]);
    expect(queryKeyCalls).toContainEqual([
      'chat-session-index', 'agent-1', 'operator-root-session', 'gitline-axis', 'operator', 'Incident evidence review',
    ]);
    expect(queryKeyCalls).toContainEqual([
      'chat-session-workbench', 'agent-1', 'operator-root-session', 'gitline-axis', 'operator', 'Incident evidence review',
    ]);

    queryOptionCalls.length = 0;
    renderOperatorAudit('   ');
    const disabledOperatorReads = queryOptionCalls.filter(({ queryKey }) => (
      ['chat-session-workbench', 'chat-session-index', 'chat-session-decisions', 'chat-session-context-usage'].includes(String(queryKey[0]))
      && queryKey.includes('operator')
    ));
    expect(disabledOperatorReads).toHaveLength(6);
    expect(disabledOperatorReads.every(({ enabled }) => enabled === false)).toBe(true);
    expect(disabledOperatorReads.every(({ queryKey }) => queryKey.at(-1) === '')).toBe(true);
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

  it('summarizes external activation component types for the unified extensions view', () => {
    expect(externalActivationComponentSummary({ skill: 1, mcp_server: 2, hook: 0 })).toBe(
      'skill 1 · mcp_server 2',
    );
    expect(externalActivationComponentSummary({})).toBe('');
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

  it('does not render branch or rewind actions on user prompts', () => {
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
        chatMessages={[{ id: 'msg-user-1', transcriptEventId: 'evt-user-1', role: 'user', content: 'Use the Railway logs.' }]}
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
        onRunSessionCommand={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).not.toContain('data-testid="message-action-like"');
    expect(markup).not.toContain('data-testid="message-action-dislike"');
    expect(markup).not.toContain('data-testid="message-action-branch"');
    expect(markup).not.toContain('data-testid="message-action-rewind"');
  });

  it('renders transcript-anchored conversation branch actions on assistant replies', () => {
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
        chatMessages={[
          { id: 'msg-user-1', transcriptEventId: 'evt-user-1', role: 'user', content: 'Use the Railway logs.' },
          { id: 'msg-assistant-1', transcriptEventId: 'evt-assistant-1', role: 'assistant', content: 'I checked them.' },
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
        onBranchMessage={vi.fn()}
        onRunSessionCommand={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('data-testid="message-action-like"');
    expect(markup).toContain('data-testid="message-action-dislike"');
    expect(markup).toContain('data-testid="message-action-branch"');
    expect(markup).toContain('data-testid="message-action-rewind"');
    expect(markup).not.toContain('data-testid="message-action-fork"');
    expect(markup).not.toContain('data-testid="message-action-edit"');
    expect(markup).not.toContain('data-testid="message-action-insert-before"');
    expect(markup).not.toContain('data-testid="message-action-insert-after"');
    expect(markup).not.toContain('data-testid="message-action-reply"');
    expect(markup).not.toContain('data-testid="message-action-regenerate"');
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
    expect(markup).toContain('data-testid="run-disclosure-block"');
    expect(markup).toContain('Processed');
    expect(markup).toContain('Checkpoint trail updated.');
    expect(markup).not.toContain('Thinking');
    expect(markup).not.toContain('I checked the current branch state.');
    expect(markup).toContain('aria-expanded="false"');
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
          { id: 'edit', parent_session_id: 'root', title: 'Original (edit) (edit)', branch: { branch_mode: 'edit' } },
        ]}
        onSelectSession={vi.fn()}
      />,
    );

    expect(markup).toContain('data-testid="branch-lineage-panel"');
    expect(markup).toContain('Original');
    expect(markup).not.toContain('(edit)');
    expect(markup).toContain('edit');
    expect(branchModeLabel({
      id: 'legacy-rewind',
      branch: { branch_mode: 'rewind' },
    })).toBe('legacy rewind branch');
  });

  it('selects the live checkpoint from the current session scroll position', () => {
    const anchors = [
      { id: 'checkpoint-1', top: 120 },
      { id: 'checkpoint-2', top: 520 },
      { id: 'checkpoint-3', top: 980 },
    ];

    expect(pickFocusedCheckpointIdForScroll(anchors, 40)).toBe('checkpoint-1');
    expect(pickFocusedCheckpointIdForScroll(anchors, 360)).toBe('checkpoint-1');
    expect(pickFocusedCheckpointIdForScroll(anchors, 660)).toBe('checkpoint-2');
    expect(pickFocusedCheckpointIdForScroll(anchors, 1200)).toBe('checkpoint-3');
    expect(pickFocusedCheckpointIdForScroll([], 660)).toBeNull();
  });

  it('classifies session GitLine density for centered and scrollable checkpoint rails', () => {
    expect(getSessionGitLineDensity(0)).toBe('empty');
    expect(getSessionGitLineDensity(4)).toBe('sparse');
    expect(getSessionGitLineDensity(18)).toBe('regular');
    expect(getSessionGitLineDensity(50)).toBe('scrollable');
  });

  it('uses prompt intent text for session GitLine checkpoint previews', () => {
    const preview = sessionCheckpointPreview({
      checkpoint_event_id: '1782721813033911000.11602f20-12fb-4bba-8604-c8efca5d90dc',
      sequence: 12,
      content_preview: '我希望创建一个专注于 AI 硬件的产品分析师，持续跟踪 GPU/NPU/TPU 芯片发布。',
    }, 0);

    expect(preview).toBe('我希望创建一个专注于 AI 硬件的产品...');
    expect(preview.length).toBeLessThanOrEqual(28);
    expect(preview).not.toContain('1782721813033911000');
    expect(preview).not.toContain('持续跟踪 GPU/NPU/TPU 芯片发布');

    expect(sessionCheckpointPreview({
      checkpoint_event_id: '1782721813033911000.11602f20-12fb-4bba-8604-c8efca5d90dc',
    }, 2)).toBe('Checkpoint 3');
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
    expect(markup).toContain('data-session-action="focus-checkpoint"');
    expect(markup).toContain('data-testid="session-checkpoint-rewind-action"');
    expect(markup).toContain('data-testid="session-checkpoint-branch-action"');
    expect(markup).toContain('data-testid="session-rewind-mode-conversation"');
    expect(markup).toContain('data-testid="session-rewind-mode-workspace"');
    expect(markup).toContain('data-testid="session-rewind-mode-both"');
    expect(markup).toContain('Rewind this session here');
    expect(markup).toContain('Branch into new session');
    expect(markup).toContain('第一次输入');
    expect(markup).toContain('第二次输入');
  });

  it('builds explicit workspace rewind command arguments from the checkpoint selector', () => {
    expect(buildSessionRewindCommandArgs('evt-1', 'conversation')).toEqual({
      checkpoint_event_id: 'evt-1',
      mode: 'conversation',
    });
    expect(buildSessionRewindCommandArgs('evt-1', 'workspace')).toEqual({
      checkpoint_event_id: 'evt-1',
      mode: 'workspace',
    });
    expect(buildSessionRewindCommandArgs('evt-1', 'both', true)).toEqual({
      checkpoint_event_id: 'evt-1',
      mode: 'both',
      confirm_workspace_restore: true,
    });
    expect(buildSessionRewindCommandArgs('evt-1', 'conversation', false, 12)).toEqual({
      checkpoint_event_id: 'evt-1',
      mode: 'conversation',
      expected_last_sequence: 12,
    });
  });

  it('disables Rewind but keeps Branch available while a turn is active', () => {
    const markup = renderToStaticMarkup(
      <SessionCommandControlPanel
        control={{
          type: 'checkpoint_selector',
          title: 'Select checkpoint',
          checkpoints: [{ checkpoint_event_id: 'evt-1', sequence: 1, content: 'First turn' }],
          payload: { rewind_guard: { last_sequence: 12 } },
        }}
        rewindUnavailableReason="Stop the current turn before rewinding."
        onDismiss={vi.fn()}
        onRunCommand={vi.fn()}
      />,
    );

    expect(markup).toMatch(/data-testid="session-checkpoint-rewind-action"[^>]*disabled=""/);
    expect(markup).toMatch(/data-testid="session-checkpoint-branch-action"(?![^>]*disabled="")[^>]*>/);
    expect(markup).toContain('Stop the current turn before rewinding.');
  });

  it('renders workspace rewind confirmation as an explicit command panel action', () => {
    const markup = renderToStaticMarkup(
      <SessionCommandControlPanel
        control={{
          type: 'workspace_restore_confirmation' as any,
          title: 'Confirm workspace rewind',
          message: 'Workspace rewind will restore files from the selected checkpoint.',
          command: 'rewind',
          payload: {
            action: 'workspace_restore_requires_confirmation',
            checkpoint: { checkpoint_event_id: 'evt-1', content: 'Restore this point' },
            debug_payload: { requested_mode: 'both', checkpoint_event_id: 'evt-1' },
          },
        }}
        onDismiss={vi.fn()}
        onRunCommand={vi.fn()}
      />,
    );

    expect(markup).toContain('data-testid="session-workspace-restore-confirm-action"');
    expect(markup).toContain('data-session-command="rewind"');
    expect(markup).toContain('data-rewind-mode="both"');
    expect(markup).toContain('Confirm restore');
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

  it('renders context, usage, and permission commands as novice-readable panels without internal ids', () => {
    const context = renderToStaticMarkup(
      <SessionCommandControlPanel
        control={{
          type: 'context_panel',
          title: 'Session context',
          payload: { session_id: 'session-private-id', agent_id: 'agent-private-id' },
        }}
        contextUsage={{
          schema: 'hive.ccplus.session_context_usage.v1',
          session_id: 'session-private-id',
          agent_id: 'agent-private-id',
          model_window_tokens: 128000,
          used_tokens: 32000,
          free_space_tokens: 96000,
          counts: { selected_contexts: 4, suppressed_contexts: 1 },
          loaded_skills: ['research'],
          active_tool_names: ['read_file', 'write_file'],
        }}
        onDismiss={vi.fn()}
        onRunCommand={vi.fn()}
      />,
    );
    expect(context).toContain('32,000 / 128,000 tokens');
    expect(context).toContain('4 selected');
    expect(context).toContain('1 restricted or unavailable');
    expect(context).not.toContain('session-private-id');
    expect(context).not.toContain('agent-private-id');

    const usage = renderToStaticMarkup(
      <SessionCommandControlPanel
        control={{
          type: 'usage_panel',
          title: 'Session usage',
          payload: {
            session_id: 'session-private-id',
            usage: { input_tokens: 700, output_tokens: 500, total_tokens: 1200 },
            cost: { cost_usd: 0.01 },
          },
        }}
        agentUsage={{ usedToday: 4000, limitToday: 10000, usedMonth: 12000, limitMonth: 50000 }}
        onDismiss={vi.fn()}
        onRunCommand={vi.fn()}
      />,
    );
    expect(usage).toContain('1,200 tokens');
    expect(usage).toContain('$0.01');
    expect(usage).toContain('4,000 / 10,000 tokens');
    expect(usage).not.toContain('session-private-id');

    const permissions = renderToStaticMarkup(
      <SessionCommandControlPanel
        control={{
          type: 'permissions_panel',
          title: 'Session permissions',
          payload: { user_id: 'user-private-id' },
        }}
        agentPermissions={{ scope_type: 'user', access_level: 'manage', is_owner: true }}
        sessionPermissionMode="bypassPermissions"
        onDismiss={vi.fn()}
        onRunCommand={vi.fn()}
      />,
    );
    expect(permissions).toContain('Manage');
    expect(permissions).toContain('Full access');
    expect(permissions).toContain('Enterprise policies still apply');
    expect(permissions).not.toContain('user-private-id');
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
    expect(markup).not.toContain('workspace_rewind_applied');
    expect(markup).not.toContain('restored_count');
  });

  it('renders interrupted Resume as a user action without internal command fields', () => {
    const markup = renderToStaticMarkup(
      <SessionCommandControlPanel
        control={{
          type: 'resume_picker',
          title: 'Continue interrupted work',
          message: 'The previous turn stopped before completing. Continue from the last saved checkpoint.',
          command: 'resume',
          payload: {
            interrupted: true,
            repair_strategy: 'transcript_replay_chain_repair',
            session_id: 'session-private-id',
            next_query: 'Continue from where you left off.',
          },
        }}
        onDismiss={vi.fn()}
        onRunCommand={vi.fn()}
        onContinueSession={vi.fn()}
      />,
    );

    expect(markup).toContain('data-testid="session-command-control-panel"');
    expect(markup).toContain('Continue interrupted work');
    expect(markup).toContain('data-testid="session-resume-continue-action"');
    expect(markup).not.toContain('transcript_replay_chain_repair');
    expect(markup).not.toContain('session-private-id');
    expect(markup).not.toContain('resume_status');
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
          creator_username: 'example-owner',
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
    expect(markup).toContain('Runtime Protection');
    expect(markup).toContain('A recent request was safely stopped by a platform safeguard.');
    expect(markup).toContain('Retry the original request.');
    expect(markup).not.toContain('hook');
    expect(markup).not.toContain('handler');
    expect(markup).not.toContain('pre_compaction');
    expect(zh.agent.status.runtimeSafeguardInterrupted).toBe('最近一次请求被平台安全保护中止。');
    expect(zh.agent.status.runtimeSafeguardRetry).toBe('请重试原请求；若仍失败，请联系支持人员。');
    expect(markup).toContain('Handles release coordination.');
    expect(markup).toContain('Sent release reminder');
    expect(markup).toContain('Capability Install Status');
    expect(markup).toContain('smithery/github');
    expect(markup).toContain('OAuth required');
    expect(markup).toContain('telegram');
    expect(markup).toContain('Channel');
  });

  it('does not present an active budget envelope as an active task or safeguard intervention', () => {
    const markup = renderToStaticMarkup(
      <AgentStatusSection
        agent={{
          id: 'agent-active-budget-envelope',
          agent_type: 'native',
          status: 'idle',
          tokens_used_today: 0,
          tokens_used_month: 0,
          tokens_used_total: 0,
          created_at: '2026-08-29T19:22:21Z',
          primary_model_id: 'model-1',
        }}
        llmModels={[{ id: 'model-1', label: 'DeepSeek V4 Flash', model: 'deepseek-v4-flash', provider: 'deepseek' }]}
        activityLogs={[]}
        statusKey="idle"
        onSelectTab={() => {}}
      />,
    );

    expect(markup).toContain('No protected runs');
    expect(markup).not.toContain('System safeguard intervened');
    expect(markup).not.toContain('Wait for the current run');
  });

  it('renders recent tool activity as user actions without raw tool identifiers', () => {
    const markup = renderToStaticMarkup(
      <AgentStatusSection
        agent={{
          id: 'agent-1',
          agent_type: 'native',
          tokens_used_today: 0,
          tokens_used_month: 0,
          tokens_used_total: 0,
          created_at: '2026-08-29T19:22:21Z',
          primary_model_id: 'model-1',
        }}
        llmModels={[{ id: 'model-1', label: 'DeepSeek V4 Flash', model: 'deepseek-v4-flash', provider: 'deepseek' }]}
        activityLogs={[
          {
            id: 'log-track-todo',
            created_at: '2026-08-29T19:23:00Z',
            summary: "Called tool track_todo: {'todo_id': 'private-item'}",
            action_type: 'tool_call',
            detail: { tool: 'track_todo', result: "{'todo_id': 'private-item'}" },
          },
          {
            id: 'log-read-ledger',
            created_at: '2026-08-29T19:24:00Z',
            summary: 'Called tool read_ledger',
            action_type: 'tool_call',
            detail: { tool: 'read_ledger' },
          },
        ]}
        statusKey="idle"
        onSelectTab={() => {}}
      />,
    );

    expect(markup).toContain('Recent Activity');
    expect(markup).toContain('View all');
    expect(markup).toContain('Updated work progress');
    expect(markup).toContain('Reviewed work progress');
    expect(markup).not.toContain('track_todo');
    expect(markup).not.toContain('read_ledger');
    expect(markup).not.toContain('private-item');
    expect(zh.dashboard.globalActivity).toBe('最近动态');
    expect(zh.dashboard.home.viewAllTasks).toBe('查看全部');
    expect(zh.dashboard.activity.toolTrackTodo).toBe('更新了任务进度');
    expect(zh.dashboard.activity.toolReadLedger).toBe('查看了任务进度');
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
        canUseOperatorView
        operatorView={false}
        onOperatorViewChange={() => {}}
      />,
    );

    expect(markup).toContain('User Actions');
    expect(markup).toContain('Heartbeat completed');
    expect(markup).toContain('cycle');
    expect(markup).toContain('Tool Failure Summary');
    expect(markup).toContain('firecrawl_fetch');
    expect(markup).toContain('quota_or_billing');
    expect(markup).toContain('Enter operator view');
    expect(markup).not.toContain('Tenant-wide activity and failures are visible');
  });

  it('hides raw tool payloads and raw action codes in the activity log summary row', () => {
    // UI-002 production reproduction on Agent Detail: legacy rows whose
    // persisted summary embeds the raw tool result must render a clean label
    // (derived from the structured detail), and the meta line must not leak
    // the raw snake_case action code to normal users.
    const markup = renderToStaticMarkup(
      <AgentActivityLogSection
        activityLogs={[
          {
            id: 'log-legacy-tool',
            created_at: '2026-08-25T08:00:00Z',
            summary:
              "Called tool track_todo: {'todo_id': 'item-internal-77', 'status': 'in_progress', 'trace': 'op-9931'}",
            action_type: 'tool_call',
            detail: { tool: 'track_todo', result: "{'todo_id': 'item-internal-77'}" },
          },
          {
            id: 'log-plain',
            created_at: '2026-08-25T08:05:00Z',
            summary: 'Saved Q2 research outline',
            action_type: 'file_written',
            detail: null,
          },
        ]}
        logFilter="all"
        expandedLogId={null}
        onFilterChange={() => {}}
        onToggleExpandedLog={() => {}}
      />,
    );

    expect(markup).not.toContain('item-internal-77');
    expect(markup).not.toContain("{'");
    expect(markup).toContain('Updated work progress');
    expect(markup).not.toContain('track_todo');
    expect(markup).not.toContain('· tool_call');
    expect(markup).not.toContain('· file_written');
    // Non-tool summaries keep rendering verbatim.
    expect(markup).toContain('Saved Q2 research outline');
  });

  it('sanitizes tool-call rows even when the structured detail is missing', () => {
    // Legacy rows without detail must not fall back to the persisted raw
    // summary: the action_type alone decides the clean generic label.
    const markup = renderToStaticMarkup(
      <AgentActivityLogSection
        activityLogs={[
          {
            id: 'log-no-detail',
            created_at: '2026-08-25T08:00:00Z',
            summary: "Called tool web_fetch: {'url': 'https://internal-host/payload', 'item': 'raw-internal-88'}",
            action_type: 'tool_call',
            detail: null,
          },
          {
            id: 'log-approved-no-detail',
            created_at: '2026-08-25T08:05:00Z',
            summary: "Approved-executed send_email: {'message_id': 'raw-internal-99'}",
            action_type: 'tool_call_approved',
            detail: undefined,
          },
        ]}
        logFilter="all"
        expandedLogId={null}
        onFilterChange={() => {}}
        onToggleExpandedLog={() => {}}
      />,
    );

    expect(markup).not.toContain('raw-internal-88');
    expect(markup).not.toContain('raw-internal-99');
    expect(markup).not.toContain("{'");
    expect(markup).toContain('Tool call');
    expect(markup).toContain('Approved tool call');
  });

  it('keeps unknown aware kinds, schedules, and states out of the normal-user DOM entirely', () => {
    // Raw codes/prose must not appear anywhere in the rendered output —
    // text, title, aria-label, or data-* attributes included.
    const markup = renderToStaticMarkup(
      <AgentAwareSection
        agentId="agent-1"
        awareTriggers={[]}
        reflectionSessions={[]}
        reflectionMessages={{}}
        autonomyOverview={{
          agent_id: 'agent-1',
          lookback_hours: 24,
          totals: { triggers: 3, recent_attempts: 0, findings: 0 },
          triggers: [
            {
              id: 'trigger-unknown-everything',
              // Unknown machine kind; title empty so the kind label path runs.
              display_kind: 'experimental_bridge_runtime',
              display_title: '',
              // Legacy payload without structured schedule: the English prose
              // fallback must not reach the user DOM.
              display_schedule: 'Every 30 minutes',
              attention_state: 'unmapped_future_state',
              attention_reason: 'Unmapped future prose reason.',
              next_action: null,
              linked_objective: null,
              last_attempt: null,
              last_artifact: null,
            },
            {
              id: 'trigger-known-schedule',
              display_kind: 'scheduled_job',
              display_title: 'Daily launch report',
              schedule: { kind: 'interval', minutes: 15 },
              attention_state: 'active',
              attention_reason: null,
              next_action: null,
              linked_objective: null,
              last_attempt: null,
              last_artifact: null,
            },
          ],
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
      />,
    );

    // Unknown machine values never render (any attribute surface).
    expect(markup).not.toContain('experimental_bridge_runtime');
    expect(markup).not.toContain('Every 30 minutes');
    expect(markup).not.toContain('unmapped_future_state');
    expect(markup).not.toContain('Unmapped future prose reason');
    // Neutral localized labels render instead.
    expect(markup).toContain('Unknown state');
    expect(markup).toContain('Automation');
    // Known structured schedules keep rendering localized typed labels.
    expect(markup).toContain('Every 15 min');
  });

  it('renders AgentApprovalsSection as a standalone approvals module', () => {
    const markup = renderToStaticMarkup(<AgentApprovalsSection agentId="agent-1" />);

    expect(markup).toContain('Run one workspace command');
    expect(markup).toContain('Release the verified frontend build.');
    expect(markup).toContain('railway up --service frontend');
    expect(markup).toContain('Send work to a connected local employee');
    expect(markup).toContain('Succeeded');
    expect(markup).toContain('Continuing original session');
    expect(markup).not.toContain('workspace.command.escalation');
    expect(markup).not.toContain('local_agent.execute');
    expect(markup).not.toContain('user-internal-123');
    expect(markup).not.toContain('message-internal-456');
    expect(markup).not.toContain('secret-token');
    expect(markup).not.toContain('execution_envelope');
    expect(markup).not.toContain('&quot;command&quot;');
  });

  it('renders AgentSkillsSection as a standalone skills module', () => {
    const markup = renderToStaticMarkup(<AgentSkillsSection agentId="agent-1" />);

    expect(markup).toContain('Import from URL');
    expect(markup).toContain('Browse ClawHub');
    expect(markup).toContain('Skill Format:');
    expect(markup).toContain('Installed skills');
    expect(markup).toContain('web-research');
    expect(markup).toContain('market-research');
    expect(markup).toContain('github-briefing');
    expect(markup).toContain('MCP servers and plugins are managed in the MCP &amp; Plugins tab.');
    expect(markup).not.toContain('MCP-backed capabilities');
    expect(markup).not.toContain('filesystem-mcp');
    expect(markup).not.toContain('<h4 class="agent-skills-panel-title">Plugins</h4>');
    expect(markup).not.toContain('paperclip');
    expect(markup).not.toContain('root=skills');
  });

  it('refreshes installed skill read models after agent skill imports', () => {
    const calls: unknown[] = [];

    invalidateAgentSkillQueries(
      {
        invalidateQueries: (args: unknown) => {
          calls.push(args);
          return Promise.resolve();
        },
      },
      'agent-1',
    );

    expect(calls).toEqual([
      { queryKey: ['files', 'agent-1', 'skills'] },
      { queryKey: ['agent-extensions', 'agent-1'] },
    ]);
  });

  it('renders AgentWorkspaceSection as a standalone workspace module', () => {
    const markup = renderToStaticMarkup(<AgentWorkspaceSection agentId="agent-1" />);

    expect(markup).toContain('File Browser Mock');
    expect(markup).not.toContain('Deploy Playbook');
    expect(markup).not.toContain('canary rollout');
    expect(markup).not.toContain('Search shared memory');
    expect(markup).not.toContain('Save to shared memory');
    expect(markup).not.toContain('Delete entry');
    expect(markup).not.toContain('Document rollback before the final promotion.');
  });

  it('keeps managers in owner view until they explicitly enter operator view', () => {
    const markup = renderToStaticMarkup(
      <AgentWorkspaceSection agentId="agent-1" canUseOperatorView operatorReason="Incident review" />,
    );

    expect(markup).toContain('Enter operator view');
    expect(markup).not.toContain('tenant-wide workspace resources');
  });

  it('renders AgentAwareSection as a standalone aware module', async () => {
    const markup = await renderWithLazyHrPreview(
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
    expect(markup).toContain('Configuration &amp; sources');
    expect(markup).toContain('Not enabled');
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
    // The reason line derives from the stable attention_state code; backend
    // English prose and raw codes never enter the normal-user DOM.
    expect(markup).toContain('Cooling down after a recent failure.');
    expect(markup).not.toContain('Waiting to retry after a recent failure.');
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

    expect(markup).toContain('The complete identity and behavior contract this employee receives at the start of every conversation.');
    expect(markup).toContain('Long-term knowledge curated from conversations. Feedback, strategies, blocked patterns, and project knowledge.');
    expect(markup).toContain('Curation history, performance scorecard, and blocked approaches.');
    expect(markup).toContain('soul.md is governed by Dream/Soul promotion.');
    expect(markup).toContain('Current identity');
    expect(markup).toContain('Read only');
    expect(markup).toContain('schema: hive.soul.v2');
    expect(markup).toContain('Own the verified outcome.');
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
          execution_mode: 'coordinator_strict',
          // max_tokens_per_day: 10000,
          // max_tokens_per_month: 200000,
          max_triggers: 10,
          min_poll_interval_min: 5,
          webhook_rate_limit: 5,
          default_session_permission_mode: 'auto',
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
          default_session_permission_mode: 'auto',
          smart_model_routing_enabled: false,
          execution_mode: 'coordinator_strict',
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
    expect(markup).toContain('Execution Mode');
    expect(markup).toContain('Strict coordinator');
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
    expect(markup).toContain('Access Permissions');
    expect(markup).toContain('Action boundaries');
    expect(markup).toContain('External actions');
    expect(markup).toContain('Ask first');
    expect(markup).toContain('New conversation default');
    expect(markup).toContain('name="default_session_permission_mode"');
    expect(markup).toContain('value="default"');
    expect(markup).toContain('value="auto" selected=""');
    expect(markup).toContain('value="bypassPermissions"');
    expect(markup).toContain('Private to me');
    expect(markup).toContain('Company shared');
    expect(markup).toContain('Channel Config Mock');
    expect(markup).not.toContain('deleteAgent');
  });

  it('exposes all three session permission modes to every authorized session operator', () => {
    expect(sessionPermissionModeOptions().map((option) => option.value)).toEqual([
      'default',
      'auto',
      'bypassPermissions',
    ]);
  });

  it('keeps full access session-local while enterprise governance remains mandatory', async () => {
    const fsModuleId = 'node:fs';
    const { readFileSync } = (await import(/* @vite-ignore */ fsModuleId)) as {
      readFileSync: (path: URL, encoding: string) => string;
    };
    const settingsSource = readFileSync(new URL('./AgentSettingsSection.tsx', import.meta.url), 'utf8');

    expect(settingsSource).not.toContain('disabled={!isAdmin}');
    expect(settingsSource).not.toContain('60-minute session-scoped grant');
    expect(settingsSource).toContain('Enterprise access, safety, and destructive-action rules always apply.');
  });

  it('keeps company capability policy management out of Agent Detail', async () => {
    const fsModuleId = 'node:fs';
    const { readFileSync } = (await import(/* @vite-ignore */ fsModuleId)) as {
      readFileSync: (path: URL, encoding: string) => string;
    };
    const settingsSource = readFileSync(new URL('./AgentSettingsSection.tsx', import.meta.url), 'utf8');
    const detailSource = readFileSync(new URL('../AgentDetail.tsx', import.meta.url), 'utf8');

    expect(settingsSource).not.toContain('renderCapabilityPolicyRow');
    expect(settingsSource).not.toContain('handleScopeChange');
    expect(settingsSource).not.toContain('handleAccessLevelChange');
    expect(settingsSource).not.toContain('showDeleteConfirm');
    expect(Array.from(AGENT_DETAIL_TABS)).not.toContain('governance');
    expect(detailSource).not.toContain('capability-policies');
    expect(detailSource).not.toContain('AgentGovernanceSection');
  });

  it('removes the retired Office Online tag and dedicated tab from Agent Detail', async () => {
    const fsModuleId = 'node:fs';
    const { readFileSync } = (await import(/* @vite-ignore */ fsModuleId)) as {
      readFileSync: (path: URL, encoding: string) => string;
    };
    const detailSource = readFileSync(new URL('../AgentDetail.tsx', import.meta.url), 'utf8');
    const documentsArea = AGENT_WORKBENCH_AREAS.find((area) => area.id === 'documents');

    expect(Array.from(AGENT_DETAIL_TABS)).not.toContain('office');
    expect(documentsArea?.tabs).toEqual(['workspace']);
    expect(getAgentDetailHashTab('#office', AGENT_DETAIL_TABS)).toBeNull();
    expect(detailSource).not.toContain('OfficeWorkbenchSection');
    expect(detailSource).not.toContain("activeTab === 'office'");
  });

  it('treats Local Agent as a real agent with a local runtime label and focused detail tabs', () => {
    expect(isLocalAgentRuntimeType({ agent_type: 'local_agent' })).toBe(true);
    expect(isLocalAgentRuntimeType({ agent_type: 'native' })).toBe(false);
    expect(getVisibleAgentDetailTabs({ agent_type: 'local_agent' })).toEqual(['chat', 'workspace', 'settings']);
    expect(getVisibleAgentDetailTabs({ access_level: 'operator', agent_type: 'native' })).toEqual([
      'chat',
      'workspace',
      'activityLog',
    ]);
    expect(Array.from(AGENT_DETAIL_TABS)).toEqual(expect.arrayContaining(['chat', 'workspace', 'settings']));
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

  it('requires a real patrol decision only for an enable transition', () => {
    expect(patrolSaveDisposition(true, false)).toBe('review_required');
    expect(patrolSaveDisposition(true, false, 'enable_without_plan')).toBe('apply_with_opt_out');
    expect(patrolSaveDisposition(true, true)).toBe('apply');
    expect(patrolSaveDisposition(false, true)).toBe('apply');
    expect(patrolEnabledUpdateValue(true, true)).toBeUndefined();
    expect(patrolEnabledUpdateValue(false, true)).toBe(false);
    expect(patrolEnabledUpdateValue(true, false)).toBe(true);
  });

  it('builds a concrete Plan Mode handoff for patrol review', () => {
    expect(buildPatrolPlanReviewRequest({
      minutes: 90,
      activeHours: '10:00-19:00',
      timezone: 'Asia/Shanghai',
    })).toContain('every 90 minutes');
    expect(buildPatrolPlanReviewRequest({
      minutes: 90,
      activeHours: '10:00-19:00',
      timezone: 'Asia/Shanghai',
    })).toContain('10:00-19:00 (Asia/Shanghai)');
  });

  it('renders Agent access permissions inside detail settings', () => {
    const markup = renderToStaticMarkup(
      <AgentSettingsSection
        agentId="agent-1"
        agent={{
          id: 'agent-1',
          agent_type: 'native',
          primary_model_id: 'model-1',
          fallback_model_id: '',
          execution_mode: 'standard',
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
          default_session_permission_mode: 'default',
          smart_model_routing_enabled: false,
          execution_mode: 'standard',
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

    expect(markup).toContain('Access Permissions');
    expect(markup).toContain('Default Access Level');
    expect(markup).toMatch(/name="perm_scope"/);
    expect(markup).not.toContain('data-testid="agent-operator-grants"');
    expect(markup).not.toContain('Delete Agent');
  });

  it('shows operator inspection grants only with server-derived permission authority', () => {
    const markup = renderToStaticMarkup(
      <AgentSettingsSection
        agentId="agent-1"
        agent={{
          id: 'agent-1',
          agent_type: 'native',
          primary_model_id: 'model-1',
          execution_mode: 'standard',
          max_triggers: 10,
          min_poll_interval_min: 5,
          webhook_rate_limit: 5,
          security_zone: 'standard',
        }}
        llmModels={[]}
        canManage
        canManagePermissions
        settingsForm={{
          primary_model_id: 'model-1',
          fallback_model_id: '',
          max_triggers: 10,
          min_poll_interval_min: 5,
          webhook_rate_limit: 5,
          default_session_permission_mode: 'default',
          smart_model_routing_enabled: false,
          execution_mode: 'standard',
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

    expect(markup).toContain('data-testid="agent-operator-grants"');
    expect(markup).toContain('Operator inspection access');
    expect(markup).toContain('This never permits mutations.');
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

    expect(queryKeyCalls).toContainEqual(['chat-session-index', 'route-agent', 'session-1', 'owner']);
    expect(queryKeyCalls).toContainEqual(['chat-session-decisions', 'route-agent', 'session-1', 'owner']);
    expect(queryKeyCalls).not.toContainEqual(['chat-session-index', 'stale-agent', 'session-1', 'owner']);
    expect(queryKeyCalls).not.toContainEqual(['chat-session-decisions', 'stale-agent', 'session-1', 'owner']);
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
    expect(markup).toContain('<button type="button" data-testid="chat-artifact-row-open"');
    expect(markup).not.toContain('role="button"');
    expect(markup).not.toContain('token=');
    expect(markup).toContain('<button type="button" class="chat-artifact-action"');
  });

  it('folds completed ordinary tool-call steps while keeping raw results hidden by default', () => {
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
            toolName: 'track_todo',
            toolStatus: 'done',
            toolArgs: { title: 'Close the real Session path', status: 'completed' },
          },
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
    expect(markup).not.toContain('Read file');
    expect(markup).not.toContain('report.md');
    expect(markup).toContain('aria-expanded="false"');
    expect(markup).not.toContain('path:');
    expect(markup).not.toContain('RAW FILE CONTENT SHOULD NOT BE INLINE');
  });

  it('keeps raw tool-produced workspace artifacts out of user-facing delivery cards', () => {
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

    expect(markup).toContain('Processed');
    expect(markup).not.toContain('Edit document');
    expect(markup).not.toContain('proposal.docx');
    expect(markup).not.toContain('{&quot;ok&quot;: true}');
    expect(markup).toContain('session-runtime-panel is-collapsed');
    expect(markup).not.toContain('data-testid="session-runtime-collapsed-deliverables"');
    expect(markup).not.toContain('data-testid="session-workspace-documents-unattributed"');
    expect(markup).not.toContain('data-testid="session-workspace-documents-current"');
    expect(markup).not.toContain('data-testid="chat-artifact-row-open"');
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
    expect(markup).toContain('Processed');
    expect(markup).toContain('最终答案已经完成。');
    expect(markup).not.toContain('Thinking');
    expect(markup).not.toContain('Inspect code before answering.');
    expect(markup).toContain('Read 1 file');
    expect(markup).not.toContain('Ran 1 command');
    expect(markup).not.toContain('Read file');
    expect(markup).not.toContain('Context Compacted');
    expect(markup).not.toContain('Run command');
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

    expect(markup).toContain('Approval required');
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

    expect(markup).toContain('Approval required');
    expect(markup).toContain('run_command');
    expect(markup).toContain('Allow once');
    expect(markup).not.toContain('Allow for this session');
    expect(markup).toContain('Delete actions can only be allowed once.');
    expect(markup).toContain('Deny');
  });

  it('keeps child session runtime evidence behind the collapsed ordinary-user rail', () => {
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

    expect(markup).toContain('data-testid="run-disclosure-block"');
    expect(markup).toContain('session-runtime-panel is-collapsed');
    expect(markup).not.toContain('data-testid="session-runtime-console"');
    expect(markup).not.toContain('session:child-session-1');
    expect(markup).not.toContain('run:run-1');
  });

  it('routes chat artifacts to the session inspector only when the file type is previewable', () => {
    expect(getArtifactOpenMode({ name: 'report.md', path: 'workspace/report.md', previewKind: 'markdown' })).toBe('inspector_preview');
    expect(getArtifactOpenMode({ name: 'notes.txt', path: 'workspace/notes.txt', previewKind: 'text' })).toBe('inspector_preview');
    expect(getArtifactOpenMode({ name: 'chart.png', path: 'workspace/chart.png', previewKind: 'image' })).toBe('inspector_preview');
    expect(getArtifactOpenMode({ name: 'slides.pdf', path: 'workspace/slides.pdf', previewKind: 'pdf' })).toBe('inspector_preview');
    expect(getArtifactOpenMode({ name: 'deck.pptx', path: 'workspace/deck.pptx', previewKind: 'office' })).toBe('inspector_preview');
    expect(getArtifactOpenMode({ name: 'archive.zip', path: 'workspace/archive.zip', previewKind: 'download' })).toBe('download');
  });

  it('keeps raw tool workspace writes out of user-facing delivery cards', () => {
    expect(isUserFacingDeliveryArtifact({ name: 'draft.md', path: 'workspace/draft.md', source: 'workspace_write' }, 'tool')).toBe(false);
    expect(isUserFacingDeliveryArtifact({ name: 'report.md', path: 'workspace/report.md', source: 'workspace_write' }, 'assistant')).toBe(true);
    expect(isUserFacingDeliveryArtifact({ name: 'child.md', path: 'workspace/child.md', source: 'a2a_delivery_ref' }, 'tool')).toBe(true);
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

  it('renders the real running Task ledger as a persistent composer panel and folds raw Task tool calls', () => {
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
            toolName: 'task_create',
            toolStatus: 'done',
            toolArgs: { subject: 'Collect and grade sources' },
          },
          {
            role: 'tool_call',
            content: '',
            toolName: 'task_update',
            toolStatus: 'done',
            toolArgs: { task_id: '1', status: 'in_progress' },
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
    expect(markup).not.toContain('data-testid="session-workbench-inspector"');
    expect(markup).not.toContain('data-testid="session-native-controls"');
    expect(markup).toContain('data-testid="chat-work-ledger-dock"');
    expect(markup).toContain('data-testid="chat-work-ledger-summary"');
    expect(markup).toContain('data-testid="chat-work-ledger-panel"');
    expect(markup).toContain('data-presentation="persistent"');
    expect(markup).toContain('Task 1-2 of 2');
    expect(markup).not.toContain('data-testid="run-disclosure-tool-group"');
    expect(markup).not.toContain('Used tools: Update tasks');
    expect(markup).not.toContain('Update tasks');
    expect(markup).not.toContain('data-presentation="surface"');
    expect(markup).not.toContain('track_todo');
    expect(markup).not.toContain('task_create');
    expect(markup).not.toContain('task_update');
    expect(markup).not.toContain('Start goal');
    expect(markup).not.toContain('Create team');
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

  it('keeps the persistent work ledger dock inside the composer rail instead of a chat-side column', () => {
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
    expect(markup).not.toContain('data-testid="session-workbench-inspector"');
    expect(markup).not.toContain('data-testid="session-native-controls"');
    expect(markup).toContain('data-testid="chat-work-ledger-dock"');
    expect(markup).toContain('data-testid="chat-work-ledger-summary"');
    expect(markup).toContain('data-testid="chat-work-ledger-panel"');
  });

  it('keeps the ordinary-user runtime rail collapsed with glanceable live counts on first render', () => {
    const markup = renderToStaticMarkup(
      <AgentChatSection
        agent={{ id: 'agent-1', name: 'Runtime Bot' }}
        currentUser={{ id: 'user-1' }}
        isAdmin={false}
        chatScope="mine"
        onSetChatScope={vi.fn()}
        onLoadAllSessions={vi.fn()}
        onCreateNewSession={vi.fn()}
        sessionsLoading={false}
        sessions={[]}
        activeSession={{
          id: 'runtime-panel-session',
          user_id: 'user-1',
          title: 'Runtime panel session',
          created_at: '2026-06-28T09:00:00Z',
        }}
        branchLineage={[
          { id: 'runtime-panel-session', parent_session_id: null, title: 'Main session', branch: {} },
          {
            id: 'branch-session-1',
            parent_session_id: 'runtime-panel-session',
            title: 'Existing branch',
            branch: { branch_mode: 'branch', anchor_event_id: 'checkpoint-user-1' },
          },
          {
            id: 'branch-session-2',
            parent_session_id: 'runtime-panel-session',
            title: 'Second branch',
            branch: { branch_mode: 'branch', anchor_event_id: 'checkpoint-user-1' },
          },
          {
            id: 'branch-session-3',
            parent_session_id: 'runtime-panel-session',
            title: 'Third branch',
            branch: { branch_mode: 'branch', anchor_event_id: 'checkpoint-user-1' },
          },
          {
            id: 'branch-session-4',
            parent_session_id: 'runtime-panel-session',
            title: 'Assistant side branch',
            branch: { branch_mode: 'branch', anchor_event_id: 'checkpoint-assistant-2' },
          },
        ]}
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
            content: 'Generated a report.',
            artifacts: [
              {
                name: 'runtime-report.md',
                path: 'workspace/runtime-report.md',
                previewKind: 'markdown',
                size: 2048,
                runtimeTaskId: 'run-1',
                snapshotHash: 'sha256-runtime',
                sourceAgentName: 'Reviewer Bot',
              },
              {
                name: 'historical-report.md',
                path: 'workspace/historical-report.md',
                previewKind: 'markdown',
                source: 'historical_session',
              },
              {
                name: 'scratch.txt',
                path: 'workspace/scratch.txt',
                previewKind: 'text',
              },
            ],
          },
        ]}
        chatMessagesSessionId="runtime-panel-session"
        runtimeSummary={{
          model: {
            label: 'GPT-5.4',
            provider: 'openai',
            name: 'gpt-5.4',
            context_window_tokens: 128000,
          },
          runtime: {
            connected: true,
            estimated_input_tokens: 72000,
            remaining_tokens_estimate: 56000,
          },
          activated_tool_groups: ['web-research'],
          used_tools: ['web_search', 'spawn_subagent'],
          blocked_capabilities: [],
          compaction_count: 1,
        }}
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
        onSelectBranchSession={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('data-testid="session-runtime-panel"');
    expect(markup).toContain('session-runtime-panel is-collapsed');
    expect(markup).toContain('data-testid="session-runtime-collapse-toggle"');
    expect(markup).toContain('Expand runtime panel');
    expect(markup).toContain('data-testid="session-runtime-collapsed-deliverables"');
    expect(markup).toContain('Show deliverables (1)');
    expect(markup).toContain('data-testid="session-runtime-collapsed-attention"');
    expect(markup).toContain('Show items waiting for you (4)');
    expect(markup).toContain('data-testid="session-runtime-collapsed-running"');
    expect(markup).toContain('Show running items (3)');
    expect(markup).toContain('data-testid="session-gitline"');
    expect(markup).toContain('data-testid="session-gitline-checkpoint"');
    expect(markup).toContain('data-testid="session-gitline-branch"');
    expect(markup).toContain('data-testid="session-gitline-branch-cluster"');
    expect(markup).toContain('+3');
    expect(markup).toContain('data-session-action="navigate-checkpoint"');
    expect(markup).toContain('data-session-action="navigate-branch"');
    expect(markup).not.toContain('data-session-command="branch"');
    expect(markup).toContain('data-testid="chat-work-ledger-dock"');
    expect(markup).toContain('data-testid="chat-work-ledger-summary"');
    expect(markup).toContain('data-testid="chat-work-ledger-panel"');
    expect(markup).toContain('Task 1-2 of 2');
    expect(markup).toContain('runtime-report.md');
    expect(markup).not.toContain('data-testid="session-runtime-console"');
    expect(markup).not.toContain('data-testid="session-runtime-summary-strip"');
    expect(markup).not.toContain('data-testid="session-runtime-waiters"');
    expect(markup).not.toContain('data-testid="session-runtime-segment-team"');
    expect(markup).not.toContain('data-testid="session-runtime-segment-workers"');
    expect(markup).not.toContain('data-testid="session-runtime-segment-workflow"');
    expect(markup).not.toContain('data-testid="session-runtime-segment-activity"');
    expect(markup).not.toContain('Run status');
    expect(markup).not.toContain('data-testid="session-runtime-main-row"');
    expect(markup).not.toContain('data-testid="session-runtime-metrics"');
    expect(markup).not.toContain('data-testid="session-runtime-agent-teams"');
    expect(markup).not.toContain('data-testid="session-runtime-subagents"');
    expect(markup).not.toContain('data-testid="session-runtime-workflows"');
    expect(markup).not.toContain('data-testid="session-runtime-background"');
    expect(markup).not.toContain('data-testid="session-runtime-notifications"');
    expect(markup).not.toContain('data-testid="session-runtime-runs"');
    expect(markup).not.toContain('data-testid="session-runtime-raw"');
    expect(markup).not.toContain('Agent Team / Sub-agent');
    expect(markup).not.toContain('data-testid="session-runtime-tabs"');
    expect(markup).not.toContain('data-testid="session-runtime-tab-tasks"');
    expect(markup).not.toContain('data-testid="session-runtime-tab-checks"');
    expect(markup).not.toContain('data-testid="session-runtime-tab-runs"');
    expect(markup).not.toContain('data-testid="session-runtime-checks"');
    expect(markup).not.toContain('data-testid="session-runtime-commands"');
    expect(markup).not.toContain('Commands / Tools');
    expect(markup).not.toContain('Tool calls');
    expect(markup).not.toContain('Checks');
    expect(markup).not.toContain('data-testid="session-workbench-inspector"');
    expect(markup).not.toContain('data-testid="session-native-controls"');
  });

  it('omits repeated artifact counts and keeps the ordinary-user rail collapsed by default', () => {
    const markup = renderToStaticMarkup(
      <AgentChatSection
        agent={{ id: 'agent-1', name: 'Runtime Bot' }}
        currentUser={{ id: 'user-1' }}
        isAdmin={false}
        chatScope="mine"
        onSetChatScope={vi.fn()}
        onLoadAllSessions={vi.fn()}
        onCreateNewSession={vi.fn()}
        sessionsLoading={false}
        sessions={[]}
        activeSession={{
          id: 'session-repeat-artifact',
          user_id: 'user-1',
          title: 'Repeat artifact session',
          created_at: '2026-06-28T09:00:00Z',
        }}
        branchLineage={[]}
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
            content: 'Delivered.',
            artifacts: [
              {
                name: 'repeat-report.md',
                path: 'workspace/repeat-report.md',
                previewKind: 'markdown',
                size: 100,
                runtimeTaskId: 'run-1',
                snapshotHash: 'sha256-repeat-1',
              },
            ],
          },
          {
            role: 'assistant',
            content: 'Delivered again.',
            artifacts: [
              {
                name: 'repeat-report.md',
                path: 'workspace/repeat-report.md',
                previewKind: 'markdown',
                size: 120,
                runtimeTaskId: 'run-1',
                snapshotHash: 'sha256-repeat-2',
              },
            ],
          },
        ]}
        chatMessagesSessionId="session-repeat-artifact"
        runtimeSummary={null}
        transportNotice={null}
        isWaiting={false}
        activeRunStatus="idle"
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

    expect(markup).toContain('repeat-report.md');
    expect(markup).not.toContain('×2');
    expect(markup).not.toContain('2 deliveries');
    expect(markup).toContain('session-runtime-panel is-collapsed');
    expect(markup).toContain('data-testid="session-runtime-collapse-toggle"');
    expect(markup).toContain('data-testid="session-runtime-collapsed-deliverables"');
    expect(markup).not.toContain('data-testid="session-runtime-console"');
    expect(markup).not.toContain('data-testid="session-runtime-segment-team"');
    expect(markup).not.toContain('data-testid="session-runtime-segment-workers"');
    expect(markup).not.toContain('data-testid="session-runtime-segment-workflow"');
    expect(markup).not.toContain('data-testid="session-runtime-segment-activity"');
    expect(markup).not.toContain('data-testid="session-runtime-background"');
    expect(markup).not.toContain('data-testid="session-runtime-notifications"');
    expect(markup).not.toContain('data-testid="session-runtime-runs"');
    expect(markup).not.toContain('data-testid="session-runtime-raw"');
    expect(markup).not.toContain('No active collaboration surfaces');
  });

  it('keeps one-shot Sub-agent worker detail behind the collapsed ordinary-user rail', () => {
    const markup = renderToStaticMarkup(
      <AgentChatSection
        agent={{ id: 'agent-1', name: 'Runtime Bot' }}
        currentUser={{ id: 'user-1' }}
        isAdmin={false}
        chatScope="mine"
        onSetChatScope={vi.fn()}
        onLoadAllSessions={vi.fn()}
        onCreateNewSession={vi.fn()}
        sessionsLoading={false}
        sessions={[]}
        activeSession={{
          id: 'session-subagent-worker',
          user_id: 'user-1',
          title: 'Sub-agent worker session',
          created_at: '2026-07-04T00:00:00Z',
        }}
        branchLineage={[]}
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
        chatMessagesSessionId="session-subagent-worker"
        runtimeSummary={null}
        transportNotice={null}
        isWaiting={false}
        activeRunStatus="idle"
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

    expect(markup).toContain('session-runtime-panel is-collapsed');
    expect(markup).toContain('data-testid="session-runtime-collapse-toggle"');
    expect(markup).not.toContain('data-testid="session-runtime-console"');
    expect(markup).not.toContain('data-testid="session-runtime-segment-body-workers"');
  });

  it('offers a new-worker request for ordinary Sub-agent failure but not reconciliation replay', () => {
    expect(subagentWorkerRecoveryModel({
      status: 'failed',
      raw: {
        subagent_decision_entry: {
          required_user_action: 'inspect_failure_and_decide_retry',
          retry_available: false,
        },
      },
    } as any)).toEqual({
      canRequestNewWorker: true,
      requiresPlatformAdmin: false,
    });
    expect(subagentWorkerRecoveryModel({
      status: 'needs_reconciliation',
      raw: {
        subagent_decision_entry: {
          required_user_action: 'approve_reconciliation_retry',
          retry_available: true,
        },
      },
    } as any)).toEqual({
      canRequestNewWorker: false,
      requiresPlatformAdmin: true,
    });
  });

  it('renders Agent Team member sessions as enterable child-session windows with composer target', () => {
    const markup = renderToStaticMarkup(
      <AgentChatSection
        agent={{ id: 'agent-1', name: 'Runtime Bot' }}
        currentUser={{ id: 'user-1' }}
        isAdmin={false}
        chatScope="mine"
        onSetChatScope={vi.fn()}
        onLoadAllSessions={vi.fn()}
        onCreateNewSession={vi.fn()}
        sessionsLoading={false}
        sessions={[]}
        activeSession={{
          id: 'member-session-1',
          user_id: 'user-1',
          title: 'Research Team / Reviewer',
          source_channel: 'agent_team',
          session_kind: 'team_member',
          runtime_source: 'team_member',
          parent_session_id: 'parent-session-1',
          root_session_id: 'parent-session-1',
          transcript_metadata_json: {
            team_id: 'team-1',
            team_name: 'Research Team',
            member_name: 'Reviewer',
            member_role: 'audit',
          },
        }}
        branchLineage={[
          { id: 'parent-session-1', parent_session_id: null, title: 'Main session', branch: {} },
          {
            id: 'member-session-1',
            parent_session_id: 'parent-session-1',
            root_session_id: 'parent-session-1',
            title: 'Research Team / Reviewer',
            branch: {},
          },
        ]}
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
        chatMessages={[{ role: 'assistant', content: 'Member evidence.' }]}
        chatMessagesSessionId="member-session-1"
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
        onSelectBranchSession={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('data-testid="session-team-member-window"');
    expect(markup).toContain('Main &gt; Agent: Reviewer');
    expect(markup).toContain('data-testid="session-active-session-tab"');
    expect(markup).toContain('running');
    expect(markup).toContain('data-testid="session-composer-target"');
    expect(markup).toContain('Agent Team member');
    expect(markup).toContain('Reviewer');
  });

  it('renders a Dynamic Workflow run window where leaf rows without child sessions are detail-only', () => {
    const workflow = {
      id: 'workflow-run-1',
      label: 'ABS diligence workflow',
      status: 'running',
      state: 'running',
      runtimeKind: 'workflow',
      summary: 'fanout then critic',
      childSessionId: null,
      enterable: false,
      metrics: {
        elapsedSeconds: null,
        elapsedLabel: null,
        tokenCount: null,
        tokenLabel: null,
      toolUseCount: null,
      toolUseLabel: null,
      lastActivityLabel: null,
      },
      workflow_controls: {
        run_id: 'workflow-run-1',
        gate_status: 'waiting',
        wait_status: 'waiting_for_gate',
        repairable: false,
        model_promotion_review: {},
        actions: [
          { action: 'approve_gate', enabled: true, run_id: 'workflow-run-1', step_id: 'approve-send', preview_id: 'preview-1', reason: 'approval required' },
          { action: 'reject_gate', enabled: true, run_id: 'workflow-run-1', step_id: 'approve-send', preview_id: 'preview-1', reason: 'approval required' },
          { action: 'cancel', enabled: true, run_id: 'workflow-run-1', preview_id: 'preview-1', reason: 'run is active' },
        ],
      },
      members: [],
      steps: [
        {
          id: 'step-1',
          label: 'Collect evidence',
          status: 'completed',
          state: 'completed',
          runtimeKind: 'workflow_step',
          summary: '',
          childSessionId: null,
          enterable: false,
          metrics: {
            elapsedSeconds: null,
            elapsedLabel: null,
            tokenCount: null,
            tokenLabel: null,
            toolUseCount: null,
            toolUseLabel: null,
            lastActivityLabel: null,
          },
          members: [],
          steps: [],
          leafCalls: [],
          raw: {},
        },
      ],
      leafCalls: [
        {
          id: 'leaf-1',
          label: 'CLO source review',
          status: 'completed',
          state: 'completed',
          runtimeKind: 'workflow_leaf',
          summary: 'No child session attached',
          childSessionId: null,
          enterable: false,
          metrics: {
            elapsedSeconds: null,
            elapsedLabel: null,
            tokenCount: null,
            tokenLabel: null,
            toolUseCount: null,
            toolUseLabel: null,
            lastActivityLabel: null,
          },
          members: [],
          steps: [],
          leafCalls: [],
          raw: {},
        },
      ],
      raw: {},
    };

    const markup = renderToStaticMarkup(
      <WorkflowRunFocusPanel
        workflow={workflow}
        onClose={vi.fn()}
        onSelectSession={vi.fn()}
      />,
    );

    expect(markup).toContain('data-testid="session-workflow-run-window"');
    expect(markup).toContain('ABS diligence workflow');
    expect(markup).toContain('data-testid="session-workflow-step-row"');
    expect(markup).toContain('data-testid="session-workflow-gate-status"');
    expect(markup).toContain('waiting');
    expect(markup).toContain('data-testid="session-workflow-wait-status"');
    expect(markup).toContain('waiting_for_gate');
    expect(markup).toContain('data-testid="session-workflow-action-approve_gate"');
    expect(markup).toContain('data-testid="session-workflow-action-reject_gate"');
    expect(markup).toContain('data-testid="session-workflow-action-cancel"');
    expect(markup).not.toContain('data-workflow-run-id');
    expect(markup).not.toContain('data-preview-id');
    expect(markup).toContain('data-testid="session-workflow-leaf-detail"');
    expect(markup).toContain('CLO source review');
    expect(markup).not.toContain('leaf-1');
    expect(markup).not.toContain('workflow_leaf');
    expect(markup).not.toContain('data-testid="session-workflow-leaf-enter"');
  });

  it('wires Dynamic Workflow control clicks and enterable leaf sessions', () => {
    const workflow = {
      id: 'workflow-run-1',
      label: 'ABS diligence workflow',
      status: 'running',
      state: 'running',
      runtimeKind: 'workflow',
      summary: 'fanout then critic',
      childSessionId: null,
      enterable: false,
      metrics: {
        elapsedSeconds: null,
        elapsedLabel: null,
        tokenCount: null,
        tokenLabel: null,
        toolUseCount: null,
        toolUseLabel: null,
        lastActivityLabel: null,
      },
      workflow_controls: {
        run_id: 'workflow-run-1',
        gate_status: 'waiting',
        wait_status: 'waiting_for_gate',
        actions: [
          { action: 'approve_gate', enabled: true, run_id: 'workflow-run-1', step_id: 'approve-send', preview_id: 'preview-1', reason: 'approval required' },
          { action: 'reject_gate', enabled: true, run_id: 'workflow-run-1', step_id: 'approve-send', preview_id: 'preview-1', reason: 'approval required' },
        ],
      },
      members: [],
      steps: [],
      leafCalls: [
        {
          id: 'leaf-1',
          label: 'CLO source review',
          status: 'running',
          state: 'running',
          runtimeKind: 'workflow_leaf',
          summary: 'Child session attached',
          childSessionId: 'leaf-session-1',
          enterable: true,
          metrics: {
            elapsedSeconds: null,
            elapsedLabel: null,
            tokenCount: null,
            tokenLabel: null,
            toolUseCount: null,
            toolUseLabel: null,
            lastActivityLabel: null,
          },
          members: [],
          steps: [],
          leafCalls: [],
          raw: {},
        },
      ],
      raw: {},
    };
    const workflowActions: string[] = [];
    const selectedSessions: string[] = [];

    const tree = WorkflowRunFocusPanel({
      workflow,
      onClose: vi.fn(),
      onSelectSession: (sessionId) => {
        selectedSessions.push(sessionId);
      },
      onWorkflowAction: (action, item) => {
        workflowActions.push(`${action.action}:${action.runId}:${action.previewId}:${item.id}`);
      },
    });

    const approveButton = findElementByTestId(tree, 'session-workflow-action-approve_gate');
    const rejectButton = findElementByTestId(tree, 'session-workflow-action-reject_gate');
    const leafEnter = findElementByTestId(tree, 'session-workflow-leaf-enter');

    expect(approveButton.props.disabled).toBe(false);
    expect(rejectButton.props.disabled).toBe(false);
    approveButton.props.onClick();
    leafEnter.props.onClick();

    expect(workflowActions).toEqual(['approve_gate:workflow-run-1:preview-1:workflow-run-1']);
    expect(selectedSessions).toEqual(['leaf-session-1']);
  });

  it('keeps branch GitLine on the root checkpoint axis and exposes a return-to-main node', () => {
    queryKeyCalls.length = 0;

    const markup = renderToStaticMarkup(
      <AgentChatSection
        agent={{ id: 'agent-1', name: 'Runtime Bot' }}
        currentUser={{ id: 'user-1' }}
        isAdmin={false}
        chatScope="mine"
        onSetChatScope={vi.fn()}
        onLoadAllSessions={vi.fn()}
        onCreateNewSession={vi.fn()}
        sessionsLoading={false}
        sessions={[]}
        activeSession={{
          id: 'branch-session-1',
          user_id: 'user-1',
          title: 'Existing branch',
          root_session_id: 'runtime-panel-session',
          parent_session_id: 'runtime-panel-session',
          created_at: '2026-06-28T09:30:00Z',
          transcript_metadata_json: {
            branch_mode: 'branch',
            root_session_id: 'runtime-panel-session',
            source_session_id: 'runtime-panel-session',
            anchor_event_id: 'checkpoint-user-1',
            anchor_sequence: 1,
          },
        }}
        branchLineage={[
          { id: 'runtime-panel-session', parent_session_id: null, title: 'Main session', branch: {} },
          {
            id: 'branch-session-1',
            parent_session_id: 'runtime-panel-session',
            root_session_id: 'runtime-panel-session',
            title: 'Existing branch',
            branch: {
              branch_mode: 'branch',
              root_session_id: 'runtime-panel-session',
              source_session_id: 'runtime-panel-session',
              anchor_event_id: 'checkpoint-user-1',
              anchor_sequence: 1,
            },
          },
        ]}
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
            content: 'Branch-local continuation.',
          },
        ]}
        chatMessagesSessionId="branch-session-1"
        runtimeSummary={null}
        transportNotice={null}
        isWaiting={false}
        activeRunStatus="completed"
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
        onSelectBranchSession={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(queryKeyCalls).toContainEqual(['chat-session-index', 'agent-1', 'runtime-panel-session', 'gitline-axis', 'owner']);
    expect(markup).toContain('data-axis-session-id="runtime-panel-session"');
    expect(markup).toContain('data-active-session-id="branch-session-1"');
    expect(markup).toContain('data-session-action="navigate-root-session"');
    expect(markup).toContain('data-branch-session-id="runtime-panel-session"');
    expect(markup).toContain('data-session-action="navigate-branch"');
    expect(markup).toContain('data-checkpoint-id="checkpoint-user-1"');
    expect(markup).not.toContain('data-testid="session-gitline-branches"');
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
    expect(markup).toContain('Working');
    expect(markup).toContain('Thinking');
    expect(markup).not.toContain('Active run:');
    expect(markup).not.toContain('continuing this turn');
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
        agent={{ id: 'agent-1', name: '__system_hr__', agent_class: 'internal_system' }}
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
            fallback_name: 'gpt-5.4-mini',
            route_reason: 'primary_model',
            routing_config_source: 'runtime_default',
            routing_locked: true,
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
    expect(markup).toContain('data-testid="session-composer-action-goal-switch"');
    expect(markup).not.toContain('data-testid="session-composer-action-schedule-switch"');
    expect(markup).toContain('role="switch"');
    expect(markup).toContain('aria-checked="false"');
    expect(markup).toContain('Approve for me');
    expect(markup).toContain('Ask first');
    expect(markup).toContain('Full access');
    expect(markup).toContain('data-testid="session-composer-permission-mode-auto"');
    expect(markup).toContain('data-testid="session-composer-permission-mode-default"');
    expect(markup).toContain('data-testid="session-composer-permission-mode-bypassPermissions"');
    expect(markup).toContain('data-testid="session-composer-permission-mode-default"');
    expect(markup).not.toContain('Manage access');
    expect(markup).not.toContain('acceptEdits');
    expect(markup).toContain('GPT-5.4');
    expect(markup).not.toContain('25% used');
    expect(markup).toContain('HR Agent');
    expect(markup).not.toContain('__system_hr__');
    expect(markup).not.toContain('primary_model');
    expect(markup).not.toContain('runtime_default');
    expect(markup).not.toContain('fallback:');
    expect(markup).not.toContain('model locked by user');
    expect(markup).not.toMatch(/microphone|voice|语音/i);
  });

  it('renders A2A agent sessions as read-only and disables composer actions', () => {
    const markup = renderToStaticMarkup(
      <AgentChatSection
        agent={{ id: 'agent-a', name: 'Lead Agent' }}
        currentUser={{ id: 'user-1' }}
        isAdmin={false}
        chatScope="mine"
        onSetChatScope={vi.fn()}
        onLoadAllSessions={vi.fn()}
        onCreateNewSession={vi.fn()}
        sessionsLoading={false}
        sessions={[]}
        activeSession={{
          id: 'a2a-session-1',
          agent_id: 'agent-b',
          peer_agent_id: 'agent-a',
          user_id: 'user-1',
          title: 'A2A handoff',
          username: 'Researcher ↔ Lead Agent',
          session_kind: 'agent_chat',
          created_at: '2026-06-29T09:00:00Z',
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
        historyMsgs={[{ id: 'msg-1', role: 'assistant', sender_name: 'Researcher', content: 'Done.' }]}
        historyMessagesSessionId="a2a-session-1"
        showHistoryScrollBtn={false}
        onScrollHistoryToBottom={vi.fn()}
        chatContainerRef={React.createRef<HTMLDivElement>()}
        onChatScroll={vi.fn()}
        chatMessages={[]}
        chatMessagesSessionId={null}
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
        chatInput="should not send"
        onSetChatInput={vi.fn()}
        onHandlePaste={vi.fn()}
        onSendChatMsg={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('Agent Conversation');
    expect(markup).not.toContain('data-testid="session-composer"');
    expect(markup).not.toContain('data-testid="session-composer-input"');
    expect(markup).not.toContain('data-testid="session-composer-send"');
  });

  it('keeps a newly created web session writable even before user_id hydrates', () => {
    const markup = renderToStaticMarkup(
      <AgentChatSection
        agent={{ id: 'agent-1', name: 'Lead Agent' }}
        currentUser={{ id: 'user-1' }}
        isAdmin={false}
        chatScope="mine"
        onSetChatScope={vi.fn()}
        onLoadAllSessions={vi.fn()}
        onCreateNewSession={vi.fn()}
        sessionsLoading={false}
        sessions={[]}
        activeSession={{
          id: 'new-session-1',
          agent_id: 'agent-1',
          title: 'Session',
          source_channel: 'web',
          listed_surface: 'chat',
          created_at: '2026-07-01T02:05:00Z',
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
        chatMessagesSessionId="new-session-1"
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

    expect(markup).not.toContain('Read-only');
    expect(markup).toContain('data-testid="session-composer"');
    expect(markup).toContain('data-testid="session-composer-shell"');
    expect(markup).toContain('class="chat-input"');
    expect(markup).toContain('aria-label="send"');
  });

  it('keeps a writable web session editable while realtime transport is reconnecting', () => {
    const markup = renderToStaticMarkup(
      <AgentChatSection
        agent={{ id: 'agent-1', name: 'Lead Agent' }}
        currentUser={{ id: 'user-1' }}
        isAdmin={false}
        chatScope="mine"
        onSetChatScope={vi.fn()}
        onLoadAllSessions={vi.fn()}
        onCreateNewSession={vi.fn()}
        sessionsLoading={false}
        sessions={[]}
        activeSession={{
          id: 'new-session-transport-pending',
          agent_id: 'agent-1',
          user_id: 'user-1',
          title: 'Session',
          source_channel: 'web',
          listed_surface: 'chat',
          session_kind: 'human_chat',
          created_at: '2026-07-01T02:05:00Z',
        }}
        wsConnected={false}
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
        chatMessagesSessionId="new-session-transport-pending"
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
        chatInput="hello"
        onSetChatInput={vi.fn()}
        onHandlePaste={vi.fn()}
        onSendChatMsg={vi.fn()}
        isStreaming={false}
        onAbortGeneration={vi.fn()}
      />,
    );

    expect(markup).toContain('data-testid="session-composer"');
    expect(markup).not.toContain('Read-only');
    expect(markup).not.toContain('Connecting...');
    expect(markup).toContain('role="status"');
    expect(markup).toContain('aria-live="polite"');
    expect(markup).toMatch(/<textarea[^>]*class="chat-input"(?![^>]*disabled)/);
    expect(markup).toMatch(/<button[^>]*aria-label="send"(?![^>]*disabled)/);
  });

  it('keeps a backend-confirmed current-user web session writable even when user id hydration differs', () => {
    const markup = renderToStaticMarkup(
      <AgentChatSection
        agent={{ id: 'agent-1', name: 'Lead Agent' }}
        currentUser={{ id: 'client-user-id' }}
        isAdmin={false}
        chatScope="mine"
        onSetChatScope={vi.fn()}
        onLoadAllSessions={vi.fn()}
        onCreateNewSession={vi.fn()}
        sessionsLoading={false}
        sessions={[]}
        activeSession={{
          id: 'new-session-2',
          agent_id: 'agent-1',
          user_id: 'server-user-id',
          is_current_user_session: true,
          title: 'Session',
          source_channel: 'web',
          listed_surface: 'chat',
          created_at: '2026-07-01T02:05:00Z',
        } as any}
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
        chatMessagesSessionId="new-session-2"
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

    expect(markup).not.toContain('Read-only');
    expect(markup).toContain('data-testid="session-composer"');
    expect(markup).toContain('aria-label="send"');
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
    expect(markup).toContain('Loading durable session history...');
    expect(markup).not.toContain('old-task-id');
    expect(markup).not.toContain('startConversation');
  });

  it('renders structured HR preview details for tool results', async () => {
    const markup = await renderWithLazyHrPreview(
      <StructuredToolResultBody
          toolName="preview_agent_blueprint"
          toolResult='{"status":"preview"}'
          toolMeta={{
            kind: 'hr_preview',
            blueprintId: 'draft-1',
            blueprintVersion: 1,
            blueprintHash: 'sha256:canonical',
            status: 'awaiting_confirmation',
            name: 'Research Bot',
            mission: 'Research competitors and write briefs.',
            firstMission: 'Create the first competitor landscape brief.',
            primaryUsers: ['Research team'],
            coreOutputs: ['Competitor brief'],
            boundaries: 'Cite sources.',
            permissionScope: 'company',
            sourceAttributions: [],
            riskClass: 'standard',
            missingGates: [],
            knowledgeDebt: [],
            confirmationRequirements: [],
            readyNow: ['Builtin tools + default skills + memory loop'],
            willInstall: ['mcp: github'],
            deferredCapabilities: ['github-research'],
            warnings: ['primary_users is empty — the agent may be less clear about who it serves.'],
            manualSteps: ['Validate the first deliverable before expanding capabilities.'],
          }}
      />,
    );

    expect(markup).toContain('Research Bot');
    expect(markup).toContain('Configuration &amp; sources');
    expect(markup).toContain('Included capabilities');
    expect(markup).toContain('Added during setup');
    expect(markup).toContain('Not enabled');
    expect(markup).toContain('Needs attention');
    expect(markup).toContain('Needs your attention');
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
    expect(markup).toContain('Nothing has run yet.');
    expect(markup).not.toContain('Call preview_workflow');
  });

  it('renders a durable Workflow preview with an exact confirm-and-run action', () => {
    const markup = renderToStaticMarkup(
      <StructuredToolResultBody
        agentId="agent-1"
        toolName="preview_workflow"
        toolResult='{"ok":true,"preview_id":"preview-1"}'
        toolMeta={{
          kind: 'workflow_preview',
          previewId: 'preview-1',
          sessionId: 'session-1',
          previewStatus: 'ready',
          proposalId: 'proposal-1',
          candidateId: 'fanout-critic',
          confirmationRequired: true,
          confirmationReasons: ['External effect'],
          plannedLeafCalls: 3,
          budgetTokens: 12000,
        }}
      />,
    );

    expect(markup).toContain('Workflow ready to run');
    expect(markup).toContain('3 planned work units');
    expect(markup).toContain('Why confirmation is needed');
    expect(markup).toContain('External effect');
    expect(markup).toContain('Confirm and run');
    expect(markup).not.toContain('preview-1');
    expect(markup).not.toContain('proposal-1');
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

  it('keeps a submitted clarification locked while transcript reconciliation temporarily reorders it after the answer', () => {
    const messages = [
      {
        role: 'user' as const,
        content: 'Scope: Mine',
        timestamp: '2026-08-29T00:14:00.000Z',
      },
      {
        role: 'tool_call' as const,
        content: '',
        timestamp: '2026-08-29T00:13:00.000Z',
        toolName: 'ask_user_question',
        toolStatus: 'done' as const,
        toolMeta: {
          kind: 'user_clarification' as const,
          questions: [{ question: 'Scope?', header: 'Scope', options: [{ label: 'Mine', description: '' }], multiSelect: false }],
          blocking: true,
          nextAction: null,
        },
      },
    ];

    expect(isClarificationCardAnsweredByLaterUserMessage(messages, 1)).toBe(true);
  });

  it('does not lock a clarification from an older user message', () => {
    const messages = [
      {
        role: 'user' as const,
        content: 'An unrelated earlier message',
        timestamp: '2026-08-29T00:12:00.000Z',
      },
      {
        role: 'tool_call' as const,
        content: '',
        timestamp: '2026-08-29T00:13:00.000Z',
        toolName: 'ask_user_question',
        toolStatus: 'done' as const,
        toolMeta: {
          kind: 'user_clarification' as const,
          questions: [{ question: 'Scope?', header: 'Scope', options: [{ label: 'Mine', description: '' }], multiSelect: false }],
          blocking: true,
          nextAction: null,
        },
      },
    ];

    expect(isClarificationCardAnsweredByLaterUserMessage(messages, 1)).toBe(false);
  });

  it('routes questions, workflow confirmations, and permission tools to dedicated inline cards', () => {
    expect(isInlineToolCardMessage({
      role: 'tool_call',
      content: '',
      toolName: 'ask_user_question',
      toolMeta: {
        kind: 'user_clarification',
        questions: [{ question: 'Scope?', header: 'Scope', options: [], multiSelect: false }],
        blocking: true,
        nextAction: null,
      },
    })).toBe(true);
    expect(isInlineToolCardMessage({
      role: 'tool_call',
      content: '',
      toolName: 'preview_workflow',
      toolMeta: {
        kind: 'workflow_preview',
        previewId: 'preview-1',
        sessionId: 'session-1',
        previewStatus: 'ready',
        proposalId: null,
        candidateId: null,
        confirmationRequired: true,
        confirmationReasons: [],
        plannedLeafCalls: 1,
        budgetTokens: 1000,
      },
    })).toBe(true);
    expect(isInlineToolCardMessage({
      role: 'tool_call',
      content: '',
      toolName: 'delete_file',
      sessionPermissionRequest: {
        permission_request_id: 'permission-1',
        tool_name: 'delete_file',
        arguments: {},
      },
    })).toBe(true);
    expect(isInlineToolCardMessage({
      role: 'tool_call',
      content: '',
      toolName: 'read_file',
      toolStatus: 'running',
    })).toBe(false);
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
        open_questions: [],
        authorization_scopes: [
          {
            action_kind: 'create_enabled_trigger',
            target_ref: 'internal:trigger:new:opaque-id',
            arguments: { secret_runtime_field: 'do-not-render' },
            summary: 'Create one weekday 09:00 industry brief schedule',
            max_uses: 1,
          },
        ],
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
    expect(markup).toContain('Approved actions');
    expect(markup).toContain('Create one weekday 09:00 industry brief schedule');
    expect(markup).toContain('single use');
    expect(markup).not.toContain('internal:trigger:new:opaque-id');
    expect(markup).not.toContain('secret_runtime_field');
    // Actionable while awaiting confirmation; confirmation should clearly start handoff.
    expect(markup).toContain('Implement this plan');
    expect(markup).toContain('Adjust plan');
    expect(markup).toContain('Ignore / exit plan');
    expect(markup).toContain('data-testid="plan-revision-composer"');
    expect(markup).toContain('data-testid="plan-reject-composer"');
    expect(markup).toContain('Tell the agent what to adjust');
    expect(markup).toContain('Reason for leaving Plan Mode');
  });

  it('renders PlanCard open questions as a clarification flow and disables implementation', () => {
    const plan = {
      id: 'plan-open-questions',
      agent_id: 'agent-1',
      tenant_id: null,
      session_id: null,
      runtime_task_id: null,
      requested_by_user_id: null,
      source: 'web_chat',
      intent_type: 'long_task',
      original_request: 'Prepare a detailed ABS report',
      status: 'awaiting_confirmation',
      plan_version: 1,
      plan_hash: 'sha256:open-questions',
      plan_markdown_path: null,
      plan_json: {
        title: 'ABS report plan',
        objective: 'Prepare the ABS report after scope is clarified.',
        open_questions: ['Should the report focus on China credit ABS or overseas CLO/LBO high-yield debt?'],
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

    expect(markup).toContain('data-testid="plan-clarification-required"');
    expect(markup).toContain('data-testid="plan-clarification-composer"');
    expect(markup).toContain('Should the report focus on China credit ABS or overseas CLO/LBO high-yield debt?');
    expect(markup).toContain('Answer questions');
    expect(markup).toContain('Send answers');
    expect(markup).toContain('data-testid="plan-implement-disabled"');
    expect(markup).toContain('disabled=""');
    expect(markup).toContain('Answer the open questions before implementing.');
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
    expect(skippedMarkup).toContain('Retry execution');
    expect(skippedMarkup).not.toContain('Run:');
  });
});

describe('rewind trim fallback (never a silent no-op)', () => {
  it('falls back to the checkpoint timestamp when live messages lack transcript event ids', async () => {
    const { trimMessagesBeforeTranscriptEvent } = await import('../AgentDetail');
    const messages = [
      { id: 'm1', role: 'user', content: 'old', timestamp: '2026-07-02T10:00:00Z' },
      { id: 'm2', role: 'assistant', content: 'old answer', timestamp: '2026-07-02T10:01:00Z' },
      { id: 'm3', role: 'user', content: 'rewound prompt', timestamp: '2026-07-02T10:05:00Z' },
      { id: 'm4', role: 'assistant', content: 'tail', timestamp: '2026-07-02T10:06:00Z' },
    ] as unknown as import('./chatRuntime').AgentChatMessage[];

    const trimmed = trimMessagesBeforeTranscriptEvent(messages, 'evt-missing', '2026-07-02T10:05:00Z');

    expect(trimmed.map((message) => message.id)).toEqual(['m1', 'm2']);
  });

  it('still prefers the exact transcript event anchor when present', async () => {
    const { trimMessagesBeforeTranscriptEvent } = await import('../AgentDetail');
    const messages = [
      { id: 'm1', transcriptEventId: 'evt-1', role: 'user', content: 'a', timestamp: '2026-07-02T09:00:00Z' },
      { id: 'm2', transcriptEventId: 'evt-2', role: 'user', content: 'b', timestamp: '2026-07-02T10:00:00Z' },
    ] as unknown as import('./chatRuntime').AgentChatMessage[];

    const trimmed = trimMessagesBeforeTranscriptEvent(messages, 'evt-2', '2026-07-02T09:30:00Z');

    expect(trimmed.map((message) => message.id)).toEqual(['m1']);
  });
});

describe('ActiveTailStatusLine (§3 seam 2)', () => {
  it('renders the phase label, tool detail, and a stopwatch', () => {
    const markup = renderToStaticMarkup(
      <ActiveTailStatusLine phase="tool_running" detail="write_file" startedAt={null} />,
    );
    expect(markup).toContain('data-phase="tool_running"');
    expect(markup).toContain('write_file');
    expect(markup).toContain('session-tui-active-tail-elapsed');
  });

  it('renders nothing for unknown phases (forward compatibility)', () => {
    const markup = renderToStaticMarkup(
      <ActiveTailStatusLine phase="warp_speed" detail={null} startedAt={null} />,
    );
    expect(markup).toBe('');
  });

  it('marks parked phases so the dot renders as a warning, not a spinner', () => {
    const markup = renderToStaticMarkup(
      <ActiveTailStatusLine phase="awaiting_approval" detail={null} startedAt={null} />,
    );
    expect(markup).toContain('data-phase="awaiting_approval"');
  });
});
