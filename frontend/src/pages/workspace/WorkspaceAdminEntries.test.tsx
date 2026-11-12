import { renderToStaticMarkup } from 'react-dom/server';
import { expect, it, vi } from 'vitest';
import WorkspaceInfoSection from './WorkspaceInfoSection';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
vi.mock('react-router-dom', () => ({ Link: ({ to, children }: any) => <a href={to}>{children}</a> }));
vi.mock('./WorkspaceStorageSection', () => ({ default: () => null }));

it('links company connections to the existing company knowledge upload and import page', () => {
  const markup = renderToStaticMarkup(<WorkspaceInfoSection selectedTenantId="company" canManageCompanyContent
    canEditCompanyIntro={false} companyNameEditor={null} companyTimezoneEditor={null} companyIntro=""
    onCompanyIntroChange={() => {}} onSaveCompanyIntro={() => {}} companyIntroSaving={false} companyIntroSaved={false}
    legacyCompanyFilesCard={null} themeColorPicker={null} broadcastSection={null} onDeleteCompany={() => {}} />);
  expect(markup).toContain('href="/enterprise/knowledge"');
  expect(markup).toContain('Company knowledge');
  expect(markup).toContain('Upload and import');
  // G4b: the link lands directly on the MCP 与插件 tab, not the AI 资产 tab.
  expect(markup).toContain('href="/enterprise/extensions?view=mcp"');
  expect(markup).toContain('Connect applications and review tools');
});
