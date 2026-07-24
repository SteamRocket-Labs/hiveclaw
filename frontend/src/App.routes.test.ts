import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

describe('application surface routes', () => {
  it('mounts a real user home instead of redirecting members to the employee list', () => {
    const source = readFileSync(new URL('./App.tsx', import.meta.url), 'utf8');

    expect(source).toContain("const Dashboard = lazy(() => import('./pages/Dashboard'))");
    expect(source).toContain('<Route path="home" element={<Dashboard />} />');
    expect(source).not.toContain('<Route path="home" element={<Navigate to="/agents" replace />} />');
  });

  it('keeps the design gallery out of the production public surface', () => {
    const source = readFileSync(new URL('./App.tsx', import.meta.url), 'utf8');

    expect(source).not.toContain("const DesignGallery = lazy(() => import('./pages/DesignGallery'))");
    expect(source).toContain('const DesignGallery = import.meta.env.DEV');
    expect(source).toContain('DesignGallery ? (');
    expect(source).toContain('<Route path="/design-gallery" element={<Navigate to="/" replace />} />');
  });
});
