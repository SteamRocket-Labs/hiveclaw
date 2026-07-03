import { HTMLAttributes, ReactNode } from 'react';

export interface TooltipProps extends HTMLAttributes<HTMLSpanElement> {
    /** 提示文案 — 纯文本（CSS ::after 渲染）。 */
    label: string;
    children: ReactNode;
}

/** CSS-only tooltip — hover/focus 显示，无 JS 定位。 */
export default function Tooltip({ label, className, children, ...rest }: TooltipProps) {
    const classes = ['ui-tooltip-host'];
    if (className) classes.push(className);
    return (
        <span className={classes.join(' ')} data-tooltip={label} {...rest}>
            {children}
        </span>
    );
}
