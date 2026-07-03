import { useEffect, useState } from 'react';
import {
    Badge,
    Button,
    Card,
    Chip,
    EmptyState,
    IconButton,
    Input,
    Modal,
    SegmentedControl,
    Select,
    Spinner,
    Textarea,
    Tooltip,
} from '@/components/ui';

const TYPE_SCALE = [
    { cls: 'u-tiny', name: 'tiny 10px', sample: 'COUNTER · BADGE' },
    { cls: 'u-meta', name: 'meta 11px', sample: '时间戳 · 来源 · 辅助说明 metadata' },
    { cls: 'u-row', name: 'row 12px', sample: '表行 · 文件行 · 次级标签 row text' },
    { cls: 'u-body', name: 'body 13px', sample: '正文、消息、输入框——全站主字号。The quick brown fox jumps over the lazy dog.' },
    { cls: 'u-emphasis', name: 'emphasis 14px', sample: '表单 label · 组标题 emphasis' },
    { cls: 'u-title', name: 'title 15px', sample: '页面 / 面板标题 Page title' },
    { cls: 'u-display', name: 'display 20px', sample: '空态 / 引导大字 Display' },
];

const COLOR_TOKENS = [
    'bg-primary', 'bg-secondary', 'bg-tertiary', 'bg-elevated', 'bg-hover', 'bg-active',
    'border-subtle', 'border-default', 'border-strong',
    'text-primary', 'text-secondary', 'text-tertiary',
    'accent-primary', 'success', 'warning', 'error', 'info',
];

function Section({ title, children }: { title: string; children: React.ReactNode }) {
    return (
        <section style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
            <div className="u-label">{title}</div>
            {children}
        </section>
    );
}

/** 原子组件 gallery — P0 验收面（双主题截图用），无业务数据。 */
export default function DesignGallery() {
    const [theme, setTheme] = useState<'dark' | 'light'>(
        () => (document.documentElement.getAttribute('data-theme') as 'dark' | 'light') || 'dark',
    );
    const [modalOpen, setModalOpen] = useState(false);
    const [segment, setSegment] = useState<'list' | 'board' | 'timeline'>('list');

    useEffect(() => {
        document.documentElement.setAttribute('data-theme', theme);
    }, [theme]);

    return (
        <div style={{ maxWidth: '860px', margin: '0 auto', padding: 'var(--space-8) var(--space-5)', display: 'flex', flexDirection: 'column', gap: 'var(--space-8)' }}>
            <header style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <div>
                    <div className="u-title">Design Gallery</div>
                    <div className="u-meta u-tertiary">tokens + primitives · docs/frontend-design-refinement-2026-07-03.md</div>
                </div>
                <SegmentedControl
                    aria-label="theme"
                    options={[
                        { value: 'dark', label: 'Dark' },
                        { value: 'light', label: 'Light' },
                    ]}
                    value={theme}
                    onChange={setTheme}
                />
            </header>

            <Section title="Type scale — 7 档">
                <Card padding="md" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
                    {TYPE_SCALE.map((row) => (
                        <div key={row.cls} style={{ display: 'flex', alignItems: 'baseline', gap: 'var(--space-4)' }}>
                            <span className="u-meta u-tertiary u-mono" style={{ width: '110px', flexShrink: 0 }}>{row.name}</span>
                            <span className={row.cls}>{row.sample}</span>
                        </div>
                    ))}
                </Card>
            </Section>

            <Section title="Colors">
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 'var(--space-2)' }}>
                    {COLOR_TOKENS.map((name) => (
                        <div key={name} style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
                            <span style={{ width: '20px', height: '20px', borderRadius: 'var(--radius-sm)', border: '1px solid var(--border-default)', background: `var(--${name})`, flexShrink: 0 }} />
                            <span className="u-meta u-mono u-secondary">--{name}</span>
                        </div>
                    ))}
                </div>
            </Section>

            <Section title="Button — variants × sizes × states">
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-2)', alignItems: 'center' }}>
                    <Button variant="primary">Primary</Button>
                    <Button variant="secondary">Secondary</Button>
                    <Button variant="ghost">Ghost</Button>
                    <Button variant="danger">Danger</Button>
                    <Button variant="primary" size="sm">Small</Button>
                    <Button variant="secondary" size="sm">Small</Button>
                    <Button variant="primary" loading>Loading</Button>
                    <Button variant="secondary" disabled>Disabled</Button>
                    <IconButton label="设置"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg></IconButton>
                </div>
            </Section>

            <Section title="Chip / Badge — 彩色只上小字与小点">
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-2)', alignItems: 'center' }}>
                    <Chip tone="neutral" dot>idle</Chip>
                    <Chip tone="success" dot>running</Chip>
                    <Chip tone="warning" dot>waiting</Chip>
                    <Chip tone="error" dot>failed</Chip>
                    <Chip tone="info">v1.7.0</Chip>
                    <Badge tone="neutral">12</Badge>
                    <Badge tone="success">3 passed</Badge>
                    <Badge tone="warning">2 pending</Badge>
                    <Badge tone="error">1 failed</Badge>
                    <Badge tone="info">new</Badge>
                </div>
            </Section>

            <Section title="Input / Select / Textarea">
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-2)', alignItems: 'center' }}>
                    <Input placeholder="搜索 agents…" />
                    <Input size="md" placeholder="32px 输入" />
                    <Input invalid defaultValue="非法输入" />
                    <Select>
                        <option>全部状态</option>
                        <option>运行中</option>
                    </Select>
                </div>
                <Textarea placeholder="多行输入…" rows={3} style={{ maxWidth: '420px' }} />
            </Section>

            <Section title="Card / EmptyState / Spinner / Tooltip">
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--space-3)' }}>
                    <Card interactive>
                        <div className="u-body" style={{ fontWeight: 500 }}>可点卡片</div>
                        <div className="u-row u-tertiary">hover 只提升背景与边框一档，无阴影无位移。</div>
                    </Card>
                    <Card padding="none">
                        <EmptyState
                            icon={<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" /></svg>}
                            title="暂无会话"
                            description="从左侧选择一个数字员工开始对话。"
                            action={<Button variant="primary" size="sm">新建会话</Button>}
                        />
                    </Card>
                </div>
                <div style={{ display: 'flex', gap: 'var(--space-4)', alignItems: 'center' }}>
                    <Spinner label="加载中" />
                    <Spinner size="sm" label="加载中" />
                    <Tooltip label="这是一个 CSS-only tooltip">
                        <Button variant="ghost">Hover 我</Button>
                    </Tooltip>
                    <Button variant="secondary" onClick={() => setModalOpen(true)}>打开 Modal</Button>
                </div>
            </Section>

            <Section title="SegmentedControl">
                <div>
                    <SegmentedControl
                        aria-label="视图切换"
                        options={[
                            { value: 'list', label: '列表' },
                            { value: 'board', label: '看板' },
                            { value: 'timeline', label: '时间线' },
                        ]}
                        value={segment}
                        onChange={setSegment}
                    />
                </div>
            </Section>

            <Modal
                open={modalOpen}
                onClose={() => setModalOpen(false)}
                title="确认操作"
                footer={
                    <>
                        <Button variant="secondary" onClick={() => setModalOpen(false)}>取消</Button>
                        <Button variant="primary" onClick={() => setModalOpen(false)}>确认</Button>
                    </>
                }
            >
                浮层才配阴影——这个 Modal 使用 --shadow-modal，面内元素零阴影。
            </Modal>
        </div>
    );
}
