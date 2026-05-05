import { describe, expect, it } from 'vitest';
import nginxConf from '../../nginx.conf?raw';

describe('nginx access logging', () => {
    it('uses a custom access log format that omits query strings', () => {
        expect(nginxConf).toContain('log_format main_sanitized');
        expect(nginxConf).toContain('$request_method $uri $server_protocol');
        expect(nginxConf).toContain('access_log /dev/stdout main_sanitized;');
        expect(nginxConf).not.toContain('$request"');
    });
});
