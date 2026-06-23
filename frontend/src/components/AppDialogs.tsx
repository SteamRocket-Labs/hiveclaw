import { useEffect, useState } from 'react';

import ConfirmModal from './ConfirmModal';

type ToastType = 'success' | 'error' | 'info';

interface ToastRequest {
  id: number;
  message: string;
  type: ToastType;
}

interface ConfirmRequest {
  id: number;
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  resolve: (value: boolean) => void;
}

const TOAST_EVENT = 'hive-app-toast';
const CONFIRM_EVENT = 'hive-app-confirm';

let nextDialogId = 1;

export function showAppToast(message: string, type: ToastType = 'info') {
  if (typeof window === 'undefined' || typeof window.dispatchEvent !== 'function') return;
  window.dispatchEvent(new CustomEvent<ToastRequest>(TOAST_EVENT, {
    detail: {
      id: nextDialogId++,
      message,
      type,
    },
  }));
}

export function requestAppConfirm(options: Omit<ConfirmRequest, 'id' | 'resolve'>): Promise<boolean> {
  if (typeof window === 'undefined' || typeof window.dispatchEvent !== 'function') {
    return Promise.resolve(false);
  }
  return new Promise((resolve) => {
    window.dispatchEvent(new CustomEvent<ConfirmRequest>(CONFIRM_EVENT, {
      detail: {
        id: nextDialogId++,
        ...options,
        resolve,
      },
    }));
  });
}

export default function AppDialogs() {
  const [toast, setToast] = useState<ToastRequest | null>(null);
  const [confirmRequest, setConfirmRequest] = useState<ConfirmRequest | null>(null);

  useEffect(() => {
    const handleToast = (event: Event) => {
      const detail = (event as CustomEvent<ToastRequest>).detail;
      if (!detail?.message) return;
      setToast(detail);
      window.setTimeout(() => {
        setToast((current) => (current?.id === detail.id ? null : current));
      }, detail.type === 'error' ? 4200 : 2600);
    };
    const handleConfirm = (event: Event) => {
      const detail = (event as CustomEvent<ConfirmRequest>).detail;
      if (!detail?.message) return;
      setConfirmRequest(detail);
    };
    window.addEventListener(TOAST_EVENT, handleToast);
    window.addEventListener(CONFIRM_EVENT, handleConfirm);
    return () => {
      window.removeEventListener(TOAST_EVENT, handleToast);
      window.removeEventListener(CONFIRM_EVENT, handleConfirm);
    };
  }, []);

  const closeConfirm = (value: boolean) => {
    const current = confirmRequest;
    setConfirmRequest(null);
    current?.resolve(value);
  };

  return (
    <>
      {toast && (
        <div className={`app-toast ${toast.type}`} role="status">
          {toast.message}
        </div>
      )}
      <ConfirmModal
        open={Boolean(confirmRequest)}
        title={confirmRequest?.title || ''}
        message={confirmRequest?.message || ''}
        confirmLabel={confirmRequest?.confirmLabel}
        cancelLabel={confirmRequest?.cancelLabel}
        danger={confirmRequest?.danger}
        onConfirm={() => closeConfirm(true)}
        onCancel={() => closeConfirm(false)}
      />
    </>
  );
}
