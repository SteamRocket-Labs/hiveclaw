import type React from 'react';
import { useRef, useState } from 'react';
import type { TFunction } from 'i18next';
import type { UploadedChatFile } from '../../api/domains/chat';
import { uploadFileWithProgress } from '../../api/core/upload-progress';

export function useChatAttachments(
    id: string | undefined,
    t: TFunction,
    onToast: (message: string, type: 'error') => void,
) {
    const [uploading, setUploading] = useState(false);
    const [uploadProgress, setUploadProgress] = useState(-1);
    const uploadAbortRef = useRef<(() => void) | null>(null);
    const pendingUploadsRef = useRef(new Set<{ count: number; cancel: () => void }>());
    const [attachedFiles, setAttachedFiles] = useState<{
        originalAttachment?: UploadedChatFile['original_attachment'];
        name: string;
        text: string;
        path?: string;
        size?: number;
        mimeType?: string;
        imageUrl?: string;
        conversion?: {
            status?: string;
            markdownPath?: string;
            metadataPath?: string;
            engine?: string;
        };
    }[]>([]);
    const fileInputRef = useRef<HTMLInputElement>(null);

    const uploadFiles = async (files: File[]) => {
        if (!files.length) return;
        const pendingCount = [...pendingUploadsRef.current].reduce((count, batch) => count + batch.count, 0);
        const allowedFiles = files.slice(0, Math.max(0, 10 - attachedFiles.length - pendingCount));
        if (!allowedFiles.length) {
            onToast(t('agent.upload.limit', 'Limit of 10 attached files reached.'), 'error');
            return;
        }

        let cancelled = false;
        const uploads = allowedFiles.map(file => uploadFileWithProgress(
            '/chat/upload', file, undefined, id ? { agent_id: id } : undefined,
        ));
        const batch = { count: allowedFiles.length, cancel: () => {
            cancelled = true;
            uploads.forEach(upload => upload.abort());
        } };
        pendingUploadsRef.current.add(batch);
        setUploading(true); setUploadProgress(101);
        uploadAbortRef.current = () => {
            pendingUploadsRef.current.forEach(pending => pending.cancel());
            pendingUploadsRef.current.clear();
            setUploading(false); setUploadProgress(-1); uploadAbortRef.current = null;
            if (fileInputRef.current) fileInputRef.current.value = '';
        };
        try {
            const results = await Promise.all(uploads.map(upload => upload.promise));
            if (cancelled) return;
            const newAttached = results.map((data, index) => ({
                originalAttachment: data.original_attachment,
                name: data.filename,
                text: data.extracted_text,
                path: data.workspace_path,
                size: data.size,
                mimeType: allowedFiles[index].type || undefined,
                imageUrl: data.image_data_url || undefined,
                conversion: data.conversion ? {
                    status: data.conversion.status,
                    markdownPath: data.conversion.markdown_path,
                    metadataPath: data.conversion.metadata_path,
                    engine: data.conversion.engine,
                } : undefined,
            }));
            setAttachedFiles(prev => [...prev, ...newAttached].slice(0, 10));
        } catch (err: any) {
            if (!cancelled) {
                batch.cancel();
                if (err?.message !== 'Upload cancelled') onToast(t('agent.upload.failed'), 'error');
            }
        } finally {
            pendingUploadsRef.current.delete(batch);
            if (!pendingUploadsRef.current.size) {
                setUploading(false); setUploadProgress(-1); uploadAbortRef.current = null;
                if (fileInputRef.current) fileInputRef.current.value = '';
            }
        }
    };

    const handleChatFile = (e: React.ChangeEvent<HTMLInputElement>) => uploadFiles(Array.from(e.target.files || []));

    // Clipboard paste handler — auto-upload pasted images
    const handlePaste = async (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
        const items = e.clipboardData?.items;
        if (!items) return;

        const filesToUpload: File[] = [];
        for (let i = 0; i < items.length; i++) {
            if (items[i].type.startsWith('image/')) {
                const blob = items[i].getAsFile();
                if (blob) {
                    const ext = blob.type.split('/')[1] || 'png';
                    const fileName = `paste-${Date.now()}-${i}.${ext}`;
                    filesToUpload.push(new File([blob], fileName, { type: blob.type }));
                }
            }
        }

        if (!filesToUpload.length) return;
        e.preventDefault();
        await uploadFiles(filesToUpload);
    };

    return { uploading, uploadProgress, uploadAbortRef, attachedFiles, setAttachedFiles,
        fileInputRef, handleChatFile, handlePaste };
}
