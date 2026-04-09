import { useTranslation } from 'react-i18next';

interface NotificationCenterProps {
  isOpen: boolean;
  isChinese?: boolean;
  unreadCount: number;
  notifications: any[];
  notifCategory: string;
  onSetNotifCategory: (category: string) => void;
  onMarkAllRead: () => void;
  onClose: () => void;
  onNotificationClick: (notification: any) => void;
  selectedNotification: any | null;
  onCloseDetail: () => void;
}

const NOTIFICATION_TAB_KEYS = ['all', 'tool', 'approval', 'social'] as const;

export default function NotificationCenter({
  isOpen,
  unreadCount,
  notifications,
  notifCategory,
  onSetNotifCategory,
  onMarkAllRead,
  onClose,
  onNotificationClick,
  selectedNotification,
  onCloseDetail,
}: NotificationCenterProps) {
  const { t } = useTranslation();
  return (
    <>
      {isOpen && (
        <>
          <div style={{ position: 'fixed', inset: 0, zIndex: 9998, background: 'rgba(0,0,0,0.5)' }} onClick={onClose} />
          <div
            style={{
              position: 'fixed',
              top: '50%',
              left: '50%',
              transform: 'translate(-50%, -50%)',
              width: 'calc(100vw - 80px)',
              maxWidth: '800px',
              height: '80vh',
              maxHeight: '800px',
              background: 'var(--bg-primary)',
              border: '1px solid var(--border-subtle)',
              borderRadius: '12px',
              boxShadow: '0 20px 60px rgba(0,0,0,0.3)',
              zIndex: 9999,
              display: 'flex',
              flexDirection: 'column',
              overflow: 'hidden',
            }}
          >
            <div style={{ borderBottom: '1px solid var(--border-subtle)', flexShrink: 0 }}>
              <div style={{ padding: '16px 24px 0', display: 'flex', alignItems: 'center', gap: '8px' }}>
                <h3 style={{ margin: 0, fontSize: '16px', fontWeight: 600, flex: 1 }}>{t('notifications.title')}</h3>
                {unreadCount > 0 && (
                  <button className="btn btn-ghost" onClick={onMarkAllRead} style={{ fontSize: '12px', padding: '4px 10px' }}>
                    {t('notifications.markAllRead')}
                  </button>
                )}
                <button className="btn btn-ghost" onClick={onClose} style={{ padding: '4px 8px', fontSize: '18px', lineHeight: 1 }}>
                  ×
                </button>
              </div>
              <div style={{ display: 'flex', gap: '0', padding: '0 24px', marginTop: '12px' }}>
                {NOTIFICATION_TAB_KEYS.map((key) => (
                  <button
                    key={key}
                    onClick={() => onSetNotifCategory(key)}
                    style={{
                      background: 'none',
                      border: 'none',
                      cursor: 'pointer',
                      padding: '8px 14px',
                      fontSize: '13px',
                      fontWeight: 500,
                      color: notifCategory === key ? 'var(--text-primary)' : 'var(--text-tertiary)',
                      borderBottom: notifCategory === key ? '2px solid var(--accent-primary)' : '2px solid transparent',
                      marginBottom: '-1px',
                      transition: 'all 0.15s',
                    }}
                  >
                    {t(`notifications.tabs.${key}`)}
                  </button>
                ))}
              </div>
            </div>

            <div style={{ flex: 1, overflowY: 'auto', padding: '8px 0' }}>
              {notifications.length === 0 && (
                <div style={{ textAlign: 'center', padding: '60px 20px', color: 'var(--text-tertiary)', fontSize: '13px' }}>
                  {t('notifications.empty')}
                </div>
              )}
              {notifications.map((notification) => (
                <div
                  key={notification.id}
                  onClick={() => onNotificationClick(notification)}
                  style={{
                    padding: '14px 24px',
                    cursor: 'pointer',
                    borderBottom: '1px solid var(--border-subtle)',
                    background: notification.is_read ? 'transparent' : 'var(--bg-secondary)',
                    transition: 'background 0.15s',
                  }}
                  onMouseEnter={(event) => {
                    event.currentTarget.style.background = 'var(--bg-tertiary)';
                  }}
                  onMouseLeave={(event) => {
                    event.currentTarget.style.background = notification.is_read ? 'transparent' : 'var(--bg-secondary)';
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '4px' }}>
                    {!notification.is_read && (
                      <span style={{ width: '6px', height: '6px', borderRadius: '50%', background: 'var(--accent-primary)', flexShrink: 0 }} />
                    )}
                    <span style={{ fontSize: '13px', fontWeight: 500, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {notification.title}
                    </span>
                  </div>
                  {notification.body && (
                    <div
                      style={{
                        fontSize: '12px',
                        color: 'var(--text-tertiary)',
                        lineHeight: '1.4',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      {notification.body}
                    </div>
                  )}
                  <div style={{ fontSize: '11px', color: 'var(--text-quaternary)', marginTop: '4px' }}>
                    {notification.created_at ? new Date(notification.created_at).toLocaleString() : ''}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </>
      )}

      {selectedNotification && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 10000,
            background: 'rgba(0,0,0,0.5)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
          onClick={onCloseDetail}
        >
          <div
            style={{
              background: 'var(--bg-primary)',
              borderRadius: '12px',
              border: '1px solid var(--border-subtle)',
              width: '480px',
              maxHeight: '90vh',
              display: 'flex',
              flexDirection: 'column',
              boxShadow: '0 20px 60px rgba(0,0,0,0.3)',
            }}
            onClick={(event) => event.stopPropagation()}
          >
            <div
              style={{
                padding: '20px 24px',
                borderBottom: '1px solid var(--border-subtle)',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
              }}
            >
              <h3 style={{ margin: 0, fontSize: '16px', fontWeight: 600 }}>{selectedNotification.title}</h3>
              <button
                onClick={onCloseDetail}
                style={{ background: 'none', border: 'none', color: 'var(--text-tertiary)', fontSize: '20px', cursor: 'pointer', padding: '0' }}
              >
                ×
              </button>
            </div>
            <div
              style={{
                padding: '20px 24px',
                overflowY: 'auto',
                fontSize: '14px',
                lineHeight: '1.6',
                color: 'var(--text-primary)',
                whiteSpace: 'pre-wrap',
              }}
            >
              {selectedNotification.body || t('notifications.noDetails')}
            </div>
            <div
              style={{
                padding: '16px 24px',
                borderTop: '1px solid var(--border-subtle)',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                color: 'var(--text-tertiary)',
                fontSize: '12px',
              }}
            >
              <span>
                {selectedNotification.sender_name
                  ? t('notifications.from', { name: selectedNotification.sender_name })
                  : ''}
              </span>
              <span>{selectedNotification.created_at ? new Date(selectedNotification.created_at).toLocaleString() : ''}</span>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
