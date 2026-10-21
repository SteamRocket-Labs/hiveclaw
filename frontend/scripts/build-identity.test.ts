// @vitest-environment node
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, expect, it } from 'vitest';
import { frontendBuildIdentity } from './build-identity';

const roots: string[] = [];
afterEach(() => { for (const root of roots.splice(0)) fs.rmSync(root, { recursive: true }); });

it('identifies source bytes and executable modes independently of edit timestamps', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hive-frontend-identity-'));
  roots.push(root);
  const file = path.join(root, 'main.ts');
  fs.writeFileSync(file, 'AAAA');
  const first = frontendBuildIdentity(root, ['main.ts']);
  fs.utimesSync(file, new Date(0), new Date(0));
  expect(frontendBuildIdentity(root, ['main.ts'])).toEqual(first);
  fs.writeFileSync(file, 'BBBB');
  const changed = frontendBuildIdentity(root, ['main.ts']);
  expect(changed.sha256).not.toEqual(first.sha256);
  fs.chmodSync(file, 0o755);
  expect(frontendBuildIdentity(root, ['main.ts']).sha256).not.toEqual(changed.sha256);
});

it('refuses a linked source instead of identifying another tree', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hive-frontend-identity-'));
  roots.push(root);
  fs.writeFileSync(path.join(root, 'actual.ts'), 'PRIVATE');
  fs.symlinkSync(path.join(root, 'actual.ts'), path.join(root, 'linked.ts'));
  expect(() => frontendBuildIdentity(root, ['linked.ts'])).toThrow('canonical source');
});
