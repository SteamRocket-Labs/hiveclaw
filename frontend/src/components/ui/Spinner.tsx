export interface SpinnerProps {
    size?: 'md' | 'sm';
    /** 可读加载说明（默认"加载中"语义由调用方文案承担）。 */
    label?: string;
}

/** 加载指示 — 细边框旋转，无大动效。 */
export default function Spinner({ size = 'md', label }: SpinnerProps) {
    const classes = ['ui-spinner'];
    if (size === 'sm') classes.push('ui-spinner-sm');
    return <span className={classes.join(' ')} role="status" aria-label={label} />;
}
