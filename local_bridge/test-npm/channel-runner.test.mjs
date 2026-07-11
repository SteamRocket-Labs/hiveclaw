import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { describe, it } from 'node:test';

import { HiveBridgeChannelRunner } from '../src/channel-runner.mjs';

class FakeClient {
  constructor() {
    this.ticketRequests = 0;
  }

  async createChannelWsTicket() {
    this.ticketRequests += 1;
    return { ticket: 'hbt_test', expires_in: 60, single_use: true };
  }

  channelWsUrl(ticket) {
    assert.equal(ticket, 'hbt_test');
    return `wss://hive.example/local-bridge/channel/ws?ticket=${ticket}`;
  }
}

function memoryReceiptStore() {
  const records = new Map();
  return {
    async get(key) { return records.has(key) ? structuredClone(records.get(key)) : null; },
    async put(key, value) { if (!records.has(key)) records.set(key, structuredClone(value)); },
  };
}

describe('npm hive-bridge channel runner', () => {
  it('replays a durable result without executing the same replay key twice', async () => {
    const sent = [];
    const records = new Map();
    const receiptStore = {
      async get(key) { return records.has(key) ? structuredClone(records.get(key)) : null; },
      async put(key, value) { if (!records.has(key)) records.set(key, structuredClone(value)); },
    };
    const runner = new HiveBridgeChannelRunner({
      client: new FakeClient(),
      runtime: 'noop',
      receiptStore,
    });
    let preparations = 0;
    runner.prepareMessage = async (message) => {
      preparations += 1;
      return { ...message, attachments: [] };
    };
    const ws = { send(payload) { sent.push(JSON.parse(payload)); } };
    const message = {
      id: 'message-1',
      session_id: 'session-1',
      replay_key: 'local:message-1',
      content: 'run once',
      attachments: [],
    };

    await runner.handleMessage(ws, message);
    await runner.handleMessage(ws, { ...message, id: 'message-replay' });

    assert.equal(preparations, 1);
    const results = sent.filter((payload) => payload.type === 'result');
    assert.equal(results.length, 2);
    assert.equal(results[1].metadata.idempotent_replay, true);
    assert.equal(results[1].metadata.replay_key, 'local:message-1');
  });

  it('fails closed when an executed result cannot be persisted for replay', async () => {
    const sent = [];
    const runner = new HiveBridgeChannelRunner({
      client: new FakeClient(),
      runtime: 'noop',
      receiptStore: {
        async get() { return null; },
        async put() { throw new Error('receipt disk unavailable'); },
      },
    });
    const ws = { send(payload) { sent.push(JSON.parse(payload)); } };

    await runner.handleMessage(ws, {
      id: 'message-1',
      session_id: 'session-1',
      replay_key: 'local:message-1',
      content: 'run once',
      attachments: [],
    });

    const result = sent.at(-1);
    assert.equal(result.status, 'failed');
    assert.equal(result.metadata.error_code, 'receipt_persistence_failed');
    assert.equal(result.metadata.requires_reconciliation, true);
    assert.equal(result.metadata.original_status, 'completed');
  });

  it('treats websocket close before work as offline presence, not login failure', async () => {
    const sent = [];
    class ClosingWebSocket extends EventEmitter {
      constructor(url) {
        super();
        this.url = url;
        queueMicrotask(() => {
          this.emit('message', Buffer.from(JSON.stringify({ type: 'hello' })));
          this.emit('message', Buffer.from(JSON.stringify({ type: 'ready_ack', status: 'online' })));
          this.emit('close');
        });
      }

      send(payload) {
        sent.push(JSON.parse(payload));
      }

      close() {
        this.emit('close');
      }
    }

    const client = new FakeClient();
    const runner = new HiveBridgeChannelRunner({
      client,
      runtime: 'codex',
      webSocketClass: ClosingWebSocket,
      reconnectDelayMs: 0,
    });

    assert.equal(await runner.runOnce(), 0);
    assert.equal(client.ticketRequests, 1);
    assert.deepEqual(sent, [
      {
        type: 'ready',
        runtime_kind: 'codex',
        capabilities: { file_upload: true, file_download: true, runtime: 'codex' },
      },
    ]);
  });

  it('keeps the foreground loop alive across transient websocket failures', async () => {
    const runner = new HiveBridgeChannelRunner({
      client: new FakeClient(),
      reconnectDelayMs: 0,
    });
    let attempts = 0;
    runner.runSession = async () => {
      attempts += 1;
      if (attempts === 1) throw new Error('network dropped');
      return 0;
    };

    assert.equal(await runner.runForever({ maxRuns: 2 }), 2);
    assert.equal(attempts, 2);
  });

  it('processes multiple channel messages on one websocket session', async () => {
    const sent = [];
    class MultiMessageWebSocket extends EventEmitter {
      constructor(url) {
        super();
        this.url = url;
        queueMicrotask(() => {
          this.emit('message', Buffer.from(JSON.stringify({ type: 'hello' })));
          this.emit('message', Buffer.from(JSON.stringify({ type: 'ready_ack', status: 'online' })));
          this.emit('message', Buffer.from(JSON.stringify({
            type: 'message',
            message: { id: 'message-1', session_id: 'session-1', content: 'first', attachments: [] },
          })));
          this.emit('message', Buffer.from(JSON.stringify({
            type: 'message',
            message: { id: 'message-2', session_id: 'session-1', content: 'second', attachments: [] },
          })));
          this.emit('close');
        });
      }

      send(payload) {
        sent.push(JSON.parse(payload));
      }

      close() {
        this.emit('close');
      }
    }

    const runner = new HiveBridgeChannelRunner({
      client: new FakeClient(),
      runtime: 'noop',
      webSocketClass: MultiMessageWebSocket,
      reconnectDelayMs: 0,
      receiptStore: memoryReceiptStore(),
    });

    assert.equal(await runner.runSession({ maxMessages: 2 }), 2);
    assert.deepEqual(
      sent.filter((payload) => payload.type === 'ack').map((payload) => payload.message_id),
      ['message-1', 'message-2'],
    );
    assert.deepEqual(
      sent.filter((payload) => payload.type === 'result').map((payload) => payload.message_id),
      ['message-1', 'message-2'],
    );
  });

  it('streams command stdout as local channel delta events before the final result', async () => {
    const sent = [];
    class OneMessageWebSocket extends EventEmitter {
      constructor(url) {
        super();
        this.url = url;
        queueMicrotask(() => {
          this.emit('message', Buffer.from(JSON.stringify({ type: 'hello' })));
          this.emit('message', Buffer.from(JSON.stringify({ type: 'ready_ack', status: 'online' })));
          this.emit('message', Buffer.from(JSON.stringify({
            type: 'message',
            message: { id: 'message-1', session_id: 'session-1', content: 'ignored', attachments: [] },
          })));
        });
      }

      send(payload) {
        const parsed = JSON.parse(payload);
        sent.push(parsed);
        if (parsed.type === 'result') this.emit('close');
      }

      close() {
        this.emit('close');
      }
    }

    const workDir = join(tmpdir(), `hive-bridge-runner-${Date.now()}`);
    await mkdir(workDir, { recursive: true });
    const script = join(workDir, 'stream-output.mjs');
    await writeFile(
      script,
      'process.stdout.write("alpha\\n"); setTimeout(() => process.stdout.write("beta\\n"), 10);',
      'utf8',
    );

    const runner = new HiveBridgeChannelRunner({
      client: new FakeClient(),
      runtime: 'command',
      command: [process.execPath, script],
      webSocketClass: OneMessageWebSocket,
      reconnectDelayMs: 0,
      receiptStore: memoryReceiptStore(),
    });

    assert.equal(await runner.runSession({ maxMessages: 1 }), 1);
    const deltas = sent.filter((payload) => payload.type === 'event' && payload.event_type === 'delta');
    assert.ok(deltas.length >= 1);
    assert.match(deltas.map((payload) => payload.payload.text).join(''), /alpha/);
    assert.equal(sent.at(-1).type, 'result');
    assert.match(sent.at(-1).output, /beta/);
  });

  it('reports a failed channel result when local message handling throws', async () => {
    const sent = [];
    class OneMessageWebSocket extends EventEmitter {
      constructor(url) {
        super();
        this.url = url;
        queueMicrotask(() => {
          this.emit('message', Buffer.from(JSON.stringify({ type: 'hello' })));
          this.emit('message', Buffer.from(JSON.stringify({ type: 'ready_ack', status: 'online' })));
          this.emit('message', Buffer.from(JSON.stringify({
            type: 'message',
            message: { id: 'message-1', session_id: 'session-1', content: 'boom', attachments: [] },
          })));
        });
      }

      send(payload) {
        const parsed = JSON.parse(payload);
        sent.push(parsed);
        if (parsed.type === 'result') this.emit('close');
      }

      close() {
        this.emit('close');
      }
    }

    const runner = new HiveBridgeChannelRunner({
      client: new FakeClient(),
      runtime: 'noop',
      webSocketClass: OneMessageWebSocket,
      reconnectDelayMs: 0,
      receiptStore: memoryReceiptStore(),
    });
    runner.prepareMessage = async () => {
      throw new Error('prepare exploded');
    };

    assert.equal(await runner.runSession({ maxMessages: 1 }), 1);
    assert.deepEqual(sent.filter((payload) => payload.type === 'ack'), [
      { type: 'ack', message_id: 'message-1' },
    ]);
    assert.deepEqual(sent.filter((payload) => payload.type === 'event' && payload.event_type === 'error'), [
      {
        type: 'event',
        session_id: 'session-1',
        message_id: 'message-1',
        event_type: 'error',
        payload: {
          error: 'prepare exploded',
          error_type: 'Error',
          runtime_kind: 'noop',
          replay_key: 'local-message:message-1',
        },
      },
    ]);
    assert.deepEqual(sent.at(-1), {
      type: 'result',
      session_id: 'session-1',
      message_id: 'message-1',
      status: 'failed',
      output: 'Local runtime failed: prepare exploded',
      artifacts: [],
      metadata: {
        error: 'prepare exploded',
        error_type: 'Error',
        runtime_kind: 'noop',
        replay_key: 'local-message:message-1',
      },
    });
  });
});
