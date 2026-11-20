import { describe, expect, it } from 'vitest';
import nginxConf from '../../nginx.conf?raw';
import entrypoint from '../../docker-entrypoint.sh?raw';

describe('runtime pool nginx routing', () => {
    it('routes control-plane paths to backend-api and leaves unknown API paths on runtime backend', () => {
        expect(nginxConf).toContain('set $backend_api_upstream backend-api:8000;');
        expect(nginxConf).toContain('set $backend_runtime_upstream backend:8000;');
        expect(nginxConf).toContain('location ~ ^/api/(v1/)?auth/');
        expect(nginxConf).toContain('notifications');
        // P3a1 routes new-session sends through the same API upstream as existing-session sends.
        expect(nginxConf).toContain('location ~ ^/api/(v1/)?agents/[^/]+/sessions/(runs/?$|[^/]+/runs) {');
        expect(nginxConf).toContain('location /api/');
        expect(nginxConf).toContain('proxy_pass http://$backend_runtime_upstream;');
    });

    it('matches both send routes without admitting new-session subpaths', () => {
        const match = nginxConf.match(/^\s*location ~ (\^\/api\/\S+\/sessions\/\S*runs\S*) \{/m);
        expect(match).not.toBeNull();
        const location = new RegExp(match![1]);

        for (const prefix of ['/api', '/api/v1']) {
            const sessions = `${prefix}/agents/x/sessions`;
            expect(location.test(`${sessions}/runs`)).toBe(true);
            expect(location.test(`${sessions}/runs/`)).toBe(true);
            expect(location.test(`${sessions}/y/runs`)).toBe(true);
            expect(location.test(sessions)).toBe(false);
            expect(location.test(`${sessions}/y/messages`)).toBe(false);
            expect(location.test(`${sessions}/y`)).toBe(false);
            expect(location.test(`${sessions}/runs/extra`)).toBe(false);
        }
    });

    it('lets production set different internal hosts for api and runtime planes', () => {
        expect(entrypoint).toContain('BACKEND_API_HOST');
        expect(entrypoint).toContain('BACKEND_RUNTIME_HOST');
        expect(entrypoint).toContain('backend-api:8000');
        expect(entrypoint).toContain('backend:8000');
    });

    it('compresses large API JSON responses before they reach the browser', () => {
        expect(nginxConf).toContain('gzip on;');
        expect(nginxConf).toContain('gzip_types');
        expect(nginxConf).toContain('application/json');
    });
});
