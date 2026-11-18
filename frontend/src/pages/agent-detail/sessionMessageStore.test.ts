import { describe, expect, it } from 'vitest';

import {
  createSessionMessageStore,
  createPendingUserMessageActions,
  type SessionMessageFrameScheduler,
} from './sessionMessageStore';
import type { AgentChatMessage, PendingUserMessage } from './chatRuntime';
import { mergeCanonicalTerminalMessages } from './sessionEventConsumer';

function message(id: string, content = id): AgentChatMessage {
  return { id, role: 'assistant', content };
}

function createManualScheduler(): SessionMessageFrameScheduler & { runNext: () => void; pendingCount: () => number } {
  const callbacks = new Map<number, FrameRequestCallback>();
  let nextId = 1;
  return {
    requestFrame: (callback) => {
      const id = nextId++;
      callbacks.set(id, callback);
      return id;
    },
    cancelFrame: (id) => {
      callbacks.delete(id);
    },
    runNext: () => {
      const first = callbacks.entries().next().value as [number, FrameRequestCallback] | undefined;
      if (!first) return;
      callbacks.delete(first[0]);
      first[1](performance.now());
    },
    pendingCount: () => callbacks.size,
  };
}

describe('session message store', () => {
  it('X9a.1 retries one input without duplicating it or replacing its durable display', () => {
    const store = createSessionMessageStore(createManualScheduler());
    const pending = { current: {} as Record<string, PendingUserMessage[]> };
    const actions = createPendingUserMessageActions(pending, store.updateAfterQueued, message => message);
    store.updateAfterQueued('work', () => [{ id: 'input', role: 'user', content: 'Use [REDACTED_SECRET]' }]);
    const input: AgentChatMessage = { id: 'input', role: 'user', content: 'Use original credential', sendState: 'sending' };
    actions.appendOptimisticUserMessage('bot:work', 'work', input);
    actions.updateOptimisticUserMessage('bot:work', 'work', 'input', { sendState: 'failed' });
    actions.appendOptimisticUserMessage('bot:work', 'work', input);
    expect(store.getSnapshot('work')).toEqual([expect.objectContaining({ id: 'input', content: 'Use [REDACTED_SECRET]', sendState: 'sending' })]);
    expect(pending.current['bot:work']).toHaveLength(1);
    actions.updateOptimisticUserMessage('bot:work', 'work', 'input', { sendState: undefined });
    expect(store.getSnapshot('work')[0].sendState).toBeUndefined();
    expect(pending.current['bot:work']).toBeUndefined();
  });

  it('coalesces multiple active-session updates into one frame notification', () => {
    const scheduler = createManualScheduler();
    const store = createSessionMessageStore(scheduler);
    const snapshots: AgentChatMessage[][] = [];
    store.subscribe('session-1', () => snapshots.push(store.getSnapshot('session-1')));

    store.enqueueUpdate('session-1', (prev) => [...prev, message('a')]);
    store.enqueueUpdate('session-1', (prev) => [...prev, message('b')]);

    expect(scheduler.pendingCount()).toBe(1);
    expect(snapshots).toHaveLength(0);

    scheduler.runNext();

    expect(snapshots).toHaveLength(1);
    expect(store.getSnapshot('session-1').map((item) => item.id)).toEqual(['a', 'b']);
  });

  it('keeps subscriptions isolated per session', () => {
    const scheduler = createManualScheduler();
    const store = createSessionMessageStore(scheduler);
    const sessionOneSnapshots: AgentChatMessage[][] = [];
    const sessionTwoSnapshots: AgentChatMessage[][] = [];
    store.subscribe('session-1', () => sessionOneSnapshots.push(store.getSnapshot('session-1')));
    store.subscribe('session-2', () => sessionTwoSnapshots.push(store.getSnapshot('session-2')));

    store.enqueueUpdate('session-1', (prev) => [...prev, message('only-session-1')]);
    scheduler.runNext();

    expect(sessionOneSnapshots).toHaveLength(1);
    expect(sessionTwoSnapshots).toHaveLength(0);
  });

  it('applies queued streaming chunks before terminal replacement updates', () => {
    const scheduler = createManualScheduler();
    const store = createSessionMessageStore(scheduler);

    store.enqueueUpdate('session-1', (prev) => [...prev, message('queued')]);
    store.updateAfterQueued('session-1', (prev) => [...prev, message('terminal')]);

    expect(scheduler.pendingCount()).toBe(0);
    expect(store.getSnapshot('session-1').map((item) => item.id)).toEqual(['queued', 'terminal']);
  });
});

describe('X9c pending user messages', () => {
  const sessionId = 'work';
  const key = 'bot:work';
  const prior = message('earlier-answer');

  function humanInput(id: string, content = ''): AgentChatMessage {
    return {
      id, role: 'user', content, transcriptEventId: `event-${id}`,
      sessionItem: {
        id, kind: 'human_input', lifecycle: 'accepted', content, payload: {},
        scope: { level: 'session', session_id: sessionId, thread_id: sessionId },
        terminal: false, revision: 1, actor: { type: 'user' }, visibility: { audience: 'direct_user' },
        occurredAt: '2026-11-18T00:00:00Z', first_sequence: 2, last_sequence: 2,
      },
    };
  }

  function run(inputId: string, lifecycle = 'running'): AgentChatMessage {
    const input = humanInput(inputId);
    return {
      id: 'run-1', role: 'event', content: '',
      sessionItem: {
        ...input.sessionItem!, id: 'run-1', kind: 'run', lifecycle, payload: { input_id: inputId },
        scope: { level: 'run', session_id: sessionId, thread_id: sessionId, turn_id: 'turn-1', run_id: 'run-1' },
        first_sequence: 3, last_sequence: 3,
      },
    };
  }

  type Bubble = [id: string, content: string, sendState?: AgentChatMessage['sendState']];

  function setup() {
    const store = createSessionMessageStore(createManualScheduler());
    const pending = { current: {} as Record<string, PendingUserMessage[]> };
    const actions = createPendingUserMessageActions(pending, store.updateAfterQueued, input => input);
    const snapshot = () => store.getSnapshot(sessionId);
    const compose = (...rows: AgentChatMessage[]) => {
      store.updateAfterQueued(sessionId, () => actions.mergePendingForSession(key, [prior, ...rows]));
    };
    const expectVisible = (stage: string, ...bubbles: Bubble[]) => {
      expect(snapshot().filter(row => row.role === 'user').map(row => [row.id, row.content, row.sendState]), stage)
        .toEqual(bubbles.map(([id, content, sendState]) => [id, content, sendState]));
    };
    const update = (id: string, patch: Partial<AgentChatMessage>) => {
      actions.updateOptimisticUserMessage(key, sessionId, id, patch);
    };
    compose();
    return {
      store, pending, snapshot, compose, expectVisible, update,
      send: (id: string, content: string) => {
        actions.appendOptimisticUserMessage(key, sessionId, { id, role: 'user', content, sendState: 'sending' });
      },
      acknowledge: (id: string) => update(id, { sendState: undefined, sendError: undefined, retrySend: undefined }),
      commitTerminal: (...rows: AgentChatMessage[]) => {
        store.updateAfterQueued(sessionId, previous => actions.mergePendingForSession(key,
          mergeCanonicalTerminalMessages(previous, rows, 'run-1')));
      },
      confirm: (...bubbles: Bubble[]) => {
        compose(...bubbles.map(([id, content]) => humanInput(id, content)));
        expectVisible('saved text', ...bubbles);
        expect(pending.current[key]).toBeUndefined();
      },
    };
  }

  it('S1 keeps the acknowledged send through an identity-only transcript page', () => {
    const chat = setup();
    chat.send('input-1', 'hello');
    chat.expectVisible('send', ['input-1', 'hello', 'sending']);
    chat.acknowledge('input-1');
    chat.expectVisible('POST returns', ['input-1', 'hello']);
    chat.compose(humanInput('input-1'));
    chat.expectVisible('identity-only page', ['input-1', 'hello']);
    chat.confirm(['input-1', 'hello']);
  });

  it('S2 keeps the send when a live input event precedes the POST response', () => {
    const chat = setup();
    const input = humanInput('input-1');
    chat.send('input-1', 'hello');
    chat.expectVisible('send', ['input-1', 'hello', 'sending']);
    chat.compose(input);
    chat.expectVisible('identity-only event before POST returns', ['input-1', 'hello', 'sending']);
    chat.acknowledge('input-1');
    chat.expectVisible('POST returns', ['input-1', 'hello']);
    expect(chat.snapshot()[1]).toMatchObject({ sessionItem: input.sessionItem, transcriptEventId: input.transcriptEventId });
    chat.compose(input, run('input-1'));
    chat.expectVisible('next live event', ['input-1', 'hello']);
    chat.confirm(['input-1', 'hello']);
  });

  it('S3 keeps the first text when a second send reuses the current snapshot', () => {
    const chat = setup();
    chat.send('input-1', 'first');
    chat.expectVisible('first send', ['input-1', 'first', 'sending']);
    chat.acknowledge('input-1');
    chat.expectVisible('first POST returns', ['input-1', 'first']);
    chat.send('input-2', 'second');
    chat.expectVisible('second send', ['input-1', 'first'], ['input-2', 'second', 'sending']);
    chat.compose(humanInput('input-1'));
    chat.expectVisible('first identity-only page', ['input-1', 'first'], ['input-2', 'second', 'sending']);
    chat.acknowledge('input-2');
    chat.expectVisible('second POST returns', ['input-1', 'first'], ['input-2', 'second']);
    chat.confirm(['input-1', 'first'], ['input-2', 'second']);
  });

  it('S4 keeps an inline or question-answer retry using the same input id', () => {
    const chat = setup();
    const retrySend = () => chat.send('input-1', 'answer');
    chat.send('input-1', 'answer');
    chat.expectVisible('send', ['input-1', 'answer', 'sending']);
    chat.update('input-1', { sendState: 'failed', sendError: 'Send failed', retrySend });
    chat.expectVisible('failure', ['input-1', 'answer', 'failed']);
    const failed = chat.snapshot()[1];
    expect(failed).toMatchObject({ sendError: 'Send failed', retrySend });
    failed.retrySend?.();
    chat.expectVisible('same-id retry', ['input-1', 'answer', 'sending']);
    chat.acknowledge('input-1');
    chat.expectVisible('retry POST returns', ['input-1', 'answer']);
    expect(chat.snapshot()[1].sendError).toBeUndefined();
    expect(chat.snapshot()[1].retrySend).toBeUndefined();
    chat.compose(humanInput('input-1'));
    chat.expectVisible('identity-only page after retry', ['input-1', 'answer']);
    chat.confirm(['input-1', 'answer']);
  });

  it('S5 keeps the composer retry after removing the old failed bubble', () => {
    const chat = setup();
    const retrySend = () => {
      chat.pending.current[key] = chat.pending.current[key].filter(item => item.message.id !== 'input-1');
      chat.store.updateAfterQueued(sessionId, previous => previous.filter(row => row.id !== 'input-1'));
      chat.expectVisible('old failed bubble removed');
      chat.send('input-1', 'hello');
    };
    chat.send('input-1', 'hello');
    chat.expectVisible('send', ['input-1', 'hello', 'sending']);
    chat.update('input-1', { sendState: 'failed', sendError: 'Send failed', retrySend });
    chat.expectVisible('failure', ['input-1', 'hello', 'failed']);
    chat.snapshot()[1].retrySend?.();
    chat.expectVisible('composer retry', ['input-1', 'hello', 'sending']);
    chat.acknowledge('input-1');
    chat.expectVisible('retry POST returns', ['input-1', 'hello']);
    chat.compose(humanInput('input-1'));
    chat.expectVisible('identity-only page after composer retry', ['input-1', 'hello']);
    chat.confirm(['input-1', 'hello']);
  });

  it('S6 reconciles a saved copy already in the snapshot at POST acknowledgement', () => {
    const chat = setup();
    chat.send('input-1', 'hello');
    chat.expectVisible('send', ['input-1', 'hello', 'sending']);
    chat.store.enqueueUpdate(sessionId, previous => [...previous, humanInput('input-1', 'saved hello')]);
    chat.expectVisible('saved row queued before POST returns', ['input-1', 'hello', 'sending']);
    chat.acknowledge('input-1');
    chat.expectVisible('POST returns after the queued update is applied', ['input-1', 'saved hello']);
    expect(chat.pending.current[key]).toBeUndefined();
    chat.confirm(['input-1', 'saved hello']);
  });

  it('S7 clears the queued state once the run starts while keeping the text', () => {
    const chat = setup();
    chat.send('input-1', 'hello');
    chat.expectVisible('send', ['input-1', 'hello', 'sending']);
    chat.update('input-1', { sendState: 'queued' });
    chat.expectVisible('queued', ['input-1', 'hello', 'queued']);
    chat.compose(humanInput('input-1'), run('input-1'));
    chat.expectVisible('run starts', ['input-1', 'hello']);
    chat.compose(humanInput('input-1'));
    chat.expectVisible('next identity-only page', ['input-1', 'hello']);
    chat.confirm(['input-1', 'hello']);
  });

  it('S8 keeps the text when a terminal event retains the local row in the prefix', () => {
    const chat = setup();
    chat.send('input-1', 'hello');
    chat.expectVisible('send', ['input-1', 'hello', 'sending']);
    chat.acknowledge('input-1');
    chat.expectVisible('POST returns', ['input-1', 'hello']);
    chat.commitTerminal(run('input-1', 'completed'), message('final-answer'));
    chat.expectVisible('terminal event keeps the prefix', ['input-1', 'hello']);
    chat.compose(humanInput('input-1'));
    chat.expectVisible('identity-only page after terminal', ['input-1', 'hello']);
    chat.confirm(['saved-input', 'hello']);
  });
});
