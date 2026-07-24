import { expect, test } from '@playwright/test';

const AGENT_ID = '7e57a9e7-0000-4000-8000-000000000010';
const VERSION_ID = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const CURRENT_HASH = 'sha256-current-workspace-file';
const VERSION_HASH = 'sha256-previous-workspace-file';
const FILE_PATH = 'workspace/release-notes.txt';

test('owner inspects and restores a workspace file checkpoint without exposing internal refs', async ({ page }) => {
  let currentContent = 'Current release notes';
  let restoreRequest: Record<string, unknown> | null = null;
  let versionContentReads = 0;

  await page.addInitScript(() => {
    localStorage.setItem('token', 'workspace-version-e2e-token');
    localStorage.setItem('i18nextLng', 'en');
    localStorage.setItem(
      'auth-storage',
      JSON.stringify({
        state: {
          token: 'workspace-version-e2e-token',
          user: {
            id: 'u-1',
            username: 'workspace-owner',
            display_name: 'Workspace Owner',
            role: 'user',
            tenant_id: 't-1',
          },
        },
        version: 0,
      }),
    );
  });

  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    if (!path.startsWith('/api/')) return route.fallback();

    if (path.endsWith('/auth/me')) {
      return route.fulfill({
        json: {
          id: 'u-1',
          username: 'workspace-owner',
          display_name: 'Workspace Owner',
          role: 'user',
          tenant_id: 't-1',
        },
      });
    }
    if (path.endsWith(`/agents/${AGENT_ID}`)) {
      return route.fulfill({
        json: {
          id: AGENT_ID,
          name: 'Release Steward',
          status: 'idle',
          agent_type: 'native',
          access_level: 'manage',
          role_description: 'Owns release evidence',
        },
      });
    }
    if (path.endsWith(`/agents/${AGENT_ID}/files/`) && url.searchParams.get('path') === 'workspace') {
      return route.fulfill({
        json: [{
          name: 'release-notes.txt',
          path: FILE_PATH,
          type: 'file',
          is_dir: false,
          size: currentContent.length,
        }],
      });
    }
    if (path.endsWith(`/agents/${AGENT_ID}/files/content`) && url.searchParams.get('path') === FILE_PATH) {
      return route.fulfill({
        json: {
          path: FILE_PATH,
          content: currentContent,
          content_hash: currentContent === 'Current release notes' ? CURRENT_HASH : VERSION_HASH,
        },
      });
    }
    if (path.endsWith(`/agents/${AGENT_ID}/files/versions`) && url.searchParams.get('path') === FILE_PATH) {
      return route.fulfill({
        json: {
          path: FILE_PATH,
          current: {
            exists: true,
            content_hash: currentContent === 'Current release notes' ? CURRENT_HASH : VERSION_HASH,
            size: currentContent.length,
          },
          versions: [{
            version_id: VERSION_ID,
            created_at: '2026-07-23T09:30:00Z',
            state: 'available',
            size: 22,
            content_hash: VERSION_HASH,
            restorable: true,
          }],
          total: 1,
          offset: 0,
          limit: 20,
          has_more: false,
          coverage_complete: true,
        },
      });
    }
    if (
      path.endsWith(`/agents/${AGENT_ID}/files/versions/${VERSION_ID}/content`)
      && url.searchParams.get('path') === FILE_PATH
    ) {
      versionContentReads += 1;
      return route.fulfill({
        json: {
          path: FILE_PATH,
          version_id: VERSION_ID,
          state: 'available',
          content: 'Previous release notes',
          content_hash: VERSION_HASH,
          size: 22,
          is_binary: false,
        },
      });
    }
    if (
      method === 'POST'
      && path.endsWith(`/agents/${AGENT_ID}/files/versions/${VERSION_ID}/restore`)
      && url.searchParams.get('path') === FILE_PATH
    ) {
      restoreRequest = request.postDataJSON() as Record<string, unknown>;
      currentContent = 'Previous release notes';
      return route.fulfill({
        json: {
          status: 'restored',
          path: FILE_PATH,
          version_id: VERSION_ID,
          current: {
            exists: true,
            content_hash: VERSION_HASH,
            size: currentContent.length,
          },
        },
      });
    }
    if (method === 'GET') return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });

  await page.goto(`/agents/${AGENT_ID}#workspace`);
  await page.getByText('release-notes.txt', { exact: true }).click();
  await expect(page.locator('.file-browser-pre')).toHaveText('Current release notes');

  await page.getByRole('button', { name: /Version history/ }).click();
  await expect(page.getByRole('region', { name: 'Version history' })).toBeVisible();
  await page.locator('.file-version-row').click();
  await expect(page.locator('.file-version-preview-content')).toHaveText('Previous release notes');
  await expect(page.locator('body')).not.toContainText(VERSION_ID);
  await expect(page.locator('body')).not.toContainText(CURRENT_HASH);
  await expect(page.locator('body')).not.toContainText(VERSION_HASH);

  await page.getByRole('button', { name: 'Restore this version' }).click();
  await expect(page.getByRole('dialog', { name: 'Restore this version?' })).toBeVisible();
  await page.getByRole('button', { name: 'Confirm restore' }).click();

  await expect.poll(() => restoreRequest).toEqual({
    expected_current_exists: true,
    expected_current_hash: CURRENT_HASH,
  });
  await expect(page.getByText('File version restored', { exact: true })).toBeVisible();
  await expect(page.locator('.file-browser-pre')).toHaveText('Previous release notes');
  expect(versionContentReads).toBe(1);
});
