import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { enterpriseApi, type InvitationCode } from '../api/domains/enterprise';
import './InvitationCodes.css';

function errorMessage(cause: unknown, fallback: string): string {
    return cause instanceof Error && cause.message ? cause.message : fallback;
}

export default function InvitationCodes() {
    const { t } = useTranslation();
    const [codes, setCodes] = useState<InvitationCode[]>([]);
    const [total, setTotal] = useState(0);
    const [page, setPage] = useState(1);
    const [search, setSearch] = useState('');
    const pageSize = 20;
    const [batchCount, setBatchCount] = useState(5);
    const [maxUses, setMaxUses] = useState(5);
    const [creating, setCreating] = useState(false);
    const [deactivatingId, setDeactivatingId] = useState('');
    const [exporting, setExporting] = useState(false);
    const [loadError, setLoadError] = useState('');
    const [actionError, setActionError] = useState('');
    const [toast, setToast] = useState('');

    const loadCodes = useCallback(async (currentPage: number, currentSearch: string) => {
        setLoadError('');
        const params = new URLSearchParams({
            page: String(currentPage),
            page_size: String(pageSize),
        });
        if (currentSearch) params.set('search', currentSearch);
        try {
            const data = await enterpriseApi.listInvitationCodes(params.toString());
            setCodes(data.items || []);
            setTotal(data.total || 0);
        } catch (cause) {
            setLoadError(errorMessage(
                cause,
                t('enterprise.invites.loadFailed', 'Could not load invitation codes.'),
            ));
        }
    }, [t]);

    useEffect(() => { void loadCodes(page, search); }, [loadCodes, page, search]);

    const totalPages = Math.max(1, Math.ceil(total / pageSize));

    const handleSearch = (value: string) => {
        setSearch(value);
        setPage(1);
    };

    const createBatch = async () => {
        setCreating(true);
        setActionError('');
        try {
            await enterpriseApi.createInvitationCode({ count: batchCount, max_uses: maxUses });
            const alreadyOnFirstPage = page === 1 && search === '';
            setPage(1);
            setSearch('');
            if (alreadyOnFirstPage) await loadCodes(1, '');
            setToast(t('enterprise.invites.createdToast', 'Invitation codes created.'));
            setTimeout(() => setToast(''), 2000);
        } catch (cause) {
            setActionError(errorMessage(
                cause,
                t('enterprise.invites.createFailed', 'Could not create invitation codes.'),
            ));
        } finally {
            setCreating(false);
        }
    };

    const deactivate = async (id: string) => {
        setDeactivatingId(id);
        setActionError('');
        try {
            await enterpriseApi.deleteInvitationCode(id);
            await loadCodes(page, search);
        } catch (cause) {
            setActionError(errorMessage(
                cause,
                t('enterprise.invites.disableFailed', 'Could not disable the invitation code.'),
            ));
        } finally {
            setDeactivatingId('');
        }
    };

    const exportCsv = async () => {
        setExporting(true);
        setActionError('');
        try {
            const blob = await enterpriseApi.exportInvitationCodesCsv();
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'invitation_codes.csv';
            a.click();
            URL.revokeObjectURL(a.href);
        } catch (cause) {
            setActionError(errorMessage(
                cause,
                t('enterprise.invites.exportFailed', 'Could not export invitation codes.'),
            ));
        } finally {
            setExporting(false);
        }
    };

    return (
        <div className="content-area invitation-codes-page">
            {toast && (
                <div className="invitation-codes-toast" role="status" aria-live="polite">{toast}</div>
            )}

            <h2 className="invitation-codes-title">
                {t('enterprise.invites.pageTitle', 'Invitation Codes')}
            </h2>
            <p className="invitation-codes-subtitle">
                {t('enterprise.invites.pageDesc', 'Manage invitation codes for new team members to join this company workspace.')}
            </p>

            {loadError && (
                <div className="invitation-codes-error" role="alert">
                    <span>{loadError}</span>
                    <button type="button" className="btn btn-secondary" onClick={() => void loadCodes(page, search)}>
                        {t('common.retry', 'Retry')}
                    </button>
                </div>
            )}
            {actionError && (
                <div className="invitation-codes-error" role="alert">{actionError}</div>
            )}

            {/* Batch Create */}
            <div className="card invitation-codes-card">
                <div className="invitation-codes-create-title">
                    {t('enterprise.invites.createTitle', 'Create Invitation Codes')}
                </div>
                <div className="invitation-codes-create-row">
                    <div className="invitation-codes-field">
                        <label className="invitation-codes-label" htmlFor="invitation-code-count">
                            {t('enterprise.invites.count', 'Number of Codes')}
                        </label>
                        <input id="invitation-code-count" className="form-input" type="number" min={1} max={100}
                            value={batchCount} onChange={e => setBatchCount(Number(e.target.value))} />
                    </div>
                    <div className="invitation-codes-field">
                        <label className="invitation-codes-label" htmlFor="invitation-code-max-uses">
                            {t('enterprise.invites.maxUses', 'Max Uses per Code')}
                        </label>
                        <input id="invitation-code-max-uses" className="form-input" type="number" min={1}
                            value={maxUses} onChange={e => setMaxUses(Number(e.target.value))} />
                    </div>
                    <button type="button" className="btn btn-primary invitation-codes-gen-btn" onClick={() => void createBatch()} disabled={creating}>
                        {creating ? t('common.loading') : t('enterprise.invites.createBtn', 'Generate')}
                    </button>
                </div>
            </div>

            {/* Search + Codes Table */}
            <div className="card">
                <div className="invitation-codes-list-head">
                    <div className="invitation-codes-list-title">
                        {t('enterprise.invites.listTitle', 'All Invitation Codes')} ({total})
                    </div>
                    <div className="invitation-codes-search-wrap">
                        <input
                            className="form-input invitation-codes-search"
                            placeholder={t('common.search', 'Search') + '...'}
                            aria-label={t('common.search', 'Search')}
                            value={search}
                            onChange={e => handleSearch(e.target.value)}
                        />
                        <button type="button" className="btn btn-secondary invitation-codes-export-btn" onClick={() => void exportCsv()} disabled={exporting}>
                            {exporting ? t('common.loading') : t('enterprise.invites.exportCsv')}
                        </button>
                    </div>
                </div>

                {/* Table header */}
                <div className="invitation-codes-thead">
                    <div>{t('enterprise.invites.code', 'Code')}</div>
                    <div>{t('enterprise.invites.usage', 'Usage')}</div>
                    <div>{t('enterprise.invites.status', 'Status')}</div>
                    <div>{t('enterprise.invites.created', 'Created')}</div>
                    <div></div>
                </div>

                {!loadError && codes.length === 0 && (
                    <div className="invitation-codes-empty">
                        {t('common.noData')}
                    </div>
                )}

                {codes.map((c) => (
                    <div key={c.id} className="invitation-codes-row">
                        <div className="invitation-codes-code">{c.code}</div>
                        <div>
                            <span className="invitation-codes-usage-used">{c.used_count}</span>
                            <span className="invitation-codes-usage-total"> / {c.max_uses}</span>
                        </div>
                        <div>
                            {!c.is_active ? (
                                <span className="badge badge-neutral">
                                    {t('enterprise.invites.deactivated', 'Disabled')}
                                </span>
                            ) : c.used_count >= c.max_uses ? (
                                <span className="badge badge-warning">
                                    {t('enterprise.invites.exhausted', 'Exhausted')}
                                </span>
                            ) : (
                                <span className="badge badge-success">
                                    {t('enterprise.invites.active', 'Active')}
                                </span>
                            )}
                        </div>
                        <div className="invitation-codes-date">
                            {c.created_at ? new Date(c.created_at).toLocaleDateString() : '-'}
                        </div>
                        <div>
                            {c.is_active && c.used_count < c.max_uses && (
                                <button type="button" className="btn btn-secondary invitation-codes-disable-btn"
                                    onClick={() => void deactivate(c.id)} disabled={deactivatingId === c.id}>
                                    {deactivatingId === c.id
                                        ? t('common.loading')
                                        : t('enterprise.invites.disable', 'Disable')}
                                </button>
                            )}
                        </div>
                    </div>
                ))}

                {/* Pagination */}
                {totalPages > 1 && (
                    <div className="invitation-codes-pagination">
                        <button type="button" className="btn btn-secondary invitation-codes-page-btn"
                            aria-label={t('enterprise.invites.previousPage', 'Previous page')}
                            disabled={page <= 1} onClick={() => setPage(p => p - 1)}>
                            ←
                        </button>
                        <span className="invitation-codes-page-info">
                            {page} / {totalPages}
                        </span>
                        <button type="button" className="btn btn-secondary invitation-codes-page-btn"
                            aria-label={t('enterprise.invites.nextPage', 'Next page')}
                            disabled={page >= totalPages} onClick={() => setPage(p => p + 1)}>
                            →
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
}
