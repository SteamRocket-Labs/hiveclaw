import { describe, expect, it } from 'vitest';

import { getRuntimeEventMessage, replayTranscriptEvents, type AgentChatMessage, type ChatTranscriptEventPayload } from './chatRuntime';
import { compatibilityProjectionEvent, consumeSessionEnvelope, hydrateSessionTranscriptEvents, projectSessionEventStoreToMessages } from './sessionEventConsumer';
import type { SessionCompatibilityEvent, SessionEventV2 } from '../session-workbench/sessionEventStore';
import { buildSessionRightPanelModel, buildWorkspaceDocumentsModel } from '../session-workbench/timelineModel';

const artifact = {
  type: 'artifact', artifact_id: 'child-artifact', path: 'workspace/report.md', name: 'report.md',
  source: 'a2a_delivery_ref', runtime_task_id: 'child-run', revision_id: 'pinned-revision',
  content_hash: 'b'.repeat(64), snapshot_storage_path: 'runtime_artifacts/snapshots/report.md',
  owner_agent_id: 'employee', producer_agent_id: 'employee', source_agent_id: 'employee',
  download_agent_id: 'employee', delivery_agent_id: 'assistant', source_session_id: 'child-session',
  root_session_id: 'parent-session', preview_snapshot_content: 'Report body', preview_snapshot_truncated: false,
};

const returned = {
  type: 'child_session', status: 'completed', message: 'Report ready.', runtime_task_id: 'child-run',
  child_session_id: 'child-session', parent_session_id: 'parent-session', parts: [artifact],
};

function expectOriginalWorkDelivery(messages: AgentChatMessage[]) {
  expect(messages).toHaveLength(1);
  expect(messages[0].artifacts).toHaveLength(1);
  expect(messages[0].artifacts?.[0]).toMatchObject({
    id: 'child-artifact', path: 'workspace/report.md', revisionId: 'pinned-revision',
    ownerAgentId: 'employee', downloadAgentId: 'employee', deliveryAgentId: 'assistant',
    sourceSessionId: 'child-session', previewSnapshotContent: 'Report body',
  });
  const documents = buildWorkspaceDocumentsModel(messages, new Set(['child-run']));
  expect(documents.total).toBe(1);
  expect(documents.currentSession.items).toHaveLength(1);
}

function canonicalReturn(): SessionEventV2 {
  return {
    schema: 'hive.session_event', schema_version: 2, event_id: 'return-event', sequence: 1,
    tenant_id: 'tenant', scope: { level: 'run', session_id: 'parent-session', thread_id: 'parent-session',
      turn_id: 'parent-turn', run_id: 'child-run' }, item_id: 'delegation', item_kind: 'a2a_delegation',
    kind: 'a2a_delegation.completed', lifecycle: 'completed',
    payload_schema: 'hive.session.payload.a2a_delegation.completed.v2', actor: { type: 'runtime' },
    visibility: { audience: 'direct_user' }, payload: returned,
    occurred_at: '2026-10-25T00:00:00Z', persisted_at: '2026-10-25T00:00:00Z',
  };
}

function projectCanonical(event: SessionEventV2) {
  const store = consumeSessionEnvelope(event as unknown as ChatTranscriptEventPayload, undefined, 0).store;
  expect(store).toBeDefined();
  return projectSessionEventStoreToMessages(store!);
}

function originalWorkPanel(file: Record<string, unknown> = {}, metadata: Record<string, unknown> = {}, canonical = false) {
  const part = { ...artifact, ...file };
  const carrier: SessionCompatibilityEvent = {
    schema: 'hive.session_event_compatibility', schema_version: 1,
    compatibility_status: 'needs_reconciliation', reason: 'unmapped_legacy_kind',
    event_id: 'returned-event', sequence: 1, legacy_event_type: 'child_session',
    payload: {
      content: returned.message, parts: [part],
      metadata: { ...returned, role: 'system', artifacts: [part], ...metadata },
    },
  };
  const envelope = canonical
    ? { ...canonicalReturn(), payload: { ...returned, parts: [part], ...metadata } }
    : carrier;
  const messages = hydrateSessionTranscriptEvents([envelope as unknown as ChatTranscriptEventPayload]).messages;
  return buildSessionRightPanelModel({
    messages,
    activeSession: { id: 'parent-session' },
    activeRunStatus: 'completed',
    presentationStatus: 'complete',
    sessionWorkbench: {
      runtime_sections: {
        runs: [{ id: 'parent-run', runtime_task_id: 'parent-run', status: 'completed' }],
      },
    },
  });
}

describe('delegation file delivery in the original work', () => {
  it.each(['compatibility', 'canonical'])('lists the %s child file in its original work without rebinding its task or producer', (format) => {
    const model = originalWorkPanel({}, {}, format === 'canonical');
    expect(model.workspaceDocuments.currentSession.items).toHaveLength(1);
    expect(model.workspaceDocuments.historical.items).toEqual([]);
    expect(model.workspaceDocuments.currentSession.items[0].artifact).toMatchObject({
      id: 'child-artifact', runtimeTaskId: 'child-run', sourceSessionId: 'child-session',
      rootSessionId: 'parent-session', ownerAgentId: 'employee', downloadAgentId: 'employee',
      revisionId: 'pinned-revision', contentHash: 'b'.repeat(64),
    });
  });

  it.each([
    { label: 'another original work', file: { root_session_id: 'other-work' }, metadata: {} },
    { label: 'another task', file: { runtime_task_id: 'other-run' }, metadata: {} },
    { label: 'another producer session', file: { source_session_id: 'other-child' }, metadata: {} },
    { label: 'a return addressed to another work', file: {}, metadata: { parent_session_id: 'other-work' } },
  ])('keeps files from $label outside current work deliveries', ({ file, metadata }) => {
    const model = originalWorkPanel(file, metadata);
    expect(model.workspaceDocuments.currentSession.items).toEqual([]);
    expect(model.workspaceDocuments.historical.items).toHaveLength(1);
  });

  it('keeps actual producer references on a runtime activity message', () => {
    const message = getRuntimeEventMessage(returned);
    expect(message).not.toBeNull();
    expectOriginalWorkDelivery([message!]);
  });

  it('replays the stored child-session carrier with its pinned file', () => {
    const carrier: SessionCompatibilityEvent = {
      schema: 'hive.session_event_compatibility', schema_version: 1,
      compatibility_status: 'needs_reconciliation', reason: 'unmapped_legacy_kind',
      event_id: 'stored-return', sequence: 1, legacy_event_type: 'child_session',
      payload: { content: returned.message, parts: [artifact], metadata: returned },
    };
    const projection = compatibilityProjectionEvent(carrier);
    expectOriginalWorkDelivery(replayTranscriptEvents([projection]).messages);
  });

  it('keeps the same file in the canonical event projection', () => {
    expectOriginalWorkDelivery(projectCanonical(canonicalReturn()));
  });

  it('does not publish a file whose canonical source is held', () => {
    const event = canonicalReturn();
    event.payload = { ...event.payload, source_access_receipt: {
      schema: 'hive.source_access_receipt.v1', status: 'denied', code: 'content_source_access_denied', retryable: false,
    } };
    const messages = projectCanonical(event);
    expect(messages[0].eventType).toBe('source_access_notice');
    expect(messages[0].artifacts).toBeUndefined();
    expect(buildWorkspaceDocumentsModel(messages).total).toBe(0);
  });

  it('does not reattach held legacy source files during activity normalization', () => {
    const message = getRuntimeEventMessage({ ...returned, source_access_receipt: {
      schema: 'hive.source_access_receipt.v1', status: 'unavailable', code: 'content_source_missing', retryable: false,
    } });
    expect(message?.artifacts).toBeUndefined();
  });
});
