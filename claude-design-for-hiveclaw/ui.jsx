/* ============================================================
   Hive Prototype — shared UI primitives, icons, data model
   Exposes window.HiveUI
   ============================================================ */
const { useState, useEffect, useRef, createContext, useContext, Fragment } = React;

/* ---------------- Icons ---------------- */
const ICON_PATHS = {
  home: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/>',
  bot: '<rect x="4" y="8" width="16" height="11" rx="2.5"/><path d="M12 8V4M9 13h.01M15 13h.01"/><path d="M2 13h2M20 13h2"/>',
  chat: '<path d="M21 12a8 8 0 0 1-11.5 7.2L3 21l1.8-6.5A8 8 0 1 1 21 12Z"/>',
  flow: '<rect x="3" y="3" width="6" height="6" rx="1.5"/><rect x="15" y="15" width="6" height="6" rx="1.5"/><path d="M9 6h4a2 2 0 0 1 2 2v7"/>',
  brain: '<path d="M9 4a3 3 0 0 0-3 3 3 3 0 0 0-1 5 3 3 0 0 0 2 4 3 3 0 0 0 5 1 3 3 0 0 0 5-1 3 3 0 0 0 2-4 3 3 0 0 0-1-5 3 3 0 0 0-3-3 3 3 0 0 0-3.5-1A3 3 0 0 0 9 4Z"/><path d="M12 4v15"/>',
  doc: '<path d="M6 3h8l4 4v14H6Z"/><path d="M14 3v4h4M9 12h6M9 16h6"/>',
  check: '<path d="M4 12a8 8 0 1 0 16 0 8 8 0 0 0-16 0Z"/><path d="M9 12l2 2 4-4"/>',
  checkPlain: '<path d="M5 12l4.5 4.5L19 7"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
  users: '<circle cx="9" cy="8" r="3"/><path d="M3 20a6 6 0 0 1 12 0M16 5.5a3 3 0 0 1 0 5.8M21 20a6 6 0 0 0-4-5.6"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  box: '<path d="M21 8 12 3 3 8l9 5 9-5Z"/><path d="M3 8v8l9 5 9-5V8"/>',
  shield: '<path d="M12 3 5 6v5c0 4 3 7 7 9 4-2 7-5 7-9V6Z"/><path d="M9 12l2 2 4-4"/>',
  coins: '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/>',
  plug: '<path d="M9 7V3M15 7V3M7 7h10v4a5 5 0 0 1-10 0Z"/><path d="M12 16v5"/>',
  audit: '<path d="M6 3h9l3 3v15H6Z"/><path d="M9 8h6M9 12h6M9 16h4"/>',
  bolt: '<path d="M13 2 4 14h6l-1 8 9-12h-6Z"/>',
  send: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  clip: '<path d="M21 11.5 12.5 20a5 5 0 0 1-7-7l8-8a3.5 3.5 0 0 1 5 5l-8 8a2 2 0 0 1-3-3l7.5-7.5"/>',
  sparkle: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8Z"/><path d="M19 15l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7Z"/>',
  bell: '<path d="M6 9a6 6 0 1 1 12 0c0 5 2 6 2 6H4s2-1 2-6Z"/><path d="M10 19a2 2 0 0 0 4 0"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  arrowRight: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  arrowUpRight: '<path d="M7 17 17 7M9 7h8v8"/>',
  dots: '<circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/>',
  file: '<path d="M6 3h8l4 4v14H6Z"/><path d="M14 3v4h4"/>',
  download: '<path d="M12 3v12M7 11l5 5 5-5M5 21h14"/>',
  play: '<path d="M7 5l12 7-12 7Z"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  alert: '<path d="M12 3 2 20h20Z"/><path d="M12 10v4M12 17h.01"/>',
  link: '<path d="M9 15l6-6M10 6l1-1a4 4 0 0 1 6 6l-1 1M14 18l-1 1a4 4 0 0 1-6-6l1-1"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  filter: '<path d="M3 5h18l-7 8v6l-4 2v-8Z"/>',
  star: '<path d="M12 3l2.6 6.3 6.4.5-4.9 4.1 1.5 6.1L12 17l-5.1 3.1 1.5-6.1L3.5 9.8l6.4-.5Z"/>',
  template: '<rect x="3" y="3" width="18" height="18" rx="2.5"/><path d="M3 9h18M9 9v12"/>',
  wand: '<path d="M15 4V2M15 10V8M11 6H9M21 6h-2M19 19 9 9M6 13 4 21l8-2"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-3-6.7L21 8M21 4v4h-4"/>',
  pin: '<path d="M9 4h6l-1 6 3 3v2H7v-2l3-3Z"/><path d="M12 15v5"/>',
  branch: '<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="8" r="2.5"/><path d="M6 8.5v7M8.5 6.5 15.5 8M15.5 9.5c0 4-9 1.5-9 6"/>',
  layers: '<path d="M12 3 3 8l9 5 9-5Z"/><path d="m3 13 9 5 9-5M3 18l9 5 9-5"/>',
};

function Icon({ name, size = 17, sw = 1.6, style }) {
  return React.createElement('svg', {
    width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
    strokeWidth: sw, strokeLinecap: 'round', strokeLinejoin: 'round',
    style: { flex: '0 0 auto', display: 'block', ...style },
    dangerouslySetInnerHTML: { __html: ICON_PATHS[name] || '' },
  });
}

/* ---------------- Hexagon avatar ---------------- */
function Hex({ children, bg = 'var(--honey)', fg = '#fff', size = 34, fs, style }) {
  return (
    <span className="hex" style={{
      display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
      width: size, height: size, background: bg, color: fg, flex: '0 0 auto',
      fontFamily: 'var(--mono)', fontWeight: 600, fontSize: fs || Math.round(size * 0.37),
      letterSpacing: '.02em', ...style,
    }}>{children}</span>
  );
}

/* ---------------- Status chip ---------------- */
const CHIP_MAP = {
  idle:   ['var(--bg-sunk)', 'var(--text-2)', 'var(--border-2)'],
  ok:     ['var(--ok-soft)', 'var(--ok)', 'transparent'],
  warn:   ['var(--warn-soft)', 'var(--honey-deep)', 'transparent'],
  danger: ['var(--danger-soft)', 'var(--danger)', 'transparent'],
  info:   ['var(--info-soft)', 'var(--info)', 'transparent'],
  purple: ['var(--purple-soft)', 'var(--purple)', 'transparent'],
  solid:  ['var(--text-1)', '#fff', 'transparent'],
};
function Chip({ children, kind = 'idle', dot = false, style }) {
  const [bg, fg, bd] = CHIP_MAP[kind] || CHIP_MAP.idle;
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 5, height: 21, padding: '0 9px',
      borderRadius: 999, background: bg, color: fg, border: `1px solid ${bd}`,
      fontFamily: 'var(--mono)', fontSize: 10.5, fontWeight: 500, whiteSpace: 'nowrap', ...style,
    }}>
      {dot && <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'currentColor', opacity: .9 }} />}
      {children}
    </span>
  );
}

/* ---------------- Buttons ---------------- */
function Btn({ children, variant = 'primary', icon, size = 'md', onClick, style, disabled }) {
  const [hover, setHover] = useState(false);
  const sizes = { sm: [30, '0 11px', 12.5], md: [36, '0 15px', 13], lg: [42, '0 20px', 14.5] };
  const [h, pad, fs] = sizes[size];
  const variants = {
    primary: { background: disabled ? 'var(--text-4)' : (hover ? '#000' : 'var(--text-1)'), color: '#fff', border: '1px solid transparent', boxShadow: 'var(--shadow-1)' },
    secondary: { background: hover ? 'var(--hover)' : 'var(--surface)', color: 'var(--text-1)', border: '1px solid var(--border-2)' },
    ghost: { background: hover ? 'var(--hover)' : 'transparent', color: 'var(--text-2)', border: '1px solid transparent' },
    honey: { background: hover ? 'oklch(0.66 0.12 70)' : 'var(--honey)', color: '#fff', border: '1px solid transparent', boxShadow: 'var(--shadow-1)' },
    danger: { background: hover ? 'var(--danger)' : 'var(--danger-soft)', color: hover ? '#fff' : 'var(--danger)', border: '1px solid transparent' },
  };
  return (
    <button onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      onClick={disabled ? undefined : onClick} disabled={disabled}
      style={{
        height: h, padding: pad, borderRadius: 'var(--r)', fontFamily: 'var(--sans)', fontWeight: 600,
        fontSize: fs, cursor: disabled ? 'not-allowed' : 'pointer', display: 'inline-flex',
        alignItems: 'center', justifyContent: 'center', gap: 7, transition: 'all .14s var(--ease)',
        ...variants[variant], ...style,
      }}>
      {icon && <Icon name={icon} size={fs + 3} />}{children}
    </button>
  );
}

function IconBtn({ name, onClick, size = 32, iconSize = 17, active = false, title, style }) {
  const [hover, setHover] = useState(false);
  return (
    <button title={title} onClick={onClick} onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      style={{
        width: size, height: size, borderRadius: 'var(--r-sm)', border: 0, cursor: 'pointer',
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        background: active ? 'var(--hover)' : (hover ? 'var(--hover)' : 'transparent'),
        color: active ? 'var(--text-1)' : 'var(--text-2)', transition: 'all .12s', ...style,
      }}>
      <Icon name={name} size={iconSize} />
    </button>
  );
}

/* ---------------- Card ---------------- */
function Card({ children, pad = 18, hover = false, onClick, style }) {
  const [h, setH] = useState(false);
  return (
    <div onClick={onClick} onMouseEnter={() => setH(true)} onMouseLeave={() => setH(false)}
      style={{
        background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--r-lg)',
        padding: pad, boxShadow: hover && h ? 'var(--shadow-2)' : 'var(--shadow-1)',
        cursor: onClick ? 'pointer' : 'default', transition: 'all .15s var(--ease)',
        transform: hover && h ? 'translateY(-1px)' : 'none',
        borderColor: hover && h ? 'var(--border-2)' : 'var(--border)', ...style,
      }}>{children}</div>
  );
}

/* ---------------- Toggle ---------------- */
function Toggle({ on, onChange, size = 'md' }) {
  const w = size === 'sm' ? 32 : 38, h = size === 'sm' ? 18 : 22, k = h - 4;
  return (
    <button onClick={() => onChange(!on)} style={{
      width: w, height: h, borderRadius: 999, border: 0, cursor: 'pointer', padding: 0,
      background: on ? 'var(--text-1)' : 'var(--border-2)', position: 'relative', transition: 'background .18s', flex: '0 0 auto',
    }}>
      <span style={{
        position: 'absolute', top: 2, left: on ? w - k - 2 : 2, width: k, height: k, borderRadius: '50%',
        background: '#fff', transition: 'left .18s var(--ease)', boxShadow: '0 1px 2px rgba(0,0,0,.2)',
      }} />
    </button>
  );
}

/* ---------------- Tabs ---------------- */
function Tabs({ tabs, active, onChange, style }) {
  return (
    <div style={{ display: 'flex', gap: 2, borderBottom: '1px solid var(--border)', ...style }}>
      {tabs.map((t) => {
        const id = typeof t === 'string' ? t : t.id;
        const label = typeof t === 'string' ? t : t.label;
        const on = active === id;
        return (
          <button key={id} onClick={() => onChange(id)} style={{
            position: 'relative', padding: '10px 14px', border: 0, background: 'transparent',
            fontFamily: 'var(--sans)', fontSize: 13.5, fontWeight: on ? 600 : 500,
            color: on ? 'var(--text-1)' : 'var(--text-3)', cursor: 'pointer', transition: 'color .12s',
            display: 'inline-flex', alignItems: 'center', gap: 7,
          }}>
            {label}
            {t.count != null && <Chip kind={on ? 'solid' : 'idle'} style={{ height: 17, padding: '0 6px', fontSize: 9.5 }}>{t.count}</Chip>}
            {on && <span style={{ position: 'absolute', left: 8, right: 8, bottom: -1, height: 2, background: 'var(--text-1)', borderRadius: 2 }} />}
          </button>
        );
      })}
    </div>
  );
}

/* ---------------- Page header ---------------- */
function PageHead({ crumbs, title, sub, meta, actions, icon }) {
  return (
    <div style={{ marginBottom: 22 }}>
      {crumbs && (
        <div className="mono" style={{ fontSize: 11, color: 'var(--text-3)', display: 'flex', alignItems: 'center', gap: 7, marginBottom: 13 }}>
          {crumbs.map((c, i) => (
            <Fragment key={i}>{i > 0 && <span style={{ color: 'var(--text-4)' }}>/</span>}<span>{c}</span></Fragment>
          ))}
        </div>
      )}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            {icon}
            <h1 style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 29, margin: 0, letterSpacing: '-.01em' }}>{title}</h1>
            {meta && <span className="mono" style={{ fontSize: 12, color: 'var(--text-3)', whiteSpace: 'nowrap' }}>{meta}</span>}
          </div>
          {sub && <p style={{ fontSize: 14, color: 'var(--text-2)', maxWidth: '64ch', margin: '8px 0 0', lineHeight: 1.6 }}>{sub}</p>}
        </div>
        {actions && <div style={{ display: 'flex', gap: 9, flex: '0 0 auto' }}>{actions}</div>}
      </div>
    </div>
  );
}

/* ---------------- Empty state (hive honeycomb) ---------------- */
function EmptyState({ title, sub, action }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '70px 20px', textAlign: 'center' }}>
      <div style={{ position: 'relative', width: 120, height: 108, marginBottom: 24, opacity: .9 }}>
        {[[44, 0], [0, 38], [88, 38], [44, 76]].map(([l, t], i) => (
          <span key={i} className="hex" style={{
            position: 'absolute', left: l, top: t, width: 34, height: 38,
            background: i === 0 ? 'var(--honey-soft)' : 'var(--bg-sunk)',
            border: '1px solid var(--border-2)',
          }} />
        ))}
        <Hex size={34} bg="var(--honey)" style={{ position: 'absolute', left: 44, top: 38, width: 34, height: 38 }}>H</Hex>
      </div>
      <div style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 18, marginBottom: 7 }}>{title}</div>
      <p style={{ fontSize: 13.5, color: 'var(--text-2)', maxWidth: '42ch', margin: '0 0 20px', lineHeight: 1.6 }}>{sub}</p>
      {action}
    </div>
  );
}

/* ---------------- Section label ---------------- */
function Eyebrow({ children, style }) {
  return <div className="eyebrow" style={style}>{children}</div>;
}

/* ============================================================
   DATA MODEL
   ============================================================ */
const WS = { name: 'Acme Inc.', domain: 'hive.acme.com', plan: 'Enterprise' };
const ME = { name: '示例用户 A', role: '产品 · Product', color: 'oklch(0.55 0.11 285)', abbr: 'UA' };

const AGENTS = [
  { id: 'atlas', abbr: 'DR', name: '调研助理 Atlas', en: 'Atlas', role: '市场与竞品研究', status: 'ok', statusLabel: '运行中', color: 'oklch(0.60 0.13 250)', scope: '研究组可见', scopeIcon: 'users', owner: '我', perm: 'manage', caps: ['对话与任务', '文件与产物', '工具', '技能', 'A2A 协作'], last: '正在汇总 Q2 竞品报告 · 步骤 3/5', when: '2 分钟前', alert: false, tasks: 3 },
  { id: 'ledger', abbr: 'FC', name: '财务对账 Ledger', en: 'Ledger', role: '月度对账与报表', status: 'warn', statusLabel: '计划待确认', color: 'oklch(0.62 0.12 150)', scope: '财务组可见', scopeIcon: 'users', owner: '我', perm: 'manage', caps: ['对话与任务', '文件与产物', '工具', '审批'], last: '已生成 8 月对账计划，等待你确认', when: '12 分钟前', alert: true, tasks: 1 },
  { id: 'pace', abbr: 'HR', name: '招聘协调 Pace', en: 'Pace', role: '简历筛选与排期', status: 'idle', statusLabel: '空闲', color: 'oklch(0.60 0.12 25)', scope: '仅自己可见', scopeIcon: 'lock', owner: '我', perm: 'manage', caps: ['对话与任务', '渠道', '技能'], last: '上次任务已完成', when: '昨天', alert: false, tasks: 0 },
  { id: 'relay', abbr: 'OP', name: '运营自动化 Relay', en: 'Relay', role: '工单分发与跟进', status: 'purple', statusLabel: 'A2A 协作中', color: 'oklch(0.56 0.11 300)', scope: '全公司可见', scopeIcon: 'globe', owner: '示例用户 B', perm: 'use', caps: ['对话与任务', '工作流', 'A2A 协作', '渠道'], last: '正委派 Warden 执行安全检查', when: '刚刚', alert: false, tasks: 2 },
  { id: 'warden', abbr: 'SE', name: '安全审查 Warden', en: 'Warden', role: '合规与风险扫描', status: 'danger', statusLabel: '最近失败', color: 'oklch(0.52 0.05 60)', scope: '全公司可见', scopeIcon: 'globe', owner: '公司标准', perm: 'use', caps: ['对话与任务', '工具', '审批', '工作流'], last: '工具「漏洞库」连接超时', when: '1 小时前', alert: true, tasks: 0 },
  { id: 'quill', abbr: 'WR', name: '文档撰写 Quill', en: 'Quill', role: '报告与纪要生成', status: 'ok', statusLabel: '运行中', color: 'oklch(0.60 0.12 200)', scope: '研究组可见', scopeIcon: 'users', owner: '我', perm: 'manage', caps: ['对话与任务', '文件与产物', '记忆与知识', '技能'], last: '正在整理用户访谈纪要', when: '8 分钟前', alert: false, tasks: 1 },
];

const CAP_TYPES = [
  { id: 'chat', name: '对话与任务', icon: 'chat', desc: '接收任务并输出结果', state: 'on' },
  { id: 'file', name: '文件与产物', icon: 'doc', desc: '读取资料、生成文档、保存结果', state: 'on' },
  { id: 'memory', name: '记忆与知识', icon: 'brain', desc: '记住偏好、经验与公司知识', state: 'on' },
  { id: 'tools', name: '工具', icon: 'gear', desc: '使用公司开放的工具', state: 'on' },
  { id: 'skills', name: '技能', icon: 'bolt', desc: '启用特定工作技能', state: 'available' },
  { id: 'experts', name: '专家角色', icon: 'users', desc: '调用内部专家分工', state: 'available' },
  { id: 'workflow', name: '工作流', icon: 'flow', desc: '执行稳定流程', state: 'admin' },
  { id: 'channel', name: '渠道', icon: 'plug', desc: '在飞书、Slack、微信、邮件工作', state: 'approval' },
  { id: 'approval', name: '审批', icon: 'shield', desc: '高风险动作请求人工确认', state: 'on' },
  { id: 'a2a', name: 'A2A 协作', icon: 'branch', desc: '与其他数字员工协作', state: 'on' },
];

const CAP_STATE = {
  on:        { label: '已启用', kind: 'ok' },
  available: { label: '可启用', kind: 'idle' },
  admin:     { label: '需管理员配置', kind: 'warn' },
  approval:  { label: '需审批', kind: 'info' },
  locked:    { label: '公司未开放', kind: 'idle' },
  failed:    { label: '最近失败', kind: 'danger' },
};

window.HiveUI = {
  Icon, Hex, Chip, Btn, IconBtn, Card, Toggle, Tabs, PageHead, EmptyState, Eyebrow,
  WS, ME, AGENTS, CAP_TYPES, CAP_STATE,
  useState, useEffect, useRef, createContext, useContext, Fragment,
};
