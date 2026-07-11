import assert from 'node:assert/strict';
import { mkdtemp, readdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { describe, it } from 'node:test';

import { LocalExecutionReceiptStore } from '../src/execution-receipts.mjs';

describe('npm local execution receipt ledger', () => {
  it('preserves concurrent writers and keeps the first result for a replay key', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hive-receipts-'));
    const path = join(root, 'execution-receipts.jsonl');
    const stores = Array.from({ length: 40 }, () => new LocalExecutionReceiptStore({ path, maxRecords: 100 }));

    await Promise.all(stores.map((store, index) => store.put(`key-${index}`, { output: `result-${index}` })));
    await stores[0].put('key-0', { output: 'must-not-overwrite' });

    for (let index = 0; index < stores.length; index += 1) {
      assert.deepEqual(await stores[0].get(`key-${index}`), { output: `result-${index}` });
    }
  });

  it('quarantines corrupt ledgers and recovers stale process locks', async () => {
    const root = await mkdtemp(join(tmpdir(), 'hive-receipts-corrupt-'));
    const path = join(root, 'execution-receipts.jsonl');
    await writeFile(path, '{not-json}\n', 'utf8');
    await writeFile(
      `${path}.lock`,
      JSON.stringify({ pid: 99999999, created_at_ms: Date.now() - 120_000 }),
      'utf8',
    );
    const store = new LocalExecutionReceiptStore({ path, lockTimeoutMs: 1000, staleLockMs: 10 });

    assert.equal(await store.get('missing'), null);
    await store.put('safe', { output: 'recovered' });

    assert.deepEqual(await store.get('safe'), { output: 'recovered' });
    assert.ok((await readdir(root)).some((name) => name.startsWith('execution-receipts.jsonl.corrupt-')));
  });
});
