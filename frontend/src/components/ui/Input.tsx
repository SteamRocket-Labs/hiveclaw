import { InputHTMLAttributes, TextareaHTMLAttributes, forwardRef } from 'react';

export interface InputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'size'> {
    size?: 'sm' | 'md';
    invalid?: boolean;
}

/** 紧凑输入框 — 28px（sm）/ 32px（md）。 */
export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
    { size = 'sm', invalid = false, className, ...rest },
    ref,
) {
    const classes = ['ui-input'];
    if (size === 'md') classes.push('ui-input-md');
    if (invalid) classes.push('ui-input-invalid');
    if (className) classes.push(className);
    return <input ref={ref} className={classes.join(' ')} {...rest} />;
});

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
    invalid?: boolean;
}

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
    { invalid = false, className, ...rest },
    ref,
) {
    const classes = ['ui-textarea'];
    if (invalid) classes.push('ui-input-invalid');
    if (className) classes.push(className);
    return <textarea ref={ref} className={classes.join(' ')} {...rest} />;
});

export default Input;
