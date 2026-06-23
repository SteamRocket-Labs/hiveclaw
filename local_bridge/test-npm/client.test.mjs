import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { mkdtemp, readFile, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { HiveBridgeClient } from '../src/client.mjs';
import { FileTokenStore } from '../src/token-store.mjs';

describe('npm hive-bridge client', () => {
  it('uses bridge bearer auth and API v1 paths', async () => {
    const seen = [];
    const client = new HiveBridgeClient({
      baseUrl: 'https://hive.example/',
      token: 'hb_test',
      fetchImpl: async (url, init) => {
        seen.push({ url: String(url), method: init.method, authorization: init.headers.authorization });
        return Response.json({ status: 'ok' });
      },
    });

    assert.deepEqual(await client.status(), { status: 'ok' });

    assert.deepEqual(seen, [
      {
        url: 'https://hive.example/api/v1/local-bridge/status',
        method: 'GET',
        authorization: 'Bearer hb_test',
      },
    ]);
  });

  it('stores local bridge token as 0600 json', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'hive-bridge-npm-'));
    const configPath = join(dir, 'config.json');
    try {
      const store = new FileTokenStore({ configPath });
      await store.save({
        base_url: 'https://hive.example',
        token: 'hb_live',
        connection_id: 'connection-1',
        tenant_id: 'tenant-1',
      });

      assert.deepEqual(await store.load(), {
        base_url: 'https://hive.example',
        token: 'hb_live',
        connection_id: 'connection-1',
        tenant_id: 'tenant-1',
      });
      assert.equal((await stat(configPath)).mode & 0o777, 0o600);
      assert.match(await readFile(configPath, 'utf8'), /hb_live/);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

});
