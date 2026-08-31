import { readFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../shared/SurfaceLayout', () => ({
  default: ({
    headingKey,
    headingFallback,
    navItems,
  }: {
    headingKey: string;
    headingFallback: string;
    navItems: Array<{ to: string; fallbackLabel: string }>;
  }) => (
    <div data-heading-key={headingKey} data-heading={headingFallback}>
      {navItems.map((item) => <a key={item.to} href={item.to}>{item.fallbackLabel}</a>)}
    </div>
  ),
}));

import AdminLayout from './AdminLayout';

describe('AdminLayout navigation', () => {
  it('keeps platform settings visible and provides a direct route back to the app', () => {
    const markup = renderToStaticMarkup(<AdminLayout />);

    expect(markup).toContain('data-heading-key="nav.platformSettings"');
    expect(markup).toContain('href="/admin/platform-settings"');
    expect(markup).toContain('href="/home"');
    expect(markup).toContain('Back to App');
  });

  it('points between routes that are mounted by the production app router', () => {
    const appSource = readFileSync(new URL('../../App.tsx', import.meta.url), 'utf8');

    expect(appSource).toContain('<Route path="/admin" element={<ProtectedRoute><AdminGuard><AdminLayout /></AdminGuard></ProtectedRoute>}>');
    expect(appSource).toContain('<Route path="platform-settings" element={<AdminCompanies />} />');
    expect(appSource).toContain('<Route path="home" element={<Dashboard />} />');
  });
});
