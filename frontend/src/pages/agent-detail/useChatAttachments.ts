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
    const [attachedFiles, setAttachedFiles] = useState<{
        originalAttachment?: UploadedChatFile['original_attachment'];
        name: string;
        text: string;
        path?: string;
        imageUrl?: string;
        conversion?: {
            status?: string;
            markdownPath?: string;
            metadataPath?: string;
            engine?: string;
        };
    }[]>([]);
    const fileInputRef = useRef<HTMLInputElement>(null);

    const handleChatFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const files = Array.from(e.target.files || []);
        if (!files.length) return;
        const allowedFiles = files.slice(0, 10 - attachedFiles.length);
        if (!allowedFiles.length) {
            onToast(t('agent.upload.limit', 'Limit of 10 attached files reached.'), 'error');
            return;
        }

        setUploading(true); setUploadProgress(0);
        try {
            const uploadPromises = allowedFiles.map(file => {
                const { promise } = uploadFileWithProgress(
                    `/chat/upload`,
                    file,
                    () => {}, // Avoid updating progress per file to prevent flickering, could implement total progress
                    id ? { agent_id: id } : undefined,
                );
                return promise;
            });
            const results = await Promise.all(uploadPromises);
            const newAttached = results.map(data => ({
                originalAttachment: data.original_attachment,
                name: data.filename,
                text: data.extracted_text,
                path: data.workspace_path,
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
            if (err?.message !== 'Upload cancelled') onToast(t('agent.upload.failed'), 'error');
        } finally {
            setUploading(false); setUploadProgress(-1); uploadAbortRef.current = null;
            if (fileInputRef.current) fileInputRef.current.value = '';
        }
    };

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
        const allowedFiles = filesToUpload.slice(0, 10 - attachedFiles.length);
        if (!allowedFiles.length) {
            onToast(t('agent.upload.limit', 'Limit of 10 attached files reached.'), 'error');
            return;
        }

        setUploading(true); setUploadProgress(0);
        try {
            const uploadPromises = allowedFiles.map(file => {
                const { promise } = uploadFileWithProgress(
                    `/chat/upload`,
                    file,
                    () => {},
                    id ? { agent_id: id } : undefined,
                );
                return promise;
            });
            const results = await Promise.all(uploadPromises);
            const newAttached = results.map(data => ({
                originalAttachment: data.original_attachment,
                name: data.filename,
                text: data.extracted_text,
                path: data.workspace_path,
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
            if (err?.message !== 'Upload cancelled') onToast(t('agent.upload.failed'), 'error');
        } finally { setUploading(false); setUploadProgress(-1); uploadAbortRef.current = null; }
    };

    return { uploading, uploadProgress, uploadAbortRef, attachedFiles, setAttachedFiles,
        fileInputRef, handleChatFile, handlePaste };
}
