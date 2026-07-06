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

describe('npm hive-bridge channel runner', () => {
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
      },
    });
  });
});
