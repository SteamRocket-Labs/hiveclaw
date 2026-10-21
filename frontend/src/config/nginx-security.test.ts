import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const nginxConfigPath = fileURLToPath(new URL('../../nginx.conf', import.meta.url));

describe('nginx browser security headers', () => {
  it('keeps CSP in locations that override inherited add_header directives', () => {
    const config = readFileSync(nginxConfigPath, 'utf8');
    const cspOccurrences = config.match(/add_header Content-Security-Policy/g) || [];

    // Server default, source identity, SPA and hashed assets.
    expect(cspOccurrences).toHaveLength(4);
    const identityLocation = config.match(/location = \/build-identity\.json \{([^}]+)\}/)?.[1];
    expect(identityLocation).toContain('try_files $uri =404');
    expect(identityLocation).toContain('add_header Cache-Control "no-store"');
    expect(identityLocation).toContain('add_header Content-Security-Policy');
  });
});
