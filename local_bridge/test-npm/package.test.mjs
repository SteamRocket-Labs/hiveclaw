import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { describe, it } from 'node:test';

describe('npm package contract', () => {
  it('publishes hive-bridge as the npm-installed CLI and includes the skill', async () => {
    const pkg = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'));
    const skill = await readFile(new URL('../skills/hive-bridge/SKILL.md', import.meta.url), 'utf8');
    const readme = await readFile(new URL('../README.md', import.meta.url), 'utf8');
    const cli = await readFile(new URL('../bin/hive-bridge.mjs', import.meta.url), 'utf8');

    assert.equal(pkg.name, '@hiveclaw243/hive-bridge');
    assert.equal(pkg.version, '0.1.5');
    assert.equal(pkg.bin['hive-bridge'], 'bin/hive-bridge.mjs');
    assert.ok(pkg.files.includes('skills/hive-bridge/SKILL.md'));
    assert.ok(pkg.scripts.test.includes('node --test'));
    assert.doesNotMatch(pkg.description, /MCP/i);
    assert.ok(!pkg.files.includes('src/mcp-server.mjs'));
    assert.match(skill, /npm install -g @hiveclaw243\/hive-bridge/);
    assert.match(skill, /long-lived binding/i);
    assert.match(skill, /automatically approve/i);
    assert.match(skill, /reconnect/i);
    assert.doesNotMatch(skill, /\bmcp\b/i);
    assert.doesNotMatch(skill, /approve the Local Agent Link/i);
    assert.match(cli, /https:\/\/frontend-production-0346\.up\.railway\.app/);
    assert.match(cli, /browser login/i);
    assert.doesNotMatch(cli, /approve the Local Agent Link/i);
    assert.doesNotMatch(cli, /https:\/\/try\.hive\.ai/);
    assert.match(readme, /hive-bridge login/);
    assert.doesNotMatch(readme, /login --base-url/);
    assert.doesNotMatch(skill, /login --base-url/);
  });

  it('ships a standard skills CLI package shape for npx skills add', async () => {
    const skillPackageReadme = await readFile(new URL('../skill-package/README.md', import.meta.url), 'utf8');
    const skillPackageSkill = await readFile(
      new URL('../skill-package/skills/hive-bridge/SKILL.md', import.meta.url),
      'utf8',
    );

    assert.match(
      skillPackageReadme,
      /npx skills add https:\/\/github\.com\/rocky2431\/hive-bridge-skill --skill hive-bridge/,
    );
    assert.match(skillPackageReadme, /npm install -g @hiveclaw243\/hive-bridge/);
    assert.match(skillPackageSkill, /name: hive-bridge/);
    assert.match(skillPackageSkill, /npm install -g @hiveclaw243\/hive-bridge/);
    assert.match(skillPackageSkill, /long-lived binding/i);
    assert.match(skillPackageSkill, /automatically approve/i);
    assert.match(skillPackageSkill, /reconnect/i);
    assert.match(skillPackageSkill, /hive-bridge login/);
    assert.match(skillPackageSkill, /hive-bridge status/);
    assert.match(skillPackageSkill, /hive-bridge run --transport websocket/);
    assert.doesNotMatch(skillPackageReadme, /\bmcp\b/i);
    assert.doesNotMatch(skillPackageSkill, /\bmcp\b/i);
    assert.doesNotMatch(skillPackageSkill, /approve the Local Agent Link/i);
  });
});
