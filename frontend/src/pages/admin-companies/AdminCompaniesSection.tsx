import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { IconFilter } from '@tabler/icons-react';

import { adminApi } from '../../api/domains/admin';

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

  const loadCompanies = async () => {
    setLoading(true);
    try {
      const data = await adminApi.listCompanies();
      setCompanies(data);
      setError('');
    } catch (e: any) {
      setError(e.message);
    }
    setLoading(false);
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
      setCreatedCompanyName(newName.trim());
      setCreatedCode(result.admin_invitation_code || '');
      setCodeCopied(false);
      setNewName('');
      setShowCreate(false);
      await loadCompanies();
    } catch (e: any) {
      showToast(e.message || 'Failed', 'error');
    }
    setCreating(false);
  };

  const handleCopyCode = () => {
    navigator.clipboard.writeText(createdCode).then(() => {
      setCodeCopied(true);
      setTimeout(() => setCodeCopied(false), 2000);
    });
  };

  const handleToggle = async (id: string, currentlyActive: boolean) => {
    const action = currentlyActive ? 'disable' : 'enable';
    if (currentlyActive && !confirm(t('admin.confirmDisable', 'Disable this company? All users and agents will be paused.'))) return;
    try {
      await adminApi.toggleCompany(id);
      await loadCompanies();
      showToast(`Company ${action}d`);
    } catch (e: any) {
      showToast(e.message || 'Failed', 'error');
    }
  };

  const SortArrow = ({ col }: { col: SortKey }) => {
    if (sortKey !== col) return <span style={{ opacity: 0.3, marginLeft: '2px' }}>&#x2195;</span>;
    return <span style={{ marginLeft: '2px' }}>{sortDir === 'asc' ? '\u2191' : '\u2193'}</span>;
  };

  const thStyle: React.CSSProperties = {
    cursor: 'pointer',
    userSelect: 'none',
    display: 'flex',
    alignItems: 'center',
    gap: '2px',
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
  const actionColFlex = '80px';
  const gridCols = columns.map((c) => c.flex).join(' ') + ' ' + statusColFlex + ' ' + actionColFlex;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      {toast && (
        <div
          style={{
            position: 'fixed',
            top: '20px',
            right: '20px',
            padding: '10px 20px',
            borderRadius: '8px',
            background: toast.type === 'success' ? 'var(--success)' : 'var(--error)',
            color: '#fff',
            fontSize: '13px',
            zIndex: 9999,
            boxShadow: '0 4px 12px rgba(0,0,0,0.2)',
          }}
        >
          {toast.msg}
        </div>
      )}

      {createdCode && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0,0,0,0.5)',
            zIndex: 10000,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            backdropFilter: 'blur(4px)',
          }}
          onClick={() => setCreatedCode('')}
        >
          <div
            className="card"
            style={{
              padding: '32px',
              maxWidth: '480px',
              width: '90%',
              boxShadow: '0 20px 60px rgba(0,0,0,0.3)',
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={{ textAlign: 'center', marginBottom: '20px' }}>
              <div
                style={{
                  width: '48px',
                  height: '48px',
                  borderRadius: '50%',
                  background: 'rgba(34,197,94,0.1)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  margin: '0 auto 12px',
                  fontSize: '20px',
                }}
              >
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#22c55e" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
                  <polyline points="22 4 12 14.01 9 11.01" />
                </svg>
              </div>
              <h2 style={{ fontSize: '18px', fontWeight: 600, marginBottom: '4px' }}>{t('admin.companyCreated', 'Company Created')}</h2>
              <p style={{ fontSize: '13px', color: 'var(--text-tertiary)' }}>
                <span style={{ fontWeight: 500, color: 'var(--text-primary)' }}>{createdCompanyName}</span> {t('admin.companyCreatedDesc', 'has been created successfully.')}
              </p>
            </div>

            <div
              style={{
                padding: '16px',
                borderRadius: '8px',
                background: 'var(--bg-secondary)',
                border: '1px solid var(--border-subtle)',
                marginBottom: '16px',
              }}
            >
              <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '8px' }}>
                {t('admin.inviteCodeLabel', 'Admin Invitation Code')}
              </div>
              <div
                style={{
                  fontFamily: 'monospace',
                  fontSize: '22px',
                  fontWeight: 700,
                  letterSpacing: '3px',
                  color: 'var(--success)',
                  textAlign: 'center',
                  padding: '8px 0',
                  userSelect: 'all',
                }}
              >
                {createdCode}
              </div>
            </div>

            <div
              style={{
                fontSize: '12px',
                color: 'var(--text-tertiary)',
                lineHeight: '1.6',
                marginBottom: '20px',
                padding: '12px',
                borderRadius: '6px',
                background: 'rgba(59,130,246,0.06)',
                border: '1px solid rgba(59,130,246,0.12)',
              }}
            >
              <div style={{ fontWeight: 600, color: 'var(--text-secondary)', marginBottom: '4px' }}>{t('admin.inviteCodeHowTo', 'How to use this code:')}</div>
              {t(
                'admin.inviteCodeExplain',
                'Send this code to the person who will manage this company. They should register a new account on the platform, then enter this code to join. The first person to use it will automatically become the Org Admin of this company. This code is single-use.',
              )}
            </div>

            <div style={{ display: 'flex', gap: '8px' }}>
              <button className="btn btn-primary" onClick={handleCopyCode} style={{ flex: 1, height: '36px', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '6px' }}>
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
              <button className="btn btn-secondary" onClick={() => setCreatedCode('')} style={{ height: '36px', padding: '0 20px' }}>
                {t('common.close', 'Close')}
              </button>
            </div>
          </div>
        </div>
      )}

      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: '16px' }}>
        <button className="btn btn-primary" onClick={() => { setShowCreate(true); setCreatedCode(''); }}>
          + {t('admin.createCompany', 'Create Company')}
        </button>
      </div>

      {showCreate && (
        <div className="card" style={{ padding: '16px', marginBottom: '16px', border: '1px solid var(--accent-primary)' }}>
          <div style={{ fontSize: '13px', fontWeight: 600, marginBottom: '12px' }}>{t('admin.createCompany', 'Create Company')}</div>
          <div style={{ display: 'flex', gap: '8px' }}>
            <input
              className="form-input"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder={t('admin.companyNamePlaceholder', 'Company name')}
              onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
              style={{ flex: 1 }}
              autoFocus
            />
            <button className="btn btn-primary" onClick={handleCreate} disabled={creating || !newName.trim()}>
              {creating ? '...' : t('common.create', 'Create')}
            </button>
            <button className="btn btn-secondary" onClick={() => setShowCreate(false)}>
              {t('common.cancel', 'Cancel')}
            </button>
          </div>
        </div>
      )}

      <div className="card" style={{ padding: '0', flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'visible' }}>
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: gridCols,
            gap: '12px',
            padding: '10px 16px',
            fontSize: '11px',
            fontWeight: 600,
            color: 'var(--text-tertiary)',
            textTransform: 'uppercase',
            letterSpacing: '0.05em',
            borderBottom: '1px solid var(--border-subtle)',
            background: 'var(--bg-secondary)',
            borderRadius: 'var(--radius-lg) var(--radius-lg) 0 0',
            flexShrink: 0,
            position: 'relative',
            zIndex: 10,
          }}
        >
          {columns.map((col) => (
            <div key={col.key} style={thStyle} onClick={() => handleSort(col.key)}>
              {col.label}
              <SortArrow col={col.key} />
            </div>
          ))}
          <div ref={statusDropdownRef} style={{ position: 'relative', display: 'flex', alignItems: 'center', gap: '4px' }}>
            {t('admin.status', 'Status')}
            <button
              onClick={() => setShowStatusDropdown((value) => !value)}
              style={{
                background: 'none',
                border: 'none',
                padding: '2px',
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                borderRadius: '4px',
                color: statusFilter !== 'all' ? 'var(--accent-primary)' : 'var(--text-tertiary)',
                transition: 'color 0.15s',
              }}
              title={t('admin.filterStatus', 'Filter by status')}
            >
              <IconFilter size={14} stroke={statusFilter !== 'all' ? 2.5 : 1.8} />
            </button>
            {showStatusDropdown && (
              <div
                style={{
                  position: 'absolute',
                  top: '100%',
                  left: 0,
                  marginTop: '4px',
                  background: 'var(--bg-primary)',
                  border: '1px solid var(--border-subtle)',
                  borderRadius: '8px',
                  boxShadow: '0 8px 24px rgba(0,0,0,0.12)',
                  zIndex: 100,
                  minWidth: '120px',
                  padding: '4px',
                  overflow: 'hidden',
                }}
              >
                {(['all', 'active', 'disabled'] as const).map((value) => (
                  <div
                    key={value}
                    onClick={() => {
                      setStatusFilter(value);
                      setPage(0);
                      setShowStatusDropdown(false);
                    }}
                    style={{
                      padding: '6px 10px',
                      fontSize: '12px',
                      cursor: 'pointer',
                      borderRadius: '6px',
                      transition: 'background 0.1s',
                      color: statusFilter === value ? 'var(--accent-primary)' : 'var(--text-secondary)',
                      fontWeight: statusFilter === value ? 600 : 400,
                      background: statusFilter === value ? 'var(--bg-secondary)' : 'transparent',
                    }}
                  >
                    {value === 'all' ? t('admin.all', 'All') : value === 'active' ? t('admin.active', 'Active') : t('admin.disabled', 'Disabled')}
                  </div>
                ))}
              </div>
            )}
          </div>
          <div>{t('admin.action', 'Action')}</div>
        </div>

        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
          {loading && (
            <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)', fontSize: '13px' }}>{t('common.loading', 'Loading...')}</div>
          )}

          {error && <div style={{ textAlign: 'center', padding: '24px', color: 'var(--error)', fontSize: '13px' }}>{error}</div>}

          {!loading &&
            paged.map((company: any) => (
              <div
                key={company.id}
                style={{
                  display: 'grid',
                  gridTemplateColumns: gridCols,
                  gap: '12px',
                  padding: '12px 16px',
                  alignItems: 'center',
                  borderBottom: '1px solid var(--border-subtle)',
                  fontSize: '13px',
                  opacity: company.is_active ? 1 : 0.5,
                }}
              >
                <div>
                  <div style={{ fontWeight: 500 }}>{company.name}</div>
                  <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', fontFamily: 'monospace' }}>{company.slug}</div>
                </div>
                <div style={{ fontSize: '12px', color: company.org_admin_email ? 'var(--text-primary)' : 'var(--text-tertiary)' }}>{company.org_admin_email || '-'}</div>
                <div>{company.user_count ?? '-'}</div>
                <div>{company.agent_count ?? '-'}</div>
                <div style={{ fontSize: '12px', fontFamily: 'var(--font-mono)' }}>{formatTokens(company.total_tokens)}</div>
                <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{formatDate(company.created_at)}</div>
                <div>
                  <span className={`badge ${company.is_active ? 'badge-success' : 'badge-error'}`} style={{ fontSize: '10px' }}>
                    {company.is_active ? t('admin.active', 'Active') : t('admin.disabled', 'Disabled')}
                  </span>
                </div>
                <div>
                  <button
                    className="btn btn-ghost"
                    style={{
                      padding: '2px 8px',
                      fontSize: '11px',
                      height: '24px',
                      color: company.slug === 'default' ? 'var(--text-tertiary)' : company.is_active ? 'var(--error)' : 'var(--success)',
                      cursor: company.slug === 'default' ? 'not-allowed' : 'pointer',
                      opacity: company.slug === 'default' ? 0.5 : 1,
                    }}
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
            <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)', fontSize: '13px' }}>
              {statusFilter !== 'all' ? t('admin.noFilterResults', 'No companies match the current filter.') : t('common.noData', 'No data')}
            </div>
          )}
        </div>

        {!loading && totalPages > 1 && (
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              padding: '10px 16px',
              borderTop: '1px solid var(--border-subtle)',
              fontSize: '12px',
              color: 'var(--text-tertiary)',
              background: 'var(--bg-secondary)',
              flexShrink: 0,
              borderRadius: '0 0 var(--radius-lg) var(--radius-lg)',
            }}
          >
            <span>
              {t('admin.showing', '{{start}}-{{end}} of {{total}}', {
                start: page * PAGE_SIZE + 1,
                end: Math.min((page + 1) * PAGE_SIZE, sorted.length),
                total: sorted.length,
              })}
            </span>
            <div style={{ display: 'flex', gap: '4px' }}>
              <button className="btn btn-ghost" style={{ padding: '4px 10px', fontSize: '12px' }} disabled={page === 0} onClick={() => setPage((current) => current - 1)}>
                &lsaquo; {t('admin.prev', 'Prev')}
              </button>
              <button className="btn btn-ghost" style={{ padding: '4px 10px', fontSize: '12px' }} disabled={page >= totalPages - 1} onClick={() => setPage((current) => current + 1)}>
                {t('admin.next', 'Next')} &rsaquo;
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
