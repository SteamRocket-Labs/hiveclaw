import { mkdir, readFile, rm, chmod, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { homedir } from 'node:os';

export function defaultConfigPath() {
  return join(homedir(), '.hive', 'bridge', 'config.json');
}

export class FileTokenStore {
  constructor({ configPath = defaultConfigPath() } = {}) {
    this.configPath = configPath;
  }

  async load() {
    try {
      return JSON.parse(await readFile(this.configPath, 'utf8'));
    } catch (error) {
      if (error?.code === 'ENOENT') return null;
      throw error;
    }
  }

  async save(config) {
    await mkdir(dirname(this.configPath), { recursive: true });
    const tmpPath = `${this.configPath}.tmp`;
    await writeFile(tmpPath, `${JSON.stringify(config, null, 2)}\n`, 'utf8');
    await chmod(tmpPath, 0o600);
    await rm(this.configPath, { force: true });
    await writeFile(this.configPath, await readFile(tmpPath));
    await chmod(this.configPath, 0o600);
    await rm(tmpPath, { force: true });
  }

  async clear() {
    await rm(this.configPath, { force: true });
  }
}
