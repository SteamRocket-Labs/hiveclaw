import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

describe('nginx entrypoint resolver rewrite', () => {
    it('keeps IPv6 DNS lookups disabled when replacing the resolver address', () => {
        const script = readFileSync(resolve(process.cwd(), 'docker-entrypoint.sh'), 'utf8');

        const resolverRewrite = script
            .split('\n')
            .find(line => line.includes('resolver 127.0.0.11 valid=10s ipv6=off'));

        expect(resolverRewrite).toBeDefined();
        expect(resolverRewrite).toContain('resolver $RESOLVER valid=10s ipv6=off');
    });
});
