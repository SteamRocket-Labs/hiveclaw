import type { SessionIndex } from '../../api/domains/chat';
import {
  buildRunTimelineFromMessages,
  isDisclosureStepMessage,
  type RunTimelineSnapshot,
} from '../agent-detail/chatDisclosureReducer';
import type { AgentChatMessage, ChatRuntimeSummary } from '../agent-detail/chatRuntime';

export type ThreadTimelineCell =
  | {
      kind: 'user_turn';
      id: string;
      message: AgentChatMessage;
      index: number;
    }
  | {
      kind: 'assistant_final';
      id: string;
      message: AgentChatMessage;
      index: number;
    }
  | {
      kind: 'active_run';
      id: string;
      timeline: RunTimelineSnapshot;
      sourceMessages: Array<{ message: AgentChatMessage; index: number }>;
      answer?: AgentChatMessage;
      answerIndex?: number;
    }
  | {
      kind: 'boundary';
      id: string;
      title: string;
      summary?: string;
      index: number;
    };

export type SessionWorkbenchStatus = 'idle' | 'running' | 'waiting' | 'streaming' | 'complete' | 'failed';

export interface SessionWorkbenchHeaderModel {
  sessionId: string | null;
  title: string;
  status: SessionWorkbenchStatus;
  modelLabel: string | null;
  providerLabel: string | null;
  resumeHealth: string;
  checkpointCount: number;
  branchDepth: number;
  compactionCount: number;
  activeRunStatus: string | null;
}

export interface SessionWorkbenchInspectorModel {
  sessionEventCount: number | null;
  t0SegmentCount: number;
  latestCheckpointLabel: string | null;
  usedToolCount: number;
  blockedCapabilityCount: number;
  activatedToolGroupCount: number;
}

export interface ThreadTimelineModel {
  cells: ThreadTimelineCell[];
  header: SessionWorkbenchHeaderModel;
  inspector: SessionWorkbenchInspectorModel;
}

export interface BuildThreadTimelineInput {
  messages: AgentChatMessage[];
  activeSession?: Record<string, unknown> | null;
  runtimeSummary?: ChatRuntimeSummary | null;
  sessionIndex?: SessionIndex | null;
  branchLineage?: Array<{ id: unknown; parent_session_id?: unknown }> | null;
  isWaiting: boolean;
  isStreaming: boolean;
  activeRunStatus?: string | null;
}

function messageId(message: AgentChatMessage, fallback: string): string {
  return message.id || fallback;
}

function getSessionTitle(activeSession?: Record<string, unknown> | null): string {
  const raw = activeSession?.title;
  return typeof raw === 'string' && raw.trim() ? raw : 'Untitled session';
}

function getSessionId(activeSession?: Record<string, unknown> | null): string | null {
  const raw = activeSession?.id;
  return raw == null ? null : String(raw);
}

function getResumeHealth(sessionIndex?: SessionIndex | null): string {
  const health = sessionIndex?.resume_health;
  if (!health || typeof health !== 'object') return 'unknown';
  const status = health.status ?? health.state ?? health.kind;
  return typeof status === 'string' && status.trim() ? status : 'unknown';
}

function getLatestCheckpointLabel(sessionIndex?: SessionIndex | null): string | null {
  const latest = sessionIndex?.checkpoints?.[sessionIndex.checkpoints.length - 1];
  if (!latest) return null;
  const raw = latest.checkpoint_kind ?? latest.kind ?? latest.type ?? latest.id;
  return raw == null ? null : String(raw);
}

function getBranchDepth(activeSession?: Record<string, unknown> | null, branchLineage?: Array<{ id: unknown; parent_session_id?: unknown }> | null): number {
  if (!activeSession?.id || !Array.isArray(branchLineage) || branchLineage.length <= 1) return 0;
  const activeId = String(activeSession.id);
  const byId = new Map(branchLineage.map((item) => [String(item.id), item]));
  let depth = 0;
  let cursor = byId.get(activeId);
  const seen = new Set<string>();
  while (cursor?.parent_session_id && !seen.has(String(cursor.id))) {
    seen.add(String(cursor.id));
    const parent = byId.get(String(cursor.parent_session_id));
    if (!parent) break;
    depth += 1;
    cursor = parent;
  }
  return depth;
}

function getHeaderStatus(
  cells: ThreadTimelineCell[],
  isWaiting: boolean,
  isStreaming: boolean,
  activeRunStatus?: string | null,
): SessionWorkbenchStatus {
  if (isWaiting) return 'waiting';
  if (isStreaming) return 'streaming';
  if (activeRunStatus) return activeRunStatus === 'failed' ? 'failed' : 'running';
  const lastRun = [...cells].reverse().find((cell): cell is Extract<ThreadTimelineCell, { kind: 'active_run' }> => cell.kind === 'active_run');
  if (lastRun?.timeline.status === 'failed') return 'failed';
  if (lastRun?.timeline.status === 'running') return 'running';
  if (lastRun?.timeline.status === 'blocked') return 'waiting';
  if (cells.length > 0) return 'complete';
  return 'idle';
}

function isRenderableAssistantAnswer(message: AgentChatMessage): boolean {
  return message.role === 'assistant' && Boolean(message.content?.trim());
}

function buildRunCell(
  runIndex: number,
  sourceMessages: Array<{ message: AgentChatMessage; index: number }>,
  answer?: { message: AgentChatMessage; index: number },
): Extract<ThreadTimelineCell, { kind: 'active_run' }> {
  const timelineMessages = answer ? [...sourceMessages.map((entry) => entry.message), answer.message] : sourceMessages.map((entry) => entry.message);
  return {
    kind: 'active_run',
    id: `run-${runIndex}-${sourceMessages[0]?.index ?? answer?.index ?? 0}`,
    timeline: buildRunTimelineFromMessages(timelineMessages),
    sourceMessages,
    answer: answer?.message,
    answerIndex: answer?.index,
  };
}

function hasOpenRunCell(cells: ThreadTimelineCell[]): boolean {
  return cells.some((cell) => (
    cell.kind === 'active_run' &&
    (cell.timeline.status === 'running' || cell.timeline.status === 'blocked')
  ));
}

function buildPendingRunCell(input: BuildThreadTimelineInput): Extract<ThreadTimelineCell, { kind: 'active_run' }> | null {
  if (!input.isWaiting && !input.isStreaming && !input.activeRunStatus) return null;
  const status: RunTimelineSnapshot['status'] = input.activeRunStatus === 'failed' ? 'failed' : 'running';
  const title = input.isStreaming ? 'Streaming response' : 'Waiting for model';
  const summary = input.activeRunStatus
    ? `Active run: ${input.activeRunStatus}`
    : input.isStreaming
      ? 'The assistant is streaming this turn.'
      : 'The assistant is continuing this turn.';
  return {
    kind: 'active_run',
    id: 'active-run-pending',
    sourceMessages: [],
    timeline: {
      id: 'active-run-pending',
      status,
      steps: [
        {
          id: 'active-run-pending-step',
          kind: 'reasoning',
          title,
          status,
          summary,
          visibility: 'visible',
        },
      ],
    },
  };
}

function buildCells(messages: AgentChatMessage[]): ThreadTimelineCell[] {
  const cells: ThreadTimelineCell[] = [];
  const pendingRun: Array<{ message: AgentChatMessage; index: number }> = [];
  let runIndex = 0;

  const flushRun = () => {
    if (pendingRun.length === 0) return;
    cells.push(buildRunCell(runIndex, [...pendingRun]));
    runIndex += 1;
    pendingRun.length = 0;
  };

  messages.forEach((message, index) => {
    if (isDisclosureStepMessage(message)) {
      pendingRun.push({ message, index });
      return;
    }

    if (isRenderableAssistantAnswer(message)) {
      if (pendingRun.length > 0) {
        cells.push(buildRunCell(runIndex, [...pendingRun], { message, index }));
        runIndex += 1;
        pendingRun.length = 0;
        return;
      }
      cells.push({
        kind: 'assistant_final',
        id: messageId(message, `assistant-${index}`),
        message,
        index,
      });
      return;
    }

    flushRun();
    if (message.role === 'user') {
      cells.push({
        kind: 'user_turn',
        id: messageId(message, `user-${index}`),
        message,
        index,
      });
      return;
    }

    if (message.role === 'event') {
      cells.push({
        kind: 'boundary',
        id: messageId(message, `boundary-${index}`),
        title: message.eventTitle || message.eventType || 'Session event',
        summary: message.content || message.eventReason || message.eventNextStep,
        index,
      });
    }
  });

  flushRun();
  return cells;
}

export function buildThreadTimeline(input: BuildThreadTimelineInput): ThreadTimelineModel {
  const cells = buildCells(input.messages);
  const pendingRunCell = buildPendingRunCell(input);
  if (pendingRunCell && !hasOpenRunCell(cells)) {
    cells.push(pendingRunCell);
  }
  const sessionIndex = input.sessionIndex && !Array.isArray(input.sessionIndex) ? input.sessionIndex : null;
  const runtimeSummary = input.runtimeSummary || null;
  const headerStatus = getHeaderStatus(cells, input.isWaiting, input.isStreaming, input.activeRunStatus);

  return {
    cells,
    header: {
      sessionId: getSessionId(input.activeSession),
      title: getSessionTitle(input.activeSession),
      status: headerStatus,
      modelLabel: runtimeSummary?.model?.label || runtimeSummary?.model?.name || null,
      providerLabel: runtimeSummary?.model?.provider || null,
      resumeHealth: getResumeHealth(sessionIndex),
      checkpointCount: sessionIndex?.checkpoints?.length ?? 0,
      branchDepth: getBranchDepth(input.activeSession, input.branchLineage),
      compactionCount: runtimeSummary?.compaction_count ?? 0,
      activeRunStatus: input.activeRunStatus || null,
    },
    inspector: {
      sessionEventCount: typeof sessionIndex?.event_count === 'number' ? sessionIndex.event_count : null,
      t0SegmentCount: sessionIndex?.t0_segments?.length ?? 0,
      latestCheckpointLabel: getLatestCheckpointLabel(sessionIndex),
      usedToolCount: runtimeSummary?.used_tools?.length ?? 0,
      blockedCapabilityCount: runtimeSummary?.blocked_capabilities?.length ?? 0,
      activatedToolGroupCount: runtimeSummary?.activated_tool_groups?.length ?? 0,
    },
  };
}
