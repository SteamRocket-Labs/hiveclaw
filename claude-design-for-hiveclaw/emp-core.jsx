/* ============================================================
   Hive Prototype — Employee: Home + Digital Employees list
   ============================================================ */
const HU = window.HiveUI;
const { Icon: EIcon, Hex: EHex, Chip: EChip, Btn: EBtn, IconBtn: EIconBtn, Card: ECard, PageHead: EPageHead,
        Tabs: ETabs, EmptyState: EEmpty, Eyebrow: EEye, AGENTS: EAGENTS, ME: EME, WS: EWS, CAP_STATE: ECAP } = HU;
const { useState: euS, Fragment: EFrag } = HU;

function statusKind(s) { return s; }

/* ---------------- Agent card ---------------- */
function AgentCard({ a, navigate }) {
  return (
    <ECard hover pad={16} onClick={() => navigate('workspace', { agentId: a.id })} style={{ display: 'flex', flexDirection: 'column' }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
        <EHex size={38} bg={a.color} fs={13}>{a.abbr}</EHex>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
            <span style={{ fontWeight: 600, fontSize: 14.5, lineHeight: 1.2 }}>{a.name}</span>
            {a.alert && <span style={{ color: 'var(--honey)', display: 'flex' }} title="有待处理事项"><EIcon name="alert" size={14} /></span>}
          </div>
          <div style={{ fontSize: 12.5, color: 'var(--text-2)', marginTop: 3 }}>{a.role}</div>
        </div>
        <span style={{ color: 'var(--text-4)', display: 'flex' }}><EIcon name="chevron" size={16} /></span>
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-3)', margin: '13px 0 0', lineHeight: 1.5, display: 'flex', gap: 6 }}>
        <span style={{ color: a.status === 'danger' ? 'var(--danger)' : a.status === 'ok' ? 'var(--ok)' : 'var(--text-3)', marginTop: 1, flex: '0 0 auto' }}><EIcon name={a.status === 'ok' ? 'play' : a.status === 'danger' ? 'alert' : 'clock'} size={12} /></span>
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{a.last}</span>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginTop: 13, paddingTop: 12, borderTop: '1px solid var(--border)' }}>
        <EChip kind={statusKind(a.status)} dot>{a.statusLabel}</EChip>
        <EChip kind="idle" style={{ gap: 4 }}><EIcon name={a.scopeIcon} size={11} />{a.scope.replace('可见', '')}</EChip>
        <span style={{ marginLeft: 'auto', fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-4)' }}>{a.perm === 'manage' ? '可管理' : '可使用'}</span>
      </div>
    </ECard>
  );
}

/* ---------------- HOME ---------------- */
function HomePage({ navigate }) {
  const hour = 14;
  const greet = hour < 12 ? '早上好' : hour < 18 ? '下午好' : '晚上好';
  const attention = EAGENTS.filter(a => a.alert);
  const running = EAGENTS.filter(a => a.status === 'ok' || a.status === 'purple');

  const quick = [
    ['plus', '创建数字员工', '空白 / 模板 / 自然语言', 'create', {}, 'honey'],
    ['chat', '交办新任务', '向数字员工下达工作', 'tasks', {}, 'plain'],
    ['flow', '从成功任务建流程', '把一次任务沉淀为自动化', 'automations', {}, 'plain'],
    ['box', '浏览公司资产库', '标准模板、技能、专家角色', 'admin-assets', {}, 'plain'],
  ];

  return (
    <div style={{ padding: '34px 40px 60px', maxWidth: 1180, margin: '0 auto' }}>
      <div style={{ marginBottom: 26 }}>
        <div className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)', marginBottom: 8 }}>{EWS.name} · 周五 6 月 7 日</div>
        <h1 style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 32, margin: 0, letterSpacing: '-.01em' }}>{greet}，林见 👋</h1>
        <p style={{ fontSize: 14.5, color: 'var(--text-2)', margin: '8px 0 0' }}>你有 <b style={{ color: 'var(--text-1)' }}>2 件事</b>需要确认，<b style={{ color: 'var(--text-1)' }}>{running.length} 个数字员工</b>正在工作。</p>
      </div>

      {/* quick actions */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 12, marginBottom: 30 }}>
        {quick.map(([ic, t, s, pg, pm, kind], i) => (
          <ECard key={i} hover pad={16} onClick={() => navigate(pg, pm)}>
            <div style={{ width: 34, height: 34, borderRadius: 9, display: 'flex', alignItems: 'center', justifyContent: 'center', marginBottom: 12, background: kind === 'honey' ? 'var(--honey-soft)' : 'var(--bg-sunk)', color: kind === 'honey' ? 'var(--honey-deep)' : 'var(--text-1)' }}>
              <EIcon name={ic} size={18} />
            </div>
            <div style={{ fontWeight: 600, fontSize: 13.5 }}>{t}</div>
            <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 3, lineHeight: 1.4 }}>{s}</div>
          </ECard>
        ))}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1.55fr 1fr', gap: 24 }}>
        {/* left: attention + running */}
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 13 }}>
            <EEye>需要你确认 · Needs you</EEye>
            <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: '#fff', background: 'var(--honey)', borderRadius: 999, padding: '1px 7px' }}>{attention.length}</span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginBottom: 28 }}>
            {attention.map((a) => (
              <ECard key={a.id} hover pad={15} onClick={() => navigate(a.status === 'warn' ? 'plan' : 'workspace', { agentId: a.id })}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  <EHex size={36} bg={a.color} fs={13}>{a.abbr}</EHex>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <span style={{ fontWeight: 600, fontSize: 14, whiteSpace: 'nowrap' }}>{a.name}</span>
                      <EChip kind={a.status} dot>{a.statusLabel}</EChip>
                    </div>
                    <div style={{ fontSize: 12.5, color: 'var(--text-2)', marginTop: 4 }}>{a.last}</div>
                  </div>
                  <EBtn size="sm" variant={a.status === 'warn' ? 'honey' : 'secondary'}>{a.status === 'warn' ? '查看计划' : '查看'}</EBtn>
                </div>
              </ECard>
            ))}
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 13 }}>
            <EEye>正在工作 · In progress</EEye>
          </div>
          <ECard pad={0}>
            {running.map((a, i) => (
              <div key={a.id} onClick={() => navigate('workspace', { agentId: a.id })} style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '13px 16px', borderBottom: i < running.length - 1 ? '1px solid var(--border)' : 0, cursor: 'pointer' }}
                onMouseEnter={(e) => e.currentTarget.style.background = 'var(--surface-2)'} onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}>
                <EHex size={30} bg={a.color} fs={11}>{a.abbr}</EHex>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13.5, fontWeight: 500 }}>{a.name}</div>
                  <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{a.last}</div>
                </div>
                {a.status === 'purple' && <EChip kind="purple" dot>A2A</EChip>}
                <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)', whiteSpace: 'nowrap', flex: '0 0 auto' }}>{a.when}</span>
              </div>
            ))}
          </ECard>
        </div>

        {/* right: usage + activity */}
        <div>
          <EEye style={{ marginBottom: 13 }}>本月用量 · This month</EEye>
          <ECard pad={18} style={{ marginBottom: 24 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 4 }}>
              <span style={{ fontFamily: 'var(--display)', fontSize: 26, fontWeight: 600 }}>1,284</span>
              <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>/ 2,000 任务</span>
            </div>
            <div style={{ height: 7, borderRadius: 999, background: 'var(--bg-sunk)', overflow: 'hidden', margin: '8px 0 14px' }}>
              <div style={{ width: '64%', height: '100%', background: 'var(--honey)', borderRadius: 999 }} />
            </div>
            {[['模型预算', '¥ 3,210 / ¥ 5,000', 64], ['A2A 委派', '212 次', 40], ['活跃数字员工', '6 个', 100]].map(([k, v, w], i) => (
              <div key={i} style={{ display: 'flex', justifyContent: 'space-between', gap: 10, fontSize: 12.5, padding: '7px 0', borderTop: i ? '1px solid var(--border)' : 0 }}>
                <span style={{ color: 'var(--text-2)', whiteSpace: 'nowrap' }}>{k}</span><span className="mono" style={{ color: 'var(--text-1)', fontSize: 11.5, whiteSpace: 'nowrap' }}>{v}</span>
              </div>
            ))}
          </ECard>

          <EEye style={{ marginBottom: 13 }}>最近动态 · Activity</EEye>
          <ECard pad={0}>
            {[
              ['Atlas', '完成市场摘要并保存产物', '2 小时前', 'oklch(0.60 0.13 250)', 'DR'],
              ['Ledger', '生成 8 月对账计划', '12 分钟前', 'oklch(0.62 0.12 150)', 'FC'],
              ['Relay', '委派 Warden 执行安全检查', '刚刚', 'oklch(0.56 0.11 300)', 'OP'],
              ['你', '将「竞品扫描」保存为流程', '昨天', EME.color, 'JL'],
            ].map((r, i, arr) => (
              <div key={i} style={{ display: 'flex', gap: 11, padding: '12px 16px', borderBottom: i < arr.length - 1 ? '1px solid var(--border)' : 0 }}>
                <EHex size={26} bg={r[3]} fs={10}>{r[4]}</EHex>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 12.5, lineHeight: 1.45 }}><b style={{ fontWeight: 600 }}>{r[0]}</b> <span style={{ color: 'var(--text-2)' }}>{r[1]}</span></div>
                  <div className="mono" style={{ fontSize: 10, color: 'var(--text-4)', marginTop: 2 }}>{r[2]}</div>
                </div>
              </div>
            ))}
          </ECard>
        </div>
      </div>
    </div>
  );
}

/* ---------------- DIGITAL EMPLOYEES LIST ---------------- */
function EmployeesPage({ navigate, params }) {
  const [tab, setTab] = euS('all');
  const [empty, setEmpty] = euS(false);
  const tabs = [
    { id: 'all', label: '全部', count: EAGENTS.length },
    { id: 'mine', label: '我创建的', count: EAGENTS.filter(a => a.owner === '我').length },
    { id: 'rec', label: '公司推荐', count: 2 },
    { id: 'shared', label: '协作中', count: EAGENTS.filter(a => a.perm === 'use').length },
  ];
  let list = EAGENTS;
  if (tab === 'mine') list = EAGENTS.filter(a => a.owner === '我');
  if (tab === 'shared') list = EAGENTS.filter(a => a.perm === 'use');
  if (tab === 'rec') list = EAGENTS.filter(a => a.owner === '公司标准' || a.owner === '陈航 Hang');

  return (
    <div style={{ padding: '30px 40px 60px', maxWidth: 1180, margin: '0 auto' }}>
      <EPageHead
        crumbs={['我的工作区', 'Digital Employees']}
        title="数字员工"
        meta={`${EAGENTS.length} active · 2 shared`}
        sub="你的 AI 工作伙伴。创建、配置、交办任务，并查看它们的进度与产物。"
        actions={<>
          <EBtn variant="secondary" icon="template" onClick={() => navigate('create')}>从模板</EBtn>
          <EBtn variant="primary" icon="plus" onClick={() => navigate('create')}>创建数字员工</EBtn>
        </>}
      />

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 18 }}>
        <ETabs tabs={tabs} active={tab} onChange={setTab} style={{ border: 0 }} />
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, height: 32, padding: '0 11px', background: 'var(--surface)', border: '1px solid var(--border-2)', borderRadius: 7, color: 'var(--text-3)', width: 190 }}>
            <EIcon name="search" size={14} /><span style={{ fontSize: 12.5 }}>搜索…</span>
          </div>
          {['状态', '能力', '可见范围'].map((f) => (
            <button key={f} style={{ height: 32, padding: '0 11px', background: 'var(--surface)', border: '1px solid var(--border-2)', borderRadius: 7, fontFamily: 'var(--sans)', fontSize: 12.5, color: 'var(--text-2)', display: 'inline-flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
              {f}<span style={{ color: 'var(--text-4)', display: 'flex' }}><EIcon name="chevronDown" size={13} /></span>
            </button>
          ))}
          <EIconBtn name="filter" title="切换空状态演示" onClick={() => setEmpty(!empty)} active={empty} />
        </div>
      </div>

      {empty ? (
        <ECard pad={0}>
          <EEmpty title="还没有数字员工" sub="创建你的第一个 AI 工作伙伴 —— 从空白开始、套用公司模板，或用自然语言描述你想要什么。"
            action={<EBtn variant="honey" icon="plus" onClick={() => navigate('create')}>创建数字员工</EBtn>} />
        </ECard>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 14 }}>
          {list.map((a) => <AgentCard key={a.id} a={a} navigate={navigate} />)}
          <ECard hover pad={16} onClick={() => navigate('create')} style={{ borderStyle: 'dashed', display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: 150, color: 'var(--text-3)', flexDirection: 'column', gap: 10 }}>
            <div style={{ width: 38, height: 38, borderRadius: 10, background: 'var(--bg-sunk)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><EIcon name="plus" size={20} /></div>
            <span style={{ fontSize: 13, fontWeight: 500 }}>创建数字员工</span>
          </ECard>
        </div>
      )}
    </div>
  );
}

window.HivePages = window.HivePages || {};
window.HivePages.home = HomePage;
window.HivePages.employees = EmployeesPage;
window.HiveAgentCard = AgentCard;
