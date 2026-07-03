import { HTMLAttributes } from 'react';

export type BadgeTone = 'neutral' | 'success' | 'warning' | 'error' | 'info';

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
    tone?: BadgeTone;
}

/** 计数/状态徽章 — 复用全站 .badge class 体系。 */
export default function Badge({ tone = 'neutral', className, children, ...rest }: BadgeProps) {
    const classes = ['badge', `badge-${tone}`];
    if (className) classes.push(className);
    return (
        <span className={classes.join(' ')} {...rest}>
            {children}
        </span>
    );
}
