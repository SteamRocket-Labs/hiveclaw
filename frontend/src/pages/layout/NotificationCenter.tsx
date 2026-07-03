import { useTranslation } from 'react-i18next';
import './NotificationCenter.css';

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
          <div className="notification-center-backdrop" onClick={onClose} />
          <div className="notification-center-panel">
            <div className="notification-center-header">
              <div className="notification-center-header-top">
                <h3 className="notification-center-title">{t('notifications.title')}</h3>
                {unreadCount > 0 && (
                  <button className="btn btn-ghost" onClick={onMarkAllRead}>
                    {t('notifications.markAllRead')}
                  </button>
                )}
                <button className="notification-center-close" onClick={onClose}>
                  ×
                </button>
              </div>
              <div className="notification-center-tabs">
                {NOTIFICATION_TAB_KEYS.map((key) => (
                  <button
                    key={key}
                    onClick={() => onSetNotifCategory(key)}
                    className={`notification-center-tab${notifCategory === key ? ' active' : ''}`}
                  >
                    {t(`notifications.tabs.${key}`)}
                  </button>
                ))}
              </div>
            </div>

            <div className="notification-center-list">
              {notifications.length === 0 && (
                <div className="notification-center-empty">
                  {t('notifications.empty')}
                </div>
              )}
              {notifications.map((notification) => (
                <div
                  key={notification.id}
                  onClick={() => onNotificationClick(notification)}
                  className={`notification-center-item${notification.is_read ? '' : ' notification-center-item--unread'}`}
                >
                  <div className="notification-center-item-head">
                    {!notification.is_read && <span className="notification-center-dot" />}
                    <span className="notification-center-item-title">
                      {notification.title}
                    </span>
                  </div>
                  {notification.body && (
                    <div className="notification-center-item-body">
                      {notification.body}
                    </div>
                  )}
                  <div className="notification-center-item-time">
                    {notification.created_at ? new Date(notification.created_at).toLocaleString() : ''}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </>
      )}

      {selectedNotification && (
        <div className="ui-modal-overlay" onClick={onCloseDetail}>
          <div className="notification-center-detail" onClick={(event) => event.stopPropagation()}>
            <div className="notification-center-detail-header">
              <h3 className="notification-center-detail-title">{selectedNotification.title}</h3>
              <button className="notification-center-detail-close" onClick={onCloseDetail}>
                ×
              </button>
            </div>
            <div className="notification-center-detail-body">
              {selectedNotification.body || t('notifications.noDetails')}
            </div>
            <div className="notification-center-detail-footer">
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
