import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const nginxConfigPath = fileURLToPath(new URL('../../nginx.conf', import.meta.url));

describe('nginx browser security headers', () => {
  it('keeps CSP in locations that override inherited add_header directives', () => {
    const config = readFileSync(nginxConfigPath, 'utf8');
    const cspOccurrences = config.match(/add_header Content-Security-Policy/g) || [];

    // server default + location / + location /assets/
    expect(cspOccurrences).toHaveLength(3);
  });
});
