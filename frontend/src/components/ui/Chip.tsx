import { HTMLAttributes } from 'react';

export type ChipTone = 'neutral' | 'success' | 'warning' | 'error' | 'info';

export interface ChipProps extends HTMLAttributes<HTMLSpanElement> {
    tone?: ChipTone;
    /** 左侧状态小点。 */
    dot?: boolean;
}

/** 元信息胶囊 — 11px，彩色只上小字与小点，不上大面积。 */
export default function Chip({ tone = 'neutral', dot = false, className, children, ...rest }: ChipProps) {
    const classes = ['ui-chip', `ui-chip-${tone}`];
    if (className) classes.push(className);
    return (
        <span className={classes.join(' ')} {...rest}>
            {dot && <span className="ui-chip-dot" aria-hidden="true" />}
            {children}
        </span>
    );
}
