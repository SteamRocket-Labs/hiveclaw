import { useMemo, useState } from 'react';
import type { CSSProperties } from 'react';

import { useTranslation } from 'react-i18next';

import { skillApi } from '../../api/domains/skills';
import FileBrowser from '../../components/FileBrowser';
import type { FileBrowserApi } from '../../components/FileBrowser';

interface TokenStatus {
  configured: boolean;
  source: string;
  masked: string;
  clawhub_configured?: boolean;
  clawhub_masked?: string;
}

export default function WorkspaceSkillsSection() {
  const { t } = useTranslation();
  const [refreshKey, setRefreshKey] = useState(0);
  const [showClawhubModal, setShowClawhubModal] = useState(false);
  const [showUrlModal, setShowUrlModal] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<any[]>([]);
  const [searching, setSearching] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const [installing, setInstalling] = useState<string | null>(null);
  const [urlInput, setUrlInput] = useState('');
  const [urlPreview, setUrlPreview] = useState<any | null>(null);
  const [urlPreviewing, setUrlPreviewing] = useState(false);
  const [urlImporting, setUrlImporting] = useState(false);
  const [toast, setToast] = useState<{ message: string; type: 'success' | 'error' } | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [tokenInput, setTokenInput] = useState('');
  const [tokenStatus, setTokenStatus] = useState<TokenStatus | null>(null);
  const [savingToken, setSavingToken] = useState(false);
  const [clawhubKeyInput, setClawhubKeyInput] = useState('');
  const [savingClawhubKey, setSavingClawhubKey] = useState(false);

  const showToast = (message: string, type: 'success' | 'error' = 'success') => {
    setToast({ message, type });
    setTimeout(() => setToast(null), 4000);
  };

  const adapter: FileBrowserApi = useMemo(() => ({
    list: (path: string) => skillApi.browse.list(path),
    read: (path: string) => skillApi.browse.read(path),
    write: (path: string, content: string) => skillApi.browse.write(path, content),
    delete: (path: string) => skillApi.browse.delete(path),
  }), []);

  const handleSearch = async () => {
    if (!searchQuery.trim()) return;
    setSearching(true);
    setSearchResults([]);
    setHasSearched(true);
    try {
      const results = await skillApi.clawhub.search(searchQuery);
      setSearchResults(results);
    } catch (error: any) {
      showToast(error.message || 'Search failed', 'error');
    }
    setSearching(false);
  };

  const handleInstall = async (slug: string) => {
    setInstalling(slug);
    try {
      const result = await skillApi.clawhub.install(slug);
      const tierLabel =
        result.tier === 1
          ? 'Tier 1 (Pure Prompt)'
          : result.tier === 2
            ? 'Tier 2 (CLI/API)'
            : 'Tier 3 (OpenClaw Native)';
      showToast(`Installed "${result.name}" — ${tierLabel}, ${result.file_count} files`);
      setRefreshKey((value) => value + 1);
      setSearchResults((current) => current.filter((row) => row.slug !== slug));
    } catch (error: any) {
      showToast(error.message || 'Install failed', 'error');
    }
    setInstalling(null);
  };

  const handleUrlPreview = async () => {
    if (!urlInput.trim()) return;
    setUrlPreviewing(true);
    setUrlPreview(null);
    try {
      const preview = await skillApi.previewUrl(urlInput);
      setUrlPreview(preview);
    } catch (error: any) {
      showToast(error.message || 'Preview failed', 'error');
    }
    setUrlPreviewing(false);
  };

  const handleUrlImport = async () => {
    if (!urlInput.trim()) return;
    setUrlImporting(true);
    try {
      const result = await skillApi.importFromUrl(urlInput);
      showToast(`Imported "${result.name}" — ${result.file_count} files`);
      setRefreshKey((value) => value + 1);
      setShowUrlModal(false);
      setUrlInput('');
      setUrlPreview(null);
    } catch (error: any) {
      showToast(error.message || 'Import failed', 'error');
    }
    setUrlImporting(false);
  };

  const tierBadge = (tier: number) => {
    const styles: Record<number, { bg: string; color: string; label: string }> = {
      1: { bg: 'rgba(52,199,89,0.12)', color: 'var(--success, #34c759)', label: 'Tier 1 · Pure Prompt' },
      2: { bg: 'rgba(255,159,10,0.12)', color: 'var(--warning, #ff9f0a)', label: 'Tier 2 · CLI/API' },
      3: { bg: 'rgba(255,59,48,0.12)', color: 'var(--error, #ff3b30)', label: 'Tier 3 · OpenClaw Native' },
    };
    const style = styles[tier] || styles[1];
    return (
      <span
        style={{
          padding: '2px 8px',
          borderRadius: '4px',
          fontSize: '11px',
          fontWeight: 500,
          background: style.bg,
          color: style.color,
        }}
      >
        {style.label}
      </span>
    );
  };

  return (
    <div>
      <div style={{ marginBottom: '12px', display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          <h3>{t('enterprise.tabs.skills', 'Skill Registry')}</h3>
          <p style={{ fontSize: '13px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
            {t('enterprise.tools.manageGlobalSkills', 'Manage shared skills available across the workspace.')}
          </p>
        </div>
        <div style={{ display: 'flex', gap: '8px', flexShrink: 0 }}>
          <button
            className="btn btn-secondary"
            style={{ fontSize: '13px', padding: '6px 10px', minWidth: 'auto' }}
            onClick={async () => {
              setShowSettings((value) => !value);
              if (!tokenStatus) {
                try {
                  const status = await skillApi.settings.getToken();
                  setTokenStatus(status);
                } catch {
                  // Ignore read failure so file browser remains usable.
                }
              }
            }}
            title="Settings"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          </button>
          <button
            className="btn btn-secondary"
            style={{ fontSize: '13px' }}
            onClick={() => {
              setShowUrlModal(true);
              setUrlInput('');
              setUrlPreview(null);
            }}
          >
            {t('enterprise.tools.importFromUrl', 'Import from URL')}
          </button>
          <button
            className="btn btn-primary"
            style={{ fontSize: '13px' }}
            onClick={() => {
              setShowClawhubModal(true);
              setSearchQuery('');
              setSearchResults([]);
              setHasSearched(false);
            }}
          >
            {t('enterprise.tools.browseClawhub', 'Browse ClawHub')}
          </button>
        </div>
      </div>

      {showSettings ? (
        <div
          style={{
            marginBottom: '16px',
            padding: '16px',
            borderRadius: '8px',
            border: '1px solid var(--border-primary)',
            background: 'var(--bg-secondary, rgba(255,255,255,0.02))',
          }}
        >
          <div style={{ fontSize: '13px', fontWeight: 600, marginBottom: '8px', display: 'flex', alignItems: 'center', gap: '6px' }}>
            {t('enterprise.tools.githubToken', 'GitHub Token')}
          </div>
          <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
            {t('enterprise.tools.githubTokenDesc', 'Configure a token for importing skills from GitHub and ClawHub.')}
          </p>
          {tokenStatus?.configured ? (
            <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '8px' }}>
              {t('enterprise.tools.currentToken', 'Current Token')} <code style={{ padding: '2px 6px', borderRadius: '4px', background: 'var(--bg-tertiary)', fontSize: '11px' }}>{tokenStatus.masked}</code>
              <span style={{ marginLeft: '8px', color: 'var(--text-tertiary)' }}>({tokenStatus.source})</span>
            </div>
          ) : null}
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
            <input type="text" name="prevent_autofill_user" style={{ display: 'none' }} tabIndex={-1} />
            <input type="password" name="prevent_autofill_pass" style={{ display: 'none' }} tabIndex={-1} />
            <input
              type="text"
              className="input"
              autoComplete="off"
              data-form-type="other"
              placeholder="ghp_xxxxxxxxxxxx"
              value={tokenInput}
              onChange={(event) => setTokenInput(event.target.value)}
              style={{ flex: 1, fontSize: '13px', fontFamily: 'monospace', WebkitTextSecurity: 'disc' } as CSSProperties}
            />
            <button
              className="btn btn-primary"
              style={{ fontSize: '13px' }}
              disabled={!tokenInput.trim() || savingToken}
              onClick={async () => {
                setSavingToken(true);
                try {
                  await skillApi.settings.setToken(tokenInput.trim());
                  const status = await skillApi.settings.getToken();
                  setTokenStatus(status);
                  setTokenInput('');
                  showToast(t('enterprise.tools.githubTokenSaved', 'Token saved'));
                } catch (error: any) {
                  showToast(error.message || t('enterprise.tools.failedToSave', 'Failed to save'), 'error');
                }
                setSavingToken(false);
              }}
            >
              {savingToken ? t('enterprise.tools.saving', 'Saving...') : t('enterprise.tools.save', 'Save')}
            </button>
            {tokenStatus?.configured && tokenStatus.source === 'tenant' ? (
              <button
                className="btn btn-secondary"
                style={{ fontSize: '13px' }}
                onClick={async () => {
                  try {
                    await skillApi.settings.setToken('');
                    const status = await skillApi.settings.getToken();
                    setTokenStatus(status);
                    showToast(t('enterprise.tools.tokenCleared', 'Token cleared'));
                  } catch (error: any) {
                    showToast(error.message || t('enterprise.tools.failed', 'Failed'), 'error');
                  }
                }}
              >
                {t('enterprise.tools.clear', 'Clear')}
              </button>
            ) : null}
          </div>

          <div style={{ marginTop: '16px' }}>
            <div style={{ fontSize: '13px', fontWeight: 600, marginBottom: '8px', display: 'flex', alignItems: 'center', gap: '6px' }}>
              {t('enterprise.tools.clawhubApiKey', 'ClawHub API Key')}
            </div>
            <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
              {t('enterprise.tools.authenticatedRequestsGetHigherRateLimits', 'Authenticated requests receive higher rate limits.')}
            </p>
            {tokenStatus?.clawhub_configured ? (
              <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '8px' }}>
                {t('enterprise.tools.currentKey', 'Current Key')} <code style={{ padding: '2px 6px', borderRadius: '4px', background: 'var(--bg-tertiary)', fontSize: '11px' }}>{tokenStatus.clawhub_masked}</code>
              </div>
            ) : null}
            <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
              <input type="text" name="prevent_autofill_ch_user" style={{ display: 'none' }} tabIndex={-1} />
              <input type="password" name="prevent_autofill_ch_pass" style={{ display: 'none' }} tabIndex={-1} />
              <input
                type="text"
                className="input"
                autoComplete="off"
                data-form-type="other"
                placeholder="sk-ant-xxxxxxxxxxxx"
                value={clawhubKeyInput}
                onChange={(event) => setClawhubKeyInput(event.target.value)}
                style={{ flex: 1, fontSize: '13px', fontFamily: 'monospace', WebkitTextSecurity: 'disc' } as CSSProperties}
              />
              <button
                className="btn btn-primary"
                style={{ fontSize: '13px' }}
                disabled={!clawhubKeyInput.trim() || savingClawhubKey}
                onClick={async () => {
                  setSavingClawhubKey(true);
                  try {
                    await skillApi.settings.setClawhubKey(clawhubKeyInput.trim());
                    const status = await skillApi.settings.getToken();
                    setTokenStatus(status);
                    setClawhubKeyInput('');
                    showToast(t('enterprise.tools.clawhubApiKeySaved', 'ClawHub key saved'));
                  } catch (error: any) {
                    showToast(error.message || t('enterprise.tools.failedToSave', 'Failed to save'), 'error');
                  }
                  setSavingClawhubKey(false);
                }}
              >
                {savingClawhubKey ? t('enterprise.tools.saving', 'Saving...') : t('enterprise.tools.save', 'Save')}
              </button>
              {tokenStatus?.clawhub_configured ? (
                <button
                  className="btn btn-secondary"
                  style={{ fontSize: '13px' }}
                  onClick={async () => {
                    try {
                      await skillApi.settings.setClawhubKey('');
                      const status = await skillApi.settings.getToken();
                      setTokenStatus(status);
                      showToast(t('enterprise.tools.tokenCleared', 'Token cleared'));
                    } catch (error: any) {
                      showToast(error.message || t('enterprise.tools.failed', 'Failed'), 'error');
                    }
                  }}
                >
                  {t('enterprise.tools.clear', 'Clear')}
                </button>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}

      <FileBrowser
        key={refreshKey}
        api={adapter}
        features={{ newFile: true, newFolder: true, edit: true, delete: true, directoryNavigation: true }}
        title={t('agent.skills.skillFiles', 'Skill Files')}
        onRefresh={() => setRefreshKey((value) => value + 1)}
      />

      {toast ? (
        <div
          style={{
            position: 'fixed',
            bottom: '24px',
            right: '24px',
            zIndex: 10000,
            padding: '12px 20px',
            borderRadius: '8px',
            fontSize: '13px',
            fontWeight: 500,
            background: toast.type === 'error' ? 'rgba(255,59,48,0.95)' : 'rgba(52,199,89,0.95)',
            color: '#fff',
            boxShadow: '0 4px 16px rgba(0,0,0,0.2)',
            maxWidth: '400px',
            animation: 'fadeIn 200ms ease',
          }}
        >
          {toast.message}
        </div>
      ) : null}

      {showClawhubModal ? (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 9999,
            background: 'rgba(0,0,0,0.5)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
          onClick={() => setShowClawhubModal(false)}
        >
          <div
            style={{
              background: 'var(--bg-primary)',
              borderRadius: '12px',
              width: '640px',
              maxHeight: '80vh',
              display: 'flex',
              flexDirection: 'column',
              border: '1px solid var(--border-default)',
              boxShadow: '0 16px 48px rgba(0,0,0,0.2)',
            }}
            onClick={(event) => event.stopPropagation()}
          >
            <div style={{ padding: '20px 24px 16px', borderBottom: '1px solid var(--border-subtle)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                <h3 style={{ margin: 0, fontSize: '16px' }}>{t('enterprise.tools.browseClawhub', 'Browse ClawHub')}</h3>
                <button className="btn btn-ghost" onClick={() => setShowClawhubModal(false)} style={{ padding: '4px 8px', fontSize: '16px', lineHeight: 1 }}>x</button>
              </div>
              <div style={{ display: 'flex', gap: '8px' }}>
                <input
                  className="input"
                  placeholder={t('enterprise.tools.searchSkills', 'Search skills')}
                  value={searchQuery}
                  onChange={(event) => setSearchQuery(event.target.value)}
                  onKeyDown={(event) => event.key === 'Enter' && handleSearch()}
                  autoFocus
                  style={{ flex: 1, fontSize: '13px' }}
                />
                <button className="btn btn-primary" onClick={handleSearch} disabled={searching} style={{ fontSize: '13px' }}>
                  {searching ? t('enterprise.tools.searching', 'Searching...') : t('enterprise.tools.search', 'Search')}
                </button>
              </div>
            </div>
            <div style={{ flex: 1, overflowY: 'auto', padding: '12px 24px' }}>
              {searchResults.length === 0 && !searching ? (
                <div style={{ textAlign: 'center', padding: '40px 0', color: 'var(--text-tertiary)', fontSize: '13px' }}>
                  {hasSearched ? t('enterprise.tools.noResultsFound', 'No results found') : t('enterprise.tools.searchForSkills', 'Search for skills')}
                </div>
              ) : null}
              {searching ? (
                <div style={{ textAlign: 'center', padding: '40px 0', color: 'var(--text-tertiary)', fontSize: '13px' }}>
                  {t('enterprise.tools.searchingClawhub', 'Searching ClawHub...')}
                </div>
              ) : null}
              {searchResults.map((result) => (
                <div
                  key={result.slug}
                  style={{
                    padding: '12px 0',
                    borderBottom: '1px solid var(--border-subtle)',
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'flex-start',
                    gap: '12px',
                  }}
                >
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
                      <span style={{ fontWeight: 600, fontSize: '14px' }}>{result.displayName}</span>
                      <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', fontFamily: 'var(--font-mono)' }}>{result.slug}</span>
                      {result.version ? (
                        <span style={{ fontSize: '10px', color: 'var(--accent-text)', background: 'var(--accent-subtle)', padding: '1px 6px', borderRadius: '4px' }}>
                          v{result.version}
                        </span>
                      ) : null}
                    </div>
                    <div style={{ fontSize: '12px', color: 'var(--text-secondary)', lineHeight: '1.4' }}>
                      {result.summary?.slice(0, 160)}
                      {result.summary?.length > 160 ? '...' : ''}
                    </div>
                    {result.updatedAt ? (
                      <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                        Updated {new Date(result.updatedAt).toLocaleDateString()}
                      </div>
                    ) : null}
                  </div>
                  <button
                    className="btn btn-secondary"
                    style={{ fontSize: '12px', flexShrink: 0 }}
                    disabled={installing === result.slug}
                    onClick={() => handleInstall(result.slug)}
                  >
                    {installing === result.slug ? t('enterprise.tools.installing', 'Installing...') : t('enterprise.tools.install', 'Install')}
                  </button>
                </div>
              ))}
            </div>
          </div>
        </div>
      ) : null}

      {showUrlModal ? (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 9999,
            background: 'rgba(0,0,0,0.5)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
          onClick={() => setShowUrlModal(false)}
        >
          <div
            style={{
              background: 'var(--bg-primary)',
              borderRadius: '12px',
              width: '560px',
              border: '1px solid var(--border-default)',
              boxShadow: '0 16px 48px rgba(0,0,0,0.2)',
            }}
            onClick={(event) => event.stopPropagation()}
          >
            <div style={{ padding: '20px 24px 16px', borderBottom: '1px solid var(--border-subtle)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
                <h3 style={{ margin: 0, fontSize: '16px' }}>{t('enterprise.tools.importFromUrl', 'Import from URL')}</h3>
                <button className="btn btn-ghost" onClick={() => setShowUrlModal(false)} style={{ padding: '4px 8px', fontSize: '16px', lineHeight: 1 }}>x</button>
              </div>
              <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', margin: '0 0 12px' }}>
                {t('enterprise.tools.pasteGithubUrl', 'Paste a GitHub URL to preview and import a skill.')}
              </p>
              <div style={{ display: 'flex', gap: '8px' }}>
                <input
                  className="input"
                  placeholder={t('enterprise.tools.githubUrlPlaceholder', 'https://github.com/...')}
                  value={urlInput}
                  onChange={(event) => {
                    setUrlInput(event.target.value);
                    setUrlPreview(null);
                  }}
                  autoFocus
                  style={{ flex: 1, fontSize: '13px', fontFamily: 'var(--font-mono)' }}
                  onKeyDown={(event) => event.key === 'Enter' && handleUrlPreview()}
                />
                <button className="btn btn-secondary" onClick={handleUrlPreview} disabled={urlPreviewing || !urlInput.trim()} style={{ fontSize: '12px' }}>
                  {urlPreviewing ? t('enterprise.tools.loading', 'Loading...') : t('enterprise.tools.preview', 'Preview')}
                </button>
              </div>
            </div>

            {urlPreview ? (
              <div style={{ padding: '16px 24px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
                  <span style={{ fontWeight: 600, fontSize: '14px' }}>{urlPreview.name}</span>
                  {tierBadge(urlPreview.tier)}
                  {urlPreview.has_scripts ? (
                    <span style={{ padding: '2px 8px', borderRadius: '4px', fontSize: '11px', background: 'rgba(255,59,48,0.1)', color: 'var(--error, #ff3b30)' }}>
                      Contains scripts
                    </span>
                  ) : null}
                </div>
                {urlPreview.description ? (
                  <p style={{ fontSize: '12px', color: 'var(--text-secondary)', margin: '0 0 8px' }}>{urlPreview.description}</p>
                ) : null}
                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
                  {urlPreview.files?.length} files, {(urlPreview.total_size / 1024).toFixed(1)} KB
                </div>
                <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
                  <button className="btn btn-secondary" onClick={() => setShowUrlModal(false)} style={{ fontSize: '13px' }}>
                    {t('common.cancel', 'Cancel')}
                  </button>
                  <button className="btn btn-primary" onClick={handleUrlImport} disabled={urlImporting} style={{ fontSize: '13px' }}>
                    {urlImporting ? t('enterprise.tools.importing', 'Importing...') : t('enterprise.tools.import', 'Import')}
                  </button>
                </div>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
