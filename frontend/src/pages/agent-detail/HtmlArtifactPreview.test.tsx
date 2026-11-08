// @vitest-environment jsdom
import { fireEvent, render, screen, cleanup } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ArtifactPreviewPanel, getArtifactOpenMode, getEffectiveArtifactPreviewKind } from './ArtifactSurface';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback?: string) => fallback || _key }) }));
vi.mock('./AgentWorkspaceSection', () => ({ default: () => null }));
afterEach(cleanup);
const t = ((_key: string, fallback?: string) => fallback || _key) as never;
const content = '<h1>Static report</h1><script>parent.postMessage("approval", "*")</script>';

it.each(['report.html', 'REPORT.HTM'])('opens %s as HTML despite legacy preview metadata', name => {
  for (const previewKind of ['text', 'download', undefined]) {
    const artifact = { name, path: `workspace/${name}`, previewKind };
    expect(getEffectiveArtifactPreviewKind(artifact)).toBe('html');
    expect(getArtifactOpenMode(artifact)).toBe('inspector_preview');
  }
});

it('renders a static frame and keeps original source and download available', () => {
  const download = vi.fn();
  render(<ArtifactPreviewPanel preview={{ artifact: { name: 'report.html', path: 'workspace/report.html', previewKind: 'text' }, content }}
    onClose={vi.fn()} onDownload={download} t={t} />);
  const frame = screen.getByTitle('report.html');
  expect(frame.tagName).toBe('IFRAME');
  expect(frame.getAttribute('sandbox')).toBe('');
  expect(frame.getAttribute('src')).toBeNull();
  const srcdoc = frame.getAttribute('srcdoc')!;
  expect(srcdoc).toContain("default-src 'none'");
  expect(srcdoc).toContain("connect-src 'none'");
  expect(srcdoc).toContain("script-src 'none'");
  expect(srcdoc).toContain('x-dns-prefetch-control');
  expect(srcdoc.indexOf('Content-Security-Policy')).toBeLessThan(srcdoc.indexOf(content));
  fireEvent.click(screen.getByRole('button', { name: 'View source' }));
  expect(screen.getByText(content).tagName).toBe('PRE');
  expect(screen.queryByTitle('report.html')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: /^Preview$/ }));
  expect(screen.getByTitle('report.html').getAttribute('sandbox')).toBe('');
  fireEvent.click(screen.getByRole('button', { name: 'Download report.html' }));
  expect(download).toHaveBeenCalledOnce();
});

it('shows source and a size note instead of executing a very large document', () => {
  const large = '<h1>Large report</h1>' + 'x'.repeat(1_000_000);
  render(<ArtifactPreviewPanel preview={{ artifact: { name: 'large.htm', path: 'workspace/large.htm' }, content: large }}
    onClose={vi.fn()} onDownload={vi.fn()} t={t} />);
  expect(screen.queryByTitle('large.htm')).toBeNull();
  expect(screen.getByText('This file is large. Source is shown; you can also download it.')).toBeTruthy();
  expect(screen.getByText(large).tagName).toBe('PRE');
  expect(screen.getByRole('button', { name: 'Download large.htm' })).toBeTruthy();
});
