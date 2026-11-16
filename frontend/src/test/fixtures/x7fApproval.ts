import type { ChatTranscriptEventPayload } from '../../pages/agent-detail/chatRuntime';
import type { SessionEventV2 } from '../../pages/session-workbench/sessionEventStore';

const runId = '11111111-1111-4111-8111-111111111111';
const session = { id: 'session-x7f', agent_id: 'agent-x7f', user_id: 'member-x7f', title: 'Plaza', session_kind: 'human_chat', source_channel: 'web', read_only: false, operator_view: false };
const key = `agent-x7f:${session.id}`;
type Mode = 'live' | 'reload';
const request = { permission_request_id: 'permission-x7f', tool_name: 'plaza_create_post',
  arguments: { content: 'Test post' }, session_id: session.id, runtime_task_id: runId,
  requested_by_user_id: session.user_id, expires_at: '2026-11-16T12:56:55Z' };
const legacyPermission: ChatTranscriptEventPayload = { id: 'legacy-permission-x7f', type: 'permission', event_type: 'permission',
  content: 'Permission required', metadata: { status: 'session_permission_required',
    permission_request_id: request.permission_request_id, permission_request: request },
  created_at: '2026-11-16T12:26:55.217711Z' };
function event(sequence: number, kind: string, lifecycle: string, at: string,
  payload: Record<string, unknown>, itemId = kind): SessionEventV2 {
  const level = kind === 'human_input' ? 'session' : kind === 'turn' ? 'turn'
    : ['run', 'control_input'].includes(kind) ? 'run' : 'round';
  return {
    schema: 'hive.session_event', schema_version: 2, event_id: `event-${sequence}`, sequence,
    tenant_id: 'tenant-x7f', item_id: itemId, item_kind: kind, kind: `${kind}.${lifecycle}`, lifecycle,
    payload_schema: `hive.session.payload.${kind}.${lifecycle}.v2`,
    scope: { level, session_id: session.id, thread_id: session.id,
      ...(level !== 'session' ? { turn_id: 'turn-x7f' } : {}),
      ...(['run', 'round'].includes(level) ? { run_id: runId } : {}),
      ...(level === 'round' ? { round_id: 'round-x7f' } : {}) } as SessionEventV2['scope'],
    actor: { type: 'runtime' }, visibility: { audience: 'direct_user' }, payload,
    occurred_at: at, persisted_at: at,
    ...(kind.startsWith('tool_') ? { invocation_id: 'call-x7f' } : {}),
  };
}

// Run23 step8 /transcript seq 1,10,122-127: retain observed timestamps,
// event order and scope levels; compact sequences and replace identities/content.
const run23Waiting = [
  event(1, 'human_input', 'accepted', '2026-11-16T12:24:06.645302Z',
    { intent: 'start_turn', content_parts: [{ type: 'text', text: 'Publish a post.' }] }),
  event(2, 'run', 'queued', '2026-11-16T12:24:11.256390Z', {}, runId),
  event(3, 'tool_call', 'started', '2026-11-16T12:26:53.618313Z',
    { tool_name: 'plaza_create_post' }, 'call-x7f'),
  legacyPermission,
  event(4, 'tool_permission', 'waiting', '2026-11-16T12:27:00.200886Z', {
    permission_request: request,
  }, 'permission-x7f'),
  event(5, 'tool_call', 'waiting', '2026-11-16T12:27:00.200903Z',
    { permission_item_id: 'permission-x7f' }, 'call-x7f'),
  event(6, 'run', 'waiting', '2026-11-16T12:27:00.200914Z',
    { reason_code: 'tool_permission_required', permission_item_id: 'permission-x7f' }, runId),
  event(7, 'turn', 'waiting', '2026-11-16T12:27:00.200944Z',
    { reason_code: 'tool_permission_required', permission_item_id: 'permission-x7f' }),
];

// Exercise the existing legacy decision contract alongside canonical facts.
// Use the canonical completion time; the raw socket arrival was not recorded.
const decision: ChatTranscriptEventPayload = {
  id: 'decision-x7f', type: 'session_permission_decision', event_type: 'session_permission_decision',
  content: JSON.stringify({ permission_request_id: 'permission-x7f', decision: 'allow_once' }),
  metadata: { permission_request_id: 'permission-x7f', decision: 'allow_once' },
  created_at: '2026-11-16T12:30:17.844117Z',
};
const run23AfterApproval = [
  ...run23Waiting,
  event(8, 'control_input', 'accepted', '2026-11-16T12:30:17.264794Z',
    { kind: 'permission_response', permission_item_id: 'permission-x7f', decision: 'allow_once' }, 'control-x7f'),
  event(9, 'control_input', 'applied', '2026-11-16T12:30:17.617022Z',
    { kind: 'permission_response', permission_item_id: 'permission-x7f', decision: 'allow_once' }, 'control-x7f'),
  event(10, 'tool_permission', 'completed', '2026-11-16T12:30:17.844117Z', { decision: 'allow_once' }, 'permission-x7f'),
  decision,
  event(11, 'tool_call', 'progress', '2026-11-16T12:30:23.897948Z', {}, 'call-x7f'),
  event(12, 'tool_call', 'completed', '2026-11-16T12:30:24.764273Z', { outcome: 'success' }, 'call-x7f'),
  event(13, 'tool_result', 'completed', '2026-11-16T12:30:24.764301Z',
    { tool_name: 'plaza_create_post', outcome: 'success', content: 'Published.' }, 'result-x7f'),
  event(14, 'run', 'queued', '2026-11-16T12:30:25.481135Z', { reason_code: 'session_permission_resolved' }, runId),
];
const run23Completed = [...run23AfterApproval,
  event(15, 'assistant_final', 'completed', '2026-11-16T12:31:15.224997Z', { content: 'Published.' }),
  event(16, 'run', 'completed', '2026-11-16T12:31:15.225009Z', { started_at: '2026-11-16T12:24:11.394376Z' }, runId),
  event(17, 'turn', 'completed', '2026-11-16T12:31:15.225023Z', {}),
];

// HTTP history contains sequenced canonical/compatibility carriers, whereas
// the raw decision also arrives on the existing live legacy channel.
function wire(events: Array<SessionEventV2 | ChatTranscriptEventPayload>) {
  let sequence = 0;
  return events.map(item => {
    if (item === legacyPermission) return {
      schema: 'hive.session_event_compatibility', schema_version: 1, compatibility_status: 'needs_reconciliation',
      reason: 'legacy_unscoped_event', event_id: 'legacy-permission-x7f', sequence: ++sequence,
      tenant_id: 'tenant-x7f', session_id: session.id, run_id: runId, legacy_event_type: 'permission',
      occurred_at: legacyPermission.created_at,
      payload: { content: legacyPermission.content, metadata: legacyPermission.metadata },
    };
    return 'schema' in item ? { ...item, sequence: ++sequence } : item;
  });
}
const waiting = wire(run23Waiting);
const resumed = wire(run23AfterApproval);
const completed = wire(run23Completed);
export { runId, session, key, request, waiting as run23Waiting, resumed as run23AfterApproval, completed as run23Completed };
