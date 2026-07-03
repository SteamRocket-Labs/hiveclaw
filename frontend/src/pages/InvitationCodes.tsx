import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { enterpriseApi } from '../api/domains/enterprise';
import './InvitationCodes.css';

export default function InvitationCodes() {
    const { t } = useTranslation();
    const [codes, setCodes] = useState<any[]>([]);
    const [total, setTotal] = useState(0);
    const [page, setPage] = useState(1);
    const [search, setSearch] = useState('');
    const pageSize = 20;
    const [batchCount, setBatchCount] = useState(5);
    const [maxUses, setMaxUses] = useState(5);
    const [creating, setCreating] = useState(false);
    const [toast, setToast] = useState('');

    const loadCodes = useCallback(async (p?: number, q?: string) => {
        const currentPage = p ?? page;
        const currentSearch = q ?? search;
        const params = new URLSearchParams({
            page: String(currentPage),
            page_size: String(pageSize),
        });
        if (currentSearch) params.set('search', currentSearch);
        const data = await enterpriseApi.listInvitationCodes(params.toString());
        setCodes(data.items || []);
        setTotal(data.total || 0);
    }, [page, search]);

    useEffect(() => { loadCodes(page, search); }, [page, search]);

    const totalPages = Math.max(1, Math.ceil(total / pageSize));

    const handleSearch = (value: string) => {
        setSearch(value);
        setPage(1);
    };

    const createBatch = async () => {
        setCreating(true);
        await enterpriseApi.createInvitationCode({ count: batchCount, max_uses: maxUses });
        setPage(1);
        setSearch('');
        await loadCodes(1, '');
        setCreating(false);
        setToast(t('enterprise.invites.createBtn', 'Created!'));
        setTimeout(() => setToast(''), 2000);
    };

    const deactivate = async (id: string) => {
        await enterpriseApi.deleteInvitationCode(id);
        await loadCodes();
    };

    const exportCsv = () => {
        const a = document.createElement('a');
        enterpriseApi.exportInvitationCodesCsv()
            .then(blob => {
                a.href = URL.createObjectURL(blob);
                a.download = 'invitation_codes.csv';
                a.click();
                URL.revokeObjectURL(a.href);
            });
    };

    return (
        <div className="content-area invitation-codes-page">
            {toast && (
                <div className="invitation-codes-toast">{toast}</div>
            )}

            <h2 className="invitation-codes-title">
                {t('enterprise.invites.pageTitle', 'Invitation Codes')}
            </h2>
            <p className="invitation-codes-subtitle">
                {t('enterprise.invites.pageDesc', 'Manage invitation codes for platform registration.')}
            </p>

            {/* Batch Create */}
            <div className="card invitation-codes-card">
                <div className="invitation-codes-create-title">
                    {t('enterprise.invites.createTitle', 'Create Invitation Codes')}
                </div>
                <div className="invitation-codes-create-row">
                    <div className="invitation-codes-field">
                        <label className="invitation-codes-label">
                            {t('enterprise.invites.count', 'Number of Codes')}
                        </label>
                        <input className="form-input" type="number" min={1} max={100}
                            value={batchCount} onChange={e => setBatchCount(Number(e.target.value))} />
                    </div>
                    <div className="invitation-codes-field">
                        <label className="invitation-codes-label">
                            {t('enterprise.invites.maxUses', 'Max Uses per Code')}
                        </label>
                        <input className="form-input" type="number" min={1}
                            value={maxUses} onChange={e => setMaxUses(Number(e.target.value))} />
                    </div>
                    <button className="btn btn-primary invitation-codes-gen-btn" onClick={createBatch} disabled={creating}>
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
                            value={search}
                            onChange={e => handleSearch(e.target.value)}
                        />
                        <button className="btn btn-secondary invitation-codes-export-btn" onClick={exportCsv}>
                            {t('enterprise.invites.exportCsv')}
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

                {codes.length === 0 && (
                    <div className="invitation-codes-empty">
                        {t('common.noData')}
                    </div>
                )}

                {codes.map((c: any) => (
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
                                <button className="btn btn-secondary invitation-codes-disable-btn"
                                    onClick={() => deactivate(c.id)}>
                                    {t('enterprise.invites.disable', 'Disable')}
                                </button>
                            )}
                        </div>
                    </div>
                ))}

                {/* Pagination */}
                {totalPages > 1 && (
                    <div className="invitation-codes-pagination">
                        <button className="btn btn-secondary invitation-codes-page-btn"
                            disabled={page <= 1} onClick={() => setPage(p => p - 1)}>
                            ←
                        </button>
                        <span className="invitation-codes-page-info">
                            {page} / {totalPages}
                        </span>
                        <button className="btn btn-secondary invitation-codes-page-btn"
                            disabled={page >= totalPages} onClick={() => setPage(p => p + 1)}>
                            →
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
}
