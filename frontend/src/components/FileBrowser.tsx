/**
 * Unified FileBrowser component
 * Replaces duplicated file browsing/editing logic across:
 * - Agent Workspace, Skills, Soul, Memory tabs
 * - Enterprise Knowledge Base
 */
import { useState, useEffect, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import MarkdownRenderer from './MarkdownRenderer';
import './FileBrowser.css';

// ─── Types ─────────────────────────────────────────────

export interface FileItem {
    name: string;
    path: string;
    is_dir: boolean;
    size?: number;
}

export interface FileBrowserApi {
    list: (path: string) => Promise<FileItem[]>;
    read: (path: string) => Promise<{ content: string }>;
    write: (path: string, content: string) => Promise<any>;
    delete: (path: string) => Promise<any>;
    upload?: (file: File, path: string, onProgress?: (pct: number) => void) => Promise<any>;
    downloadUrl?: (path: string) => string;
}

export interface FileBrowserProps {
    api: FileBrowserApi;
    rootPath?: string;
    features?: {
        upload?: boolean;
        newFile?: boolean;
        newSkill?: boolean;
        newFolder?: boolean;
        edit?: boolean;
        delete?: boolean;
        directoryNavigation?: boolean;
    };
    fileFilter?: string[];
    singleFile?: string;
    uploadAccept?: string;
    title?: string;
    readOnly?: boolean;
    onRefresh?: () => void;
}

// ─── Text file detection ───────────────────────────────

const TEXT_EXTS = ['.txt', '.md', '.csv', '.json', '.xml', '.yaml', '.yml', '.js', '.ts', '.py', '.html', '.css', '.sh', '.log', '.gitkeep', '.env'];

function isTextFile(name: string): boolean {
    const n = name.toLowerCase();
    if (TEXT_EXTS.some(ext => n.endsWith(ext))) return true;
    const base = n.split('/').pop() || '';
    return !base.includes('.') || base.startsWith('.');
}

const IMAGE_EXTS = ['.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp', '.ico'];

function isImage(name: string): boolean {
    const n = name.toLowerCase();
    return IMAGE_EXTS.some(ext => n.endsWith(ext));
}

function normalizeSkillFolderName(value: string): string {
    const cleaned = value.trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
    const withoutSkillFile = cleaned.replace(/\/SKILL\.md$/i, '');
    const withoutFlatMd = withoutSkillFile.replace(/\.md$/i, '');
    return withoutFlatMd
        .split('/')
        .filter(Boolean)
        .map(part => part.trim().replace(/\s+/g, '-'))
        .filter(Boolean)
        .join('/');
}

export function buildNewSkillFilePath(currentPath: string, value: string): string {
    const folderName = normalizeSkillFolderName(value);
    const base = currentPath.trim().replace(/\/+$/g, '');
    return base ? `${base}/${folderName}/SKILL.md` : `${folderName}/SKILL.md`;
}

// ─── Component ─────────────────────────────────────────

export default function FileBrowser({
    api,
    rootPath = '',
    features = {},
    fileFilter,
    singleFile,
    uploadAccept = '.pdf,.docx,.xlsx,.pptx,.txt,.md,.csv,.json,.xml,.yaml,.yml,.js,.ts,.py,.html,.css,.sh,.log,.png,.jpg,.jpeg,.gif,.svg,.webp',
    title,
    readOnly = false,
    onRefresh,
}: FileBrowserProps) {
    const { t } = useTranslation();
    const {
        upload = false,
        newFile = false,
        newSkill = false,
        newFolder = false,
        edit = !readOnly,
        delete: canDelete = !readOnly,
        directoryNavigation = false,
    } = features;

    // ─── State ─────────────────────────────────────────
    const [currentPath, setCurrentPath] = useState(rootPath);
    const [files, setFiles] = useState<FileItem[]>([]);
    const [loading, setLoading] = useState(false);
    const [contentLoaded, setContentLoaded] = useState(false);
    const [viewing, setViewing] = useState<string | null>(singleFile || null);
    const [content, setContent] = useState('');
    const [editing, setEditing] = useState(false);
    const [editContent, setEditContent] = useState('');
    const [saving, setSaving] = useState(false);
    const [toast, setToast] = useState<{ message: string; type: 'success' | 'error' } | null>(null);
    const [deleteTarget, setDeleteTarget] = useState<{ path: string; name: string } | null>(null);
    const [promptModal, setPromptModal] = useState<{ title: string; placeholder: string; action: string } | null>(null);
    const [promptValue, setPromptValue] = useState('');
    const [uploadProgress, setUploadProgress] = useState<{ fileName: string; percent: number } | null>(null);
    const textareaRef = useRef<HTMLTextAreaElement>(null);

    // Auto-resize textarea to match content height
    useEffect(() => {
        const el = textareaRef.current;
        if (el && editing) {
            el.style.height = 'auto';
            el.style.height = Math.max(200, el.scrollHeight) + 'px';
        }
    }, [editing, editContent]);

    // ─── Helpers ───────────────────────────────────────

    const showToast = useCallback((message: string, type: 'success' | 'error' = 'success') => {
        setToast({ message, type });
        setTimeout(() => setToast(null), 3000);
    }, []);

    // ─── Load files ───────────────────────────────────

    const reload = useCallback(async () => {
        if (singleFile) {
            // Single-file mode: just load the content
            try {
                const data = await api.read(singleFile);
                setContent(data.content || '');
            } catch {
                setContent('');
            }
            setContentLoaded(true);
            return;
        }
        setLoading(true);
        try {
            let data = await api.list(currentPath);
            if (fileFilter && fileFilter.length > 0) {
                data = data.filter(f => f.is_dir || fileFilter.some(ext => f.name.toLowerCase().endsWith(ext)));
            }
            setFiles(data);
        } catch {
            setFiles([]);
        }
        setLoading(false);
    }, [api, currentPath, singleFile, fileFilter]);

    useEffect(() => { reload(); }, [reload]);

    // ─── Load file content when viewing ───────────────

    useEffect(() => {
        if (!viewing || singleFile) return;
        api.read(viewing).then(data => {
            setContent(data.content || '');
        }).catch(() => setContent(''));
    }, [viewing, api, singleFile]);

    // ─── Actions ──────────────────────────────────────

    const handleSave = async () => {
        const target = singleFile || viewing;
        if (!target) return;
        setSaving(true);
        try {
            await api.write(target, editContent);
            setContent(editContent);
            setEditing(false);
            showToast('Saved');
            onRefresh?.();
        } catch (err: any) {
            showToast('Save failed: ' + (err.message || ''), 'error');
        }
        setSaving(false);
    };

    const handleDelete = async () => {
        if (!deleteTarget) return;
        try {
            await api.delete(deleteTarget.path);
            setDeleteTarget(null);
            if (viewing === deleteTarget.path) {
                setViewing(null);
                setEditing(false);
            }
            reload();
            onRefresh?.();
            showToast('Deleted');
        } catch (err: any) {
            showToast('Delete failed: ' + (err.message || ''), 'error');
        }
    };

    const handleUpload = () => {
        const input = document.createElement('input');
        input.type = 'file';
        input.accept = uploadAccept;
        input.multiple = true;
        input.onchange = async () => {
            if (!input.files || input.files.length === 0) return;
            try {
                const fileList = Array.from(input.files);
                for (const file of fileList) {
                    setUploadProgress({ fileName: file.name, percent: 0 });
                    await api.upload!(file, currentPath, (pct) => {
                        setUploadProgress({ fileName: file.name, percent: pct });
                    });
                }
                setUploadProgress(null);
                reload();
                onRefresh?.();
                showToast('Upload successful');
            } catch (err: any) {
                setUploadProgress(null);
                showToast('Upload failed: ' + (err.message || ''), 'error');
            }
        };
        input.click();
    };

    const handlePromptConfirm = async () => {
        const value = promptValue.trim();
        if (!value || !promptModal) return;
        const action = promptModal.action;
        setPromptModal(null);
        setPromptValue('');
        try {
            if (action === 'newFolder') {
                const folderPath = currentPath ? `${currentPath}/${value}` : value;
                await api.write(`${folderPath}/.gitkeep`, '');
            } else if (action === 'newFile') {
                const filePath = currentPath ? `${currentPath}/${value}` : value;
                await api.write(filePath, '');
                setViewing(filePath);
                setEditContent('');
                setEditing(true);
            } else if (action === 'newSkill') {
                const template = `# ${value}\n\n## Description\n_Describe the purpose and triggers_\n\n## Input\n- Param1: Description\n\n## Steps\n1. Step one\n2. Step two\n\n## Output\n_Describe the output format_\n`;
                const filePath = buildNewSkillFilePath(currentPath, value);
                await api.write(filePath, template);
                setViewing(filePath);
                setEditContent(template);
                setEditing(true);
            }
            reload();
            onRefresh?.();
        } catch (err: any) {
            showToast('Failed: ' + (err.message || ''), 'error');
        }
    };

    // ─── Breadcrumbs ──────────────────────────────────

    const pathParts = currentPath ? currentPath.split('/').filter(Boolean) : [];

    const renderBreadcrumbs = () => {
        if (!directoryNavigation || singleFile) return null;
        return (
            <div className="file-browser-breadcrumbs">
                <span
                    className="file-browser-crumb file-browser-crumb-root"
                    onClick={() => { setCurrentPath(rootPath); setViewing(null); setEditing(false); }}
                >
                    📁 {rootPath || 'root'}
                </span>
                {pathParts.slice(rootPath ? rootPath.split('/').filter(Boolean).length : 0).map((part, i) => {
                    const upTo = pathParts.slice(0, (rootPath ? rootPath.split('/').filter(Boolean).length : 0) + i + 1).join('/');
                    return (
                        <span key={upTo}>
                            <span className="u-tertiary"> / </span>
                            <span
                                className="file-browser-crumb"
                                onClick={() => { setCurrentPath(upTo); setViewing(null); setEditing(false); }}
                            >
                                {part}
                            </span>
                        </span>
                    );
                })}
            </div>
        );
    };

    // ─── Toast ─────────────────────────────────────────

    const renderToast = () => {
        if (!toast) return null;
        return (
            <div className={`file-browser-toast ${toast.type === 'success' ? 'is-success' : 'is-error'}`}>
                {toast.message}
            </div>
        );
    };

    // ─── Delete confirmation modal ────────────────────

    const renderDeleteModal = () => {
        if (!deleteTarget) return null;
        return (
            <div className="ui-modal-overlay"
                onClick={(e) => { if (e.target === e.currentTarget) setDeleteTarget(null); }}>
                <div className="file-browser-modal file-browser-modal-sm">
                    <h4 className="file-browser-modal-title">{t('common.delete')}</h4>
                    <p className="file-browser-modal-text">Delete "{deleteTarget.name}"?</p>
                    <div className="file-browser-modal-actions">
                        <button className="btn btn-secondary" onClick={() => setDeleteTarget(null)}>{t('common.cancel')}</button>
                        <button className="btn btn-danger" onClick={handleDelete}>{t('common.delete')}</button>
                    </div>
                </div>
            </div>
        );
    };

    // ─── Prompt modal ─────────────────────────────────

    const renderPromptModal = () => {
        if (!promptModal) return null;
        return (
            <div className="ui-modal-overlay"
                onClick={(e) => { if (e.target === e.currentTarget) { setPromptModal(null); setPromptValue(''); } }}>
                <div className="file-browser-modal">
                    <h4 className="file-browser-prompt-title">{promptModal.title}</h4>
                    <input
                        className="form-input file-browser-mb-4"
                        autoFocus
                        placeholder={promptModal.placeholder}
                        value={promptValue}
                        onChange={e => setPromptValue(e.target.value)}
                        onKeyDown={e => { if (e.key === 'Enter') handlePromptConfirm(); }}
                    />
                    <div className="file-browser-modal-actions">
                        <button className="btn btn-secondary" onClick={() => { setPromptModal(null); setPromptValue(''); }}>{t('common.cancel')}</button>
                        <button className="btn btn-primary" onClick={handlePromptConfirm} disabled={!promptValue.trim()}>OK</button>
                    </div>
                </div>
            </div>
        );
    };

    // ═══════════════════════════════════════════════════
    // SINGLE FILE MODE (Soul-style)
    // ═══════════════════════════════════════════════════
    if (singleFile) {
        return (
            <div className="card">
                <div className="file-browser-toolbar">
                    {title ? <h3>{title}</h3> : <div />}
                    {edit && (
                        !editing ? (
                            <button className="btn btn-secondary" onClick={() => { setEditContent(content); setEditing(true); }}>{t('agent.soul.editButton')}</button>
                        ) : (
                            <div className="file-browser-btn-row">
                                <button className="btn btn-secondary" onClick={() => setEditing(false)}>{t('common.cancel')}</button>
                                <button className="btn btn-primary" onClick={handleSave} disabled={saving}>
                                    {saving ? t('agent.soul.saving') : t('agent.soul.saveButton')}
                                </button>
                            </div>
                        )
                    )}
                </div>
                {editing ? (
                    <textarea ref={textareaRef} className="form-textarea file-browser-editor" value={editContent} onChange={e => setEditContent(e.target.value)} />
                ) : !contentLoaded ? (
                    <div className="file-browser-loading">{t('common.loading')}</div>
                ) : content ? (
                    singleFile?.endsWith('.md') ? (
                        <MarkdownRenderer content={content} className="file-browser-md" />
                    ) : (
                        <pre className="file-browser-pre">
                            {content}
                        </pre>
                    )
                ) : (
                    <div className="file-browser-empty-text">
                        {t('common.noData', 'No content yet. Click Edit to add.')}
                    </div>
                )}
                {renderToast()}
            </div>
        );
    }

    // ═══════════════════════════════════════════════════
    // FILE VIEWER MODE (viewing a specific file)
    // ═══════════════════════════════════════════════════
    if (viewing) {
        const isText = isTextFile(viewing);
        return (
            <div>
                <div className="file-browser-viewer-header">
                    <button className="btn btn-secondary"
                        onClick={() => { setViewing(null); setEditing(false); }}>← {t('common.back')}</button>
                    <span className="file-browser-viewer-path">{viewing}</span>
                    {isText && edit && (
                        !editing ? (
                            <button className="btn btn-secondary"
                                onClick={() => { setEditContent(content); setEditing(true); }}>✏️ {t('agent.soul.editButton')}</button>
                        ) : (
                            <div className="file-browser-btn-row-sm">
                                <button className="btn btn-secondary"
                                    onClick={() => setEditing(false)}>{t('common.cancel')}</button>
                                <button className="btn btn-primary"
                                    disabled={saving} onClick={handleSave}>{saving ? 'Saving...' : t('common.save')}</button>
                            </div>
                        )
                    )}
                    {api.downloadUrl && (
                        <a href={api.downloadUrl(viewing)} download className="file-browser-link-plain">
                            <button className="btn btn-secondary">⬇ {t('common.download', 'Download')}</button>
                        </a>
                    )}
                    {canDelete && (
                        <button className="btn btn-danger"
                            onClick={() => setDeleteTarget({ path: viewing, name: viewing.split('/').pop() || viewing })}>×</button>
                    )}
                </div>
                <div className="card">
                    {isText ? (
                        editing ? (
                            <textarea ref={textareaRef} className="form-textarea file-browser-editor file-browser-editor-sm" value={editContent} onChange={e => setEditContent(e.target.value)} />
                        ) : viewing?.endsWith('.md') ? (
                            <MarkdownRenderer content={content || ''} className="file-browser-md-sm" />
                        ) : (
                            <pre className="file-browser-pre file-browser-pre-sm">
                                {content || t('common.noData', 'No content yet')}
                            </pre>
                        )
                    ) : isImage(viewing) ? (
                        <div className="file-browser-image-wrap">
                            {api.downloadUrl ? (
                                <img
                                    src={api.downloadUrl(viewing)}
                                    alt={viewing.split('/').pop()}
                                    className="file-browser-image"
                                />
                            ) : (
                                <div className="file-browser-image-fallback">Cannot preview image without download URL</div>
                            )}
                        </div>
                    ) : (
                        <div className="file-browser-binary">
                            <div className="file-browser-binary-glyph">⌇</div>
                            <div className="file-browser-binary-name">{viewing.split('/').pop()}</div>
                            <div className="file-browser-binary-hint">Binary file — cannot preview</div>
                            {api.downloadUrl && (
                                <a href={api.downloadUrl(viewing)} download className="file-browser-link-plain">
                                    <button className="btn btn-primary">⬇ {t('common.download', 'Download')}</button>
                                </a>
                            )}
                        </div>
                    )}
                </div>
                {renderDeleteModal()}
                {renderToast()}
            </div>
        );
    }

    // ═══════════════════════════════════════════════════
    // FILE LIST / BROWSER MODE
    // ═══════════════════════════════════════════════════
    return (
        <div>
            {/* Toolbar */}
            <div className="file-browser-list-toolbar">
                {title && <h3 className="file-browser-title">{title}</h3>}
                {renderBreadcrumbs()}
                <div className="file-browser-toolbar-actions">
                    {upload && api.upload && (
                        <button className="btn btn-secondary" onClick={handleUpload}>⬆ Upload</button>
                    )}
                    {newFolder && (
                        <button className="btn btn-secondary"
                            onClick={() => setPromptModal({ title: t('agent.workspace.newFolder'), placeholder: t('agent.workspace.newFolderName'), action: 'newFolder' })}>
                            📁 {t('agent.workspace.newFolder')}
                        </button>
                    )}
                    {newFile && !newSkill && !fileFilter && (
                        <button className="btn btn-primary"
                            onClick={() => setPromptModal({ title: t('agent.workspace.newFile', 'New File'), placeholder: 'filename.md', action: 'newFile' })}>
                            + {t('agent.workspace.newFile', 'New File')}
                        </button>
                    )}
                    {newSkill && (
                        <button className="btn btn-primary"
                            onClick={() => setPromptModal({ title: t('agent.skills.newSkill', 'New Skill'), placeholder: 'skill-name', action: 'newSkill' })}>
                            + {t('agent.skills.newSkill', 'New Skill')}
                        </button>
                    )}
                </div>
            </div>

            {/* File list */}
            {loading ? (
                <div className="file-browser-loading">{t('common.loading')}</div>
            ) : uploadProgress ? (
                <div className="card">
                    <div className="file-browser-upload-row">
                        <span className="u-body">⬆</span>
                        <span className="file-browser-upload-name">{uploadProgress.fileName}</span>
                        <span className="file-browser-upload-pct">{uploadProgress.percent}%</span>
                    </div>
                    <div className="file-browser-progress-track">
                        <div className="file-browser-progress-fill" style={{ width: `${uploadProgress.percent}%` }} />
                    </div>
                </div>
            ) : files.length === 0 ? (
                <div className="file-browser-empty">
                    <div className="file-browser-empty-glyph">📁</div>
                    <div className="file-browser-empty-title">
                        {t('agent.workspace.noFiles', 'No files yet')}
                    </div>
                    <div className="u-meta u-tertiary">
                        {t('agent.workspace.dragOrClick')}
                    </div>
                </div>
            ) : (
                <div className="file-browser-list">
                    {/* Back button for subdirectories */}
                    {directoryNavigation && currentPath !== rootPath && (
                        <div className="file-browser-row file-browser-row-back"
                            onClick={() => {
                                const parts = currentPath.split('/').filter(Boolean);
                                parts.pop();
                                setCurrentPath(parts.join('/') || rootPath);
                                setViewing(null);
                                setEditing(false);
                            }}>
                            <span className="u-body">↩ ..</span>
                        </div>
                    )}
                    {files.map((f) => (
                        <div key={f.name} className="file-browser-row"
                            onClick={() => {
                                if (f.is_dir && directoryNavigation) {
                                    setCurrentPath(f.path || `${currentPath}/${f.name}`);
                                    setViewing(null);
                                    setEditing(false);
                                } else if (!f.is_dir) {
                                    setViewing(f.path || `${currentPath}/${f.name}`);
                                    setEditing(false);
                                }
                            }}>
                            <div className="file-browser-row-main">
                                <span className="u-body u-tertiary">{f.is_dir ? '/' : '·'}</span>
                                <span className="file-browser-row-name">{fileFilter?.includes('.md') ? f.name.replace('.md', '') : f.name}</span>
                            </div>
                            <div className="file-browser-row-main">
                                {f.size != null && <span className="u-meta u-tertiary">{(f.size / 1024).toFixed(1)} KB</span>}
                                {!f.is_dir && api.downloadUrl && (
                                    <a href={api.downloadUrl(f.path || `${currentPath}/${f.name}`)} download
                                        onClick={(e) => e.stopPropagation()}
                                        title={t('common.download', 'Download')}
                                        className="file-browser-row-download">
                                        ⬇
                                    </a>
                                )}
                                {canDelete && (
                                    <button className="file-browser-row-delete"
                                        onClick={(e) => { e.stopPropagation(); setDeleteTarget({ path: f.path || `${currentPath}/${f.name}`, name: f.name }); }}>
                                        ×
                                    </button>
                                )}
                            </div>
                        </div>
                    ))}
                </div>
            )}

            {renderDeleteModal()}
            {renderPromptModal()}
            {renderToast()}
        </div>
    );
}
