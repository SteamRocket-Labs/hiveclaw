import { ButtonHTMLAttributes, forwardRef } from 'react';

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
    /** 无文字按钮必须有可读名称（aria-label + tooltip）。 */
    label: string;
    size?: 'md' | 'sm';
    /** 显示 CSS tooltip（默认开启）。 */
    tooltip?: boolean;
}

/** 方形图标按钮 — ghost 形态，三态齐全。 */
const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
    { label, size = 'md', tooltip = true, className, children, type, ...rest },
    ref,
) {
    const classes = ['ui-icon-btn'];
    if (size === 'sm') classes.push('ui-icon-btn-sm');
    if (tooltip) classes.push('ui-tooltip-host');
    if (className) classes.push(className);
    return (
        <button
            ref={ref}
            type={type ?? 'button'}
            className={classes.join(' ')}
            aria-label={label}
            data-tooltip={tooltip ? label : undefined}
            {...rest}
        >
            {children}
        </button>
    );
});

export default IconButton;
