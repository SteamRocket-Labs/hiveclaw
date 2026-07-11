import { useMemo, useState } from 'react';
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
  onLink: (principalId: string, userId: string) => void | Promise<void>;
  onUnlink: (principalId: string) => void | Promise<void>;
}

export default function ExternalPrincipalBindingsPanel({
  principals,
  users,
  loading,
  busyPrincipalId,
  onLink,
  onUnlink,
}: Props) {
  const { t } = useTranslation();
  const [selectedUsers, setSelectedUsers] = useState<Record<string, string>>({});
  const activeUsers = useMemo(() => users.filter((user) => user.is_active), [users]);
  const usersById = useMemo(() => new Map(activeUsers.map((user) => [user.id, user])), [activeUsers]);

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
            'External senders stay separate from licensed members until an admin explicitly binds an invited account.',
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
            const selectedUserId = selectedUsers[principal.id] ?? '';
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
                  ) : linkedUser ? (
                    <>
                      <span>
                        {t('userManagement.externalPrincipalBoundTo', 'Bound to')} <strong>{linkedUser.display_name || linkedUser.username}</strong>
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
                    <>
                      <select
                        className="form-input external-principal-select"
                        aria-label={t('userManagement.externalPrincipalMember', 'Invited member')}
                        value={selectedUserId}
                        onChange={(event) => setSelectedUsers((current) => ({
                          ...current,
                          [principal.id]: event.target.value,
                        }))}
                      >
                        <option value="">{t('userManagement.externalPrincipalChooseMember', 'Choose invited member')}</option>
                        {activeUsers.map((user) => (
                          <option key={user.id} value={user.id}>
                            {user.display_name || user.username}
                          </option>
                        ))}
                      </select>
                      <button
                        className="btn btn-primary external-principal-action"
                        type="button"
                        disabled={!selectedUserId || busy}
                        onClick={() => void onLink(principal.id, selectedUserId)}
                      >
                        {t('userManagement.externalPrincipalBind', 'Bind to invited member')}
                      </button>
                    </>
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
