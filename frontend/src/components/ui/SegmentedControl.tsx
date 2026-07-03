import { ReactNode } from 'react';

export interface SegmentedOption<T extends string> {
    value: T;
    label: ReactNode;
}

export interface SegmentedControlProps<T extends string> {
    options: SegmentedOption<T>[];
    value: T;
    onChange: (value: T) => void;
    'aria-label'?: string;
}

/** 安静的分段切换 — 低对比容器，active 仅提升一档背景。 */
export default function SegmentedControl<T extends string>({
    options,
    value,
    onChange,
    'aria-label': ariaLabel,
}: SegmentedControlProps<T>) {
    return (
        <div className="ui-segmented" role="tablist" aria-label={ariaLabel}>
            {options.map((opt) => (
                <button
                    key={opt.value}
                    type="button"
                    role="tab"
                    aria-selected={opt.value === value}
                    className={opt.value === value ? 'ui-segmented-item active' : 'ui-segmented-item'}
                    onClick={() => onChange(opt.value)}
                >
                    {opt.label}
                </button>
            ))}
        </div>
    );
}
