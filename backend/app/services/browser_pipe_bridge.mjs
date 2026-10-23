// Private CDP transport. The browser never receives the host process environment.
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { createHmac, randomUUID } from 'node:crypto';
import { openSync, closeSync, writeFileSync, fsyncSync, renameSync } from 'node:fs';
import { dirname } from 'node:path';

let child;
let closing = false;
let shutdownTimer;
let buffer = Buffer.alloc(0);
let maximumBytes;
let groupCleaned = false;
let launchFailed = false;
let lifecycle;
const send = (value, callback) => process.stdout.write(`${JSON.stringify(value)}\n`, callback);
const record = (phase) => {
  if (!lifecycle) return true;
  try {
    const payload = ['hive.browser.lifecycle.v1', lifecycle.resource_id, lifecycle.nonce,
      lifecycle.generation, phase, child?.pid ?? process.pid, process.pid];
    const signature = createHmac('sha256', Buffer.from(lifecycle.secret, 'hex'))
      .update(JSON.stringify(payload)).digest('hex');
    const temporary = `${lifecycle.receipt_path}.${randomUUID()}.tmp`;
    const descriptor = openSync(temporary, 'wx', 0o600);
    try { writeFileSync(descriptor, JSON.stringify({ payload, signature })); fsyncSync(descriptor); }
    finally { closeSync(descriptor); }
    renameSync(temporary, lifecycle.receipt_path);
    const directory = openSync(dirname(lifecycle.receipt_path), 'r');
    try { fsyncSync(directory); } finally { closeSync(directory); }
    return true;
  } catch { return false; }
};
const signalGroup = (signal) => {
  if (!Number.isInteger(child?.pid)) return true;
  try { process.kill(-child.pid, signal); return true; }
  catch (error) {
    if (error.code === 'ESRCH') return true;
    send({ type: 'cleanup_failed' }); return false;
  }
};
const shutdown = () => {
  if (closing) return;
  closing = true;
  if (!child) return send({ type: 'closed' }, () => process.exit(0));
  if (child.exitCode !== null || child.signalCode !== null) return;
  signalGroup('SIGTERM');
  shutdownTimer = setTimeout(() => signalGroup('SIGKILL'), 2000);
};
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
process.stdout.on('error', shutdown);
const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on('close', shutdown);
input.on('line', (line) => {
  try {
    const frame = JSON.parse(line);
    if (!child) {
      maximumBytes = frame.maximum_message_bytes;
      lifecycle = frame.lifecycle;
      if (frame.type !== 'launch' || !Number.isSafeInteger(maximumBytes) || maximumBytes <= 0) {
        throw new Error('invalid launch');
      }
      if (lifecycle && (!/^[a-f0-9]{64}$/.test(lifecycle.secret)
          || typeof lifecycle.receipt_path !== 'string' || !record('launch_intent'))) {
        send({ type: 'launch_failed' }, () => process.exit(1)); return;
      }
      child = spawn(frame.executable, frame.arguments, {
        cwd: frame.cwd, env: frame.environment, detached: true,
        stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'],
      });
      child.once('error', () => {
        launchFailed = true;
        record('closed');
        send({ type: 'launch_failed' }, () => process.exit(1));
      });
      child.once('spawn', () => {
        if (!record('started')) { send({ type: 'protocol_failed' }); shutdown(); return; }
        send({ type: 'ready', pid: child.pid });
      });
      child.once('exit', () => {
        clearTimeout(shutdownTimer);
        // Reap descendants too, including children that ignored SIGTERM.
        groupCleaned = signalGroup('SIGKILL');
      });
      child.once('close', (code, signal) => {
        if (launchFailed) return;
        // Persist before replying: stdin/stdout may have vanished with the host.
        if (groupCleaned) record('closed');
        send({ type: groupCleaned ? 'closed' : 'cleanup_failed', code, signal },
          () => process.exit(groupCleaned ? 0 : 1));
      });
      child.stdio[3].on('error', () => { send({ type: 'pipe_failed' }); shutdown(); });
      child.stdio[4].on('error', () => { send({ type: 'pipe_failed' }); shutdown(); });
      child.stdio[4].on('data', (chunk) => {
        buffer = Buffer.concat([buffer, chunk]);
        let end;
        while ((end = buffer.indexOf(0)) !== -1) {
          if (end > maximumBytes) {
            send({ type: 'message_limit' }); shutdown(); return;
          }
          const body = buffer.subarray(0, end);
          buffer = buffer.subarray(end + 1);
          try {
            const flowing = send({ type: 'message', message: JSON.parse(body.toString('utf8')) });
            if (!flowing) child.stdio[4].pause();
          }
          catch { send({ type: 'protocol_failed' }); shutdown(); return; }
        }
        if (buffer.length > maximumBytes) { send({ type: 'message_limit' }); shutdown(); }
      });
    } else if (frame.type === 'close') {
      shutdown();
    } else if (frame.type === 'command' && !closing) {
      const body = JSON.stringify(frame.message);
      if (Buffer.byteLength(body) > maximumBytes) throw new Error('command limit');
      child.stdio[3].write(`${body}\0`);
    } else {
      throw new Error('invalid frame');
    }
  } catch {
    send({ type: 'protocol_failed' }); shutdown();
  }
});
process.stdout.on('drain', () => child?.stdio[4].resume());
