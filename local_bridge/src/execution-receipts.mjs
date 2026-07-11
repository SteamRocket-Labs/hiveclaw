import {
  mkdir,
  open,
  readFile,
  rename,
  rm,
  stat,
} from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { homedir } from 'node:os';
import { randomUUID } from 'node:crypto';

const RECORD_SCHEMA = 'hive.local_execution_receipt.v1';

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function processIsAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code !== 'ESRCH';
  }
}

function quarantineSuffix() {
  return `corrupt-${new Date().toISOString().replaceAll(/[:.]/g, '')}-${process.pid}-${randomUUID().slice(0, 8)}`;
}

export function defaultExecutionReceiptPath() {
  return join(homedir(), '.hive', 'local-agent-channel', 'execution-receipts.jsonl');
}

export class LocalExecutionReceiptStore {
  constructor({
    path = defaultExecutionReceiptPath(),
    maxRecords = 1000,
    lockTimeoutMs = 30_000,
    staleLockMs = 60_000,
  } = {}) {
    this.path = path;
    this.lockPath = `${path}.lock`;
    this.maxRecords = Math.max(1, Number(maxRecords) || 1000);
    this.lockTimeoutMs = Math.max(1, Number(lockTimeoutMs) || 30_000);
    this.staleLockMs = Math.max(1, Number(staleLockMs) || 60_000);
  }

  async _lockIsStale() {
    try {
      const [raw, details] = await Promise.all([
        readFile(this.lockPath, 'utf8'),
        stat(this.lockPath),
      ]);
      const payload = JSON.parse(raw);
      return !processIsAlive(Number(payload.pid)) || Date.now() - details.mtimeMs > this.staleLockMs;
    } catch (_error) {
      return true;
    }
  }

  async _withLock(operation) {
    await mkdir(dirname(this.path), { recursive: true });
    const deadline = Date.now() + this.lockTimeoutMs;
    let handle = null;
    while (!handle) {
      try {
        handle = await open(this.lockPath, 'wx', 0o600);
        await handle.writeFile(JSON.stringify({ pid: process.pid, created_at_ms: Date.now() }), 'utf8');
        await handle.sync();
      } catch (error) {
        if (error?.code !== 'EEXIST') throw error;
        if (await this._lockIsStale()) {
          await rm(this.lockPath, { force: true });
          continue;
        }
        if (Date.now() >= deadline) throw new Error(`Timed out waiting for receipt lock: ${this.lockPath}`);
        await sleep(10);
      }
    }
    try {
      return await operation();
    } finally {
      await handle.close();
      await rm(this.lockPath, { force: true });
    }
  }

  async _syncParentDirectory() {
    let directory = null;
    try {
      directory = await open(dirname(this.path), 'r');
      await directory.sync();
    } catch (_error) {
      // Directory fsync is unsupported on some Windows filesystems. File fsync
      // still protects the receipt bytes; rename remains atomic on one volume.
    } finally {
      await directory?.close();
    }
  }

  _serializeRecord(replayKey, result, storedAt = new Date().toISOString()) {
    return JSON.stringify({
      schema: RECORD_SCHEMA,
      replay_key: replayKey,
      stored_at: storedAt,
      result: structuredClone(result),
    });
  }

  async _writeSnapshotUnlocked(records) {
    const temporaryPath = `${this.path}.tmp-${process.pid}-${randomUUID()}`;
    const handle = await open(temporaryPath, 'wx', 0o600);
    try {
      const payload = [...records.entries()]
        .map(([key, record]) => this._serializeRecord(key, record.result, record.storedAt))
        .join('\n');
      await handle.writeFile(payload ? `${payload}\n` : '', 'utf8');
      await handle.sync();
    } finally {
      await handle.close();
    }
    await rename(temporaryPath, this.path);
    await this._syncParentDirectory();
  }

  async _loadUnlocked() {
    let raw;
    try {
      raw = await readFile(this.path, 'utf8');
    } catch (error) {
      if (error?.code === 'ENOENT') return new Map();
      throw error;
    }
    const records = new Map();
    const corruptLines = [];
    for (const line of raw.split('\n')) {
      if (!line.trim()) continue;
      try {
        const record = JSON.parse(line);
        if (
          record.schema !== RECORD_SCHEMA
          || typeof record.replay_key !== 'string'
          || !record.replay_key.trim()
          || !record.result
          || typeof record.result !== 'object'
          || Array.isArray(record.result)
        ) {
          throw new Error('invalid receipt record schema');
        }
        if (!records.has(record.replay_key)) {
          records.set(record.replay_key, {
            storedAt: String(record.stored_at || new Date(0).toISOString()),
            result: record.result,
          });
        }
      } catch (_error) {
        corruptLines.push(line);
      }
    }
    if (corruptLines.length > 0) {
      const quarantinePath = `${this.path}.${quarantineSuffix()}`;
      const quarantine = await open(quarantinePath, 'wx', 0o600);
      try {
        await quarantine.writeFile(`${corruptLines.join('\n')}\n`, 'utf8');
        await quarantine.sync();
      } finally {
        await quarantine.close();
      }
      await this._writeSnapshotUnlocked(records);
    }
    return records;
  }

  async get(replayKey) {
    const cleanKey = String(replayKey || '').trim();
    if (!cleanKey) return null;
    return this._withLock(async () => {
      const record = (await this._loadUnlocked()).get(cleanKey);
      return record ? structuredClone(record.result) : null;
    });
  }

  async put(replayKey, result) {
    const cleanKey = String(replayKey || '').trim();
    if (!cleanKey) throw new Error('replay_key is required');
    if (!result || typeof result !== 'object' || Array.isArray(result)) throw new TypeError('result must be an object');
    await this._withLock(async () => {
      const records = await this._loadUnlocked();
      if (records.has(cleanKey)) return;
      const storedAt = new Date().toISOString();
      records.set(cleanKey, { storedAt, result: structuredClone(result) });
      if (records.size > this.maxRecords) {
        const retained = [...records.entries()].slice(-this.maxRecords);
        await this._writeSnapshotUnlocked(new Map(retained));
        return;
      }
      const handle = await open(this.path, 'a', 0o600);
      try {
        await handle.writeFile(`${this._serializeRecord(cleanKey, result, storedAt)}\n`, 'utf8');
        await handle.sync();
      } finally {
        await handle.close();
      }
      await this._syncParentDirectory();
    });
  }
}
