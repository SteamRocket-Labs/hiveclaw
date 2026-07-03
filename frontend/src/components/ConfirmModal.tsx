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
    const btnRef = useRef<HTMLButtonElement>(null);

    useEffect(() => {
        if (open) setTimeout(() => btnRef.current?.focus(), 100);
    }, [open]);

    return (
        <Modal
            open={open}
            onClose={onCancel}
            title={title}
            width={380}
            footer={
                <>
                    <Button variant="secondary" onClick={onCancel}>{resolvedCancelLabel}</Button>
                    <Button ref={btnRef} variant={danger ? 'danger' : 'primary'} onClick={onConfirm}>{resolvedConfirmLabel}</Button>
                </>
            }
        >
            {message}
        </Modal>
    );
}
