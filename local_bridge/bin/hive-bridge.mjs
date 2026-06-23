#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { hostname, platform, arch } from 'node:os';
import { resolve } from 'node:path';

import { HiveBridgeClient } from '../src/client.mjs';
import { HiveBridgeChannelRunner } from '../src/channel-runner.mjs';
import { FileTokenStore } from '../src/token-store.mjs';

const DEFAULT_BASE_URL = 'https://frontend-production-0346.up.railway.app';

function parseArgs(argv) {
  const result = { _: [] };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (!arg.startsWith('--')) {
      result._.push(arg);
      continue;
    }
    const key = arg.slice(2).replace(/-([a-z])/g, (_, char) => char.toUpperCase());
    const next = argv[index + 1];
    if (!next || next.startsWith('--')) {
      result[key] = true;
      continue;
    }
    if (result[key] !== undefined) {
      result[key] = Array.isArray(result[key]) ? [...result[key], next] : [result[key], next];
    } else {
      result[key] = next;
    }
    index += 1;
  }
  return result;
}

function printJson(payload) {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
}

function sleep(ms) {
  return new Promise((resolveSleep) => setTimeout(resolveSleep, ms));
}

function openBrowser(url) {
  const command = process.platform === 'darwin' ? 'open' : process.platform === 'win32' ? 'cmd' : 'xdg-open';
  const args = process.platform === 'win32' ? ['/c', 'start', '', url] : [url];
  const child = spawn(command, args, { detached: true, stdio: 'ignore' });
  child.unref();
}

async function clientFromConfig(args) {
  const store = new FileTokenStore({ configPath: args.config ? resolve(String(args.config)) : undefined });
  const config = await store.load();
  if (!config) throw new Error('Hive Bridge is not logged in. Run `hive-bridge login` first.');
  return new HiveBridgeClient({ baseUrl: args.baseUrl || config.base_url, token: config.token });
}

async function login(args) {
  const baseUrl = String(args.baseUrl || DEFAULT_BASE_URL).replace(/\/+$/, '');
  const store = new FileTokenStore({ configPath: args.config ? resolve(String(args.config)) : undefined });
  const client = new HiveBridgeClient({ baseUrl });
  const init = await client.initPairing({
    deviceName: args.deviceName || hostname(),
    clientKind: args.clientKind || 'generic_cli',
    deviceFingerprint: args.deviceFingerprint || `${platform()}:${arch()}:${hostname()}`,
    scopes: [],
  });
  process.stdout.write(`Pairing code: ${init.user_code}\n`);
  process.stdout.write('Open this Hive activation URL to finish browser login automatically:\n');
  process.stdout.write(`${init.verification_uri_complete}\n`);
  if (!args.noBrowser) openBrowser(init.verification_uri_complete);

  const started = Date.now();
  const expiresIn = Number(init.expires_in || 900) * 1000;
  const interval = Number(init.interval || 3) * 1000;
  while (Date.now() - started < expiresIn) {
    const exchanged = await client.exchangePairing(init.device_code);
    if (exchanged.status === 'pending') {
      await sleep(interval);
      continue;
    }
    await store.save({
      base_url: baseUrl,
      token: exchanged.access_token,
      connection_id: exchanged.connection_id,
      agent_id: exchanged.agent_id,
      tenant_id: exchanged.tenant_id,
    });
    printJson({ status: 'connected', agent_id: exchanged.agent_id || null });
    return;
  }
  throw new Error('Pairing timed out before approval.');
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const command = args._[0];
  if (!command || args.help) {
    process.stdout.write('Usage: hive-bridge <login|status|logout|upload|run|service> [options]\n');
    return;
  }
  if (command === 'login') return login(args);
  if (command === 'status') return printJson(await (await clientFromConfig(args)).status());
  if (command === 'logout') {
    await new FileTokenStore({ configPath: args.config ? resolve(String(args.config)) : undefined }).clear();
    process.stdout.write('Hive Bridge local token removed.\n');
    return;
  }
  if (command === 'upload') {
    const path = args._[1];
    if (!path) throw new Error('Usage: hive-bridge upload <path>');
    return printJson(await (await clientFromConfig(args)).uploadFile(path));
  }
  if (command === 'run') {
    const client = await clientFromConfig(args);
    const runtime = args.runtime || (args.command ? 'command' : 'noop');
    const commandStart = args._.indexOf('--command');
    const commandArgs = Array.isArray(args.command) ? args.command : args.command ? [args.command] : [];
    const runner = new HiveBridgeChannelRunner({
      client,
      runtime,
      command: commandArgs.length > 0 ? commandArgs : commandStart >= 0 ? args._.slice(commandStart + 1) : null,
      workDir: args.workDir || process.cwd(),
    });
    if (args.once) return printJson({ status: 'ok', transport: 'websocket', processed: await runner.runOnce() });
    process.stderr.write('Hive Bridge Local Agent Channel started. Press Ctrl-C to stop.\n');
    return runner.runForever();
  }
  if (command === 'service') {
    const action = args._[1];
    if (action === 'start') {
      args.once = false;
      args.runtime = args.runtime || 'noop';
      return mainWith(['run', ...process.argv.slice(4)]);
    }
    if (action === 'status') {
      process.stdout.write('Hive Bridge service mode is available through `hive-bridge run --transport websocket` foreground mode.\n');
      return;
    }
    throw new Error('Native service install/stop is not enabled yet. Use `hive-bridge service start` for foreground WebSocket mode.');
  }
  throw new Error(`Unknown command: ${command}`);
}

async function mainWith(argv) {
  process.argv = [process.argv[0], process.argv[1], ...argv];
  return main();
}

main().catch((error) => {
  process.stderr.write(`${error?.message || error}\n`);
  process.exitCode = 1;
});
