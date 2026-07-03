import { SelectHTMLAttributes, forwardRef } from 'react';

export interface SelectProps extends Omit<SelectHTMLAttributes<HTMLSelectElement>, 'size'> {
    size?: 'sm' | 'md';
}

/** 原生 select 包装 — 自定义箭头，与 Input 同高。 */
const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
    { size = 'sm', className, children, ...rest },
    ref,
) {
    const classes = ['ui-select'];
    if (size === 'md') classes.push('ui-select-md');
    if (className) classes.push(className);
    return (
        <select ref={ref} className={classes.join(' ')} {...rest}>
            {children}
        </select>
    );
});

export default Select;
