import { HTMLAttributes, forwardRef } from 'react';

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
    padding?: 'md' | 'sm' | 'none';
    /** 可点卡片 — 安静的 hover（背景/边框一档，无阴影无位移）。 */
    interactive?: boolean;
}

/** 原子卡片 — 复用全站 .card class（styles/primitives.css）。 */
const Card = forwardRef<HTMLDivElement, CardProps>(function Card(
    { padding = 'md', interactive = false, className, children, ...rest },
    ref,
) {
    const classes = ['card'];
    if (padding === 'sm') classes.push('card-pad-sm');
    if (padding === 'none') classes.push('card-pad-none');
    if (interactive) classes.push('card-interactive');
    if (className) classes.push(className);
    return (
        <div ref={ref} className={classes.join(' ')} {...rest}>
            {children}
        </div>
    );
});

export default Card;
