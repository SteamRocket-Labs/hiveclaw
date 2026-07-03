/**
 * Lightweight Markdown renderer — no external dependencies.
 * Renders: headings, bold, italic, inline code, code blocks,
 * unordered/ordered lists, blockquotes, horizontal rules, links, tables.
 * All visual styling lives in index.css under `.md-content` — this module
 * emits semantic classes only (design authority: frontend-design-refinement).
 */
import React, { useMemo } from 'react';

function escapeHtml(str: string): string {
    return str
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function renderInline(text: string): string {
    return text
        // Bold + italic
        .replace(/\*\*\*(.*?)\*\*\*/g, '<strong><em>$1</em></strong>')
        // Bold
        .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
        .replace(/__(.*?)__/g, '<strong>$1</strong>')
        // Italic
        .replace(/\*(.*?)\*/g, '<em>$1</em>')
        .replace(/_(.*?)_/g, '<em>$1</em>')
        // Inline code
        .replace(/`([^`]+)`/g, '<code class="md-code">$1</code>')
        // Images
        .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (match, alt, url) => {
            let finalUrl = url;
            if (finalUrl.startsWith('/api/agents/')) {
                const token = localStorage.getItem('token');
                if (token && !finalUrl.includes('token=')) {
                    finalUrl += (finalUrl.includes('?') ? '&' : '?') + `token=${token}`;
                }
            }
            return `<a href="${finalUrl}" target="_blank" class="md-img-link"><img src="${finalUrl}" alt="${alt}" class="md-img" /></a>`;
        })
        // Links
        .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, text, url) => {
            // Avoid matching images that snuck through or weird nested stuff
            if (match.startsWith('!')) return match;
            let finalUrl = url;
            if (finalUrl.startsWith('/api/agents/')) {
                const token = localStorage.getItem('token');
                if (token && !finalUrl.includes('token=')) {
                    finalUrl += (finalUrl.includes('?') ? '&' : '?') + `token=${token}`;
                }
            }
            return `<a href="${finalUrl}" target="_blank" rel="noopener noreferrer" class="md-link">${text}</a>`;
        })
        // Strikethrough
        .replace(/~~(.*?)~~/g, '<del>$1</del>');
}

function markdownToHtml(md: string): string {
    const lines = md.split('\n');
    let html = '';
    let inCodeBlock = false;
    let codeLang = '';
    let codeLines: string[] = [];
    let inList: 'ul' | 'ol' | null = null;
    let inBlockquote = false;
    let inTable = false;
    let tableHeader = false;

    const flushList = () => {
        if (inList) { html += inList === 'ul' ? '</ul>' : '</ol>'; inList = null; }
    };
    const flushBlockquote = () => {
        if (inBlockquote) { html += '</blockquote>'; inBlockquote = false; }
    };
    const flushTable = () => {
        if (inTable) { html += '</tbody></table>'; inTable = false; tableHeader = false; }
    };

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];

        // Code block
        if (line.startsWith('```')) {
            if (!inCodeBlock) {
                flushList(); flushBlockquote(); flushTable();
                inCodeBlock = true;
                codeLang = line.slice(3).trim();
                codeLines = [];
            } else {
                const codeContent = escapeHtml(codeLines.join('\n'));
                html += `<pre class="md-pre"><code${codeLang ? ` class="language-${codeLang}"` : ''}>${codeContent}</code></pre>`;
                inCodeBlock = false;
                codeLang = '';
                codeLines = [];
            }
            continue;
        }
        if (inCodeBlock) { codeLines.push(line); continue; }

        // Blank line — block separator only; paragraph margins carry the gap.
        if (line.trim() === '') {
            flushList(); flushBlockquote(); flushTable();
            continue;
        }

        // Headings
        const hMatch = line.match(/^(#{1,6})\s+(.*)/);
        if (hMatch) {
            flushList(); flushBlockquote(); flushTable();
            const level = hMatch[1].length;
            html += `<h${level} class="md-h md-h${level}">${renderInline(hMatch[2])}</h${level}>`;
            continue;
        }

        // Horizontal rule
        if (/^[-*_]{3,}$/.test(line.trim())) {
            flushList(); flushBlockquote(); flushTable();
            html += '<hr class="md-hr">';
            continue;
        }

        // Blockquote
        if (line.startsWith('> ')) {
            flushList(); flushTable();
            if (!inBlockquote) {
                html += '<blockquote class="md-quote">';
                inBlockquote = true;
            }
            html += `<div>${renderInline(line.slice(2))}</div>`;
            continue;
        } else if (inBlockquote) {
            flushBlockquote();
        }

        // Tables
        if (line.includes('|')) {
            flushList(); flushBlockquote();
            const cols = line.split('|').map(c => c.trim()).filter((_, i, a) => i > 0 && i < a.length - 1);
            // Separator row
            if (cols.every(c => /^[-:]+$/.test(c))) {
                tableHeader = true;
                continue;
            }
            if (!inTable) {
                html += '<table class="md-table"><thead>';
                inTable = true;
                tableHeader = false;
                // This is the header row
                html += '<tr>' + cols.map(c => `<th>${renderInline(c)}</th>`).join('') + '</tr>';
                html += '</thead><tbody>';
            } else {
                html += '<tr>' + cols.map(c => `<td>${renderInline(c)}</td>`).join('') + '</tr>';
            }
            continue;
        } else if (inTable) {
            flushTable();
        }

        // Unordered list
        const ulMatch = line.match(/^(\s*)[*\-+]\s+(.*)/);
        if (ulMatch) {
            flushBlockquote(); flushTable();
            if (inList !== 'ul') { if (inList) flushList(); html += '<ul class="md-list">'; inList = 'ul'; }
            html += `<li>${renderInline(ulMatch[2])}</li>`;
            continue;
        }

        // Ordered list
        const olMatch = line.match(/^(\s*)\d+\.\s+(.*)/);
        if (olMatch) {
            flushBlockquote(); flushTable();
            if (inList !== 'ol') { if (inList) flushList(); html += '<ol class="md-list">'; inList = 'ol'; }
            html += `<li>${renderInline(olMatch[2])}</li>`;
            continue;
        }

        // Regular paragraph
        flushList(); flushBlockquote(); flushTable();
        html += `<p>${renderInline(line)}</p>`;
    }

    // Close any open structures
    flushList(); flushBlockquote(); flushTable();
    if (inCodeBlock) {
        html += `<pre class="md-pre"><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`;
    }

    return html;
}

interface MarkdownRendererProps {
    content: string;
    style?: React.CSSProperties;
    className?: string;
}

export const MarkdownRenderer = React.memo(function MarkdownRenderer({ content, style, className }: MarkdownRendererProps) {
    const html = useMemo(() => markdownToHtml(content), [content]);
    return (
        <div
            className={className ? `md-content ${className}` : 'md-content'}
            style={style}
            dangerouslySetInnerHTML={{ __html: html }}
        />
    );
});

export default MarkdownRenderer;
