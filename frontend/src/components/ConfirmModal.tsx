import { useRef, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import Modal from './ui/Modal';
import Button from './ui/Button';

interface ConfirmModalProps {
    open: boolean;
    title: string;
    message: string;
    confirmLabel?: string;
    cancelLabel?: string;
    danger?: boolean;
    onConfirm: () => void;
    onCancel: () => void;
}

export default function ConfirmModal({ open, title, message, confirmLabel, cancelLabel, danger, onConfirm, onCancel }: ConfirmModalProps) {
    const { t } = useTranslation();
    const resolvedConfirmLabel = confirmLabel ?? t('common.confirm');
    const resolvedCancelLabel = cancelLabel ?? t('common.cancel');
    const confirmRef = useRef<HTMLButtonElement>(null);
    const cancelRef = useRef<HTMLButtonElement>(null);

    useEffect(() => {
        if (!open) return;
        // A danger dialog parks the delayed initial focus on Cancel so a
        // reflexive Enter cannot confirm the destructive action; other dialogs
        // keep the confirm focus. Cleanup prevents focusing stale content
        // after close or unmount.
        const timer = setTimeout(() => (danger ? cancelRef : confirmRef).current?.focus(), 100);
        return () => clearTimeout(timer);
    }, [open, danger]);

    return (
        <Modal
            open={open}
            onClose={onCancel}
            title={title}
            width={380}
            footer={
                <>
                    <Button ref={cancelRef} variant="secondary" onClick={onCancel}>{resolvedCancelLabel}</Button>
                    <Button ref={confirmRef} variant={danger ? 'danger' : 'primary'} onClick={onConfirm}>{resolvedConfirmLabel}</Button>
                </>
            }
        >
            {message}
        </Modal>
    );
}
