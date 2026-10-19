import type { AgentChatMessage } from './chatRuntime';

const IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'];

export function parseUserAttachment(msg: AgentChatMessage, agentId?: string): AgentChatMessage {
    const parts = msg.sessionItem?.payload.content_parts;
    if (Array.isArray(parts)) {
        const records = parts.filter((part): part is Record<string, unknown> => Boolean(part) && typeof part === 'object' && !Array.isArray(part));
        const inputAttachmentFailures = records.flatMap((part) => {
            const attachment = part.type === 'attachment_ref' && part.attachment && typeof part.attachment === 'object'
                ? part.attachment as Record<string, unknown> : undefined;
            return attachment?.state === 'unavailable' && typeof attachment.name === 'string' ? [{ name: attachment.name }] : [];
        });
        const inputAttachments = records.flatMap((part) => {
            const attachment = part.type === 'attachment_ref' && part.attachment && typeof part.attachment === 'object'
                ? part.attachment as Record<string, unknown> : undefined;
            const path = attachment?.path;
            if (typeof path !== 'string' || !path.startsWith('workspace/') || path.split('/').includes('..')) return [];
            return [{ path, name: typeof attachment?.name === 'string' ? attachment.name : path.split('/').pop() || 'file',
                ...(typeof attachment?.size === 'number' ? { size: attachment.size } : {}),
                ...(typeof attachment?.content_type === 'string' ? { mimeType: attachment.content_type } : {}), source: 'human_input' }];
        });
        if (inputAttachments.length || inputAttachmentFailures.length) {
            const plain = records.length === parts.length && records.every(part => ['text', 'attachment_ref'].includes(String(part.type)))
                && inputAttachments.length + inputAttachmentFailures.length === records.filter(part => part.type === 'attachment_ref').length;
            return { ...msg, fileName: undefined, imageUrl: undefined, inputAttachments,
                inputAttachmentFailures: inputAttachmentFailures.length ? inputAttachmentFailures : undefined,
                content: plain ? records.filter(part => part.type === 'text')
                .map(part => typeof part.text === 'string' ? part.text : typeof part.content === 'string' ? part.content : '').join('\n') : msg.content };
        }
    }
    let parsed = { ...msg };
    let attachmentPath: string | undefined;
    // Qualified Feishu headers carry one file; legacy batches keep their first-marker behavior.
    const qualified = msg.content.startsWith('[file:workspace/uploads/feishu/')
        ? msg.content.match(/^\[file:([^\r\n]+)\]\n?/) : null;
    const newFmt = qualified || msg.content.match(/^\[file:([^\]]+)\]\n?/);
    if (newFmt) {
        attachmentPath = newFmt[1].startsWith('workspace/') ? newFmt[1] : `workspace/uploads/${newFmt[1]}`;
        parsed = { ...msg, fileName: newFmt[1].split('/').pop() || newFmt[1], content: msg.content.slice(newFmt[0].length).trim() };
    }
    // Feishu/Slack channel format: [文件已上传: workspace/uploads/name]
    const chanFmt = !newFmt && msg.content.match(/^\[\u6587\u4ef6\u5df2\u4e0a\u4f20: (?:workspace\/uploads\/)?([^\]\n]+)\]/);
    if (chanFmt) {
        const raw = chanFmt[1]; const fileName = raw.split('/').pop() || raw;
        attachmentPath = raw.startsWith('workspace/') ? raw : `workspace/uploads/${raw}`;
        parsed = { ...msg, fileName, content: msg.content.slice(chanFmt[0].length).trim() };
    }
    // Old format: [File: name.pdf]\nFile location:...\nQuestion: user_msg
    const oldFmt = !newFmt && !chanFmt && msg.content.match(/^\[File: ([^\]]+)\]/);
    if (oldFmt) {
        const fileName = oldFmt[1];
        const qMatch = msg.content.match(/\nQuestion: ([\s\S]+)$/);
        parsed = { ...msg, fileName, content: qMatch ? qMatch[1].trim() : '' };
    }
    // If file is an image and no imageUrl yet, build download URL for preview
    if (parsed.fileName && !parsed.imageUrl && agentId) {
        const ext = parsed.fileName.split('.').pop()?.toLowerCase() || '';
        if (IMAGE_EXTS.includes(ext)) {
            parsed.imageUrl = `/api/agents/${agentId}/files/download?path=${encodeURIComponent(attachmentPath || `workspace/uploads/${parsed.fileName}`)}`;
        }
    }
    return parsed;
}
