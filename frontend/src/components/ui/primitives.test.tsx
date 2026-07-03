import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

// 原子组件契约（docs/frontend-design-refinement-2026-07-03.md §3.3）：
// - class-based：全部样式经 styles/primitives.css 的 class 输出，组件不写 inline style
// - variant/size/state 映射为确定的 class 名（三态由 CSS 承担）
// - 无障碍：图标按钮有 aria-label，spinner 有 role，modal 有 dialog 语义

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
} from './index';

describe('Button', () => {
    it('renders variant/size classes and defaults to type=button', () => {
        const html = renderToStaticMarkup(
            <Button variant="primary" size="sm">
                Go
            </Button>,
        );
        expect(html).toContain('btn btn-primary btn-sm');
        expect(html).toContain('type="button"');
    });

    it('loading state disables the button and shows a spinner', () => {
        const html = renderToStaticMarkup(<Button loading>Save</Button>);
        expect(html).toContain('loading');
        expect(html).toContain('disabled');
        expect(html).toContain('ui-spinner');
    });

    it('does not emit any inline style', () => {
        const html = renderToStaticMarkup(<Button variant="danger">Delete</Button>);
        expect(html).not.toContain('style=');
    });
});

describe('IconButton', () => {
    it('requires a label and exposes it as aria-label + tooltip', () => {
        const html = renderToStaticMarkup(<IconButton label="关闭">×</IconButton>);
        expect(html).toContain('aria-label="关闭"');
        expect(html).toContain('data-tooltip="关闭"');
        expect(html).toContain('ui-icon-btn');
        expect(html).toContain('ui-tooltip-host');
    });

    it('can opt out of the tooltip', () => {
        const html = renderToStaticMarkup(
            <IconButton label="关闭" tooltip={false}>
                ×
            </IconButton>,
        );
        expect(html).not.toContain('data-tooltip');
        expect(html).toContain('aria-label="关闭"');
    });
});

describe('Card', () => {
    it('maps padding and interactive to classes', () => {
        const html = renderToStaticMarkup(
            <Card padding="sm" interactive>
                body
            </Card>,
        );
        expect(html).toContain('card card-pad-sm card-interactive');
    });
});

describe('Chip', () => {
    it('renders tone class and optional dot', () => {
        const html = renderToStaticMarkup(
            <Chip tone="success" dot>
                running
            </Chip>,
        );
        expect(html).toContain('ui-chip ui-chip-success');
        expect(html).toContain('ui-chip-dot');
    });
});

describe('Badge', () => {
    it('renders tone class', () => {
        const html = renderToStaticMarkup(<Badge tone="error">3</Badge>);
        expect(html).toContain('badge badge-error');
    });
});

describe('Input / Textarea / Select', () => {
    it('input maps size and invalid to classes', () => {
        const html = renderToStaticMarkup(<Input size="md" invalid placeholder="name" />);
        expect(html).toContain('ui-input ui-input-md ui-input-invalid');
    });

    it('textarea renders ui-textarea', () => {
        const html = renderToStaticMarkup(<Textarea invalid />);
        expect(html).toContain('ui-textarea ui-input-invalid');
    });

    it('select renders ui-select with options', () => {
        const html = renderToStaticMarkup(
            <Select size="md">
                <option value="a">A</option>
            </Select>,
        );
        expect(html).toContain('ui-select ui-select-md');
        expect(html).toContain('<option value="a">A</option>');
    });
});

describe('Modal', () => {
    it('renders nothing when closed', () => {
        const html = renderToStaticMarkup(
            <Modal open={false} onClose={() => {}} title="t">
                body
            </Modal>,
        );
        expect(html).toBe('');
    });

    it('renders dialog semantics, header, body, footer when open', () => {
        const html = renderToStaticMarkup(
            <Modal open onClose={() => {}} title="标题" footer={<button>ok</button>} width={480}>
                内容
            </Modal>,
        );
        expect(html).toContain('ui-modal-overlay');
        expect(html).toContain('role="dialog"');
        expect(html).toContain('aria-modal="true"');
        expect(html).toContain('ui-modal-header');
        expect(html).toContain('ui-modal-body');
        expect(html).toContain('ui-modal-footer');
        expect(html).toContain('width:480px');
    });
});

describe('EmptyState', () => {
    it('renders title, optional description and action', () => {
        const html = renderToStaticMarkup(
            <EmptyState icon={<svg />} title="暂无数据" description="说明" action={<button>新建</button>} />,
        );
        expect(html).toContain('ui-empty');
        expect(html).toContain('ui-empty-title');
        expect(html).toContain('ui-empty-desc');
        expect(html).toContain('ui-empty-action');
    });
});

describe('Spinner', () => {
    it('renders status role and size class', () => {
        const html = renderToStaticMarkup(<Spinner size="sm" label="加载中" />);
        expect(html).toContain('ui-spinner ui-spinner-sm');
        expect(html).toContain('role="status"');
        expect(html).toContain('aria-label="加载中"');
    });
});

describe('Tooltip', () => {
    it('wraps children with a CSS tooltip host', () => {
        const html = renderToStaticMarkup(
            <Tooltip label="提示">
                <span>目标</span>
            </Tooltip>,
        );
        expect(html).toContain('ui-tooltip-host');
        expect(html).toContain('data-tooltip="提示"');
    });
});

describe('SegmentedControl', () => {
    it('renders tabs with a single active item', () => {
        const html = renderToStaticMarkup(
            <SegmentedControl
                aria-label="视图"
                options={[
                    { value: 'a', label: 'A' },
                    { value: 'b', label: 'B' },
                ]}
                value="b"
                onChange={() => {}}
            />,
        );
        expect(html).toContain('ui-segmented');
        expect(html).toContain('role="tablist"');
        expect(html).toContain('aria-selected="true"');
        const activeCount = html.split('ui-segmented-item active').length - 1;
        expect(activeCount).toBe(1);
    });
});
