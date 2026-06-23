import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { basename, join } from 'node:path';

export class HiveBridgeClient {
  constructor({ baseUrl, token = null, fetchImpl = globalThis.fetch } = {}) {
    if (!baseUrl) throw new Error('baseUrl is required');
    if (!fetchImpl) throw new Error('fetch is not available; use Node.js >=20');
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    this.token = token;
    this.fetchImpl = fetchImpl;
  }

  url(path) {
    return `${this.baseUrl}/${path.replace(/^\/+/, '')}`;
  }

  headers(extra = {}) {
    return {
      ...extra,
      ...(this.token ? { authorization: `Bearer ${this.token}` } : {}),
    };
  }

  async request(method, path, { json, body, headers } = {}) {
    const response = await this.fetchImpl(this.url(path), {
      method,
      headers: this.headers({
        ...(json !== undefined ? { 'content-type': 'application/json' } : {}),
        ...(headers || {}),
      }),
      body: json !== undefined ? JSON.stringify(json) : body,
    });
    if (!response.ok) {
      const text = await response.text().catch(() => '');
      throw new Error(`Hive request failed ${response.status}: ${text || response.statusText}`);
    }
    if (response.status === 204) return {};
    const text = await response.text();
    return text ? JSON.parse(text) : {};
  }

  initPairing({ deviceName, clientKind, deviceFingerprint, scopes = [] }) {
    return this.request('POST', '/api/v1/local-bridge/pairing/init', {
      json: {
        device_name: deviceName,
        client_kind: clientKind,
        device_fingerprint: deviceFingerprint,
        scopes,
      },
    });
  }

  exchangePairing(deviceCode) {
    return this.request('POST', '/api/v1/local-bridge/pairing/exchange', {
      json: { device_code: deviceCode },
    });
  }

  status() {
    return this.request('GET', '/api/v1/local-bridge/status');
  }

  pollInbox() {
    return this.request('GET', '/api/v1/gateway/poll');
  }

  sendMessage({ target, content, channel, clientMessageId }) {
    return this.request('POST', '/api/v1/gateway/send-message', {
      json: {
        target,
        content,
        ...(channel ? { channel } : {}),
        ...(clientMessageId ? { client_message_id: clientMessageId } : {}),
      },
    });
  }

  reportResult({ messageId, result, attachments = [], metadata = {} }) {
    return this.request('POST', '/api/v1/gateway/report', {
      json: {
        message_id: messageId,
        result,
        attachments,
        metadata,
      },
    });
  }

  createChannelWsTicket() {
    return this.request('POST', '/api/v1/local-bridge/channel/ws-ticket');
  }

  channelWsUrl(ticket) {
    const base = this.baseUrl.startsWith('https://')
      ? `wss://${this.baseUrl.slice('https://'.length)}`
      : this.baseUrl.startsWith('http://')
        ? `ws://${this.baseUrl.slice('http://'.length)}`
        : this.baseUrl;
    return `${base}/api/v1/local-bridge/channel/ws?ticket=${encodeURIComponent(ticket)}`;
  }

  reportChannelResult({ sessionId, messageId, output, status = 'completed', artifacts = [], metadata = {} }) {
    return this.request('POST', '/api/v1/local-bridge/channel/report', {
      json: {
        session_id: sessionId,
        message_id: messageId,
        status,
        output,
        artifacts,
        metadata,
      },
    });
  }

  async uploadFile(path) {
    const content = await readFile(path);
    const form = new FormData();
    form.append('file', new Blob([content]), basename(path));
    return this.request('POST', '/api/v1/local-bridge/upload', { body: form });
  }

  async downloadChannelFile(path, destinationDir) {
    await mkdir(destinationDir, { recursive: true });
    const response = await this.fetchImpl(
      `${this.url('/api/v1/local-bridge/channel/workspace/download')}?path=${encodeURIComponent(path)}`,
      { method: 'GET', headers: this.headers() },
    );
    if (!response.ok) {
      const text = await response.text().catch(() => '');
      throw new Error(`Hive download failed ${response.status}: ${text || response.statusText}`);
    }
    const bytes = new Uint8Array(await response.arrayBuffer());
    const target = join(destinationDir, basename(path) || 'download');
    await writeFile(target, bytes);
    return { path: target, source_path: path, size: bytes.byteLength };
  }
}
