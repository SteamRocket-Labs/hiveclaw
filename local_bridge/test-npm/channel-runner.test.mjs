import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
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
    runner.runOnce = async () => {
      attempts += 1;
      if (attempts === 1) throw new Error('network dropped');
      return 0;
    };

    assert.equal(await runner.runForever({ maxRuns: 2 }), 2);
    assert.equal(attempts, 2);
  });
});
