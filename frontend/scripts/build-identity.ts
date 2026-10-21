import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const sourcePaths = ['src', 'public', 'scripts', 'VERSION', 'package.json', 'package-lock.json',
  'index.html', 'vite.config.ts', 'tsconfig.json', 'nginx.conf', 'docker-entrypoint.sh', 'Dockerfile'];

export function frontendBuildIdentity(root: string, includedPaths = sourcePaths) {
  const files: string[] = [];
  function visit(relative: string) {
    const absolute = path.join(root, relative);
    const info = fs.lstatSync(absolute);
    if (info.isSymbolicLink()) throw new Error('Frontend identity requires canonical source files');
    if (info.isDirectory()) {
      for (const name of fs.readdirSync(absolute)) {
        if (name === '.DS_Store') continue;
        visit(path.posix.join(relative, name));
      }
    } else if (info.isFile()) {
      files.push(relative);
    } else {
      throw new Error('Frontend identity requires regular source files');
    }
  }
  for (const relative of includedPaths) visit(relative);
  if (files.length === 0) throw new Error('Frontend source identity is empty');
  const hash = createHash('sha256');
  for (const relative of files.sort()) {
    const absolute = path.join(root, relative);
    const body = fs.readFileSync(absolute);
    const mode = (fs.lstatSync(absolute).mode & 0o777).toString(8).padStart(4, '0');
    hash.update(`${relative}\0${mode}\0${body.length}\0`);
    hash.update(body);
    hash.update('\0');
  }
  const sha256 = hash.digest('hex');
  return { schema: 'hive.build_identity.v1', status: 'ok', revision: `source-sha256:${sha256}`,
    sha256, file_count: files.length };
}

export function frontendIdentityPlugin(root: string) {
  return {
    name: 'hive-frontend-build-identity',
    apply: 'build' as const,
    generateBundle(this: { emitFile: (file: { type: 'asset'; fileName: string; source: string }) => void }) {
      this.emitFile({ type: 'asset', fileName: 'build-identity.json',
        source: JSON.stringify(frontendBuildIdentity(root)) + '\n' });
    },
  };
}
