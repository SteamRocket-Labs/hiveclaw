/* ============================================================
   Hive Prototype — App shell, Notion-tree sidebar, router
   Exposes window.HiveShell ; reads window.HivePages (page registry)
   ============================================================ */
const { Icon: SIcon, Hex: SHex, Chip: SChip, IconBtn: SIconBtn, Btn: SBtn, WS: SWS, ME: SME, AGENTS: SAGENTS } = window.HiveUI;
const { useState: uS, useEffect: uE, useRef: uR, createContext: cC, useContext: uC, Fragment: SFrag } = window.HiveUI;

const NavCtx = cC(null);
const useNav = () => uC(NavCtx);

/* ---------------- nav trees ---------------- */
const ownedAgents = SAGENTS.filter(a => a.owner === '我');

const EMP_TREE = [
  { group: [{ id: 'home', icon: 'home', label: '首页', en: 'Home' }] },
  { group: [
    { id: 'employees', icon: 'bot', label: '数字员工', en: 'Digital Employees',
      children: ownedAgents.map(a => ({ id: 'workspace', param: { agentId: a.id }, agentColor: a.color, abbr: a.abbr, label: a.name.split(' ')[0], full: a.name })) },
    { id: 'tasks', icon: 'chat', label: '对话与任务', en: 'Tasks' },
    { id: 'plan', icon: 'check', label: '计划确认', en: 'Plan Review', badge: 1 },
  ]},
  { group: [
    { id: 'automations', icon: 'flow', label: '自动化', en: 'Automations' },
    { id: 'memory', icon: 'brain', label: '记忆与知识', en: 'Memory' },
    { id: 'documents', icon: 'doc', label: '文档与研究', en: 'Documents' },
    { id: 'approvals', icon: 'shield', label: '审批', en: 'Approvals', badge: 2 },
  ]},
];

const ADMIN_TREE = [
  { group: [{ id: 'admin-overview', icon: 'grid', label: '公司总览', en: 'Overview' }] },
  { group: [
    { id: 'admin-members', icon: 'users', label: '成员与组织', en: 'Members & Org' },
    { id: 'admin-governance', icon: 'bot', label: '数字员工治理', en: 'Governance' },
  ]},
  { group: [
    { id: 'admin-budget', icon: 'coins', label: '模型与预算', en: 'Models & Budget' },
    { id: 'admin-capabilities', icon: 'gear', label: '能力与工具', en: 'Capabilities' },
    { id: 'admin-memory', icon: 'brain', label: '记忆治理', en: 'Memory Gov.' },
    { id: 'admin-channels', icon: 'plug', label: '渠道连接', en: 'Channels' },
  ]},
  { group: [
    { id: 'admin-approvals', icon: 'shield', label: '审批中心', en: 'Approvals', badge: 4 },
    { id: 'admin-audit', icon: 'audit', label: '审计记录', en: 'Audit Log' },
    { id: 'admin-assets', icon: 'box', label: '自动化与资产库', en: 'Assets' },
  ]},
];

/* ---------------- Tree item ---------------- */
function TreeItem({ node, active, onNav, depth = 0 }) {
  const [open, setOpen] = uS(node.id === 'employees');
  const [hover, setHover] = uS(false);
  const isActive = active.page === node.id && (!node.param || active.params?.agentId === node.param.agentId);
  const hasKids = node.children && node.children.length > 0;
  return (
    <div>
      <div
        onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
        onClick={() => onNav(node.id, node.param)}
        style={{
          display: 'flex', alignItems: 'center', gap: 8, height: 30, paddingLeft: 8 + depth * 18, paddingRight: 8,
          borderRadius: 6, cursor: 'pointer', position: 'relative',
          background: isActive ? 'var(--hover)' : (hover ? 'var(--hover)' : 'transparent'),
          transition: 'background .1s',
        }}>
        {hasKids ? (
          <span onClick={(e) => { e.stopPropagation(); setOpen(!open); }}
            style={{ display: 'flex', color: 'var(--text-3)', transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .15s', width: 14 }}>
            <SIcon name="chevron" size={13} />
          </span>
        ) : node.agentColor ? (
          <SHex size={18} bg={node.agentColor} fs={9}>{node.abbr}</SHex>
        ) : (
          <span style={{ color: isActive ? 'var(--text-1)' : 'var(--text-3)', display: 'flex' }}><SIcon name={node.icon} size={16.5} /></span>
        )}
        <span style={{
          fontSize: 13.5, fontWeight: isActive ? 600 : 500, color: isActive ? 'var(--text-1)' : 'var(--text-2)',
          whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', flex: 1,
        }}>{node.label}</span>
        {node.badge && !hover && (
          <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: '#fff', background: 'var(--honey)', borderRadius: 999, minWidth: 16, height: 16, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', padding: '0 4px' }}>{node.badge}</span>
        )}
        {hover && depth === 0 && !node.agentColor && (
          <span style={{ display: 'flex', color: 'var(--text-3)' }}><SIcon name="dots" size={15} /></span>
        )}
      </div>
      {hasKids && open && (
        <div style={{ marginTop: 1 }}>
          {node.children.map((c, i) => (
            <TreeItem key={i} node={c} active={active} onNav={onNav} depth={depth + 1} />
          ))}
          <div onClick={() => onNav('create')} style={{ display: 'flex', alignItems: 'center', gap: 8, height: 28, paddingLeft: 8 + (depth + 1) * 18, borderRadius: 6, cursor: 'pointer', color: 'var(--text-3)' }}
            onMouseEnter={(e) => e.currentTarget.style.background = 'var(--hover)'} onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}>
            <SIcon name="plus" size={14} /><span style={{ fontSize: 12.5 }}>新建数字员工</span>
          </div>
        </div>
      )}
    </div>
  );
}

/* ---------------- Workspace switcher ---------------- */
function WSSwitcher({ space, onSwitchSpace }) {
  const [open, setOpen] = uS(false);
  return (
    <div style={{ position: 'relative' }}>
      <button onClick={() => setOpen(!open)} style={{
        display: 'flex', alignItems: 'center', gap: 10, width: '100%', padding: 8, borderRadius: 8,
        border: 0, background: open ? 'var(--hover)' : 'transparent', cursor: 'pointer', textAlign: 'left', transition: 'background .1s',
      }} onMouseEnter={(e) => { if (!open) e.currentTarget.style.background = 'var(--hover)'; }} onMouseLeave={(e) => { if (!open) e.currentTarget.style.background = 'transparent'; }}>
        <SHex size={28} fs={13}>H</SHex>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 13.5, lineHeight: 1.1 }}>{SWS.name}</div>
          <div className="mono" style={{ fontSize: 10, color: 'var(--text-3)' }}>{space === 'admin' ? '控制中台 · Admin' : SWS.domain}</div>
        </div>
        <span style={{ color: 'var(--text-4)', display: 'flex' }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="m8 9 4-4 4 4M8 15l4 4 4-4" /></svg>
        </span>
      </button>
      {open && (
        <>
          <div onClick={() => setOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 40 }} />
          <div style={{
            position: 'absolute', top: '100%', left: 6, right: 6, marginTop: 4, zIndex: 50,
            background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 10, boxShadow: 'var(--shadow-pop)', padding: 6,
          }}>
            <div className="eyebrow" style={{ padding: '6px 10px 4px' }}>切换空间 · Space</div>
            {[['employee', 'home', '我的工作区', 'My Workspace'], ['admin', 'shield', '公司控制中台', 'Control Plane']].map(([sp, ic, cn, en]) => (
              <div key={sp} onClick={() => { onSwitchSpace(sp); setOpen(false); }} style={{
                display: 'flex', alignItems: 'center', gap: 10, padding: '9px 10px', borderRadius: 7, cursor: 'pointer',
                background: space === sp ? 'var(--hover)' : 'transparent',
              }} onMouseEnter={(e) => e.currentTarget.style.background = 'var(--hover)'} onMouseLeave={(e) => e.currentTarget.style.background = space === sp ? 'var(--hover)' : 'transparent'}>
                <span style={{ width: 30, height: 30, borderRadius: 7, background: sp === 'admin' ? 'var(--text-1)' : 'var(--honey-soft)', color: sp === 'admin' ? '#fff' : 'var(--honey-deep)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><SIcon name={ic} size={16} /></span>
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 13, fontWeight: 600 }}>{cn}</div>
                  <div className="mono" style={{ fontSize: 10, color: 'var(--text-3)' }}>{en}</div>
                </div>
                {space === sp && <span style={{ color: 'var(--honey)' }}><SIcon name="checkPlain" size={16} /></span>}
              </div>
            ))}
            <div style={{ height: 1, background: 'var(--border)', margin: '6px 4px' }} />
            <div onClick={() => window.HiveReturnToEntry && window.HiveReturnToEntry('space')} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px', borderRadius: 7, cursor: 'pointer', color: 'var(--text-2)' }}
              onMouseEnter={(e) => e.currentTarget.style.background = 'var(--hover)'} onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}>
              <span style={{ width: 30, display: 'flex', justifyContent: 'center' }}><SIcon name="plus" size={16} /></span>
              <span style={{ fontSize: 13 }}>加入或创建 workspace</span>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

/* ---------------- User footer ---------------- */
function UserFooter() {
  const { navigate } = useNav();
  const [open, setOpen] = uS(false);
  return (
    <div style={{ position: 'relative', borderTop: '1px solid var(--border)' }}>
      <div onClick={() => setOpen(!open)} style={{ display: 'flex', alignItems: 'center', gap: 9, padding: 10, cursor: 'pointer', background: open ? 'var(--hover)' : 'transparent' }}
        onMouseEnter={(e) => { if (!open) e.currentTarget.style.background = 'var(--hover)'; }} onMouseLeave={(e) => { if (!open) e.currentTarget.style.background = 'transparent'; }}>
        <SHex size={28} bg={SME.color} fs={11}>{SME.abbr}</SHex>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ fontSize: 12.5, fontWeight: 600 }}>{SME.name}</div>
          <div className="mono" style={{ fontSize: 10, color: 'var(--text-3)' }}>{SME.role}</div>
        </div>
        <span style={{ color: 'var(--text-4)', display: 'flex' }}><SIcon name="gear" size={16} /></span>
      </div>
      {open && (
        <>
          <div onClick={() => setOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 40 }} />
          <div style={{ position: 'absolute', bottom: '100%', left: 8, right: 8, marginBottom: 4, zIndex: 50, background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 10, boxShadow: 'var(--shadow-pop)', padding: 6 }}>
            {[['gear', '账户设置'], ['users', '成员与邀请'], ['plug', '偏好']].map(([ic, t]) => (
              <div key={t} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px', borderRadius: 7, cursor: 'pointer', color: 'var(--text-2)' }}
                onMouseEnter={(e) => e.currentTarget.style.background = 'var(--hover)'} onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}>
                <SIcon name={ic} size={16} /><span style={{ fontSize: 13 }}>{t}</span>
              </div>
            ))}
            <div style={{ height: 1, background: 'var(--border)', margin: '6px 4px' }} />
            <div onClick={() => { setOpen(false); window.HiveReturnToEntry && window.HiveReturnToEntry('auth'); }} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px', borderRadius: 7, cursor: 'pointer', color: 'var(--danger)' }}
              onMouseEnter={(e) => e.currentTarget.style.background = 'var(--danger-soft)'} onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}>
              <SIcon name="arrowUpRight" size={16} /><span style={{ fontSize: 13 }}>退出登录</span>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

/* ---------------- Notifications ---------------- */
function Notifications() {
  const [open, setOpen] = uS(false);
  const { navigate } = useNav();
  const items = [
    ['warn', 'Ledger 生成了对账计划', '需要你确认 · 12 分钟前', 'check', { agentId: 'ledger' }],
    ['danger', 'Warden 工具连接失败', '漏洞库连接超时 · 1 小时前', 'workspace', { agentId: 'warden' }],
    ['info', 'Relay 委派任务给 Warden', 'A2A 协作进行中 · 刚刚', 'workspace', { agentId: 'relay' }],
    ['ok', 'Atlas 完成了市场摘要', '产物已生成 · 2 小时前', 'workspace', { agentId: 'atlas' }],
  ];
  return (
    <div style={{ position: 'relative' }}>
      <button onClick={() => setOpen(!open)} style={{ position: 'relative', width: 32, height: 32, borderRadius: 8, border: 0, background: open ? 'var(--hover)' : 'transparent', cursor: 'pointer', color: 'var(--text-2)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <SIcon name="bell" size={17} />
        <span style={{ position: 'absolute', top: 6, right: 6, width: 7, height: 7, borderRadius: '50%', background: 'var(--honey)', border: '1.5px solid var(--bg)' }} />
      </button>
      {open && (
        <>
          <div onClick={() => setOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 40 }} />
          <div style={{ position: 'absolute', top: '100%', right: 0, marginTop: 6, width: 340, zIndex: 50, background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12, boxShadow: 'var(--shadow-pop)', overflow: 'hidden' }}>
            <div style={{ padding: '13px 16px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span style={{ fontWeight: 600, fontSize: 14 }}>通知</span>
              <span className="mono" style={{ fontSize: 11, color: 'var(--honey-deep)', cursor: 'pointer' }}>全部已读</span>
            </div>
            <div style={{ maxHeight: 380, overflow: 'auto' }} className="thin-scroll">
              {items.map(([k, t, m, pg, pm], i) => (
                <div key={i} onClick={() => { navigate(pg, pm); setOpen(false); }} style={{ display: 'flex', gap: 11, padding: '12px 16px', borderBottom: '1px solid var(--border)', cursor: 'pointer' }}
                  onMouseEnter={(e) => e.currentTarget.style.background = 'var(--surface-2)'} onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}>
                  <span style={{ width: 8, height: 8, borderRadius: '50%', marginTop: 5, flex: '0 0 auto', background: k === 'warn' ? 'var(--honey)' : k === 'danger' ? 'var(--danger)' : k === 'info' ? 'var(--info)' : 'var(--ok)' }} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 500, lineHeight: 1.4 }}>{t}</div>
                    <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-3)', marginTop: 2 }}>{m}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

/* ---------------- Sidebar ---------------- */
function Sidebar({ view, navigate, space, onSwitchSpace }) {
  const tree = space === 'admin' ? ADMIN_TREE : EMP_TREE;
  return (
    <aside style={{ width: 256, flex: '0 0 256px', background: 'var(--bg)', borderRight: '1px solid var(--border)', display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ padding: '10px 10px 6px' }}><WSSwitcher space={space} onSwitchSpace={onSwitchSpace} /></div>
      <div style={{ padding: '2px 12px 10px' }}>
        <button onClick={() => navigate('search')} style={{ display: 'flex', alignItems: 'center', gap: 8, height: 32, padding: '0 9px', width: '100%', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 7, color: 'var(--text-3)', cursor: 'pointer' }}>
          <SIcon name="search" size={15} /><span style={{ fontSize: 12.5 }}>搜索 Search</span>
          <span className="mono" style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--text-4)' }}>⌘K</span>
        </button>
      </div>
      <div className="thin-scroll" style={{ flex: 1, overflow: 'auto', padding: '0 12px' }}>
        {tree.map((g, gi) => (
          <div key={gi} style={{ marginBottom: 14 }}>
            {g.group.map((n) => <TreeItem key={n.id + (n.param?.agentId || '')} node={n} active={view} onNav={navigate} />)}
          </div>
        ))}
        {space === 'employee' && (
          <div onClick={() => onSwitchSpace('admin')} style={{ display: 'flex', alignItems: 'center', gap: 8, height: 32, padding: '0 8px', borderRadius: 6, cursor: 'pointer', marginTop: 4 }}
            onMouseEnter={(e) => e.currentTarget.style.background = 'var(--hover)'} onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}>
            <span style={{ color: 'var(--text-3)', display: 'flex' }}><SIcon name="shield" size={16.5} /></span>
            <span style={{ fontSize: 13.5, fontWeight: 500, color: 'var(--text-2)' }}>公司控制中台</span>
            <span style={{ marginLeft: 'auto' }}><SChip kind="idle" style={{ height: 18, fontSize: 9.5 }}>Admin</SChip></span>
          </div>
        )}
      </div>
      <UserFooter />
    </aside>
  );
}

/* ---------------- App ---------------- */
function App() {
  const [view, setView] = uS({ space: 'employee', page: 'home', params: {} });
  const [modal, setModal] = uS(null);
  const mainRef = uR(null);

  const navigate = (page, params = {}) => {
    setView((v) => ({ ...v, page, params }));
    if (mainRef.current) mainRef.current.scrollTop = 0;
  };
  const switchSpace = (space) => {
    setView({ space, page: space === 'admin' ? 'admin-overview' : 'home', params: {} });
    if (mainRef.current) mainRef.current.scrollTop = 0;
  };

  uE(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') { e.preventDefault(); setModal(modal === 'search' ? null : 'search'); }
      if (e.key === 'Escape') setModal(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  const Pages = window.HivePages || {};
  const PageComp = Pages[view.page] || (() => <div style={{ padding: 48 }}>页面建设中：{view.page}</div>);

  const ctx = { view, navigate, switchSpace, modal, setModal };

  return (
    <NavCtx.Provider value={ctx}>
      <div style={{ display: 'flex', height: '100vh', overflow: 'hidden', background: 'var(--bg)' }}>
        <Sidebar view={view} navigate={navigate} space={view.space} onSwitchSpace={switchSpace} />
        <main style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, height: '100%' }}>
          <div style={{ height: 46, flex: '0 0 46px', display: 'flex', alignItems: 'center', gap: 4, padding: '0 16px', borderBottom: '1px solid var(--border)', background: 'var(--bg)' }}>
            <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>
              {view.space === 'admin' ? '控制中台' : '我的工作区'}
            </span>
            <div style={{ flex: 1 }} />
            <button onClick={() => setModal('search')} style={{ display: 'flex', alignItems: 'center', gap: 7, height: 30, padding: '0 10px', background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 7, color: 'var(--text-3)', cursor: 'pointer', marginRight: 4 }}>
              <SIcon name="search" size={14} /><span style={{ fontSize: 12 }}>搜索</span><span className="mono" style={{ fontSize: 9.5, color: 'var(--text-4)', border: '1px solid var(--border-2)', borderRadius: 4, padding: '1px 4px' }}>⌘K</span>
            </button>
            <Notifications />
            <SIconBtn name="sparkle" size={32} iconSize={17} title="问 Hive" />
          </div>
          <div ref={mainRef} className="thin-scroll" style={{ flex: 1, overflow: 'auto', position: 'relative' }}>
            <PageComp key={view.page + JSON.stringify(view.params)} navigate={navigate} params={view.params} ctx={ctx} />
          </div>
        </main>
        {modal === 'search' && <SearchPalette onClose={() => setModal(null)} navigate={navigate} />}
        {window.HiveModals && <window.HiveModals ctx={ctx} />}
      </div>
    </NavCtx.Provider>
  );
}

/* ---------------- Search palette (⌘K) ---------------- */
function SearchPalette({ onClose, navigate }) {
  const [q, setQ] = uS('');
  const results = [
    ['bot', '数字员工', 'employees', {}],
    ['chat', '对话与任务', 'tasks', {}],
    ['shield', '审批', 'approvals', {}],
    ...SAGENTS.map(a => ['bot', a.name, 'workspace', { agentId: a.id }]),
    ['grid', '公司控制中台 · 总览', 'admin-overview', {}],
  ].filter(r => !q || r[1].includes(q));
  return (
    <div onClick={onClose} style={{ position: 'fixed', inset: 0, background: 'rgba(38,36,31,.28)', backdropFilter: 'blur(2px)', zIndex: 100, display: 'flex', alignItems: 'flex-start', justifyContent: 'center', paddingTop: '14vh' }}>
      <div onClick={(e) => e.stopPropagation()} style={{ width: 560, background: 'var(--surface)', borderRadius: 14, boxShadow: 'var(--shadow-pop)', overflow: 'hidden', border: '1px solid var(--border)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 11, padding: '15px 18px', borderBottom: '1px solid var(--border)' }}>
          <SIcon name="search" size={18} style={{ color: 'var(--text-3)' }} />
          <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索数字员工、任务、页面…或问 Hive"
            style={{ flex: 1, border: 0, outline: 'none', fontFamily: 'var(--sans)', fontSize: 15, background: 'transparent', color: 'var(--text-1)' }} />
          <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)', border: '1px solid var(--border-2)', borderRadius: 4, padding: '2px 6px' }}>ESC</span>
        </div>
        <div className="thin-scroll" style={{ maxHeight: 360, overflow: 'auto', padding: 8 }}>
          <div className="eyebrow" style={{ padding: '6px 10px' }}>{q ? '结果' : '快速跳转'}</div>
          {results.map((r, i) => (
            <div key={i} onClick={() => { navigate(r[2], r[3]); onClose(); }} style={{ display: 'flex', alignItems: 'center', gap: 11, padding: '9px 10px', borderRadius: 8, cursor: 'pointer' }}
              onMouseEnter={(e) => e.currentTarget.style.background = 'var(--hover)'} onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}>
              <span style={{ color: 'var(--text-3)', display: 'flex' }}><SIcon name={r[0]} size={16} /></span>
              <span style={{ fontSize: 13.5 }}>{r[1]}</span>
              <span style={{ marginLeft: 'auto', color: 'var(--text-4)' }}><SIcon name="arrowRight" size={14} /></span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

window.HiveShell = { App, useNav, NavCtx };
window.HivePages = window.HivePages || {};
