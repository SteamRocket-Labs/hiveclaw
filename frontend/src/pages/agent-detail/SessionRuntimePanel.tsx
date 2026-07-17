import React from 'react';
import { useTranslation } from 'react-i18next';
import { IconChevronLeft, IconChevronRight, IconFileText, IconX } from '@tabler/icons-react';

import { SessionGoalPanel } from '../session-workbench/SessionGoalPanel';
import { ThreadItemInspector } from '../session-workbench/ThreadItemInspector';
import {
  SessionAgentTeamCloseControl,
  SessionAgentTeamMemberControls,
} from '../session-workbench/SessionAgentTeamControls';
import {
  buildSessionRightPanelModel,
  buildWorkflowRunWindowModel,
  type RuntimeConsoleSegmentKey,
  type RuntimeConsoleWaiterModel,
  type RuntimeSectionItemModel,
  type WorkflowRunActionModel,
  type WorkspaceDocumentGroupModel,
  type WorkspaceDocumentModel,
} from '../session-workbench/timelineModel';
import type { ThreadItem } from '../../api/domains/threadItems.generated';
import type { SessionWorkbench } from '../../api/domains/ccParity';
import type { AgentChatMessage, ChatArtifactPart } from './chatRuntime';
import { artifactContributorLabel, formatArtifactSize } from './ArtifactSurface';

function isUuidLike(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

export type RuntimeRecord = Record<string, unknown>;

export function isRuntimeRecord(value: unknown): value is RuntimeRecord {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

export function sessionMetadata(session: any): Record<string, unknown> {
  const metadata = session?.transcript_metadata_json ?? session?.metadata_json ?? session?.metadata;
  return isRuntimeRecord(metadata) ? metadata : {};
}

export function isTeamMemberSession(session: any): boolean {
  if (!session) return false;
  const metadata = sessionMetadata(session);
  return (
    stringValue(session.session_kind).toLowerCase() === 'team_member'
    || stringValue(session.runtime_source).toLowerCase() === 'team_member'
    || stringValue(session.source_channel).toLowerCase() === 'agent_team'
    || Boolean(metadata.team_id && metadata.member_name)
  );
}

export function teamMemberSessionLabel(session: any): string {
  const metadata = sessionMetadata(session);
  return (
    stringValue(metadata.member_name)
    || stringValue(session.member_name)
    || stringValue(session.title).split('/').pop()?.trim()
    || stringValue(session.id)
  );
}

export function teamMemberRoleLabel(session: any): string {
  const metadata = sessionMetadata(session);
  return stringValue(metadata.member_role || session.member_role);
}

export function stringValue(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

export const RUNTIME_PHASE_LABEL_KEYS: Record<string, string> = {
  queued: 'agent.chat.phase.queued',
  resuming: 'agent.chat.phase.resuming',
  starting: 'agent.chat.phase.starting',
  thinking: 'agent.chat.phase.thinking',
  responding: 'agent.chat.phase.responding',
  tool_running: 'agent.chat.phase.tool_running',
  hook_evaluating: 'agent.chat.phase.hook_evaluating',
  compacting: 'agent.chat.phase.compacting',
  awaiting_approval: 'agent.chat.phase.awaiting_approval',
  awaiting_budget: 'agent.chat.phase.awaiting_budget',
  summarizing: 'agent.chat.phase.summarizing',
  continuation_gap: 'agent.chat.phase.continuation_gap',
};

export function formatElapsedSeconds(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0 ? `${minutes}m ${String(seconds).padStart(2, '0')}s` : `${seconds}s`;
}

/** §3 seam 2: the activeTail working-state line — phase label + live stopwatch. */
export function ActiveTailStatusLine({
  phase,
  detail,
  startedAt,
}: {
  phase: string;
  detail?: string | null;
  startedAt?: string | null;
}) {
  const { t } = useTranslation();
  const anchorRef = React.useRef<number | null>(null);
  if (anchorRef.current === null) {
    const parsed = startedAt ? Date.parse(startedAt) : NaN;
    anchorRef.current = Number.isFinite(parsed) ? parsed : Date.now();
  }
  const [nowTick, setNowTick] = React.useState<number>(() => Date.now());
  React.useEffect(() => {
    const timer = window.setInterval(() => setNowTick(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const labelKey = RUNTIME_PHASE_LABEL_KEYS[phase];
  if (!labelKey) return null;
  const elapsedSeconds = Math.max(0, Math.floor((nowTick - (anchorRef.current ?? nowTick)) / 1000));
  return (
    <div className="session-tui-active-tail" data-testid="active-tail-status" data-phase={phase}>
      <span className="session-tui-active-tail-dot" />
      <span className="session-tui-shimmer">{t(labelKey)}</span>
      {detail ? <span className="session-tui-active-tail-detail">{detail}</span> : null}
      <span className="session-tui-active-tail-elapsed">{formatElapsedSeconds(elapsedSeconds)}</span>
    </div>
  );
}

export const LINKED_HIGHLIGHT_CLASS = 'is-linked-highlight';

/** §3 seam 4: middle-stream <-> right-panel linking is pure DOM decoration —
 * zero React re-renders on hover (the remount-storm lesson). */
export function setRuntimeLinkHighlight(runtimeId: string | null | undefined, active: boolean): void {
  if (!runtimeId || typeof document === 'undefined') return;
  document
    .querySelectorAll(`[data-runtime-link-id="${CSS.escape(String(runtimeId))}"]`)
    .forEach((element) => element.classList.toggle(LINKED_HIGHLIGHT_CLASS, active));
}

export function scrollToRuntimeLinkMarker(runtimeId: string | null | undefined): void {
  if (!runtimeId || typeof document === 'undefined') return;
  const marker = document.querySelector(
    `.session-tui-render-cell[data-runtime-link-id="${CSS.escape(String(runtimeId))}"]`,
  );
  marker?.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

export function runtimeMetricSummary(item: RuntimeSectionItemModel): string {
  return [
    item.metrics.elapsedLabel,
    item.metrics.tokenLabel ? `${item.metrics.tokenLabel} tokens` : null,
    item.metrics.toolUseLabel ? `${item.metrics.toolUseLabel} tools` : null,
  ].filter(Boolean).join(' · ');
}

export function runtimeItemDisplayMeta(item: RuntimeSectionItemModel): string {
  const metadata = isRuntimeRecord(item.raw.metadata) ? item.raw.metadata : {};
  const role = stringValue(
    item.raw.member_role
      || item.raw.memberRole
      || item.raw.role
      || metadata.member_role
      || metadata.memberRole,
  ).trim();
  return [role, item.summary, runtimeMetricSummary(item)].filter(Boolean).join(' · ');
}

export function runtimeItemDisplayStatus(item: RuntimeSectionItemModel): string {
  const metadata = isRuntimeRecord(item.raw.metadata) ? item.raw.metadata : {};
  const status = stringValue(item.status || item.state || 'unknown').trim() || 'unknown';
  const closeStatus = item.runtimeKind === 'agent_team'
    ? stringValue(item.raw.close_status || item.raw.closeStatus).trim().toLowerCase()
    : '';
  const outcome = closeStatus === 'failed' ? 'close failed' : stringValue(
    item.raw.last_turn_status
      || item.raw.lastTurnStatus
      || metadata.last_turn_status
      || metadata.lastTurnStatus,
  ).trim();
  const safeStatus = userFacingRuntimeStatus(status);
  const safeOutcome = outcome ? userFacingRuntimeStatus(outcome) : '';
  return safeOutcome && safeOutcome !== safeStatus ? `${safeStatus} · ${safeOutcome}` : safeStatus;
}

export function subagentWorkerRecoveryModel(worker: RuntimeSectionItemModel): {
  canRequestNewWorker: boolean;
  requiresPlatformAdmin: boolean;
} {
  const status = String(worker.status || worker.state || '').trim().toLowerCase();
  const decision = isRuntimeRecord(worker.raw.subagent_decision_entry)
    ? worker.raw.subagent_decision_entry
    : {};
  const requiredUserAction = stringValue(decision.required_user_action).trim().toLowerCase();
  const requiresPlatformAdmin = status === 'needs_reconciliation'
    || worker.userBlocker?.kind === 'runtime_reconciliation'
    || requiredUserAction === 'approve_reconciliation_retry';
  return {
    canRequestNewWorker: !requiresPlatformAdmin && status === 'failed',
    requiresPlatformAdmin,
  };
}

export function runtimeItemDisplayLabel(item: RuntimeSectionItemModel, fallback: string): string {
  const label = stringValue(item.label).trim();
  if (!label || label === item.id || isUuidLike(label)) return fallback;
  return label;
}

export function userFacingRuntimeStatus(status: unknown): string {
  const normalized = String(status || '').trim().toLowerCase();
  const labels: Record<string, string> = {
    idle: 'Ready',
    ready: 'Ready',
    queued: 'Queued',
    pending: 'Queued',
    running: 'Working',
    streaming: 'Working',
    resuming: 'Resuming',
    waiting: 'Waiting',
    waiting_user: 'Waiting for you',
    awaiting_approval: 'Waiting for approval',
    waiting_budget_approval: 'Waiting for approval',
    completed: 'Completed',
    succeeded: 'Completed',
    failed: 'Needs attention',
    blocked: 'Needs attention',
    provider_error: 'Needs attention',
    not_admitted: 'Skipped',
    skipped: 'Skipped',
    suspended: 'Paused',
    killed: 'Stopped',
    cancelled: 'Cancelled',
    exhausted: 'Paused',
    hard_stopped: 'Stopped',
    needs_reconciliation: 'Needs admin review',
  };
  const exact = labels[normalized];
  if (exact) return exact;
  if (normalized.includes('approval') || normalized.includes('confirm') || normalized.includes('wait')) return 'Waiting';
  if (normalized.includes('fail') || normalized.includes('error') || normalized.includes('reconcil')) return 'Needs attention';
  if (normalized.includes('complete') || normalized.includes('success') || normalized.includes('done')) return 'Completed';
  if (normalized.includes('cancel') || normalized.includes('stop') || normalized.includes('kill')) return 'Stopped';
  if (normalized.includes('queue') || normalized.includes('pending')) return 'Queued';
  return 'Working';
}

export function SessionRuntimePanel({
  messages,
  sessionWorkbench,
  activeSession,
  agent,
  activeRunStatus,
  collapsed = false,
  onToggleCollapsed,
  onOpenDocument,
  onSelectSession,
  onSelectWorkflowRun,
  selectedThreadItem,
  onClearSelectedThreadItem,
  agentId,
  sessionId,
  onGoalChanged,
  onTeamChanged,
  onRetrySubagent,
}: {
  messages: AgentChatMessage[];
  sessionWorkbench: SessionWorkbench | null;
  activeSession?: Record<string, unknown> | null;
  agent?: Record<string, unknown> | null;
  activeRunStatus?: string | null;
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
  onOpenDocument?: (artifact: ChatArtifactPart) => void | Promise<unknown>;
  onSelectSession?: (sessionId: string) => void | Promise<unknown>;
  onSelectWorkflowRun?: (workflow: RuntimeSectionItemModel) => void | Promise<unknown>;
  selectedThreadItem?: ThreadItem | null;
  onClearSelectedThreadItem?: () => void;
  agentId?: string;
  sessionId?: string;
  onGoalChanged?: () => void | Promise<unknown>;
  onTeamChanged?: () => void | Promise<unknown>;
  onRetrySubagent?: (worker: RuntimeSectionItemModel) => void | Promise<unknown>;
}) {
  const RUNTIME_PANEL_WIDTH_KEY = 'hive.sessionRuntimePanel.width';
  const [panelWidth, setPanelWidth] = React.useState<number | null>(() => {
    try {
      const stored = window.localStorage.getItem(RUNTIME_PANEL_WIDTH_KEY);
      const parsed = stored ? Number.parseInt(stored, 10) : NaN;
      return Number.isFinite(parsed) ? Math.min(560, Math.max(360, parsed)) : null;
    } catch {
      return null;
    }
  });
  const [resizeDragging, setResizeDragging] = React.useState(false);
  const startPanelResize = React.useCallback((event: React.MouseEvent<HTMLDivElement>) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = (event.currentTarget.parentElement as HTMLElement | null)?.getBoundingClientRect().width ?? 360;
    setResizeDragging(true);
    const onMove = (move: MouseEvent) => {
      const next = Math.min(560, Math.max(360, Math.round(startWidth + (startX - move.clientX))));
      setPanelWidth(next);
    };
    const onUp = (up: MouseEvent) => {
      const finalWidth = Math.min(560, Math.max(360, Math.round(startWidth + (startX - up.clientX))));
      setPanelWidth(finalWidth);
      try {
        window.localStorage.setItem(RUNTIME_PANEL_WIDTH_KEY, String(finalWidth));
      } catch {
        // persistence is best-effort
      }
      setResizeDragging(false);
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }, []);

  const { t } = useTranslation();
  const rightPanel = buildSessionRightPanelModel({
    messages,
    sessionWorkbench: sessionWorkbench as Record<string, unknown> | null,
    activeSession,
    activeRunStatus,
  });
  const docs = rightPanel.workspaceDocuments;
  const currentSessionDocumentCount = docs.currentSession.items.length;
  const runtimeConsole = rightPanel.runtimeConsole;
  const [showAllCurrentDocuments, setShowAllCurrentDocuments] = React.useState(false);
  const [runtimeSegmentOverride, setRuntimeSegmentOverride] = React.useState<RuntimeConsoleSegmentKey | null>(null);
  const selectedRuntimeSegment: RuntimeConsoleSegmentKey = runtimeSegmentOverride && runtimeConsole.segments.some((segment) => segment.key === runtimeSegmentOverride)
    ? runtimeSegmentOverride
    : runtimeConsole.defaultSegment;
  const runStatusCount = runtimeConsole.summary.runningCount > 0
    ? t('sessionWorkbench.rightPanel.runningCount', '{{count}} running', { count: runtimeConsole.summary.runningCount })
    : t('sessionWorkbench.rightPanel.totalCount', '{{count}} total', { count: runtimeConsole.summary.totalCount });
  const runtimeWaiterTestId = (waiter: RuntimeConsoleWaiterModel): string => (
    `session-runtime-waiter-${waiter.id.replace(/[^a-zA-Z0-9_-]/g, '-')}`
  );

  const renderRuntimeItem = (item: RuntimeSectionItemModel, fallback: string) => {
    const sessionId = item.childSessionId;
    const clickable = Boolean(item.enterable && sessionId && onSelectSession);
    const meta = runtimeItemDisplayMeta(item);
    const content = (
      <>
        <span className="session-runtime-row-main">
          <span className="session-runtime-row-title">{runtimeItemDisplayLabel(item, fallback)}</span>
          <span className="session-runtime-row-meta">{meta || t('sessionWorkbench.rightPanel.noAdditionalDetails', 'No additional details')}</span>
        </span>
        <span className="session-runtime-status">{runtimeItemDisplayStatus(item)}</span>
      </>
    );
    const linkId = item.childSessionId || item.id;
    const linkProps = {
      'data-runtime-link-id': linkId,
      onMouseEnter: () => setRuntimeLinkHighlight(linkId, true),
      onMouseLeave: () => setRuntimeLinkHighlight(linkId, false),
    } as const;
    return clickable ? (
      <button
        key={item.id}
        type="button"
        className="session-runtime-row session-runtime-row-button"
        onClick={() => sessionId && onSelectSession?.(sessionId)}
        {...linkProps}
      >
        {content}
      </button>
    ) : (
      <div
        key={item.id}
        className="session-runtime-row"
        {...linkProps}
        onClick={() => scrollToRuntimeLinkMarker(linkId)}
      >
        {content}
      </div>
    );
  };

  const renderRuntimeWaiter = (waiter: RuntimeConsoleWaiterModel) => {
    const sessionId = waiter.childSessionId;
    const clickable = Boolean(waiter.enterable && sessionId && onSelectSession);
    const segmentLabel = t(`sessionWorkbench.rightPanel.runtimeSegments.${waiter.segment}`, waiter.segment);
    const blocker = waiter.userBlocker;
    const meta = blocker
      ? [blocker.reason, blocker.nextAction].filter(Boolean).join(' ')
      : [waiter.summary, segmentLabel].filter(Boolean).join(' · ');
    const content = (
      <>
        <span className="session-runtime-row-main">
          <span className="session-runtime-row-title">{blocker?.title || waiter.label}</span>
          <span className="session-runtime-row-meta">{meta || waiter.summary || waiter.label}</span>
        </span>
        <span className="session-runtime-status">{userFacingRuntimeStatus(waiter.status || 'waiting')}</span>
      </>
    );
    return clickable ? (
      <button
        key={`${waiter.segment}:${waiter.id}`}
        type="button"
        data-testid={runtimeWaiterTestId(waiter)}
        data-runtime-waiter-segment={waiter.segment}
        className="session-runtime-row session-runtime-row-button"
        title={t('sessionWorkbench.rightPanel.waiterOpenTitle', 'Open waiting session')}
        onClick={() => sessionId && onSelectSession?.(sessionId)}
      >
        {content}
      </button>
    ) : (
      <div
        key={`${waiter.segment}:${waiter.id}`}
        data-testid={runtimeWaiterTestId(waiter)}
        data-runtime-waiter-segment={waiter.segment}
        className="session-runtime-row"
      >
        {content}
      </div>
    );
  };

  const renderWorkflowRoot = (workflow: RuntimeSectionItemModel, fallback: string) => {
    const meta = runtimeItemDisplayMeta(workflow);
    const content = (
      <>
        <span className="session-runtime-row-main">
          <span className="session-runtime-row-title">{runtimeItemDisplayLabel(workflow, fallback)}</span>
          <span className="session-runtime-row-meta">{meta}</span>
        </span>
        <span className="session-runtime-status">{runtimeItemDisplayStatus(workflow)}</span>
      </>
    );
    return onSelectWorkflowRun ? (
      <button
        key={`${workflow.id}:root`}
        type="button"
        data-testid="session-runtime-workflow-open"
        data-runtime-action="open-workflow-run"
        className="session-runtime-row session-runtime-row-button"
        onClick={() => onSelectWorkflowRun(workflow)}
      >
        {content}
      </button>
    ) : (
      <div key={`${workflow.id}:root`} className="session-runtime-row">
        {content}
      </div>
    );
  };

  const renderTeamMemberItem = (
    team: RuntimeSectionItemModel,
    member: RuntimeSectionItemModel,
    fallback: string,
  ) => {
    const sessionId = member.childSessionId;
    const meta = runtimeItemDisplayMeta(member);
    return (
      <div key={member.id || fallback} className="session-runtime-row" data-testid="session-agent-team-member-row">
        <span className="session-runtime-row-main">
          <span className="session-runtime-row-title">{member.label || fallback}</span>
          <span className="session-runtime-row-meta">{meta}</span>
        </span>
        <span className="session-runtime-status">{runtimeItemDisplayStatus(member)}</span>
        {agentId ? (
          <SessionAgentTeamMemberControls
            agentId={agentId}
            teamId={team.id}
            teamStatus={team.status}
            member={member}
            onEnter={sessionId && onSelectSession ? () => onSelectSession(sessionId) : undefined}
            onChanged={onTeamChanged}
          />
        ) : null}
      </div>
    );
  };

  const renderSubagentWorkerItem = (worker: RuntimeSectionItemModel, fallback: string) => {
    const sessionId = worker.childSessionId;
    const canInspect = Boolean(sessionId && onSelectSession);
    const meta = runtimeItemDisplayMeta(worker);
    const recovery = subagentWorkerRecoveryModel(worker);
    const workerAction = (
      action: 'inspect' | 'retry',
      label: string,
      disabled: boolean,
      title: string,
      onClick?: () => void,
    ) => (
      <button
        key={action}
        type="button"
        data-runtime-action={`subagent-worker-${action}`}
        className="session-runtime-action-button"
        disabled={disabled}
        title={title}
        onClick={onClick}
      >
        {label}
      </button>
    );
    return (
      <div key={worker.id || fallback} className="session-runtime-row" data-testid="session-subagent-worker-row">
        <span className="session-runtime-row-main">
          <span className="session-runtime-row-title">{worker.label || fallback}</span>
          <span className="session-runtime-row-meta">{meta}</span>
        </span>
        <span className="session-runtime-status">{runtimeItemDisplayStatus(worker)}</span>
        <span className="session-runtime-actions" data-testid="session-subagent-worker-actions">
          {workerAction(
            'inspect',
            t('sessionWorkbench.rightPanel.workerInspect', 'Inspect'),
            !canInspect,
            canInspect
              ? t('sessionWorkbench.rightPanel.workerInspectTitle', 'Inspect worker session')
              : t('sessionWorkbench.rightPanel.workerInspectDisabled', 'No worker session is available'),
            canInspect && sessionId ? () => onSelectSession?.(sessionId) : undefined,
          )}
          {recovery.canRequestNewWorker
            ? workerAction(
                'retry',
                t('sessionWorkbench.rightPanel.workerRetry', 'Retry with new worker'),
                !onRetrySubagent,
                t(
                  'sessionWorkbench.rightPanel.workerRetryTitle',
                  'Ask the main Agent to inspect the failure and create a new one-shot worker if safe',
                ),
                onRetrySubagent ? () => void onRetrySubagent(worker) : undefined,
              )
            : null}
        </span>
      </div>
    );
  };

  const renderWorkflowSegment = () => (
    <div data-testid="session-runtime-segment-body-workflow" className="session-runtime-segment-body" role="tabpanel">
      {runtimeConsole.workflow.items.length === 0 ? (
        <div className="session-runtime-empty">{t('sessionWorkbench.rightPanel.noWorkflows', 'No active workflows.')}</div>
      ) : (
        runtimeConsole.workflow.items.map((workflow, index) => (
          <div key={workflow.id} className="session-runtime-team">
            {renderWorkflowRoot(workflow, `workflow-${index + 1}`)}
            {workflow.steps.map((step, stepIndex) => renderRuntimeItem(step, `workflow-step-${stepIndex + 1}`))}
            {workflow.leafCalls.map((leafCall, leafIndex) => renderRuntimeItem(leafCall, `workflow-leaf-${leafIndex + 1}`))}
          </div>
        ))
      )}
    </div>
  );

  const renderTeamSegment = () => (
    <div data-testid="session-runtime-segment-body-team" className="session-runtime-segment-body" role="tabpanel">
      {runtimeConsole.team.items.length === 0 ? (
        <div className="session-runtime-empty">{t('sessionWorkbench.rightPanel.noAgentTeams', 'No Agent Team containers in this session.')}</div>
      ) : (
        runtimeConsole.team.items.map((team) => (
          <div key={team.id} className="session-runtime-team">
            <div className="session-runtime-team-header">
              <span>{runtimeItemDisplayLabel(team, t('sessionWorkbench.rightPanel.agentTeam', 'Agent Team'))}</span>
              <span className="session-agent-team-close-control">
                <small>{runtimeItemDisplayStatus(team)}</small>
                {agentId ? (
                  <SessionAgentTeamCloseControl agentId={agentId} team={team} onChanged={onTeamChanged} />
                ) : null}
              </span>
            </div>
            {team.members.length === 0 ? (
              <div className="session-runtime-empty">
                {t('sessionWorkbench.rightPanel.noTeamMembers', 'No team members have started yet.')}
              </div>
            ) : (
              team.members.map((member, index) => renderTeamMemberItem(team, member, `team-member-${index + 1}`))
            )}
          </div>
        ))
      )}
    </div>
  );

  const renderA2ASegment = () => (
    <div data-testid="session-runtime-segment-body-a2a" className="session-runtime-segment-body" role="tabpanel">
      {runtimeConsole.peerA2A.items.length === 0 ? (
        <div className="session-runtime-empty">
          {t('sessionWorkbench.rightPanel.noPeerA2A', 'No peer digital-employee delegations in this session.')}
        </div>
      ) : (
        runtimeConsole.peerA2A.items.map((delegation, index) => renderRuntimeItem(delegation, `peer-a2a-${index + 1}`))
      )}
    </div>
  );

  const renderWorkersSegment = () => (
    <div data-testid="session-runtime-segment-body-workers" className="session-runtime-segment-body" role="tabpanel">
      {runtimeConsole.workers.items.length === 0 ? (
        <div className="session-runtime-empty">{t('sessionWorkbench.rightPanel.noSubagents', 'No one-shot Sub-agent workers in this session.')}</div>
      ) : (
        runtimeConsole.workers.items.map((worker, index) => renderSubagentWorkerItem(worker, `subagent-worker-${index + 1}`))
      )}
    </div>
  );

  const renderActivityBucket = (
    testId: string,
    title: string,
    items: RuntimeSectionItemModel[],
    empty: string,
  ) => {
    return (
      <div data-testid={testId} className="session-runtime-activity-bucket">
        <div className="session-runtime-card-title">{title}</div>
        {items.length === 0 ? (
          <div className="session-runtime-empty">{empty}</div>
        ) : (
          items.map((item, index) => renderRuntimeItem(item, `${title}-${index + 1}`))
        )}
      </div>
    );
  };

  const renderActivitySegment = () => (
    <div data-testid="session-runtime-segment-body-activity" className="session-runtime-segment-body" role="tabpanel">
      {renderActivityBucket(
        'session-runtime-activity-background',
        t('sessionWorkbench.rightPanel.backgroundAgents', 'Background agents'),
        runtimeConsole.activity.background,
        t('sessionWorkbench.rightPanel.noBackgroundAgents', 'No background agents running.'),
      )}
      {renderActivityBucket(
        'session-runtime-activity-notifications',
        t('sessionWorkbench.rightPanel.notifications', 'Notifications'),
        runtimeConsole.activity.notifications,
        t('sessionWorkbench.rightPanel.noCompletionWakes', 'No completion notifications.'),
      )}
      {renderActivityBucket(
        'session-runtime-activity-runs',
        t('sessionWorkbench.rightPanel.runs', 'Runs'),
        runtimeConsole.activity.runs,
        t('sessionWorkbench.rightPanel.noRuns', 'No runtime runs recorded.'),
      )}
    </div>
  );

  const renderRuntimeConsoleBody = () => {
    if (selectedRuntimeSegment === 'team') return renderTeamSegment();
    if (selectedRuntimeSegment === 'a2a') return renderA2ASegment();
    if (selectedRuntimeSegment === 'workers') return renderWorkersSegment();
    if (selectedRuntimeSegment === 'workflow') return renderWorkflowSegment();
    return renderActivitySegment();
  };

  const renderDocumentRow = (doc: WorkspaceDocumentModel) => {
    const contributorLabel = artifactContributorLabel(doc.artifact, agent);
    return (
      <button
        key={doc.key}
        type="button"
        className="session-runtime-doc-row"
        onClick={() => onOpenDocument?.(doc.artifact)}
      >
        <IconFileText size={15} />
        <span>
          <strong>{doc.name}</strong>
          <small>{[doc.previewKind, doc.status, formatArtifactSize(doc.size)].filter(Boolean).join(' · ')}</small>
          {contributorLabel ? (
            <em className="session-runtime-doc-author">
              {t('sessionWorkbench.rightPanel.documentAuthor', 'By {{name}}', { name: contributorLabel })}
            </em>
          ) : null}
        </span>
      </button>
    );
  };

  const renderDocumentGroup = (group: WorkspaceDocumentGroupModel, testId: string) => {
    if (group.items.length === 0) return null;
    const title = t(`sessionWorkbench.rightPanel.documentGroups.${group.key}`, group.title);
    const currentDocumentVisibleLimit = 5;
    const shouldLimitCurrentGroup = group.key === 'currentSession' && !showAllCurrentDocuments;
    const visibleItems = shouldLimitCurrentGroup ? group.items.slice(0, currentDocumentVisibleLimit) : group.items;
    const hiddenCount = group.items.length - visibleItems.length;
    const body = (
      <div className="session-runtime-list">
        {visibleItems.map(renderDocumentRow)}
        {hiddenCount > 0 ? (
          <button
            type="button"
            className="session-runtime-view-all"
            onClick={() => setShowAllCurrentDocuments(true)}
          >
            {t('sessionWorkbench.rightPanel.viewAllCurrentArtifacts', 'View all {{count}} current artifacts', { count: group.items.length })}
          </button>
        ) : null}
      </div>
    );
    if (group.collapsedByDefault) {
      return (
        <details key={group.key} data-testid={testId} className="session-runtime-doc-group">
          <summary>
            <span>{title}</span>
            <strong>{group.items.length}</strong>
          </summary>
          {body}
        </details>
      );
    }
    return (
      <div key={group.key} data-testid={testId} className="session-runtime-doc-group is-open">
        <div className="session-runtime-doc-group-title">
          <span>{title}</span>
          <strong>{group.items.length}</strong>
        </div>
        {body}
      </div>
    );
  };

  const sessionWindow = rightPanel.sessionWindow;

  if (collapsed) {
    return (
      <aside data-testid="session-runtime-panel" className="session-runtime-panel is-collapsed">
        <button
          type="button"
          data-testid="session-runtime-collapse-toggle"
          className="session-runtime-collapse-toggle"
          aria-label={t('sessionWorkbench.rightPanel.expandRuntime', 'Expand runtime panel')}
          title={t('sessionWorkbench.rightPanel.expandRuntime', 'Expand runtime panel')}
          onClick={onToggleCollapsed}
        >
          <IconChevronLeft size={15} stroke={1.8} />
        </button>
        <div className="session-runtime-collapsed-label">
          {t('sessionWorkbench.rightPanel.workspace', 'Workspace')}
        </div>
      </aside>
    );
  }

  return (
    <aside
      data-testid="session-runtime-panel"
      className="session-runtime-panel"
      style={panelWidth ? { flexBasis: `${panelWidth}px`, maxWidth: `${panelWidth}px` } : undefined}
    >
      <div
        data-testid="session-runtime-resize-handle"
        className={`session-runtime-resize-handle${resizeDragging ? ' is-dragging' : ''}`}
        onMouseDown={startPanelResize}
        role="separator"
        aria-orientation="vertical"
        aria-label={t('sessionWorkbench.rightPanel.resize', 'Resize runtime panel')}
      />
      <button
        type="button"
        data-testid="session-runtime-collapse-toggle"
        className="session-runtime-collapse-toggle"
        aria-label={t('sessionWorkbench.rightPanel.collapseRuntime', 'Collapse runtime panel')}
        title={t('sessionWorkbench.rightPanel.collapseRuntime', 'Collapse runtime panel')}
        onClick={onToggleCollapsed}
      >
        <IconChevronRight size={15} stroke={1.8} />
      </button>
      <section
        data-testid="session-runtime-deliverables"
        className={`session-runtime-section session-runtime-documents${currentSessionDocumentCount === 0 ? ' is-empty' : ''}`}
        aria-label={t('sessionWorkbench.rightPanel.sessionArtifacts', 'Deliverables')}
      >
        <div className="session-runtime-section-header">
          <div>
            <div className="session-tui-kicker">{t('sessionWorkbench.rightPanel.session', 'Session')}</div>
            <h3>{t('sessionWorkbench.rightPanel.sessionArtifacts', 'Deliverables')}</h3>
          </div>
          <span>{currentSessionDocumentCount}</span>
        </div>
        {currentSessionDocumentCount === 0 ? (
          <div className="session-runtime-empty">
            {t('sessionWorkbench.rightPanel.noSessionArtifacts', 'No delivered artifacts in this session yet.')}
          </div>
        ) : (
          <div className="session-runtime-doc-groups">
            {renderDocumentGroup(docs.currentSession, 'session-workspace-documents-current')}
          </div>
        )}
      </section>

      <div data-testid="session-runtime-divider" className="session-runtime-divider" aria-hidden="true" />

      <section
        data-testid="session-runtime-run-status"
        className="session-runtime-section session-runtime-lower"
        aria-label={t('sessionWorkbench.rightPanel.runStatus', 'Run status')}
      >
        <div className="session-runtime-section-header">
          <div>
            <div className="session-tui-kicker">{t('sessionWorkbench.rightPanel.session', 'Session')}</div>
            <h3>{t('sessionWorkbench.rightPanel.runStatus', 'Run status')}</h3>
          </div>
          <span>{runStatusCount}</span>
        </div>

        {agentId && sessionId && sessionWorkbench?.goals?.length ? (
          <SessionGoalPanel
            agentId={agentId}
            sessionId={sessionId}
            goals={sessionWorkbench.goals}
            onChanged={onGoalChanged}
          />
        ) : null}

        <div data-testid="session-runtime-console" className="session-runtime-console">
          <div
            data-testid="session-runtime-summary-strip"
            className="session-runtime-summary-strip"
            data-runtime-state={runtimeConsole.summary.state}
          >
            <div className="session-runtime-summary-main">
              <span className="session-runtime-summary-dot" aria-hidden="true" />
              <span>
                <strong>
                  {t(
                    `sessionWorkbench.rightPanel.runtimeStates.${runtimeConsole.summary.state}`,
                    userFacingRuntimeStatus(runtimeConsole.summary.state),
                  )}
                </strong>
                <small>
                  {sessionWindow
                    ? [sessionWindow.label, userFacingRuntimeStatus(sessionWindow.status)].filter(Boolean).join(' · ')
                    : t('sessionWorkbench.rightPanel.noMainSession', 'No selected session')}
                </small>
              </span>
            </div>
            <div className="session-runtime-summary-metrics">
              <span>{t('sessionWorkbench.rightPanel.runningCount', '{{count}} running', { count: runtimeConsole.summary.runningCount })}</span>
              <span>{t('sessionWorkbench.rightPanel.waitingCount', '{{count}} waiting', { count: runtimeConsole.summary.waitingCount })}</span>
              <span>{runtimeConsole.summary.elapsedLabel || '-'}</span>
              <span>{runtimeConsole.summary.tokenLabel || '-'}</span>
              <span>{runtimeConsole.summary.toolUseLabel || '-'}</span>
            </div>
          </div>

          {runtimeConsole.waiters.length > 0 && (
            <div
              data-testid="session-runtime-waiters"
              className="session-runtime-waiters"
              aria-label={t('sessionWorkbench.rightPanel.waiters', 'Waiting items')}
            >
              {runtimeConsole.waiters.map(renderRuntimeWaiter)}
            </div>
          )}

          <div className="session-runtime-segmented" role="tablist" aria-label={t('sessionWorkbench.rightPanel.runtimeConsole', 'Runtime Console')}>
            {runtimeConsole.segments.map((segment) => {
              const selected = selectedRuntimeSegment === segment.key;
              return (
                <button
                  key={segment.key}
                  type="button"
                  role="tab"
                  aria-selected={selected}
                  data-testid={`session-runtime-segment-${segment.key}`}
                  className={`session-runtime-segment${selected ? ' is-active' : ''}`}
                  onClick={() => setRuntimeSegmentOverride(segment.key)}
                >
                  <span>{t(`sessionWorkbench.rightPanel.runtimeSegments.${segment.key}`, segment.label)}</span>
                  <strong>{segment.count}</strong>
                </button>
              );
            })}
          </div>

          {renderRuntimeConsoleBody()}
        </div>
      </section>
      {selectedThreadItem?.audience === 'operator' && selectedThreadItem.operator_details ? (
        <div className="session-technical-drawer" role="dialog" aria-modal="false">
          <ThreadItemInspector item={selectedThreadItem} onClose={onClearSelectedThreadItem} />
        </div>
      ) : null}
    </aside>
  );
}

export function WorkflowRunFocusPanel({
  workflow,
  onClose,
  onSelectSession,
  onWorkflowAction,
}: {
  workflow: RuntimeSectionItemModel;
  onClose: () => void;
  onSelectSession?: (sessionId: string) => void | Promise<unknown>;
  onWorkflowAction?: (action: WorkflowRunActionModel, workflow: RuntimeSectionItemModel) => void | Promise<unknown>;
}) {
  const { t } = useTranslation();
  const windowModel = buildWorkflowRunWindowModel(workflow);
  const leafCalls = windowModel.leafCalls || [];
  const steps = windowModel.steps || [];
  const meta = [
    windowModel.meta,
    windowModel.metrics.elapsedLabel,
    windowModel.metrics.tokenLabel ? `${windowModel.metrics.tokenLabel} tokens` : '',
    windowModel.metrics.toolUseLabel ? `${windowModel.metrics.toolUseLabel} tools` : '',
  ].filter(Boolean).join(' · ');
  const controls = windowModel.controls;
  const actionLabel = (action: WorkflowRunActionModel['action']) => {
    if (action === 'approve_gate') return t('sessionWorkbench.workflowRunWindow.approveGate', 'Approve');
    if (action === 'reject_gate') return t('sessionWorkbench.workflowRunWindow.rejectGate', 'Reject');
    if (action === 'repair') return t('sessionWorkbench.workflowRunWindow.repair', 'Repair');
    if (action === 'cancel') return t('sessionWorkbench.workflowRunWindow.cancel', 'Cancel');
    return t('sessionWorkbench.workflowRunWindow.promote', 'Submit for approval');
  };
  const renderWorkflowAction = (action: WorkflowRunActionModel) => (
    <button
      key={action.action}
      type="button"
      data-testid={`session-workflow-action-${action.action}`}
      data-workflow-action={action.action}
      disabled={!action.enabled || !onWorkflowAction}
      title={action.reason || actionLabel(action.action)}
      onClick={() => void onWorkflowAction?.(action, workflow)}
      className="session-workflow-action-button"
    >
      {actionLabel(action.action)}
    </button>
  );
  const renderStep = (step: RuntimeSectionItemModel, index: number) => (
    <div
      key={step.id || `step-${index}`}
      data-testid="session-workflow-step-row"
      className="session-runtime-row"
    >
      <span className="session-runtime-row-main">
        <span className="session-runtime-row-title">
          {step.label || t('sessionWorkbench.workflowRunWindow.stepFallback', 'Step {{index}}', { index: index + 1 })}
        </span>
        <span className="session-runtime-row-meta">
          {step.summary || t('sessionWorkbench.workflowRunWindow.noStepDetails', 'No additional details')}
        </span>
      </span>
      <span className="session-runtime-status">{userFacingRuntimeStatus(step.status)}</span>
    </div>
  );
  const renderLeaf = (leaf: RuntimeSectionItemModel, index: number) => {
    const canEnter = Boolean(leaf.enterable && leaf.childSessionId && onSelectSession);
    const content = (
      <>
        <span className="session-runtime-row-main">
          <span className="session-runtime-row-title">
            {leaf.label || t('sessionWorkbench.workflowRunWindow.leafFallback', 'Leaf {{index}}', { index: index + 1 })}
          </span>
          <span className="session-runtime-row-meta">
            {leaf.summary || t('sessionWorkbench.workflowRunWindow.noLeafDetails', 'No additional details')}
          </span>
        </span>
        <span className="session-runtime-status">{userFacingRuntimeStatus(leaf.status)}</span>
      </>
    );
    return canEnter ? (
      <button
        key={leaf.id || `leaf-${index}`}
        type="button"
        data-testid="session-workflow-leaf-enter"
        className="session-runtime-row session-runtime-row-button"
        onClick={() => leaf.childSessionId && onSelectSession?.(leaf.childSessionId)}
      >
        {content}
      </button>
    ) : (
      <div
        key={leaf.id || `leaf-${index}`}
        data-testid="session-workflow-leaf-detail"
        className="session-runtime-row"
      >
        {content}
      </div>
    );
  };

  return (
    <section
      data-testid="session-workflow-run-window"
      className="session-runtime-section session-runtime-lower"
      aria-label={t('sessionWorkbench.workflowRunWindow.title', 'Workflow Run Window')}
    >
      <div className="session-runtime-section-header">
        <div>
          <div className="session-tui-kicker">
            {t('sessionWorkbench.workflowRunWindow.breadcrumb', windowModel.breadcrumb)}
          </div>
          <h3>{windowModel.label}</h3>
          {meta ? <small>{meta}</small> : null}
        </div>
        <button
          type="button"
          className="session-runtime-collapse-toggle"
          aria-label={t('sessionWorkbench.workflowRunWindow.close', 'Close workflow run')}
          title={t('sessionWorkbench.workflowRunWindow.close', 'Close workflow run')}
          onClick={onClose}
        >
          <IconX size={15} stroke={1.8} />
        </button>
      </div>

      <div className="session-runtime-card" data-testid="session-workflow-controls">
        <div className="session-runtime-card-title">
          {t('sessionWorkbench.workflowRunWindow.controls', 'Controls')}
        </div>
        <div data-testid="session-workflow-gate-status" className="session-runtime-metric-row">
          <span>{t('sessionWorkbench.workflowRunWindow.gateStatus', 'Gate')}</span>
          <strong>{controls.gateStatus}</strong>
        </div>
        <div data-testid="session-workflow-wait-status" className="session-runtime-metric-row">
          <span>{t('sessionWorkbench.workflowRunWindow.waitStatus', 'Wait')}</span>
          <strong>{controls.waitStatus}</strong>
        </div>
        <div className="session-workflow-action-row">
          {controls.actions.length === 0 ? (
            <span className="session-runtime-empty">
              {t('sessionWorkbench.workflowRunWindow.noActions', 'No workflow actions available.')}
            </span>
          ) : (
            controls.actions.map(renderWorkflowAction)
          )}
        </div>
      </div>

      <div className="session-runtime-card">
        <div className="session-runtime-card-title">
          {t('sessionWorkbench.workflowRunWindow.steps', 'Steps')}
        </div>
        {steps.length === 0 ? (
          <div className="session-runtime-empty">
            {t('sessionWorkbench.workflowRunWindow.noSteps', 'No workflow steps recorded.')}
          </div>
        ) : (
          steps.map(renderStep)
        )}
      </div>

      <div className="session-runtime-card">
        <div className="session-runtime-card-title">
          {t('sessionWorkbench.workflowRunWindow.leafCalls', 'Leaf calls')}
        </div>
        {leafCalls.length === 0 ? (
          <div className="session-runtime-empty">
            {t('sessionWorkbench.workflowRunWindow.noLeafCalls', 'No leaf calls recorded.')}
          </div>
        ) : (
          leafCalls.map(renderLeaf)
        )}
      </div>
    </section>
  );
}
