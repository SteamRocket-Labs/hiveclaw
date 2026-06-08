/* ============================================================
   Hive Prototype — Control Plane (admin) pages
   ============================================================ */
const AU = window.HiveUI;
const { Icon: AIcon, Hex: AHex, Chip: AChip, Btn: ABtn, IconBtn: AIconBtn, Card: ACard, Tabs: ATabs,
        PageHead: APageHead, Eyebrow: AEye, Toggle: AToggle, EmptyState: AEmpty, AGENTS: AAGENTS } = AU;
const { useState: auS, Fragment: AFrag } = AU;

/* ---------- shared metric tile ---------- */
function Metric({ label, value, sub, trend }) {
  return (
    <ACard pad={16}>
      <div className="eyebrow" style={{ marginBottom: 10 }}>{label}</div>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
        <span style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 26 }}>{value}</span>
        {trend && <span className="mono" style={{ fontSize: 11, color: trend[0] === '+' ? 'var(--ok)' : 'var(--text-3)' }}>{trend}</span>}
      </div>
      {sub && <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 4 }}>{sub}</div>}
    </ACard>
  );
}

/* ---------------- OVERVIEW ---------------- */
function AdminOverview({ navigate }) {
  return (
    <div style={{ padding: '30px 40px 60px', maxWidth: 1180, margin: '0 auto' }}>
      <APageHead crumbs={['公司控制中台', 'Overview']} title="公司总览"
        icon={<span style={{ width: 30, height: 30, borderRadius: 8, background: 'var(--text-1)', color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><AIcon name="shield" size={17} /></span>}
        sub="Acme Inc. · Enterprise · 治理整个 workspace 的数字员工、能力、预算与风险。" />

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 12, marginBottom: 28 }}>
        <Metric label="成员" value="142" sub="6 个 Group" trend="+8" />
        <Metric label="数字员工" value="38" sub="34 活跃 · 2 异常" />
        <Metric label="本月任务" value="9,420" trend="+12%" sub="A2A 委派 1,210 次" />
        <Metric label="模型预算" value="64%" sub="¥ 32k / ¥ 50k" />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1.4fr 1fr', gap: 24 }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 13 }}>
            <AEye>需要关注 · Needs attention</AEye><span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: '#fff', background: 'var(--danger)', borderRadius: 999, padding: '1px 7px' }}>3</span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginBottom: 26 }}>
            {[
              ['danger', 'Warden 工具连接失败', '安全审查 · 漏洞库超时', 'admin-governance'],
              ['warn', '4 项待审批事项', '渠道外发 · 预算超额申请', 'admin-approvals'],
              ['info', '2 个候选资产待审核', '员工提交的流程模板', 'admin-assets'],
            ].map((r, i) => (
              <ACard key={i} hover pad={14} onClick={() => navigate(r[3])}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  <span style={{ width: 8, height: 8, borderRadius: '50%', background: r[0] === 'danger' ? 'var(--danger)' : r[0] === 'warn' ? 'var(--honey)' : 'var(--info)' }} />
                  <div style={{ flex: 1 }}><div style={{ fontSize: 13.5, fontWeight: 600 }}>{r[1]}</div><div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 2 }}>{r[2]}</div></div>
                  <AIcon name="chevron" size={16} style={{ color: 'var(--text-4)' }} />
                </div>
              </ACard>
            ))}
          </div>
          <AEye style={{ marginBottom: 13 }}>能力开放概览</AEye>
          <ACard pad={18}>
            {[['工具', 12, 18], ['技能', 8, 12], ['渠道', 4, 6], ['专家角色', 9, 14]].map((c, i) => (
              <div key={i} style={{ marginBottom: i < 3 ? 14 : 0 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12.5, marginBottom: 6 }}><span style={{ fontWeight: 500 }}>{c[0]}</span><span className="mono" style={{ fontSize: 11, color: 'var(--text-3)', whiteSpace: 'nowrap' }}>{c[1]} / {c[2]} 已开放</span></div>
                <div style={{ height: 6, borderRadius: 999, background: 'var(--bg-sunk)', overflow: 'hidden' }}><div style={{ width: `${c[1] / c[2] * 100}%`, height: '100%', background: 'var(--honey)', borderRadius: 999 }} /></div>
              </div>
            ))}
          </ACard>
        </div>
        <div>
          <AEye style={{ marginBottom: 13 }}>审计动态 · Audit</AEye>
          <ACard pad={0}>
            {[
              ['周岚 Lan', '启用了渠道 · 飞书', '10 分钟前', 'config'],
              ['Ledger', '外发对账报告（已审批）', '32 分钟前', 'action'],
              ['林见 Jen', '提交候选资产「竞品对标」', '1 小时前', 'asset'],
              ['管理员', '调整研究组工具范围', '2 小时前', 'config'],
              ['Warden', '工具连接失败', '1 小时前', 'risk'],
            ].map((r, i, arr) => (
              <div key={i} style={{ display: 'flex', gap: 11, padding: '13px 16px', borderBottom: i < arr.length - 1 ? '1px solid var(--border)' : 0 }}>
                <span style={{ width: 7, height: 7, borderRadius: '50%', marginTop: 6, flex: '0 0 auto', background: r[3] === 'risk' ? 'var(--danger)' : r[3] === 'asset' ? 'var(--info)' : 'var(--text-4)' }} />
                <div style={{ flex: 1 }}><div style={{ fontSize: 12.5, lineHeight: 1.45 }}><b style={{ fontWeight: 600 }}>{r[0]}</b> <span style={{ color: 'var(--text-2)' }}>{r[1]}</span></div><div className="mono" style={{ fontSize: 10, color: 'var(--text-4)', marginTop: 2 }}>{r[2]}</div></div>
              </div>
            ))}
            <div onClick={() => navigate('admin-audit')} style={{ padding: '11px 16px', textAlign: 'center', fontSize: 12.5, color: 'var(--honey-deep)', cursor: 'pointer', fontWeight: 500 }}>查看完整审计记录</div>
          </ACard>
        </div>
      </div>
    </div>
  );
}

/* ---------------- MEMBERS & ORG ---------------- */
function AdminMembers({ navigate }) {
  const [tab, setTab] = auS('members');
  const members = [
    ['林见 Jen Lin', '产品', '管理员', 'oklch(0.55 0.11 285)', '研究组'],
    ['陈航 Hang Chen', '运营', '成员', 'oklch(0.6 0.12 145)', '运营组'],
    ['周岚 Lan Zhou', '财务', '成员', 'oklch(0.6 0.12 25)', '财务组'],
    ['吴桐 Tong Wu', '研发', '成员', 'oklch(0.58 0.11 240)', '研发组'],
    ['苏晴 Qing Su', 'HR', '成员', 'oklch(0.6 0.1 320)', '人事组'],
  ];
  const groups = [['研究组', 8, 'DR · WR'], ['财务组', 5, 'FC'], ['运营组', 12, 'OP'], ['研发组', 24, '—'], ['人事组', 4, 'HR'], ['销售组', 18, '—']];
  return (
    <div style={{ padding: '30px 40px 60px', maxWidth: 1180, margin: '0 auto' }}>
      <APageHead crumbs={['公司控制中台', 'Members & Org']} title="成员与组织" meta="142 成员 · 6 Group"
        sub="管理 workspace 成员、部门分组、邀请码与角色权限。"
        actions={<><ABtn variant="secondary" icon="link">邀请码</ABtn><ABtn variant="primary" icon="plus">邀请成员</ABtn></>} />
      <ATabs tabs={[{ id: 'members', label: '成员', count: 142 }, { id: 'groups', label: 'Group / 部门', count: 6 }, { id: 'invites', label: '邀请码' }]} active={tab} onChange={setTab} style={{ marginBottom: 18 }} />
      {tab === 'members' && (
        <ACard pad={0}>
          <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr 1fr 1fr', padding: '11px 18px', borderBottom: '1px solid var(--border)', background: 'var(--surface-2)' }}>
            {['成员', '部门', 'Group', '角色'].map((h, i) => <span key={i} className="eyebrow">{h}</span>)}
          </div>
          {members.map((m, i) => (
            <div key={i} style={{ display: 'grid', gridTemplateColumns: '2fr 1fr 1fr 1fr', alignItems: 'center', padding: '12px 18px', borderBottom: i < members.length - 1 ? '1px solid var(--border)' : 0 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 11 }}><AHex size={30} bg={m[3]} fs={11}>{m[0].slice(0, 2)}</AHex><span style={{ fontSize: 13.5, fontWeight: 500, whiteSpace: 'nowrap' }}>{m[0]}</span></div>
              <span style={{ fontSize: 13, color: 'var(--text-2)' }}>{m[1]}</span>
              <span style={{ fontSize: 13, color: 'var(--text-2)' }}>{m[4]}</span>
              <span><AChip kind={m[2] === '管理员' ? 'solid' : 'idle'}>{m[2]}</AChip></span>
            </div>
          ))}
        </ACard>
      )}
      {tab === 'groups' && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 13 }}>
          {groups.map((g, i) => (
            <ACard key={i} pad={16} hover>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
                <span style={{ width: 34, height: 34, borderRadius: 9, background: 'var(--bg-sunk)', color: 'var(--text-2)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><AIcon name="users" size={17} /></span>
                <div><div style={{ fontSize: 14, fontWeight: 600 }}>{g[0]}</div><div className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>{g[1]} 名成员</div></div>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', paddingTop: 11, borderTop: '1px solid var(--border)' }}>
                <span style={{ fontSize: 11.5, color: 'var(--text-3)' }}>数字员工</span><span className="mono" style={{ fontSize: 11, color: 'var(--text-2)' }}>{g[2]}</span>
              </div>
            </ACard>
          ))}
        </div>
      )}
      {tab === 'invites' && (
        <div style={{ maxWidth: 620 }}>
          <ACard pad={18} style={{ marginBottom: 14 }}>
            <div className="eyebrow" style={{ marginBottom: 10 }}>当前邀请码</div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <div className="mono" style={{ flex: 1, fontSize: 18, fontWeight: 600, letterSpacing: '.1em', background: 'var(--bg-sunk)', borderRadius: 9, padding: '12px 16px' }}>HIVE-AC-2026</div>
              <ABtn variant="secondary" icon="copy">复制</ABtn>
            </div>
            <div style={{ display: 'flex', gap: 18, marginTop: 14 }}>
              {[['已使用', '34 / 50'], ['有效期', '30 天'], ['默认 Group', '未指定']].map((x, i) => (
                <div key={i}><div className="eyebrow" style={{ marginBottom: 3 }}>{x[0]}</div><div style={{ fontSize: 13, fontWeight: 500 }}>{x[1]}</div></div>
              ))}
            </div>
          </ACard>
          <ABtn variant="primary" icon="plus">生成新邀请码</ABtn>
        </div>
      )}
    </div>
  );
}

/* ---------------- GOVERNANCE ---------------- */
function AdminGovernance({ navigate }) {
  const rows = AAGENTS.map(a => [a.abbr, a.name, a.color, a.owner, a.status, a.statusLabel, a.scope, a.id, a.alert]);
  return (
    <div style={{ padding: '30px 40px 60px', maxWidth: 1180, margin: '0 auto' }}>
      <APageHead crumbs={['公司控制中台', 'Governance']} title="数字员工治理" meta="38 个 · 2 异常"
        sub="查看每个数字员工的负责人、状态、能力与风险。调整可见范围、管理员，或停用异常数字员工。"
        actions={<ABtn variant="secondary" icon="filter">筛选风险</ABtn>} />
      <ACard pad={0}>
        <div style={{ display: 'grid', gridTemplateColumns: '2fr 1.2fr 1fr 1.2fr 0.6fr', padding: '11px 18px', borderBottom: '1px solid var(--border)', background: 'var(--surface-2)' }}>
          {['数字员工', '负责人', '状态', '可见范围', ''].map((h, i) => <span key={i} className="eyebrow">{h}</span>)}
        </div>
        {rows.map((r, i) => (
          <div key={i} onClick={() => navigate('workspace', { agentId: r[7] })} style={{ display: 'grid', gridTemplateColumns: '2fr 1.2fr 1fr 1.2fr 0.6fr', alignItems: 'center', padding: '13px 18px', borderBottom: i < rows.length - 1 ? '1px solid var(--border)' : 0, cursor: 'pointer' }}
            onMouseEnter={(e) => e.currentTarget.style.background = 'var(--surface-2)'} onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 11 }}><AHex size={30} bg={r[2]} fs={11}>{r[0]}</AHex><span style={{ fontSize: 13.5, fontWeight: 500, whiteSpace: 'nowrap' }}>{r[1]}</span>{r[8] && <span style={{ color: 'var(--honey)', display: 'flex' }}><AIcon name="alert" size={13} /></span>}</div>
            <span style={{ fontSize: 13, color: 'var(--text-2)' }}>{r[3]}</span>
            <span><AChip kind={r[4]} dot>{r[5]}</AChip></span>
            <span style={{ fontSize: 12.5, color: 'var(--text-2)' }}>{r[6]}</span>
            <span style={{ display: 'flex', justifyContent: 'flex-end' }}><AIconBtn name="dots" /></span>
          </div>
        ))}
      </ACard>
    </div>
  );
}

/* ---------------- CAPABILITIES & TOOLS ---------------- */
function AdminCapabilities({ navigate }) {
  const [tab, setTab] = auS('tools');
  const tools = [
    ['网页检索', 'globe', true, '研究组 · 运营组'], ['代码执行', 'gear', true, '研发组'], ['漏洞库', 'shield', false, '安全 · 失败'],
    ['数据仓库', 'box', true, '财务组 · 研究组'], ['邮件发送', 'plug', true, '需审批'], ['CRM 连接', 'users', false, '未开放'],
  ];
  return (
    <div style={{ padding: '30px 40px 60px', maxWidth: 1180, margin: '0 auto' }}>
      <APageHead crumbs={['公司控制中台', 'Capabilities']} title="能力与工具"
        sub="配置公司模型、工具、渠道与技能。设置哪些能力开放给哪些 Group 或数字员工，哪些需要审批。"
        actions={<ABtn variant="primary" icon="plus">接入能力</ABtn>} />
      <ATabs tabs={[{ id: 'tools', label: '工具', count: 18 }, { id: 'skills', label: '技能', count: 12 }, { id: 'experts', label: '专家角色', count: 14 }]} active={tab} onChange={setTab} style={{ marginBottom: 18 }} />
      <ACard pad={0}>
        {tools.map((t, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '14px 18px', borderBottom: i < tools.length - 1 ? '1px solid var(--border)' : 0 }}>
            <span style={{ width: 34, height: 34, borderRadius: 9, background: t[2] ? 'var(--honey-soft)' : 'var(--bg-sunk)', color: t[2] ? 'var(--honey-deep)' : 'var(--text-3)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><AIcon name={t[1]} size={17} /></span>
            <div style={{ flex: 1 }}><div style={{ fontSize: 13.5, fontWeight: 600 }}>{t[0]}</div><div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 2 }}>开放范围：{t[3]}</div></div>
            {t[3].includes('失败') && <AChip kind="danger" dot>连接失败</AChip>}
            {t[3].includes('审批') && <AChip kind="info">需审批</AChip>}
            <AToggle on={t[2]} onChange={() => {}} />
          </div>
        ))}
      </ACard>
    </div>
  );
}

/* ---------------- APPROVAL CENTER ---------------- */
function AdminApprovals({ navigate }) {
  const [items, setItems] = auS([
    { id: 1, who: 'Ledger', abbr: 'FC', color: 'oklch(0.62 0.12 150)', action: '向外部邮箱发送 8 月对账报告', risk: '中', scope: '财务数据外发', from: '周岚 Lan' },
    { id: 2, who: '周岚 Lan', abbr: 'LZ', color: 'oklch(0.6 0.12 25)', action: '申请开通「邮件发送」渠道', risk: '中', scope: 'Pace 数字员工', from: '财务组' },
    { id: 3, who: 'Relay', abbr: 'OP', color: 'oklch(0.56 0.11 300)', action: '预算超额 +¥2,000 申请', risk: '低', scope: '运营自动化', from: '陈航 Hang' },
    { id: 4, who: 'Warden', abbr: 'SE', color: 'oklch(0.52 0.05 60)', action: '接入外部漏洞库 API', risk: '高', scope: '全公司安全', from: '系统' },
  ]);
  const act = (id) => setItems(its => its.filter(i => i.id !== id));
  return (
    <div style={{ padding: '30px 40px 60px', maxWidth: 980, margin: '0 auto' }}>
      <APageHead crumbs={['公司控制中台', 'Approvals']} title="审批中心" meta={`${items.length} 待处理`}
        sub="集中处理公司级待审批事项：渠道开通、数据外发、预算调整、外部接入。" />
      {items.length === 0 ? <ACard pad={0}><AEmpty title="审批已清空" sub="所有待审批事项都已处理。新的请求会出现在这里。" /></ACard> : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 13 }}>
          {items.map((it) => (
            <ACard key={it.id} pad={0} style={{ overflow: 'hidden' }}>
              <div style={{ padding: '15px 18px', display: 'flex', alignItems: 'flex-start', gap: 13 }}>
                <AHex size={36} bg={it.color} fs={12}>{it.abbr}</AHex>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                    <span style={{ fontSize: 14, fontWeight: 600 }}>{it.action}</span>
                    <AChip kind={it.risk === '高' ? 'danger' : it.risk === '中' ? 'warn' : 'idle'} dot>{it.risk}风险</AChip>
                  </div>
                  <div style={{ display: 'flex', gap: 18 }}>
                    {[['影响范围', it.scope], ['发起', it.from]].map((m, i) => (
                      <div key={i} style={{ whiteSpace: 'nowrap' }}><span className="eyebrow">{m[0]} · </span><span style={{ fontSize: 12.5, color: 'var(--text-2)' }}>{m[1]}</span></div>
                    ))}
                  </div>
                </div>
              </div>
              <div style={{ display: 'flex', gap: 8, padding: '12px 18px', borderTop: '1px solid var(--border)', background: 'var(--surface-2)' }}>
                <ABtn size="sm" variant="primary" icon="checkPlain" onClick={() => act(it.id)}>批准</ABtn>
                <ABtn size="sm" variant="secondary" onClick={() => act(it.id)}>要求修改</ABtn>
                <ABtn size="sm" variant="ghost" style={{ marginLeft: 'auto', color: 'var(--text-3)' }} onClick={() => act(it.id)}>拒绝</ABtn>
              </div>
            </ACard>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---------------- ASSETS LIBRARY ---------------- */
function AdminAssets({ navigate }) {
  const [tab, setTab] = auS('candidates');
  const candidates = [
    ['竞品对标报告 · Q2', '流程', '林见 Jen', '已运行 3 次', 'oklch(0.55 0.11 285)'],
    ['新人 onboarding 排期', '流程', '苏晴 Qing', '已运行 6 次', 'oklch(0.6 0.1 320)'],
  ];
  const standard = [
    ['标准调研助理', 'template', '模板', '12 次复用'], ['财务对账', 'template', '模板', '8 次复用'],
    ['竞品分析师', 'users', '专家角色', '可用'], ['事实核查', 'users', '专家角色', '可用'],
    ['月度报告流程', 'flow', '工作流', '稳定'], ['数据清洗', 'bolt', '技能', '已发布'],
  ];
  return (
    <div style={{ padding: '30px 40px 60px', maxWidth: 1180, margin: '0 auto' }}>
      <APageHead crumbs={['公司控制中台', 'Assets']} title="自动化与资产库"
        sub="公司标准模板、技能、专家角色与流程的资产闭环。审核员工提交的候选资产，发布为公司标准。"
        actions={<ABtn variant="primary" icon="plus">发布资产</ABtn>} />
      <ATabs tabs={[{ id: 'candidates', label: '候选审核', count: 2 }, { id: 'standard', label: '公司标准资产', count: 14 }]} active={tab} onChange={setTab} style={{ marginBottom: 20 }} />
      {tab === 'candidates' ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 13 }}>
          {candidates.map((c, i) => (
            <ACard key={i} pad={0} style={{ overflow: 'hidden' }}>
              <div style={{ padding: '16px 18px', display: 'flex', alignItems: 'center', gap: 14 }}>
                <span style={{ width: 38, height: 38, borderRadius: 9, background: 'var(--bg-sunk)', color: 'var(--text-2)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><AIcon name="flow" size={18} /></span>
                <div style={{ flex: 1 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}><span style={{ fontSize: 14.5, fontWeight: 600, whiteSpace: 'nowrap' }}>{c[0]}</span><AChip kind="info">候选 · 待审核</AChip></div>
                  <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginTop: 4, whiteSpace: 'nowrap' }}>{c[1]} · 来源 {c[2]} · {c[3]}</div>
                </div>
                <div style={{ display: 'flex', gap: 8 }}>
                  <ABtn size="sm" variant="ghost" style={{ color: 'var(--text-3)' }}>拒绝</ABtn>
                  <ABtn size="sm" variant="secondary">预览</ABtn>
                  <ABtn size="sm" variant="primary" icon="checkPlain">发布为标准</ABtn>
                </div>
              </div>
            </ACard>
          ))}
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 13 }}>
          {standard.map((s, i) => (
            <ACard key={i} pad={16} hover>
              <div style={{ display: 'flex', alignItems: 'center', gap: 11, marginBottom: 12 }}>
                <span style={{ width: 34, height: 34, borderRadius: 9, background: 'var(--bg-sunk)', color: 'var(--text-2)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><AIcon name={s[1]} size={17} /></span>
                <AChip kind="idle">{s[2]}</AChip>
              </div>
              <div style={{ fontSize: 14, fontWeight: 600 }}>{s[0]}</div>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 10, paddingTop: 10, borderTop: '1px solid var(--border)' }}>
                <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>{s[3]}</span>
                <AChip kind="ok" dot>已启用</AChip>
              </div>
            </ACard>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---------------- light pages: budget / memory / channels / audit ---------------- */
function AdminBudget() {
  return (
    <div style={{ padding: '30px 40px 60px', maxWidth: 1180, margin: '0 auto' }}>
      <APageHead crumbs={['公司控制中台', 'Models & Budget']} title="模型与预算" sub="管理公司模型、用量配额与预算分配。" actions={<ABtn variant="primary" icon="plus">调整预算</ABtn>} />
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 12, marginBottom: 24 }}>
        <Metric label="本月支出" value="¥ 32k" sub="/ ¥ 50k 预算" trend="64%" />
        <Metric label="任务总量" value="9,420" trend="+12%" />
        <Metric label="A2A 委派" value="1,210" sub="占比 13%" />
      </div>
      <AEye style={{ marginBottom: 13 }}>按 Group 预算</AEye>
      <ACard pad={0}>
        {[['研究组', 12, 18], ['运营组', 8, 12], ['财务组', 5, 8], ['研发组', 7, 12]].map((g, i, arr) => (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 16, padding: '15px 18px', borderBottom: i < arr.length - 1 ? '1px solid var(--border)' : 0 }}>
            <span style={{ width: 120, fontSize: 13.5, fontWeight: 500 }}>{g[0]}</span>
            <div style={{ flex: 1, height: 7, borderRadius: 999, background: 'var(--bg-sunk)', overflow: 'hidden' }}><div style={{ width: `${g[1] / g[2] * 100}%`, height: '100%', background: g[1] / g[2] > 0.8 ? 'var(--danger)' : 'var(--honey)', borderRadius: 999 }} /></div>
            <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-2)', width: 110, textAlign: 'right' }}>¥{g[1]}k / ¥{g[2]}k</span>
          </div>
        ))}
      </ACard>
    </div>
  );
}
function AdminChannels() {
  const ch = [['飞书 Feishu', true, '研究 · 运营 · 财务'], ['Slack', true, '研发组'], ['企业微信', false, '未开放'], ['邮件 Email', true, '需审批'], ['Microsoft Teams', false, '未开放']];
  return (
    <div style={{ padding: '30px 40px 60px', maxWidth: 900, margin: '0 auto' }}>
      <APageHead crumbs={['公司控制中台', 'Channels']} title="渠道连接" sub="管理数字员工可工作的渠道：飞书、Slack、企业微信、邮件等。" actions={<ABtn variant="primary" icon="plus">连接渠道</ABtn>} />
      <ACard pad={0}>
        {ch.map((c, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '15px 18px', borderBottom: i < ch.length - 1 ? '1px solid var(--border)' : 0 }}>
            <span style={{ width: 36, height: 36, borderRadius: 9, background: c[1] ? 'var(--honey-soft)' : 'var(--bg-sunk)', color: c[1] ? 'var(--honey-deep)' : 'var(--text-3)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><AIcon name="plug" size={17} /></span>
            <div style={{ flex: 1 }}><div style={{ fontSize: 13.5, fontWeight: 600 }}>{c[0]}</div><div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 2 }}>{c[2]}</div></div>
            {c[2].includes('审批') && <AChip kind="info">需审批</AChip>}
            <AToggle on={c[1]} onChange={() => {}} />
          </div>
        ))}
      </ACard>
    </div>
  );
}
function AdminMemoryGov() {
  return (
    <div style={{ padding: '30px 40px 60px', maxWidth: 1180, margin: '0 auto' }}>
      <APageHead crumbs={['公司控制中台', 'Memory Gov.']} title="记忆治理" sub="管理公司级记忆规则与共享知识，控制敏感信息的可见范围。" actions={<ABtn variant="primary" icon="plus">共享知识</ABtn>} />
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 13 }}>
        {[['产品策略库', 'doc', '全员可读', 'ok'], ['客户档案', 'users', '销售组 · 敏感', 'warn'], ['竞品情报', 'box', '研究组', 'ok'], ['财务数据', 'coins', '财务组 · 敏感', 'warn'], ['品牌规范', 'star', '全员只读', 'ok'], ['合规手册', 'shield', '全员只读', 'ok']].map((k, i) => (
          <ACard key={i} pad={16} hover>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 11 }}>
              <span style={{ width: 34, height: 34, borderRadius: 9, background: 'var(--bg-sunk)', color: 'var(--text-2)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><AIcon name={k[1]} size={17} /></span>
              <span style={{ fontSize: 14, fontWeight: 600 }}>{k[0]}</span>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', paddingTop: 10, borderTop: '1px solid var(--border)' }}>
              <span style={{ fontSize: 12, color: 'var(--text-2)' }}>{k[2]}</span>
              {k[3] === 'warn' && <span style={{ color: 'var(--honey)', display: 'flex' }}><AIcon name="lock" size={14} /></span>}
            </div>
          </ACard>
        ))}
      </div>
    </div>
  );
}
function AdminAudit() {
  const logs = [
    ['周岚 Lan', '启用渠道 · 飞书', 'config', '今天 14:20'], ['Ledger', '外发对账报告', 'action', '今天 13:48', '已审批'],
    ['林见 Jen', '提交候选资产「竞品对标」', 'asset', '今天 13:10'], ['管理员', '调整研究组工具范围', 'config', '今天 12:30'],
    ['Warden', '工具连接失败', 'risk', '今天 13:00'], ['Relay', '委派 Warden 安全检查', 'a2a', '今天 11:42'],
    ['苏晴 Qing', '邀请新成员 ×3', 'config', '昨天 17:20'], ['Atlas', '调用外部检索工具', 'action', '昨天 16:05'],
  ];
  const km = { config: ['idle', '配置'], action: ['info', '动作'], asset: ['purple', '资产'], risk: ['danger', '风险'], a2a: ['purple', 'A2A'] };
  return (
    <div style={{ padding: '30px 40px 60px', maxWidth: 980, margin: '0 auto' }}>
      <APageHead crumbs={['公司控制中台', 'Audit Log']} title="审计记录" sub="workspace 内所有治理动作、风险与异常的完整记录。" actions={<ABtn variant="secondary" icon="download">导出</ABtn>} />
      <ACard pad={0}>
        {logs.map((l, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 13, padding: '13px 18px', borderBottom: i < logs.length - 1 ? '1px solid var(--border)' : 0 }}>
            <AChip kind={km[l[2]][0]} dot>{km[l[2]][1]}</AChip>
            <div style={{ flex: 1 }}><span style={{ fontSize: 13, fontWeight: 600 }}>{l[0]}</span> <span style={{ fontSize: 13, color: 'var(--text-2)' }}>{l[1]}</span>{l[4] && <AChip kind="ok" style={{ marginLeft: 8, height: 18 }}>{l[4]}</AChip>}</div>
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>{l[3]}</span>
          </div>
        ))}
      </ACard>
    </div>
  );
}

window.HivePages = window.HivePages || {};
Object.assign(window.HivePages, {
  'admin-overview': AdminOverview, 'admin-members': AdminMembers, 'admin-governance': AdminGovernance,
  'admin-capabilities': AdminCapabilities, 'admin-approvals': AdminApprovals, 'admin-assets': AdminAssets,
  'admin-budget': AdminBudget, 'admin-channels': AdminChannels, 'admin-memory': AdminMemoryGov, 'admin-audit': AdminAudit,
});
