import { ReactNode, useEffect, useRef } from 'react';

export interface ModalProps {
    open: boolean;
    onClose: () => void;
    title?: ReactNode;
    footer?: ReactNode;
    /** 面板宽度（px），默认 400。 */
    width?: number;
    children?: ReactNode;
}

/** Modal 基座 — overlay 点击与 ESC 关闭；浮层才配阴影。 */
export default function Modal({ open, onClose, title, footer, width, children }: ModalProps) {
    const panelRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (!open) return;
        const onKeyDown = (e: KeyboardEvent) => {
            if (e.key === 'Escape') onClose();
        };
        document.addEventListener('keydown', onKeyDown);
        return () => document.removeEventListener('keydown', onKeyDown);
    }, [open, onClose]);

    if (!open) return null;

    return (
        <div
            className="ui-modal-overlay"
            onClick={(e) => {
                if (e.target === e.currentTarget) onClose();
            }}
        >
            <div
                ref={panelRef}
                className="ui-modal"
                role="dialog"
                aria-modal="true"
                style={width ? { width: `${width}px` } : undefined}
            >
                {title != null && <div className="ui-modal-header">{title}</div>}
                <div className="ui-modal-body">{children}</div>
                {footer != null && <div className="ui-modal-footer">{footer}</div>}
            </div>
        </div>
    );
}
