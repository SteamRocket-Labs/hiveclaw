import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import WorkspaceInfoSection from './WorkspaceInfoSection';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key.split('.').pop() ?? key,
  }),
}));

describe('WorkspaceInfoSection', () => {
  it('renders the company intro and danger zone as a standalone workspace page section', () => {
    const markup = renderToStaticMarkup(
      <WorkspaceInfoSection
        selectedTenantId="tenant-1"
        companyNameEditor={<div>Company Name Editor</div>}
        companyTimezoneEditor={<div>Company Timezone Editor</div>}
        companyIntro="Acme builds AI workflows."
        onCompanyIntroChange={() => {}}
        onSaveCompanyIntro={() => {}}
        companyIntroSaving={false}
        companyIntroSaved={true}
        kbBrowser={<div>Knowledge Base Browser</div>}
        themeColorPicker={<div>Theme Color Picker</div>}
        broadcastSection={<div>Broadcast Section</div>}
        onDeleteCompany={() => {}}
      />,
    );

    expect(markup).toContain('Company Intro');
    expect(markup).toContain('Knowledge Base Browser');
    expect(markup).toContain('Delete This Company');
  });
});
