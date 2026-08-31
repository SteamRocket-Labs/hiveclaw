import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { IconFilter } from '@tabler/icons-react';

import { adminApi } from '../../api/domains/admin';
import { requestAppConfirm } from '../../components/AppDialogs';
import './AdminCompaniesSection.css';

type SortKey = 'name' | 'org_admin_email' | 'user_count' | 'agent_count' | 'total_tokens' | 'created_at';
type SortDir = 'asc' | 'desc';

const PAGE_SIZE = 15;

function formatTokens(n: number | null | undefined): string {
  if (n == null) return '-';
  if (n < 1000) return String(n);
  if (n < 1_000_000) return (n / 1000).toFixed(n < 10_000 ? 1 : 0) + 'K';
  if (n < 1_000_000_000) return (n / 1_000_000).toFixed(n < 10_000_000 ? 1 : 0) + 'M';
  return (n / 1_000_000_000).toFixed(1) + 'B';
}

function formatDate(dt: string | null | undefined): string {
  if (!dt) return '-';
  return new Date(dt).toLocaleDateString(undefined, { year: 'numeric', month: '2-digit', day: '2-digit' });
}

interface AdminCompaniesSectionProps {
  initialCompanies?: any[];
}

export default function AdminCompaniesSection({ initialCompanies }: AdminCompaniesSectionProps) {
  const { t } = useTranslation();
  const [companies, setCompanies] = useState<any[]>(initialCompanies ?? []);
  const [loading, setLoading] = useState(initialCompanies ? false : true);
  const [error, setError] = useState('');
  const [sortKey, setSortKey] = useState<SortKey>('created_at');
  const [sortDir, setSortDir] = useState<SortDir>('desc');
  const [statusFilter, setStatusFilter] = useState<'all' | 'active' | 'disabled'>('all');
  const [showStatusDropdown, setShowStatusDropdown] = useState(false);
  const statusDropdownRef = useRef<HTMLDivElement>(null);
  const [page, setPage] = useState(0);
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState('');
  const [creating, setCreating] = useState(false);
  const [assignCompany, setAssignCompany] = useState<any | null>(null);
  const [adminEmail, setAdminEmail] = useState('');
  const [assigningAdmin, setAssigningAdmin] = useState(false);
  const assignRequestActiveRef = useRef(false);
  const createdDialogRef = useRef<HTMLDivElement>(null);
  const [createdCode, setCreatedCode] = useState('');
  const [createdCompanyName, setCreatedCompanyName] = useState('');
  const [codeCopied, setCodeCopied] = useState(false);
  const [toast, setToast] = useState<{ msg: string; type: 'success' | 'error' } | null>(null);

  const showToast = (msg: string, type: 'success' | 'error' = 'success') => {
    setToast({ msg, type });
    setTimeout(() => setToast(null), 3000);
  };

  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (statusDropdownRef.current && !statusDropdownRef.current.contains(e.target as Node)) {
        setShowStatusDropdown(false);
      }
    };
    if (showStatusDropdown) document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [showStatusDropdown]);

  useEffect(() => {
    if (!createdCode) return;
    createdDialogRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setCreatedCode('');
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [createdCode]);

  const loadCompanies = async () => {
    setLoading(true);
    try {
      const data = await adminApi.listCompanies();
      setCompanies(data);
      setError('');
      return true;
    } catch (e: any) {
      setError(e.message);
      return false;
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!initialCompanies) loadCompanies();
  }, [initialCompanies]);

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((dir) => (dir === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir(key === 'name' ? 'asc' : 'desc');
    }
    setPage(0);
  };

  const sorted = useMemo(() => {
    let list = [...companies];
    if (statusFilter === 'active') list = list.filter((company) => company.is_active);
    else if (statusFilter === 'disabled') list = list.filter((company) => !company.is_active);
    list.sort((a, b) => {
      let av = a[sortKey];
      let bv = b[sortKey];
      if (sortKey === 'name' || sortKey === 'org_admin_email') {
        av = (av || '').toLowerCase();
        bv = (bv || '').toLowerCase();
      }
      if (sortKey === 'created_at') {
        av = av ? new Date(av).getTime() : 0;
        bv = bv ? new Date(bv).getTime() : 0;
      }
      if (av < bv) return sortDir === 'asc' ? -1 : 1;
      if (av > bv) return sortDir === 'asc' ? 1 : -1;
      return 0;
    });
    return list;
  }, [companies, sortKey, sortDir, statusFilter]);

  const totalPages = Math.ceil(sorted.length / PAGE_SIZE);
  const paged = sorted.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  const handleCreate = async () => {
    if (!newName.trim()) return;
    setCreating(true);
    try {
      const result = await adminApi.createCompany({ name: newName.trim() });
      setCreatedCompanyName(result.company.name);
      setCreatedCode(result.admin_invitation_code);
      setCodeCopied(false);
      setNewName('');
      setShowCreate(false);
      await loadCompanies();
    } catch (e: any) {
      showToast(e.message || 'Failed', 'error');
    }
    setCreating(false);
  };

  const handleCopyCode = async () => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard API unavailable');
      await navigator.clipboard.writeText(createdCode);
      setCodeCopied(true);
      setTimeout(() => setCodeCopied(false), 2000);
    } catch {
      showToast(t('admin.copyFailed', 'Could not copy the invitation code.'), 'error');
    }
  };

  const handleAssignAdmin = async () => {
    const email = adminEmail.trim();
    if (!assignCompany || !email || assignRequestActiveRef.current) return;
    assignRequestActiveRef.current = true;
    setAssigningAdmin(true);
    try {
      const confirmed = await requestAppConfirm({
        title: t('admin.assignAdminTitle', 'Assign company admin'),
        message: t(
          'admin.confirmAssignAdmin',
          'Assign {{email}} as an administrator of {{company}}? The account must already be registered and not belong to another company.',
          { email, company: assignCompany.name },
        ),
        confirmLabel: t('admin.assignAdmin', 'Assign admin'),
      });
      if (!confirmed) return;
      const receipt = await adminApi.assignUserToTenant(assignCompany.id, { email, role: 'org_admin' });
      showToast(receipt.status === 'already_assigned'
        ? t('admin.adminAlreadyAssigned', "This account already has company administrator access. Refresh the account's signed-in session; clients without token refresh must sign in again.")
        : t('admin.adminAssigned', "Company administrator assignment committed. Refresh the account's signed-in session; clients without token refresh must sign in again."));
      setAssignCompany(null);
      setAdminEmail('');
      if (!await loadCompanies()) {
        showToast(
          t('admin.adminAssignedRefreshFailed', 'Administrator assignment committed, but the company list could not be refreshed.'),
          'error',
        );
      }
    } catch (e: any) {
      showToast(e.message || t('admin.assignAdminFailed', 'Failed to assign company administrator.'), 'error');
    } finally {
      assignRequestActiveRef.current = false;
      setAssigningAdmin(false);
    }
  };

  const handleToggle = async (id: string, currentlyActive: boolean) => {
    const action = currentlyActive ? 'disable' : 'enable';
    if (currentlyActive) {
      const confirmed = await requestAppConfirm({
        title: t('admin.disableCompanyTitle', 'Disable company'),
        message: t(
          'admin.confirmDisable',
          'Disable this company? Users will lose company access and running employees will be stopped. Re-enabling does not restart them automatically.',
        ),
        confirmLabel: t('common.confirm', 'Confirm'),
        danger: true,
      });
      if (!confirmed) return;
    }
    try {
      await adminApi.toggleCompany(id);
      await loadCompanies();
      showToast(`Company ${action}d`);
    } catch (e: any) {
      showToast(e.message || 'Failed', 'error');
    }
  };

  const SortArrow = ({ col }: { col: SortKey }) => {
    if (sortKey !== col) return <span className="admin-companies-sort-arrow-idle">&#x2195;</span>;
    return <span className="admin-companies-sort-arrow">{sortDir === 'asc' ? '↑' : '↓'}</span>;
  };

  const columns: { key: SortKey; label: string; flex: string }[] = [
    { key: 'name', label: t('admin.company', 'Company'), flex: '2fr' },
    { key: 'org_admin_email', label: t('admin.orgAdmin', 'Admin Email'), flex: '1.5fr' },
    { key: 'user_count', label: t('admin.users', 'Users'), flex: '80px' },
    { key: 'agent_count', label: t('admin.agents', 'Agents'), flex: '80px' },
    { key: 'total_tokens', label: t('admin.tokens', 'Token Usage'), flex: '100px' },
    { key: 'created_at', label: t('admin.createdAt', 'Created'), flex: '100px' },
  ];
  const statusColFlex = '80px';
  const actionColFlex = '168px';
  const gridCols = columns.map((c) => c.flex).join(' ') + ' ' + statusColFlex + ' ' + actionColFlex;

  return (
    <div className="admin-companies-root">
      {toast && (
        <div
          className="admin-companies-toast"
          style={{ background: toast.type === 'success' ? 'var(--success)' : 'var(--error)' }}
          role={toast.type === 'error' ? 'alert' : 'status'}
          aria-live={toast.type === 'error' ? 'assertive' : 'polite'}
        >
          {toast.msg}
        </div>
      )}

      {createdCode && (
        <div className="ui-modal-overlay" onClick={() => setCreatedCode('')}>
          <div
            ref={createdDialogRef}
            className="card admin-companies-created-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="admin-company-created-title"
            tabIndex={-1}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="admin-companies-modal-head">
              <div className="admin-companies-success-circle">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
                  <polyline points="22 4 12 14.01 9 11.01" />
                </svg>
              </div>
              <h2 id="admin-company-created-title" className="admin-companies-modal-title">{t('admin.companyCreated', 'Company Created')}</h2>
              <p className="admin-companies-modal-sub">
                <span className="admin-companies-modal-sub-name">{createdCompanyName}</span> {t('admin.companyCreatedDesc', 'has been created successfully.')}
              </p>
            </div>

            <div className="admin-companies-invite-box">
              <div className="admin-companies-invite-label">
                {t('admin.inviteCodeLabel', 'Admin Invitation Code')}
              </div>
              <div className="admin-companies-invite-code">
                {createdCode}
              </div>
            </div>

            <div className="admin-companies-howto">
              <div className="admin-companies-howto-title">{t('admin.inviteCodeHowTo', 'How to use this code:')}</div>
              {t(
                'admin.inviteCodeExplain',
                'Send this code to the person who will manage this company. After registering and signing in, they enter it on Set Up Your Workspace. This code grants company administrator access and is single-use. If they already have an account, close this dialog and use Assign admin with their registered email.',
              )}
            </div>

            <div className="admin-companies-modal-actions">
              <button type="button" className="btn btn-primary admin-companies-copy-btn" onClick={() => void handleCopyCode()}>
                {codeCopied ? (
                  <>{t('admin.copied', 'Copied')}</>
                ) : (
                  <>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <rect x="9" y="9" width="13" height="13" rx="2" />
                      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                    </svg>
                    {t('admin.copyCode', 'Copy Code')}
                  </>
                )}
              </button>
              <button type="button" className="btn btn-secondary admin-companies-modal-close-btn" onClick={() => setCreatedCode('')}>
                {t('common.close', 'Close')}
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="admin-companies-toolbar">
        <button type="button" className="btn btn-primary" onClick={() => { setShowCreate(true); setCreatedCode(''); }}>
          + {t('admin.createCompany', 'Create Company')}
        </button>
      </div>

      {showCreate && (
        <div className="card admin-companies-create-form">
          <div className="admin-companies-create-title">{t('admin.createCompany', 'Create Company')}</div>
          <div className="admin-companies-row">
            <input
              className="form-input admin-companies-grow"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder={t('admin.companyNamePlaceholder', 'Company name')}
              aria-label={t('admin.companyNamePlaceholder', 'Company name')}
              onKeyDown={(e) => e.key === 'Enter' && void handleCreate()}
              autoFocus
            />
            <button type="button" className="btn btn-primary" onClick={() => void handleCreate()} disabled={creating || !newName.trim()}>
              {creating ? '...' : t('common.create', 'Create')}
            </button>
            <button type="button" className="btn btn-secondary" onClick={() => setShowCreate(false)}>
              {t('common.cancel', 'Cancel')}
            </button>
          </div>
        </div>
      )}

      {assignCompany && (
        <div className="card admin-companies-create-form">
          <div className="admin-companies-create-title">
            {t('admin.assignAdminFor', 'Assign administrator for {{company}}', { company: assignCompany.name })}
          </div>
          <div className="admin-companies-row">
            <input
              className="form-input admin-companies-grow"
              type="email"
              value={adminEmail}
              onChange={(e) => setAdminEmail(e.target.value)}
              placeholder={t('admin.registeredEmailPlaceholder', 'Registered account email')}
              aria-label={t('admin.registeredEmailPlaceholder', 'Registered account email')}
              onKeyDown={(e) => e.key === 'Enter' && void handleAssignAdmin()}
              autoFocus
            />
            <button type="button" className="btn btn-primary" onClick={() => void handleAssignAdmin()} disabled={assigningAdmin || !adminEmail.trim()}>
              {assigningAdmin ? '...' : t('admin.assignAdmin', 'Assign admin')}
            </button>
            <button type="button" className="btn btn-secondary" onClick={() => { setAssignCompany(null); setAdminEmail(''); }}>
              {t('common.cancel', 'Cancel')}
            </button>
          </div>
        </div>
      )}

      <div className="card admin-companies-table">
        <div className="admin-companies-thead" style={{ gridTemplateColumns: gridCols }}>
          {columns.map((col) => (
            <button
              key={col.key}
              type="button"
              className="admin-companies-th"
              aria-pressed={sortKey === col.key}
              onClick={() => handleSort(col.key)}
            >
              {col.label}
              <SortArrow col={col.key} />
            </button>
          ))}
          <div ref={statusDropdownRef} className="admin-companies-status-th">
            {t('admin.status', 'Status')}
            <button
              type="button"
              onClick={() => setShowStatusDropdown((value) => !value)}
              className={`admin-companies-filter-btn${statusFilter !== 'all' ? ' is-active' : ''}`}
              title={t('admin.filterStatus', 'Filter by status')}
              aria-label={t('admin.filterStatus', 'Filter by status')}
              aria-expanded={showStatusDropdown}
              aria-haspopup="menu"
            >
              <IconFilter size={14} stroke={statusFilter !== 'all' ? 2.5 : 1.8} />
            </button>
            {showStatusDropdown && (
              <div className="admin-companies-dropdown" role="menu">
                {(['all', 'active', 'disabled'] as const).map((value) => (
                  <button
                    key={value}
                    type="button"
                    role="menuitemradio"
                    aria-checked={statusFilter === value}
                    onClick={() => {
                      setStatusFilter(value);
                      setPage(0);
                      setShowStatusDropdown(false);
                    }}
                    className={`admin-companies-dropdown-item${statusFilter === value ? ' is-active' : ''}`}
                  >
                    {value === 'all' ? t('admin.all', 'All') : value === 'active' ? t('admin.active', 'Active') : t('admin.disabled', 'Disabled')}
                  </button>
                ))}
              </div>
            )}
          </div>
          <div>{t('admin.action', 'Action')}</div>
        </div>

        <div className="admin-companies-tbody">
          {loading && (
            <div className="admin-companies-empty">{t('common.loading', 'Loading...')}</div>
          )}

          {error && (
            <div className="admin-companies-error" role="alert">
              <div>{error}</div>
              <button type="button" className="btn btn-secondary" onClick={() => void loadCompanies()}>
                {t('common.retry', 'Retry')}
              </button>
            </div>
          )}

          {!loading &&
            paged.map((company: any) => (
              <div
                key={company.id}
                className="admin-companies-row-data"
                style={{ gridTemplateColumns: gridCols, opacity: company.is_active ? 1 : 0.5 }}
              >
                <div>
                  <div className="admin-companies-cell-name">{company.name}</div>
                  <div className="admin-companies-cell-slug">{company.slug}</div>
                </div>
                <div className={`admin-companies-cell-email${company.org_admin_email ? '' : ' is-muted'}`}>{company.org_admin_email || '-'}</div>
                <div>{company.user_count ?? '-'}</div>
                <div>{company.agent_count ?? '-'}</div>
                <div className="admin-companies-cell-tokens">{formatTokens(company.total_tokens)}</div>
                <div className="admin-companies-cell-date">{formatDate(company.created_at)}</div>
                <div>
                  <span className={`badge ${company.is_active ? 'badge-success' : 'badge-error'}`}>
                    {company.is_active ? t('admin.active', 'Active') : t('admin.disabled', 'Disabled')}
                  </span>
                </div>
                <div className="admin-companies-actions">
                  {company.is_active && (
                    <button
                      type="button"
                      className="btn btn-ghost admin-companies-toggle-btn"
                      onClick={() => { setAssignCompany(company); setAdminEmail(''); }}
                    >
                      {t('admin.assignAdmin', 'Assign admin')}
                    </button>
                  )}
                  <button
                    type="button"
                    className={`btn btn-ghost admin-companies-toggle-btn${company.slug === 'default' ? ' is-locked' : ''}`}
                    style={{ color: company.slug === 'default' ? 'var(--text-tertiary)' : company.is_active ? 'var(--error)' : 'var(--success)' }}
                    onClick={() => handleToggle(company.id, company.is_active)}
                    disabled={company.slug === 'default'}
                    title={company.slug === 'default' ? t('admin.cannotDisableDefault', 'Cannot disable the default company — platform admin would be locked out') : undefined}
                  >
                    {company.is_active ? t('admin.disable', 'Disable') : t('admin.enable', 'Enable')}
                  </button>
                </div>
              </div>
            ))}

          {!loading && paged.length === 0 && !error && (
            <div className="admin-companies-empty">
              {statusFilter !== 'all' ? t('admin.noFilterResults', 'No companies match the current filter.') : t('common.noData', 'No data')}
            </div>
          )}
        </div>

        {!loading && totalPages > 1 && (
          <div className="admin-companies-pagination">
            <span>
              {t('admin.showing', '{{start}}-{{end}} of {{total}}', {
                start: page * PAGE_SIZE + 1,
                end: Math.min((page + 1) * PAGE_SIZE, sorted.length),
                total: sorted.length,
              })}
            </span>
            <div className="admin-companies-page-btns">
              <button type="button" className="btn btn-ghost admin-companies-page-btn" disabled={page === 0} onClick={() => setPage((current) => current - 1)}>
                &lsaquo; {t('admin.prev', 'Prev')}
              </button>
              <button type="button" className="btn btn-ghost admin-companies-page-btn" disabled={page >= totalPages - 1} onClick={() => setPage((current) => current + 1)}>
                {t('admin.next', 'Next')} &rsaquo;
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
