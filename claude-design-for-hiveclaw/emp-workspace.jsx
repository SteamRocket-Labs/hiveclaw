/* ============================================================
   Hive Prototype — Agent Workspace (detail w/ tabs)
   Tabs: 概览 / 能力配置 / 记忆与知识 / A2A 协作 / 权限与分享 / 设置
   ============================================================ */
const WU = window.HiveUI;
const { Icon: WIcon, Hex: WHex, Chip: WChip, Btn: WBtn, IconBtn: WIconBtn, Card: WCard, Tabs: WTabs,
        Toggle: WToggle, Eyebrow: WEye, EmptyState: WEmpty, AGENTS: WAGENTS, CAP_TYPES: WCAPS, CAP_STATE: WCAPST } = WU;
const { useState: wuS, Fragment: WFrag } = WU;

function findAgent(id) { return WAGENTS.find(a => a.id === id) || WAGENTS[0]; }

/* ----- sub: capability row ----- */
function CapRow({ cap, onToggle }) {
  const st = WCAPST[cap.state];
  const enabled = cap.state === 'on';
  const blocked = ['admin', 'approval', 'locked'].includes(cap.state);
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 13, padding: '14px 16px', borderBottom: '1px solid var(--border)' }}>
      <span style={{ width: 34, height: 34, borderRadius: 9, background: enabled ? 'var(--honey-soft)' : 'var(--bg-sunk)', color: enabled ? 'var(--honey-deep)' : 'var(--text-2)', display: 'flex', alignItems: 'center', justifyContent: 'center', flex: '0 0 auto' }}>
        <WIcon name={cap.icon} size={17} />
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 13.5, fontWeight: 600 }}>{cap.name}</span>
          <WChip kind={st.kind}>{st.label}</WChip>
        </div>
        <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 3 }}>{cap.desc}</div>
      </div>
      {blocked ? (
        <WBtn size="sm" variant="secondary" icon={cap.state === 'approval' ? 'shield' : 'arrowUpRight'}>{cap.state === 'approval' ? '申请审批' : cap.state === 'admin' ? '联系管理员' : '了解'}</WBtn>
      ) : (
        <WToggle on={enabled} onChange={() => onToggle(cap.id)} />
      )}
    </div>
  );
}

/* ----- TAB: 能力配置 ----- */
function CapabilitiesTab({ agent }) {
  const [caps, setCaps] = wuS(() => WCAPS.map(c => ({ ...c, state: agent.caps.includes(c.name) ? 'on' : c.state })));
  const toggle = (id) => setCaps(cs => cs.map(c => c.id === id ? { ...c, state: c.state === 'on' ? 'available' : 'on' } : c));
  const on = caps.filter(c => c.state === 'on').length;
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 300px', gap: 24 }}>
      <div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 13 }}>
          <WEye>能力清单 · {on}/{caps.length} 已启用</WEye>
          <WChip kind="warn" style={{ gap: 5 }}><WIcon name="shield" size={11} />2 项由公司管理</WChip>
        </div>
        <WCard pad={0}>{caps.map(c => <CapRow key={c.id} cap={c} onToggle={toggle} />)}</WCard>
      </div>
      <div>
        <WEye style={{ marginBottom: 13 }}>说明</WEye>
        <WCard pad={16} style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.65 }}>
            <b style={{ color: 'var(--text-1)' }}>你可以</b>自助启用基础能力。标记<WChip kind="warn" style={{ margin: '0 3px', height: 18 }}>需管理员</WChip>或<WChip kind="info" style={{ margin: '0 3px', height: 18 }}>需审批</WChip>的能力由公司治理，需要走申请流程。
          </div>
        </WCard>
        <WCard pad={16}>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 10 }}>公司开放范围</div>
          {[['工具', '12 / 18 已开放'], ['技能', '8 已开放'], ['渠道', '飞书 · Slack · 邮件']].map(([k, v], i) => (
            <div key={i} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, padding: '6px 0', borderTop: i ? '1px solid var(--border)' : 0 }}>
              <span style={{ color: 'var(--text-2)' }}>{k}</span><span className="mono" style={{ fontSize: 11, color: 'var(--text-1)' }}>{v}</span>
            </div>
          ))}
        </WCard>
      </div>
    </div>
  );
}

/* ----- TAB: 权限与分享 ----- */
function PermissionsTab({ agent }) {
  const [scope, setScope] = wuS(agent.scope.includes('全公司') ? 'all' : agent.scope.includes('仅自己') ? 'me' : 'group');
  const scopes = [
    ['me', 'lock', '仅自己可见', '只有你能使用和管理'],
    ['users', 'users', '指定员工', '选择具体成员授予权限'],
    ['group', 'users', '指定 Group 可见', '研究组、财务组等部门范围'],
    ['all', 'globe', '全公司可见', 'workspace 内所有成员'],
  ];
  const members = [
    ['示例用户 A', '所有者', 'manage', EME_C()], ['示例用户 B', '可管理', 'manage', 'oklch(0.6 0.12 145)'],
    ['示例用户 C', '可使用', 'use', 'oklch(0.6 0.12 25)'], ['研究组 (8 人)', '可使用', 'use', 'group'],
  ];
  function EME_C() { return WU.ME.color; }
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 320px', gap: 24 }}>
      <div>
        <WEye style={{ marginBottom: 13 }}>可见范围 · Visibility</WEye>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 26 }}>
          {scopes.map(([id, ic, t, d]) => (
            <div key={id} onClick={() => setScope(id)} style={{ display: 'flex', gap: 11, padding: 14, borderRadius: 10, cursor: 'pointer', border: `1.5px solid ${scope === id ? 'var(--text-1)' : 'var(--border)'}`, background: scope === id ? 'var(--surface)' : 'var(--surface)', transition: 'all .12s' }}>
              <span style={{ width: 32, height: 32, borderRadius: 8, background: scope === id ? 'var(--text-1)' : 'var(--bg-sunk)', color: scope === id ? '#fff' : 'var(--text-2)', display: 'flex', alignItems: 'center', justifyContent: 'center', flex: '0 0 auto' }}><WIcon name={ic} size={16} /></span>
              <div>
                <div style={{ fontSize: 13, fontWeight: 600 }}>{t}</div>
                <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 2, lineHeight: 1.4 }}>{d}</div>
              </div>
            </div>
          ))}
        </div>

        <WEye style={{ marginBottom: 13 }}>成员权限 · People</WEye>
        <WCard pad={0}>
          {members.map((m, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 11, padding: '11px 16px', borderBottom: i < members.length - 1 ? '1px solid var(--border)' : 0 }}>
              {m[3] === 'group' ? <span style={{ width: 28, height: 28, borderRadius: 8, background: 'var(--bg-sunk)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-2)' }}><WIcon name="users" size={15} /></span> : <WHex size={28} bg={m[3]} fs={10}>{m[0].slice(0, 2)}</WHex>}
              <span style={{ flex: 1, fontSize: 13, fontWeight: 500 }}>{m[0]}</span>
              <WChip kind={m[2] === 'manage' ? 'solid' : 'idle'}>{m[1]}</WChip>
            </div>
          ))}
          <div style={{ padding: '11px 16px', display: 'flex', alignItems: 'center', gap: 8, color: 'var(--text-3)', cursor: 'pointer' }}><WIcon name="plus" size={15} /><span style={{ fontSize: 13 }}>添加成员或 Group</span></div>
        </WCard>
      </div>
      <div>
        <WEye style={{ marginBottom: 13 }}>你的权限</WEye>
        <WCard pad={16} style={{ marginBottom: 14 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
            <span style={{ width: 36, height: 36, borderRadius: 9, background: 'var(--ok-soft)', color: 'var(--ok)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><WIcon name="checkPlain" size={18} /></span>
            <div><div style={{ fontSize: 13.5, fontWeight: 600 }}>可管理</div><div style={{ fontSize: 11.5, color: 'var(--text-3)' }}>Full manage access</div></div>
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6 }}>你可以修改配置、能力、可见范围，并交办任务。公司治理权限（预算、渠道）由管理层控制。</div>
        </WCard>
        <WCard pad={16}>
          <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>分享链接</div>
          <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginBottom: 11 }}>有权限的成员可通过链接打开</div>
          <div style={{ display: 'flex', gap: 7 }}>
            <div className="mono" style={{ flex: 1, fontSize: 11, color: 'var(--text-2)', background: 'var(--bg-sunk)', borderRadius: 7, padding: '8px 10px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>hive.acme.com/a/{agent.id}</div>
            <WBtn size="sm" variant="secondary" icon="copy">复制</WBtn>
          </div>
        </WCard>
      </div>
    </div>
  );
}

/* ----- TAB: A2A 协作 ----- */
function A2ATab({ agent, navigate }) {
  const partners = [
    ['Warden', 'SE', '安全审查', 'oklch(0.52 0.05 60)', '可委派', 'warden'],
    ['Quill', 'WR', '文档撰写', 'oklch(0.60 0.12 200)', '可委派', 'quill'],
    ['Ledger', 'FC', '财务对账', 'oklch(0.62 0.12 150)', '需审批', 'ledger'],
  ];
  const delegations = [
    ['Warden', 'SE', 'oklch(0.52 0.05 60)', '执行安全合规扫描', 'ok', '完成 · 返回 3 项风险', '14:32'],
    ['Quill', 'WR', 'oklch(0.60 0.12 200)', '整理为研究报告', 'purple', '执行中 · 步骤 2/4', '14:40'],
    ['Ledger', 'FC', 'oklch(0.62 0.12 150)', '核对成本数据', 'warn', '等待审批', '14:45'],
  ];
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '300px 1fr', gap: 24 }}>
      <div>
        <WEye style={{ marginBottom: 13 }}>协作关系 · Partners</WEye>
        <WCard pad={0} style={{ marginBottom: 14 }}>
          {partners.map((p, i) => (
            <div key={i} onClick={() => navigate('workspace', { agentId: p[5] })} style={{ display: 'flex', alignItems: 'center', gap: 11, padding: '12px 15px', borderBottom: i < partners.length - 1 ? '1px solid var(--border)' : 0, cursor: 'pointer' }}
              onMouseEnter={(e) => e.currentTarget.style.background = 'var(--surface-2)'} onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}>
              <WHex size={32} bg={p[3]} fs={12}>{p[1]}</WHex>
              <div style={{ flex: 1 }}><div style={{ fontSize: 13, fontWeight: 600 }}>{p[0]}</div><div style={{ fontSize: 11.5, color: 'var(--text-3)' }}>{p[2]}</div></div>
              <WChip kind={p[4] === '需审批' ? 'info' : 'idle'}>{p[4]}</WChip>
            </div>
          ))}
        </WCard>
        <WBtn variant="secondary" icon="plus" style={{ width: '100%' }}>添加协作对象</WBtn>
        <div style={{ marginTop: 20 }}>
          <WEye style={{ marginBottom: 13 }}>专家角色 · Experts</WEye>
          <WCard pad={15}>
            <div style={{ fontSize: 12, color: 'var(--text-2)', lineHeight: 1.6, marginBottom: 11 }}>{agent.name.split(' ')[0]} 内部启用的专业分工：</div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 7 }}>
              {['竞品分析师', '数据清洗', '报告撰写', '事实核查'].map((e) => <WChip key={e} kind="purple" style={{ height: 24 }}><WIcon name="users" size={11} />{e}</WChip>)}
            </div>
          </WCard>
        </div>
      </div>
      <div>
        <WEye style={{ marginBottom: 13 }}>任务委派记录 · Delegations</WEye>
        <WCard pad={0}>
          {delegations.map((d, i) => (
            <div key={i} style={{ display: 'flex', gap: 13, padding: '15px 16px', borderBottom: i < delegations.length - 1 ? '1px solid var(--border)' : 0 }}>
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6 }}>
                <WHex size={20} bg={agent.color} fs={8}>{agent.abbr}</WHex>
                <span style={{ width: 1, flex: 1, background: 'var(--border-2)' }} />
                <span style={{ color: 'var(--text-4)', display: 'flex' }}><WIcon name="chevronDown" size={12} /></span>
                <WHex size={26} bg={d[2]} fs={10}>{d[1]}</WHex>
              </div>
              <div style={{ flex: 1, paddingTop: 2 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 5 }}>
                  <span style={{ fontSize: 13, fontWeight: 600 }}>委派给 {d[0]}</span>
                  <span className="mono" style={{ fontSize: 10, color: 'var(--text-4)', marginLeft: 'auto' }}>{d[6]}</span>
                </div>
                <div style={{ fontSize: 12.5, color: 'var(--text-2)', marginBottom: 8 }}>「{d[3]}」</div>
                <WChip kind={d[4]} dot>{d[5]}</WChip>
              </div>
            </div>
          ))}
        </WCard>
      </div>
    </div>
  );
}

/* ----- TAB: 记忆与知识 ----- */
function MemoryTab({ agent }) {
  const mem = [
    ['偏好', '报告统一用中文，结论先行', 'ok', '对话学习'],
    ['事实', 'Q2 主要竞品：Nova、Drift、Beacon', 'ok', '任务沉淀'],
    ['公司知识', '《2026 产品策略》文档', 'info', '公司共享', true],
    ['偏好', '财务数据精确到分', 'ok', '用户设定'],
  ];
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 300px', gap: 24 }}>
      <div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 13 }}>
          <WEye>记住了什么 · Memory</WEye>
          <WBtn size="sm" variant="ghost" icon="plus">添加</WBtn>
        </div>
        <WCard pad={0}>
          {mem.map((m, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '13px 16px', borderBottom: i < mem.length - 1 ? '1px solid var(--border)' : 0 }}>
              <WChip kind={m[2] === 'info' ? 'info' : 'idle'} style={{ flex: '0 0 auto' }}>{m[0]}</WChip>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 7 }}>{m[1]}{m[4] && <span style={{ color: 'var(--text-4)' }} title="受权限限制"><WIcon name="lock" size={12} /></span>}</div>
                <div className="mono" style={{ fontSize: 10, color: 'var(--text-4)', marginTop: 3 }}>来源 · {m[3]}</div>
              </div>
              <WIconBtn name="dots" size={28} iconSize={15} />
            </div>
          ))}
        </WCard>
      </div>
      <div>
        <WEye style={{ marginBottom: 13 }}>公司共享知识</WEye>
        <WCard pad={16}>
          {[['产品策略库', 'doc', '12 篇'], ['竞品情报', 'box', '8 份'], ['品牌规范', 'star', '只读']].map((k, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 0', borderTop: i ? '1px solid var(--border)' : 0 }}>
              <span style={{ color: 'var(--text-3)', display: 'flex' }}><WIcon name={k[1]} size={16} /></span>
              <span style={{ flex: 1, fontSize: 13 }}>{k[0]}</span>
              <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>{k[2]}</span>
            </div>
          ))}
        </WCard>
      </div>
    </div>
  );
}

/* ----- TAB: 概览 ----- */
function OverviewTab({ agent, navigate }) {
  const steps = [
    ['done', '收集竞品公开资料', '已抓取 24 个来源'],
    ['done', '提取定价与功能矩阵', '生成对比表'],
    ['active', '分析差异与机会点', '正在处理…'],
    ['todo', '撰写结论与建议', ''],
    ['todo', '生成最终报告', ''],
  ];
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 320px', gap: 24 }}>
      <div>
        <WEye style={{ marginBottom: 13 }}>当前任务 · Active task</WEye>
        <WCard pad={18} style={{ marginBottom: 22 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16 }}>
            <span style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--ok)', boxShadow: '0 0 0 4px var(--ok-soft)' }} />
            <span style={{ fontSize: 14, fontWeight: 600 }}>Q2 竞品对标报告</span>
            <WChip kind="ok" dot style={{ marginLeft: 'auto' }}>运行中</WChip>
            <WBtn size="sm" variant="secondary" icon="chat" onClick={() => navigate('tasks', { agentId: agent.id })}>打开对话</WBtn>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 0 }}>
            {steps.map((s, i) => (
              <div key={i} style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', alignSelf: 'stretch' }}>
                  <span style={{ width: 18, height: 18, borderRadius: '50%', flex: '0 0 auto', marginTop: 2, display: 'flex', alignItems: 'center', justifyContent: 'center',
                    background: s[0] === 'done' ? 'var(--ok)' : s[0] === 'active' ? 'var(--honey)' : 'var(--bg-sunk)',
                    color: '#fff', border: s[0] === 'todo' ? '1.5px solid var(--border-2)' : 0 }}>
                    {s[0] === 'done' && <WIcon name="checkPlain" size={11} />}
                    {s[0] === 'active' && <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#fff' }} />}
                  </span>
                  {i < steps.length - 1 && <span style={{ width: 1.5, flex: 1, minHeight: 22, background: s[0] === 'done' ? 'var(--ok)' : 'var(--border-2)' }} />}
                </div>
                <div style={{ paddingBottom: 14, flex: 1 }}>
                  <div style={{ fontSize: 13, fontWeight: s[0] === 'active' ? 600 : 500, color: s[0] === 'todo' ? 'var(--text-3)' : 'var(--text-1)' }}>{s[1]}</div>
                  {s[2] && <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 2 }}>{s[2]}</div>}
                </div>
              </div>
            ))}
          </div>
        </WCard>

        <WEye style={{ marginBottom: 13 }}>产物 · Artifacts</WEye>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          {[['竞品功能对比表', 'box', 'XLSX · 生成于 14:30'], ['市场定位摘要', 'doc', 'DOC · 生成于 14:12']].map((p, i) => (
            <WCard key={i} hover pad={15} onClick={() => navigate('documents', {})}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
                <span style={{ width: 32, height: 32, borderRadius: 8, background: 'var(--bg-sunk)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-2)' }}><WIcon name={p[1]} size={16} /></span>
                <span style={{ fontSize: 13, fontWeight: 600, flex: 1 }}>{p[0]}</span>
              </div>
              <div className="mono" style={{ fontSize: 10, color: 'var(--text-4)' }}>{p[2]}</div>
            </WCard>
          ))}
        </div>
      </div>

      <div>
        <WEye style={{ marginBottom: 13 }}>身份 · Identity</WEye>
        <WCard pad={16} style={{ marginBottom: 16 }}>
          {[['职责', agent.role], ['负责人', agent.owner], ['可见范围', agent.scope], ['创建', '2026-05-20']].map((r, i) => (
            <div key={i} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12.5, padding: '7px 0', borderTop: i ? '1px solid var(--border)' : 0 }}>
              <span style={{ color: 'var(--text-3)' }}>{r[0]}</span><span style={{ color: 'var(--text-1)', fontWeight: 500 }}>{r[1]}</span>
            </div>
          ))}
        </WCard>
        <WEye style={{ marginBottom: 13 }}>能力摘要</WEye>
        <WCard pad={16} style={{ marginBottom: 16 }}>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 7 }}>
            {agent.caps.map((c) => <WChip key={c} kind="idle" style={{ height: 24 }}>{c}</WChip>)}
          </div>
        </WCard>
        {agent.alert && (
          <WCard pad={15} style={{ borderColor: 'var(--honey-line)', background: 'var(--warn-soft)' }}>
            <div style={{ display: 'flex', gap: 9 }}>
              <span style={{ color: 'var(--honey-deep)', display: 'flex', marginTop: 1 }}><WIcon name="alert" size={16} /></span>
              <div><div style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--honey-deep)' }}>有待确认事项</div>
              <div style={{ fontSize: 11.5, color: 'var(--text-2)', marginTop: 3, lineHeight: 1.5 }}>{agent.last}</div></div>
            </div>
          </WCard>
        )}
      </div>
    </div>
  );
}

/* ---------------- WORKSPACE PAGE ---------------- */
function WorkspacePage({ navigate, params }) {
  const agent = findAgent(params.agentId);
  const [tab, setTab] = wuS('overview');
  const tabs = [
    { id: 'overview', label: '概览' }, { id: 'caps', label: '能力配置' },
    { id: 'memory', label: '记忆与知识' }, { id: 'a2a', label: 'A2A 协作' },
    { id: 'perms', label: '权限与分享' }, { id: 'settings', label: '设置' },
  ];
  return (
    <div style={{ padding: '28px 40px 60px', maxWidth: 1180, margin: '0 auto' }}>
      <div className="mono" style={{ fontSize: 11, color: 'var(--text-3)', display: 'flex', alignItems: 'center', gap: 7, marginBottom: 16, cursor: 'pointer' }} onClick={() => navigate('employees')}>
        <WIcon name="bot" size={13} />数字员工<span style={{ color: 'var(--text-4)' }}>/</span><span style={{ color: 'var(--text-2)' }}>{agent.en}</span>
      </div>

      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16, marginBottom: 22 }}>
        <WHex size={52} bg={agent.color} fs={18}>{agent.abbr}</WHex>
        <div style={{ flex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <h1 style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 26, margin: 0, whiteSpace: 'nowrap' }}>{agent.name}</h1>
            <WChip kind={agent.status} dot>{agent.statusLabel}</WChip>
          </div>
          <p style={{ fontSize: 13.5, color: 'var(--text-2)', margin: '6px 0 0' }}>{agent.role} · <span className="mono" style={{ fontSize: 12 }}>{agent.scope}</span></p>
        </div>
        <div style={{ display: 'flex', gap: 9 }}>
          <WBtn variant="secondary" icon="dots" style={{ width: 36, padding: 0 }} />
          <WBtn variant="primary" icon="chat" onClick={() => navigate('tasks', { agentId: agent.id })}>打开对话</WBtn>
        </div>
      </div>

      <WTabs tabs={tabs} active={tab} onChange={setTab} style={{ marginBottom: 24 }} />

      {tab === 'overview' && <OverviewTab agent={agent} navigate={navigate} />}
      {tab === 'caps' && <CapabilitiesTab agent={agent} />}
      {tab === 'memory' && <MemoryTab agent={agent} />}
      {tab === 'a2a' && <A2ATab agent={agent} navigate={navigate} />}
      {tab === 'perms' && <PermissionsTab agent={agent} />}
      {tab === 'settings' && <SettingsTab agent={agent} />}
    </div>
  );
}

function SettingsTab({ agent }) {
  return (
    <div style={{ maxWidth: 640 }}>
      <WEye style={{ marginBottom: 13 }}>基本设置</WEye>
      <WCard pad={0}>
        {[['名称', agent.name], ['职责描述', agent.role], ['头像', '六边形 · ' + agent.abbr], ['基础模型', 'Claude · 公司默认']].map((r, i, arr) => (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '14px 16px', borderBottom: i < arr.length - 1 ? '1px solid var(--border)' : 0 }}>
            <span style={{ width: 100, fontSize: 12.5, color: 'var(--text-3)' }}>{r[0]}</span>
            <span style={{ flex: 1, fontSize: 13.5 }}>{r[1]}</span>
            <WBtn size="sm" variant="ghost">编辑</WBtn>
          </div>
        ))}
      </WCard>
      <div style={{ marginTop: 22 }}>
        <WEye style={{ marginBottom: 13 }}>危险操作</WEye>
        <WCard pad={16} style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          <div style={{ flex: 1 }}><div style={{ fontSize: 13, fontWeight: 600 }}>停用数字员工</div><div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 2 }}>停用后将无法接收新任务，可随时恢复。</div></div>
          <WBtn size="sm" variant="danger">停用</WBtn>
        </WCard>
      </div>
    </div>
  );
}

window.HivePages = window.HivePages || {};
window.HivePages.workspace = WorkspacePage;
