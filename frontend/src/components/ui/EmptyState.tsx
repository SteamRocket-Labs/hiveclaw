import { ReactNode } from 'react';

export interface EmptyStateProps {
    icon?: ReactNode;
    title: ReactNode;
    description?: ReactNode;
    action?: ReactNode;
}

/** 空态 — 图标 + 一句话 + 一个动作，不做插画堆砌。 */
export default function EmptyState({ icon, title, description, action }: EmptyStateProps) {
    return (
        <div className="ui-empty">
            {icon != null && (
                <div className="ui-empty-icon" aria-hidden="true">
                    {icon}
                </div>
            )}
            <div className="ui-empty-title">{title}</div>
            {description != null && <div className="ui-empty-desc">{description}</div>}
            {action != null && <div className="ui-empty-action">{action}</div>}
        </div>
    );
}
