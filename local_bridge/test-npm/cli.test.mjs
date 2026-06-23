import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { describe, it } from 'node:test';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);

describe('npm hive-bridge CLI', () => {
  it('does not expose MCP in the P0 command surface', async () => {
    const { stdout } = await execFileAsync('node', ['bin/hive-bridge.mjs', '--help'], {
      cwd: new URL('..', import.meta.url),
    });

    assert.match(stdout, /login\|status\|logout\|upload\|run\|service/);
    assert.doesNotMatch(stdout, /\bmcp\b/i);
  });
});
