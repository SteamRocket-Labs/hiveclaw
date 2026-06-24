import { mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { spawn } from 'node:child_process';

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function commandResult(command, input, { cwd = process.cwd(), timeout = 600_000, onEvent = null } = {}) {
  return new Promise((resolve) => {
    const child = spawn(command[0], command.slice(1), { cwd, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    const timer = setTimeout(() => child.kill('SIGTERM'), timeout);
    child.stdout.on('data', (chunk) => {
      const text = chunk.toString();
      stdout += text;
      onEvent?.('delta', { stream: 'stdout', text });
    });
    child.stderr.on('data', (chunk) => {
      const text = chunk.toString();
      stderr += text;
      onEvent?.('delta', { stream: 'stderr', text });
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      if (code === 0) {
        resolve({ result: stdout.trim() || '(command completed with no output)', metadata: { runtime: 'command', exit_code: code, command } });
      } else {
        resolve({
          result: `Command adapter failed with exit code ${code}.\n\nSTDOUT:\n${stdout.trim()}\n\nSTDERR:\n${stderr.trim()}`,
          metadata: { runtime: 'command', exit_code: code, command },
        });
      }
    });
    child.stdin.end(input || '');
  });
}

async function noopResult(message) {
  return {
    result: `Received work_request locally, but no runtime adapter is configured.\n\nContent:\n${message.content || ''}`,
    metadata: { runtime: 'noop' },
  };
}

export class HiveBridgeChannelRunner {
  constructor({
    client,
    runtime = 'noop',
    command = null,
    workDir = process.cwd(),
    downloadsDir = '.hive/local-agent-channel/attachments',
    webSocketClass = null,
    reconnectDelayMs = 1000,
  }) {
    this.client = client;
    this.runtime = runtime;
    this.command = command;
    this.workDir = workDir;
    this.downloadsDir = downloadsDir;
    this.webSocketClass = webSocketClass;
    this.reconnectDelayMs = reconnectDelayMs;
  }

  async connectUrl() {
    const ticket = await this.client.createChannelWsTicket();
    return this.client.channelWsUrl(String(ticket.ticket));
  }

  async prepareMessage(message) {
    const prepared = { ...message, attachments: [] };
    for (const raw of message.attachments || []) {
      const attachment = { ...raw };
      if (attachment.path) {
        try {
          const downloaded = await this.client.downloadChannelFile(String(attachment.path), join(this.downloadsDir, String(message.id)));
          attachment.local_path = downloaded.path;
          attachment.downloaded = true;
          attachment.size = downloaded.size;
        } catch (error) {
          attachment.downloaded = false;
          attachment.error = String(error?.message || error);
        }
      }
      prepared.attachments.push(attachment);
    }
    return prepared;
  }

  sendEvent(ws, sessionId, messageId, eventType, payload = {}) {
    ws.send(JSON.stringify({
      type: 'event',
      session_id: sessionId,
      message_id: messageId,
      event_type: eventType,
      payload,
    }));
  }

  async handleMessage(ws, message) {
    await mkdir(this.downloadsDir, { recursive: true });
    const messageId = String(message.id);
    const sessionId = String(message.session_id);
    ws.send(JSON.stringify({ type: 'ack', message_id: messageId }));
    const prepared = await this.prepareMessage(message);
    this.sendEvent(ws, sessionId, messageId, 'typing', { status: 'running' });
    const result = this.runtime === 'command' && this.command
      ? await commandResult(this.command, prepared.content || '', {
        cwd: this.workDir,
        onEvent: (eventType, payload) => this.sendEvent(ws, sessionId, messageId, eventType, payload),
      })
      : await noopResult(prepared);
    ws.send(JSON.stringify({
      type: 'result',
      session_id: sessionId,
      message_id: messageId,
      status: 'completed',
      output: String(result.result || ''),
      artifacts: result.attachments || [],
      metadata: result.metadata || {},
    }));
  }

  async runSession({ maxMessages = Number.POSITIVE_INFINITY } = {}) {
    const WebSocket = this.webSocketClass || (await import('ws')).WebSocket;
    const url = await this.connectUrl();
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(url);
      let processed = 0;
      let settled = false;
      let pending = Promise.resolve();
      const settle = (fn, value) => {
        if (settled) return;
        settled = true;
        fn(value);
      };
      ws.on('open', () => {});
      ws.on('message', async (data) => {
        try {
          const incoming = JSON.parse(data.toString());
          if (incoming.type === 'hello') {
            ws.send(JSON.stringify({
              type: 'ready',
              runtime_kind: this.runtime,
              capabilities: { file_upload: true, file_download: true, runtime: this.runtime },
            }));
            return;
          }
          if (incoming.type === 'ready_ack' || incoming.type === 'pong') return;
          if (incoming.type === 'message') {
            pending = pending.then(async () => {
              await this.handleMessage(ws, incoming.message);
              processed += 1;
              if (processed >= maxMessages) {
                ws.close();
              }
            });
            await pending;
            return;
          }
          if (incoming.type === 'error') settle(reject, new Error(String(incoming.error || 'Hive channel error')));
        } catch (error) {
          settle(reject, error);
        }
      });
      ws.on('close', () => {
        pending.then(() => settle(resolve, processed)).catch((error) => settle(reject, error));
      });
      ws.on('error', (error) => settle(reject, error));
    });
  }

  async runOnce() {
    return this.runSession({ maxMessages: 1 });
  }

  async runForever({ maxRuns = Number.POSITIVE_INFINITY } = {}) {
    let runs = 0;
    while (runs < maxRuns) {
      runs += 1;
      try {
        await this.runSession();
      } catch (_error) {
        if (this.reconnectDelayMs > 0) await sleep(this.reconnectDelayMs);
      }
    }
    return runs;
  }
}
