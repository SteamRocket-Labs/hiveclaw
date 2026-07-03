import { useMemo, useState } from 'react';

import { useTranslation } from 'react-i18next';

import { skillApi } from '../../api/domains/skills';
import FileBrowser from '../../components/FileBrowser';
import type { FileBrowserApi } from '../../components/FileBrowser';

import './WorkspaceSkillsSection.css';

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
            : 'Tier 3 (Local Runtime Native)';
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
    const styles: Record<number, { cls: string; label: string }> = {
      1: { cls: 'ws-skills-tier-1', label: 'Tier 1 · Pure Prompt' },
      2: { cls: 'ws-skills-tier-2', label: 'Tier 2 · CLI/API' },
      3: { cls: 'ws-skills-tier-3', label: 'Tier 3 · Local Runtime Native' },
    };
    const style = styles[tier] || styles[1];
    return <span className={`ws-skills-tier ${style.cls}`}>{style.label}</span>;
  };

  return (
    <div>
      <div className="ws-skills-header">
        <div>
          <h3>{t('enterprise.tabs.skills', 'Skill Registry')}</h3>
          <p className="ws-skills-subtitle">
            {t('enterprise.tools.manageGlobalSkills', 'Manage shared skills available across the workspace.')}
          </p>
        </div>
        <div className="ws-skills-actions">
          <button
            className="btn btn-secondary"
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
        <div className="ws-skills-settings-panel">
          <div className="ws-skills-settings-title">
            {t('enterprise.tools.githubToken', 'GitHub Token')}
          </div>
          <p className="ws-skills-settings-desc">
            {t('enterprise.tools.githubTokenDesc', 'Configure a token for importing skills from GitHub and ClawHub.')}
          </p>
          {tokenStatus?.configured ? (
            <div className="ws-skills-current">
              {t('enterprise.tools.currentToken', 'Current Token')} <code className="ws-skills-code">{tokenStatus.masked}</code>
              <span className="ws-skills-source">({tokenStatus.source})</span>
            </div>
          ) : null}
          <div className="ws-skills-input-row">
            <input type="text" name="prevent_autofill_user" className="ws-skills-hidden" tabIndex={-1} />
            <input type="password" name="prevent_autofill_pass" className="ws-skills-hidden" tabIndex={-1} />
            <input
              type="text"
              className="input ws-skills-token-input"
              autoComplete="off"
              data-form-type="other"
              placeholder="ghp_xxxxxxxxxxxx"
              value={tokenInput}
              onChange={(event) => setTokenInput(event.target.value)}
            />
            <button
              className="btn btn-primary"
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

          <div className="ws-skills-settings-block">
            <div className="ws-skills-settings-title">
              {t('enterprise.tools.clawhubApiKey', 'ClawHub API Key')}
            </div>
            <p className="ws-skills-settings-desc">
              {t('enterprise.tools.authenticatedRequestsGetHigherRateLimits', 'Authenticated requests receive higher rate limits.')}
            </p>
            {tokenStatus?.clawhub_configured ? (
              <div className="ws-skills-current">
                {t('enterprise.tools.currentKey', 'Current Key')} <code className="ws-skills-code">{tokenStatus.clawhub_masked}</code>
              </div>
            ) : null}
            <div className="ws-skills-input-row">
              <input type="text" name="prevent_autofill_ch_user" className="ws-skills-hidden" tabIndex={-1} />
              <input type="password" name="prevent_autofill_ch_pass" className="ws-skills-hidden" tabIndex={-1} />
              <input
                type="text"
                className="input ws-skills-token-input"
                autoComplete="off"
                data-form-type="other"
                placeholder="sk-ant-xxxxxxxxxxxx"
                value={clawhubKeyInput}
                onChange={(event) => setClawhubKeyInput(event.target.value)}
              />
              <button
                className="btn btn-primary"
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
        features={{ newSkill: true, newFolder: true, edit: true, delete: true, directoryNavigation: true }}
        title={t('agent.skills.skillFiles', 'Skill Files')}
        onRefresh={() => setRefreshKey((value) => value + 1)}
      />

      {toast ? (
        <div className={`ws-skills-toast ${toast.type}`}>
          {toast.message}
        </div>
      ) : null}

      {showClawhubModal ? (
        <div
          className="ui-modal-overlay"
          onClick={() => setShowClawhubModal(false)}
        >
          <div
            className="ui-modal ws-skills-modal-wide"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="ws-skills-modal-head">
              <div className="ws-skills-modal-head-row">
                <h3 className="ws-skills-modal-title">{t('enterprise.tools.browseClawhub', 'Browse ClawHub')}</h3>
                <button className="btn btn-ghost ws-skills-modal-close" onClick={() => setShowClawhubModal(false)}>x</button>
              </div>
              <div className="ws-skills-search-row">
                <input
                  className="input ws-skills-search-input"
                  placeholder={t('enterprise.tools.searchSkills', 'Search skills')}
                  value={searchQuery}
                  onChange={(event) => setSearchQuery(event.target.value)}
                  onKeyDown={(event) => event.key === 'Enter' && handleSearch()}
                  autoFocus
                />
                <button className="btn btn-primary" onClick={handleSearch} disabled={searching}>
                  {searching ? t('enterprise.tools.searching', 'Searching...') : t('enterprise.tools.search', 'Search')}
                </button>
              </div>
            </div>
            <div className="ws-skills-modal-body">
              {searchResults.length === 0 && !searching ? (
                <div className="ws-skills-modal-empty">
                  {hasSearched ? t('enterprise.tools.noResultsFound', 'No results found') : t('enterprise.tools.searchForSkills', 'Search for skills')}
                </div>
              ) : null}
              {searching ? (
                <div className="ws-skills-modal-empty">
                  {t('enterprise.tools.searchingClawhub', 'Searching ClawHub...')}
                </div>
              ) : null}
              {searchResults.map((result) => (
                <div
                  key={result.slug}
                  className="ws-skills-result-row"
                >
                  <div className="ws-skills-result-main">
                    <div className="ws-skills-result-head">
                      <span className="ws-skills-result-name">{result.displayName}</span>
                      <span className="ws-skills-result-slug">{result.slug}</span>
                      {result.version ? (
                        <span className="ws-skills-ver-tag">
                          v{result.version}
                        </span>
                      ) : null}
                    </div>
                    <div className="ws-skills-result-summary">
                      {result.summary?.slice(0, 160)}
                      {result.summary?.length > 160 ? '...' : ''}
                    </div>
                    {result.updatedAt ? (
                      <div className="ws-skills-result-updated">
                        Updated {new Date(result.updatedAt).toLocaleDateString()}
                      </div>
                    ) : null}
                  </div>
                  <button
                    className="btn btn-secondary ws-skills-btn-shrink"
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
          className="ui-modal-overlay"
          onClick={() => setShowUrlModal(false)}
        >
          <div
            className="ui-modal ws-skills-modal-mid"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="ws-skills-modal-head">
              <div className="ws-skills-modal-head-row">
                <h3 className="ws-skills-modal-title">{t('enterprise.tools.importFromUrl', 'Import from URL')}</h3>
                <button className="btn btn-ghost ws-skills-modal-close" onClick={() => setShowUrlModal(false)}>x</button>
              </div>
              <p className="ws-skills-modal-desc">
                {t('enterprise.tools.pasteGithubUrl', 'Paste a GitHub URL to preview and import a skill.')}
              </p>
              <div className="ws-skills-search-row">
                <input
                  className="input ws-skills-url-input"
                  placeholder={t('enterprise.tools.githubUrlPlaceholder', 'https://github.com/...')}
                  value={urlInput}
                  onChange={(event) => {
                    setUrlInput(event.target.value);
                    setUrlPreview(null);
                  }}
                  autoFocus
                  onKeyDown={(event) => event.key === 'Enter' && handleUrlPreview()}
                />
                <button className="btn btn-secondary" onClick={handleUrlPreview} disabled={urlPreviewing || !urlInput.trim()}>
                  {urlPreviewing ? t('enterprise.tools.loading', 'Loading...') : t('enterprise.tools.preview', 'Preview')}
                </button>
              </div>
            </div>

            {urlPreview ? (
              <div className="ws-skills-preview">
                <div className="ws-skills-preview-head">
                  <span className="ws-skills-result-name">{urlPreview.name}</span>
                  {tierBadge(urlPreview.tier)}
                  {urlPreview.has_scripts ? (
                    <span className="ws-skills-scripts-tag">
                      Contains scripts
                    </span>
                  ) : null}
                </div>
                {urlPreview.description ? (
                  <p className="ws-skills-preview-desc">{urlPreview.description}</p>
                ) : null}
                <div className="ws-skills-preview-meta">
                  {urlPreview.files?.length} files, {(urlPreview.total_size / 1024).toFixed(1)} KB
                </div>
                <div className="ws-skills-preview-actions">
                  <button className="btn btn-secondary" onClick={() => setShowUrlModal(false)}>
                    {t('common.cancel', 'Cancel')}
                  </button>
                  <button className="btn btn-primary" onClick={handleUrlImport} disabled={urlImporting}>
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
