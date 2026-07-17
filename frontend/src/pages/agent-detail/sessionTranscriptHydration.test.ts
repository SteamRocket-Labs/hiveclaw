import { describe, expect, it, vi } from 'vitest';

import type { ChatSession } from '../../api/domains/chat';
import type { AgentChatMessage, ChatTranscriptEventPayload } from './chatRuntime';
import {
  liveSubscriptionWatermark,
  loadCanonicalSessionTranscript,
  projectCanonicalTranscriptSnapshot,
} from './sessionTranscriptHydration';
import { consumeSessionEnvelope, projectSessionEventStoreToMessages } from './sessionEventConsumer';
import type { SessionEventV2 } from '../session-workbench/sessionEventStore';

function canonicalEvent(options: {
  sequence: number;
  itemId: string;
  itemKind: 'human_input' | 'assistant_commentary';
  lifecycle: 'accepted' | 'completed';
  content: string;
}): SessionEventV2 {
  const sessionScope = options.itemKind === 'human_input';
  return {
    schema: 'hive.session_event',
    schema_version: 2,
    event_id: `event-${options.sequence}`,
    sequence: options.sequence,
    tenant_id: 'tenant-1',
    scope: sessionScope
      ? { level: 'session', session_id: 'session-1', thread_id: 'session-1' }
      : {
          level: 'round',
          session_id: 'session-1',
          thread_id: 'session-1',
          turn_id: 'turn-1',
          run_id: 'run-1',
          round_id: 'round-1',
        },
    item_id: options.itemId,
    item_kind: options.itemKind,
    kind: `${options.itemKind}.${options.lifecycle}`,
    lifecycle: options.lifecycle,
    payload_schema: `hive.session.payload.${options.itemKind}.${options.lifecycle}.v2`,
    actor: options.itemKind === 'human_input'
      ? { type: 'user', id: 'user-1' }
      : { type: 'assistant' },
    visibility: { audience: 'direct_user' },
    payload: options.itemKind === 'human_input'
      ? { content_parts: [{ type: 'text', text: options.content }], intent: 'start_turn' }
      : { phase: 'commentary', content: options.content },
    occurred_at: '2026-07-18T02:00:00Z',
    persisted_at: '2026-07-18T02:00:00Z',
  };
}

const session: ChatSession = {
  id: 'session-1',
  agent_id: 'agent-1',
  title: 'Newest-page hydration',
  created_at: '2026-07-18T02:00:00Z',
  updated_at: '2026-07-18T02:00:00Z',
};

describe('loadCanonicalSessionTranscript', () => {
  it('projects a newest-page suffix immediately and keeps the live cursor contiguous', () => {
    const accepted = canonicalEvent({
      sequence: 1206,
      itemId: 'input-1',
      itemKind: 'human_input',
      lifecycle: 'accepted',
      content: 'Do not wait for sequence one.',
    });
    const commentary = canonicalEvent({
      sequence: 1207,
      itemId: 'commentary-1',
      itemKind: 'assistant_commentary',
      lifecycle: 'completed',
      content: 'The newest durable progress is visible now.',
    });

    const projected = projectCanonicalTranscriptSnapshot({
      existing: [],
      snapshot: [
        accepted as unknown as ChatTranscriptEventPayload,
        commentary as unknown as ChatTranscriptEventPayload,
      ],
      session,
      parseMessage: (message: AgentChatMessage) => message,
    });

    expect(projected.messages.map((message) => [message.role, message.content])).toEqual([
      ['user', 'Do not wait for sequence one.'],
      ['assistant', 'The newest durable progress is visible now.'],
    ]);
    expect(projected.store).toMatchObject({
      highestContiguousSequence: 1207,
      projection: { buffered_sequences: [] },
    });
    expect(liveSubscriptionWatermark(projected.store)).toBe(1207);

    const live = canonicalEvent({
      sequence: 1208,
      itemId: 'commentary-2',
      itemKind: 'assistant_commentary',
      lifecycle: 'completed',
      content: 'Live progress continues without a reload.',
    });
    const liveStore = consumeSessionEnvelope(
      live as unknown as ChatTranscriptEventPayload,
      projected.store,
      0,
    ).store;

    expect(liveStore?.highestContiguousSequence).toBe(1208);
    if (!liveStore) throw new Error('live suffix event did not produce a canonical store');
    expect(projectSessionEventStoreToMessages(liveStore).map((message) => message.content)).toEqual([
      'Do not wait for sequence one.',
      'The newest durable progress is visible now.',
      'Live progress continues without a reload.',
    ]);
  });

  it('still detects a real gap inside a newest-page suffix', () => {
    const projected = projectCanonicalTranscriptSnapshot({
      existing: [],
      snapshot: [
        canonicalEvent({
          sequence: 1206,
          itemId: 'input-1',
          itemKind: 'human_input',
          lifecycle: 'accepted',
          content: 'Start the suffix.',
        }) as unknown as ChatTranscriptEventPayload,
        canonicalEvent({
          sequence: 1208,
          itemId: 'commentary-1',
          itemKind: 'assistant_commentary',
          lifecycle: 'completed',
          content: 'This must wait for sequence 1207.',
        }) as unknown as ChatTranscriptEventPayload,
      ],
      session,
      parseMessage: (message: AgentChatMessage) => message,
    });

    expect(projected.store).toMatchObject({
      highestContiguousSequence: 1206,
      projection: { phase: 'gap_detected', buffered_sequences: [1208] },
    });
    expect(liveSubscriptionWatermark(projected.store)).toBeNull();
    expect(projected.messages.map((message) => message.content)).toEqual(['Start the suffix.']);
  });

  it('renders the newest canonical page first, then automatically backfills every older page', async () => {
    const events = Array.from({ length: 2205 }, (_, index) => ({
      id: `event-${index + 1}`,
      sequence: index + 1,
    }));
    const fetchPage = vi.fn(async ({ beforeSequence, limit }: { beforeSequence?: number; limit: number }) => {
      const eligible = beforeSequence == null
        ? events
        : events.filter((event) => event.sequence < beforeSequence);
      return eligible.slice(-limit);
    });
    const snapshots: Array<{ sequences: number[]; complete: boolean }> = [];

    const loaded = await loadCanonicalSessionTranscript(fetchPage, (snapshot, state) => {
      snapshots.push({
        sequences: snapshot.map((event) => Number(event.sequence)),
        complete: state.complete,
      });
    });

    expect(loaded).toHaveLength(2205);
    expect(loaded[0]).toMatchObject({ sequence: 1 });
    expect(loaded.at(-1)).toMatchObject({ sequence: 2205 });
    expect(snapshots[0].sequences[0]).toBe(1206);
    expect(snapshots[0].sequences.at(-1)).toBe(2205);
    expect(snapshots[0].complete).toBe(false);
    expect(snapshots.at(-1)).toMatchObject({ complete: true });
    expect(snapshots).toHaveLength(2);
    expect(snapshots.at(-1)?.sequences).toHaveLength(2205);
    expect(fetchPage).toHaveBeenNthCalledWith(1, {
      direction: 'backward',
      limit: 1000,
      schemaVersion: 2,
    });
    expect(fetchPage).toHaveBeenNthCalledWith(2, {
      beforeSequence: 1206,
      direction: 'backward',
      limit: 1000,
      schemaVersion: 2,
    });
    expect(fetchPage).toHaveBeenNthCalledWith(3, {
      beforeSequence: 206,
      direction: 'backward',
      limit: 1000,
      schemaVersion: 2,
    });
  });

  it('fails observably when an older-page cursor cannot move toward sequence one', async () => {
    const fetchPage = vi.fn(async () => Array.from({ length: 1000 }, (_, index) => ({
      id: `event-${index + 1}`,
      sequence: index + 1,
    })));

    await expect(loadCanonicalSessionTranscript(fetchPage)).rejects.toThrow('session_transcript_pagination_stalled');
  });

  it('publishes one complete empty snapshot for a session without transcript evidence', async () => {
    const snapshots: Array<{ length: number; complete: boolean }> = [];
    const loaded = await loadCanonicalSessionTranscript(
      async () => [],
      (snapshot, state) => {
        snapshots.push({ length: snapshot.length, complete: state.complete });
      },
    );

    expect(loaded).toEqual([]);
    expect(snapshots).toEqual([{ length: 0, complete: true }]);
  });
});
