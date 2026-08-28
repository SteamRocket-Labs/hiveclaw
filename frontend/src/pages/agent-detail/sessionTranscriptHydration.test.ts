import { describe, expect, it, vi } from 'vitest';

import type { ChatSession } from '../../api/domains/chat';
import type { AgentChatMessage, ChatTranscriptEventPayload } from './chatRuntime';
import {
  liveSubscriptionWatermark,
  loadCanonicalSessionTranscript,
  projectCanonicalTranscriptSnapshot,
  realtimeSubscriptionCursor,
} from './sessionTranscriptHydration';
import { consumeSessionEnvelope, projectSessionEventStoreToMessages } from './sessionEventConsumer';
import { createSessionEventStore, type SessionEventV2 } from '../session-workbench/sessionEventStore';

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

  it('uses live-tail only when no canonical or compatibility cursor exists', () => {
    expect(realtimeSubscriptionCursor(undefined, 0, false)).toBeNull();
    expect(realtimeSubscriptionCursor(undefined, 41, false)).toBe(41);
    expect(realtimeSubscriptionCursor(createSessionEventStore(57), 0, false)).toBe(57);
    expect(realtimeSubscriptionCursor(createSessionEventStore(57), 0, true)).toBe(0);
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

  it('releases live subscription after the first safe newest suffix without awaiting older pages', async () => {
    const newest = Array.from({ length: 1000 }, (_, index) => ({
      id: `event-${index + 1206}`,
      sequence: index + 1206,
    }));
    let releaseOlder!: () => void;
    const olderBlocked = new Promise<void>((resolve) => { releaseOlder = resolve; });
    const fetchPage = vi.fn(async ({ beforeSequence }: { beforeSequence?: number }) => {
      if (beforeSequence == null) return newest;
      await olderBlocked;
      return [];
    });

    const hydration = loadCanonicalSessionTranscript(
      fetchPage,
      (snapshot) => Number(snapshot.at(-1)?.sequence ?? 0),
    );

    await expect(hydration.liveReady).resolves.toBe(2205);
    expect(fetchPage).toHaveBeenCalledTimes(2);
    releaseOlder();
    await expect(hydration).resolves.toHaveLength(1000);
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

  it('holds an unresolved rewind tail until the page carrying the checkpoint arrives (Codex REQUEST_CHANGES #4 finding C)', async () => {
    // 1002 durable events; the rewind checkpoint (event-2) lives on the
    // OLDER page, so the newest first page cannot resolve the trim anchor.
    const checkpointInput = canonicalEvent({
      sequence: 2,
      itemId: 'input-2',
      itemKind: 'human_input',
      lifecycle: 'accepted',
      content: 'PROMPT TWO',
    });
    const olderPage = [
      canonicalEvent({
        sequence: 1,
        itemId: 'input-1',
        itemKind: 'human_input',
        lifecycle: 'accepted',
        content: 'PROMPT ONE',
      }),
      checkpointInput,
    ];
    const newestPage = Array.from({ length: 1000 }, (_, index) => canonicalEvent({
      sequence: index + 3,
      itemId: `commentary-${index + 3}`,
      itemKind: 'assistant_commentary',
      lifecycle: 'completed',
      content: `PROGRESS ${index + 3}`,
    }));
    const rewindSession = {
      ...session,
      transcript_metadata_json: {
        active_projection: {
          projection_reason: 'rewind',
          checkpoint_event_id: 'event-2',
          draft_content: '',
        },
      },
    } as unknown as ChatSession;

    let releaseOlder!: () => void;
    const olderBlocked = new Promise<void>((resolve) => { releaseOlder = resolve; });
    const fetchPage = vi.fn(async ({ beforeSequence }: { beforeSequence?: number }) => {
      if (beforeSequence == null) return newestPage as unknown as ChatTranscriptEventPayload[];
      await olderBlocked;
      return olderPage as unknown as ChatTranscriptEventPayload[];
    });
    const published: Array<string[]> = [];
    const hydration = loadCanonicalSessionTranscript(
      fetchPage,
      (snapshot) => {
        const projected = projectCanonicalTranscriptSnapshot({
          existing: [],
          snapshot,
          session: rewindSession,
          parseMessage: (message: AgentChatMessage) => message,
        });
        published.push(projected.messages.map((message) => message.content));
        return liveSubscriptionWatermark(projected.store);
      },
      { session: rewindSession },
    );

    // The newest page is fully processed (the older page was requested), but
    // the unresolved rewind tail is NOT published and the live cursor is NOT
    // opened.
    await vi.waitFor(() => expect(fetchPage).toHaveBeenCalledTimes(2));
    expect(published).toEqual([]);
    let liveState: 'pending' | 'resolved' | 'rejected' = 'pending';
    void hydration.liveReady.then(
      () => { liveState = 'resolved'; },
      () => { liveState = 'rejected'; },
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(liveState).toBe('pending');

    releaseOlder();
    const loaded = await hydration;
    expect(loaded).toHaveLength(1002);

    // Exactly one publish, correctly trimmed at the checkpoint: PROMPT TWO
    // (the anchor) and the entire rewind-hidden tail stay hidden.
    expect(published).toEqual([['PROMPT ONE']]);
    await expect(hydration.liveReady).resolves.toBe(1002);
  });

  it('fails observably and never publishes the unsafe tail when hydration completes without the rewind checkpoint', async () => {
    const rewindSession = {
      ...session,
      transcript_metadata_json: {
        active_projection: {
          projection_reason: 'rewind',
          checkpoint_event_id: 'event-missing',
          draft_content: '',
        },
      },
    } as unknown as ChatSession;
    const page = [
      canonicalEvent({ sequence: 1, itemId: 'input-1', itemKind: 'human_input', lifecycle: 'accepted', content: 'PROMPT ONE' }),
      canonicalEvent({ sequence: 2, itemId: 'commentary-2', itemKind: 'assistant_commentary', lifecycle: 'completed', content: 'ANSWER ONE' }),
    ];
    const published: string[][] = [];
    const hydration = loadCanonicalSessionTranscript(
      async () => page as unknown as ChatTranscriptEventPayload[],
      (snapshot) => {
        const projected = projectCanonicalTranscriptSnapshot({
          existing: [],
          snapshot,
          session: rewindSession,
          parseMessage: (message: AgentChatMessage) => message,
        });
        published.push(projected.messages.map((message) => message.content));
        return liveSubscriptionWatermark(projected.store);
      },
      { session: rewindSession },
    );

    await expect(hydration).rejects.toThrow('session_rewind_checkpoint_unresolved');
    await expect(hydration.liveReady).rejects.toThrow('session_rewind_checkpoint_unresolved');
    // The unsafe untrimmed tail was never published.
    expect(published).toEqual([]);
  });
});
