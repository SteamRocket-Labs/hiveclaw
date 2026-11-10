import { describe, expect, it } from 'vitest';
import nginxConf from '../../nginx.conf?raw';

const locations = [...nginxConf.matchAll(/location\s+([^\n{]+)\{([^{}]*)\}/g)]
  .map((match) => ({ route: match[1].trim(), body: match[2] }));
const executeRoute = '~ ^/api/(v1/)?agents/[^/]+/commands/[^/]+/execute/?$';

describe('synchronous session command gateway', () => {
  it('extends only execute read/send timeouts and preserves every existing timeout', () => {
    const timeouts = locations.filter(({ body }) => body.includes('proxy_read_timeout'))
      .map(({ route, body }) => [route, body.match(/proxy_read_timeout (\w+);/)?.[1], body.match(/proxy_send_timeout (\w+);/)?.[1]]);
    expect(timeouts).toEqual([
      ['~ ^/api/(v1/)?auth/', '120s', '120s'],
      ['~ ^/api/(v1/)?(users|tenants|organization|feature-flags|notification|notifications|desktop-auth|desktop-sync)/', '30s', '30s'],
      ['~ ^/api/(v1/)?agents/?$', '30s', '30s'],
      ['~ ^/api/(v1/)?agents/[^/]+/?$', '30s', '30s'],
      ['~ ^/api/(v1/)?agents/[^/]+/sessions/?$', '30s', '30s'],
      ['~ ^/api/(v1/)?agents/[^/]+/sessions/[^/]+/runs', '120s', '30s'],
      ['= /api/health', '5s', '5s'],
      [executeRoute, '600s', '600s'],
      ['/api/', '120s', '120s'],
    ]);
  });

  it('reuses runtime upstream, headers and retry behavior without matching other command routes', () => {
    const execute = locations.find(({ route }) => route === executeRoute);
    const fallback = locations.find(({ route }) => route === '/api/');
    expect(execute).toBeDefined();
    expect(execute!.body.replaceAll('600s', '120s').trim()).toBe(fallback!.body.trim());
    const route = new RegExp(executeRoute.slice(2));
    for (const path of ['/api/agents/a/commands/compact/execute', '/api/v1/agents/a/commands/help/execute/']) {
      expect(route.test(path)).toBe(true);
    }
    for (const path of ['/api/agents/a/commands/compact', '/api/agents/a/commands', '/api/agents/a/sessions/s/runs', '/api/auth/login', '/api/agents/a/commands/compact/execute/other']) {
      expect(route.test(path)).toBe(false);
    }
  });
});
