import { useState, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import Modal from './ui/Modal';
import Button from './ui/Button';
import { Input } from './ui/Input';

interface PromptModalProps {
    open: boolean;
    title: string;
    placeholder?: string;
    onConfirm: (value: string) => void;
    onCancel: () => void;
}

export default function PromptModal({ open, title, placeholder, onConfirm, onCancel }: PromptModalProps) {
    const { t } = useTranslation();
    const [value, setValue] = useState('');
    const inputRef = useRef<HTMLInputElement>(null);

    useEffect(() => {
        if (open) {
            setValue('');
            setTimeout(() => inputRef.current?.focus(), 100);
        }
    }, [open]);

    return (
        <Modal
            open={open}
            onClose={onCancel}
            title={title}
            width={400}
            footer={
                <>
                    <Button variant="secondary" onClick={onCancel}>{t('common.cancel')}</Button>
                    <Button variant="primary" disabled={!value.trim()}
                        onClick={() => { if (value.trim()) onConfirm(value.trim()); }}>{t('common.confirm')}</Button>
                </>
            }
        >
            <Input
                ref={inputRef}
                size="md"
                value={value}
                onChange={e => setValue(e.target.value)}
                placeholder={placeholder || ''}
                onKeyDown={e => {
                    if (e.key === 'Enter' && value.trim()) onConfirm(value.trim());
                }}
                className="u-block"
            />
        </Modal>
    );
}
