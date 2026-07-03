import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { messageApi } from '../api/domains/messages';
import './Messages.css';

const ACTION_ICONS: Record<string, string> = {
    text: '💬',
    notify: '·',
    consult: '?',
    task_delegate: '+',
};

export default function Messages() {
    const { t, i18n } = useTranslation();
    const queryClient = useQueryClient();
    const { data: messages = [], isLoading } = useQuery({
        queryKey: ['messages-inbox'],
        queryFn: () => messageApi.inbox(100),
        refetchInterval: 15000,
    });

    const markReadMutation = useMutation({
        mutationFn: (id: string) => messageApi.markRead(id),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['messages-inbox'] });
            queryClient.invalidateQueries({ queryKey: ['unread-count'] });
        },
    });

    const markAllReadMutation = useMutation({
        mutationFn: () => messageApi.markAllRead(),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['messages-inbox'] });
            queryClient.invalidateQueries({ queryKey: ['unread-count'] });
        },
    });

    const unreadCount = messages.filter((m: any) => !m.read_at).length;

    const formatTime = (iso: string) => {
        if (!iso) return '';
        const d = new Date(iso);
        const now = new Date();
        const diffMs = now.getTime() - d.getTime();
        if (diffMs < 60000) return t('messages.justNow');
        if (diffMs < 3600000) return t('messages.minutesAgo', { count: Math.floor(diffMs / 60000) });
        if (diffMs < 86400000) return t('messages.hoursAgo', { count: Math.floor(diffMs / 3600000) });
        return d.toLocaleDateString(i18n.language === 'zh' ? 'zh-CN' : 'en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    };

    return (
        <div className="messages-page">
            <div className="messages-page-header">
                <h1 className="messages-page-title">{t('messages.title')}</h1>
                {unreadCount > 0 && (
                    <button
                        className="btn btn-ghost messages-page-mark-all"
                        onClick={() => markAllReadMutation.mutate()}
                    >
                        {t('messages.markAllRead', { count: unreadCount })}
                    </button>
                )}
            </div>

            {isLoading && (
                <div className="messages-page-loading">{t('common.loading')}</div>
            )}

            {!isLoading && messages.length === 0 && (
                <div className="messages-page-empty">
                    <div className="messages-page-empty-hint">{t('messages.empty')}</div>
                    <div>{t('messages.empty')}</div>
                </div>
            )}

            <div className="messages-page-list">
                {messages.map((msg: any) => (
                    <div
                        key={msg.id}
                        onClick={() => !msg.read_at && markReadMutation.mutate(msg.id)}
                        className={`messages-page-row${msg.read_at ? '' : ' messages-page-row--unread'}`}
                    >
                        <div className="messages-page-row-head">
                            <span className="messages-page-icon">{ACTION_ICONS[msg.msg_type] || '·'}</span>
                            <span className="messages-page-sender">
                                {msg.sender_name}
                            </span>
                            <span className="u-meta u-tertiary">
                                → {msg.receiver_name}
                            </span>
                            <span className="u-meta u-tertiary messages-page-time">
                                {formatTime(msg.created_at)}
                            </span>
                            {!msg.read_at && (
                                <span className="messages-page-dot" />
                            )}
                        </div>
                        <div className="messages-page-body">
                            {msg.content}
                        </div>
                    </div>
                ))}
            </div>
        </div>
    );
}
