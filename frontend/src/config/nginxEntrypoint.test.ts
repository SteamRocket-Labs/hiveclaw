import { describe, expect, it } from 'vitest';
import script from '../../docker-entrypoint.sh?raw';

describe('nginx entrypoint resolver rewrite', () => {
    it('keeps IPv6 DNS lookups disabled when replacing the resolver address', () => {
        const resolverRewrite = script
            .split('\n')
            .find((line: string) => line.includes('resolver 127.0.0.11 valid=10s ipv6=off'));

        expect(resolverRewrite).toBeDefined();
        expect(resolverRewrite).toContain('resolver $RESOLVER valid=10s ipv6=off');
    });
});
