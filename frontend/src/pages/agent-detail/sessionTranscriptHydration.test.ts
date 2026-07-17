import { describe, expect, it, vi } from 'vitest';

import { loadCanonicalSessionTranscript } from './sessionTranscriptHydration';

describe('loadCanonicalSessionTranscript', () => {
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
