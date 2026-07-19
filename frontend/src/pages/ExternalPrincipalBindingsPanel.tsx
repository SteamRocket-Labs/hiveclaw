import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';

import type { ExternalPrincipal } from '../api/domains/externalPrincipals';
import './ExternalPrincipalBindingsPanel.css';

export interface BindableUser {
  id: string;
  username: string;
  display_name: string;
  is_active: boolean;
}

interface Props {
  principals: ExternalPrincipal[];
  users: BindableUser[];
  loading: boolean;
  busyPrincipalId: string | null;
  onUnlink: (principalId: string) => void | Promise<void>;
}

export default function ExternalPrincipalBindingsPanel({
  principals,
  users,
  loading,
  busyPrincipalId,
  onUnlink,
}: Props) {
  const { t } = useTranslation();
  const usersById = useMemo(() => new Map(users.map((user) => [user.id, user])), [users]);

  return (
    <section className="external-principal-panel" aria-label={t(
      'userManagement.externalPrincipalsTitle',
      'External channel identities',
    )}>
      <div className="external-principal-heading">
        <div>
          <h3>{t('userManagement.externalPrincipalsTitle', 'External channel identities')}</h3>
          <p>{t(
            'userManagement.externalPrincipalsDescription',
            'Users verify their own IM identity from the channel connection flow. Admins can review or revoke a binding, but cannot assign one.',
          )}</p>
        </div>
        <span className="external-principal-count">{principals.length}</span>
      </div>

      {loading ? (
        <div className="external-principal-empty">{t('common.loading', 'Loading')}…</div>
      ) : principals.length === 0 ? (
        <div className="external-principal-empty">
          {t('userManagement.externalPrincipalsEmpty', 'No external channel identities yet.')}
        </div>
      ) : (
        <div className="external-principal-rows">
          {principals.map((principal) => {
            const linkedUser = principal.linked_user_id
              ? usersById.get(principal.linked_user_id)
              : undefined;
            const busy = busyPrincipalId === principal.id;
            return (
              <div className="external-principal-row" key={principal.id}>
                <div className="external-principal-identity">
                  <span className="external-principal-provider">{principal.provider}</span>
                  <strong>{principal.display_name}</strong>
                  <span className="external-principal-subject">{principal.subject_id}</span>
                </div>
                <div className="external-principal-binding">
                  {principal.status === 'revoked' ? (
                    <span className="external-principal-revoked">
                      {t('userManagement.externalPrincipalRevoked', 'Provider access revoked')}
                    </span>
                  ) : principal.linked_user_id ? (
                    <>
                      <span>
                        {t('userManagement.externalPrincipalBoundTo', 'Verified as')}{' '}
                        <strong>{linkedUser?.display_name || linkedUser?.username || t(
                          'userManagement.externalPrincipalBoundUserUnavailable',
                          'Unavailable member',
                        )}</strong>
                      </span>
                      <button
                        className="btn btn-secondary external-principal-action"
                        type="button"
                        disabled={busy}
                        onClick={() => void onUnlink(principal.id)}
                      >
                        {t('userManagement.externalPrincipalUnlink', 'Unlink')}
                      </button>
                    </>
                  ) : (
                    <span className="external-principal-pending">
                      {t(
                        'userManagement.externalPrincipalAwaitingVerification',
                        'Waiting for the user to verify this identity from the channel connection flow',
                      )}
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
