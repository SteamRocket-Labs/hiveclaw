import { ButtonHTMLAttributes, forwardRef } from 'react';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
export type ButtonSize = 'md' | 'sm';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
    variant?: ButtonVariant;
    size?: ButtonSize;
    loading?: boolean;
}

/** 原子按钮 — 复用全站 .btn class 体系（styles/primitives.css）。 */
const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
    { variant = 'secondary', size = 'md', loading = false, className, children, disabled, type, ...rest },
    ref,
) {
    const classes = ['btn', `btn-${variant}`];
    if (size === 'sm') classes.push('btn-sm');
    if (loading) classes.push('loading');
    if (className) classes.push(className);
    return (
        <button
            ref={ref}
            type={type ?? 'button'}
            className={classes.join(' ')}
            disabled={disabled || loading}
            {...rest}
        >
            {loading && <span className="ui-spinner ui-spinner-sm" aria-hidden="true" />}
            {children}
        </button>
    );
});

export default Button;
