/* ============================================================
   Hive Prototype — Employee: Automations / Memory / Documents / Approvals
   ============================================================ */
const MU = window.HiveUI;
const { Icon: MIcon, Hex: MHex, Chip: MChip, Btn: MBtn, IconBtn: MIconBtn, Card: MCard, Tabs: MTabs,
        PageHead: MPageHead, Eyebrow: MEye, EmptyState: MEmpty, AGENTS: MAGENTS } = MU;
const { useState: muS, Fragment: MFrag } = MU;

/* ---------------- AUTOMATIONS ---------------- */
function AutomationsPage({ navigate, ctx }) {
  const [tab, setTab] = muS('flows');
  const flows = [
    ['竞品对标报告', 'oklch(0.60 0.13 250)', 'DR', 'running', '运行中', '每周一 09:00', '已运行 12 次'],
    ['月度财务对账', 'oklch(0.62 0.12 150)', 'FC', 'approval', '等待审批', '每月 1 日', '已运行 5 次'],
    ['工单自动分发', 'oklch(0.56 0.11 300)', 'OP', 'running', '运行中', '实时触发', '今日 38 次'],
    ['访谈纪要整理', 'oklch(0.60 0.12 200)', 'WR', 'paused', '已暂停', '手动触发', '已运行 8 次'],
    ['安全合规扫描', 'oklch(0.52 0.05 60)', 'SE', 'failed', '失败', '每日 02:00', '上次失败'],
  ];
  const kindMap = { running: 'ok', approval: 'info', paused: 'idle', failed: 'danger', done: 'ok' };
  return (
    <div style={{ padding: '30px 40px 60px', maxWidth: 1180, margin: '0 auto' }}>
      <MPageHead crumbs={['我的工作区', 'Automations']} title="自动化" sub="把成功的任务沉淀为可复用流程，设置定时与触发规则，让数字员工稳定运行。"
        actions={<MBtn variant="primary" icon="plus">新建流程</MBtn>} />

      <MCard pad={15} style={{ display: 'flex', alignItems: 'center', gap: 13, marginBottom: 22, borderColor: 'var(--honey-line)', background: 'var(--warn-soft)' }}>
        <span style={{ width: 36, height: 36, borderRadius: 9, background: 'var(--surface)', color: 'var(--honey-deep)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><MIcon name="sparkle" size={18} /></span>
        <div style={{ flex: 1 }}><div style={{ fontSize: 13.5, fontWeight: 600 }}>推荐沉淀为流程</div><div style={{ fontSize: 12.5, color: 'var(--text-2)', marginTop: 2 }}>Atlas 的「竞品扫描」任务已成功运行 3 次，建议保存为流程以便复用。</div></div>
        <MBtn size="sm" variant="honey" icon="flow" onClick={() => ctx.setModal('saveFlow')}>保存为流程</MBtn>
      </MCard>

      <MTabs tabs={[{ id: 'flows', label: '流程', count: flows.length }, { id: 'sched', label: '定时任务' }, { id: 'runs', label: '运行记录' }]} active={tab} onChange={setTab} style={{ marginBottom: 18 }} />

      {tab === 'flows' && (
        <MCard pad={0}>
          {flows.map((f, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '15px 18px', borderBottom: i < flows.length - 1 ? '1px solid var(--border)' : 0 }}>
              <span style={{ width: 36, height: 36, borderRadius: 9, background: 'var(--bg-sunk)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-2)' }}><MIcon name="flow" size={18} /></span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}><span style={{ fontSize: 14, fontWeight: 600 }}>{f[0]}</span><MChip kind={kindMap[f[3]]} dot>{f[4]}</MChip></div>
                <div className="mono" style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 4, display: 'flex', gap: 12 }}><span>⏱ {f[5]}</span><span>{f[6]}</span></div>
              </div>
              <MHex size={28} bg={f[1]} fs={10}>{f[2]}</MHex>
              <MIconBtn name={f[3] === 'paused' ? 'play' : 'pause'} />
              <MIconBtn name="dots" />
            </div>
          ))}
        </MCard>
      )}
      {tab === 'sched' && (
        <MCard pad={0}>
          {flows.filter(f => f[5].includes('每')).map((f, i, arr) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '15px 18px', borderBottom: i < arr.length - 1 ? '1px solid var(--border)' : 0 }}>
              <span style={{ color: 'var(--text-3)', display: 'flex' }}><MIcon name="clock" size={18} /></span>
              <div style={{ flex: 1 }}><div style={{ fontSize: 13.5, fontWeight: 600 }}>{f[0]}</div><div className="mono" style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 3 }}>{f[5]}</div></div>
              <MChip kind={kindMap[f[3]]} dot>{f[4]}</MChip>
            </div>
          ))}
        </MCard>
      )}
      {tab === 'runs' && (
        <MCard pad={0}>
          {[['竞品对标报告', '完成', 'ok', '今天 09:02 · 耗时 4 分'], ['工单自动分发', '完成', 'ok', '今天 08:15'], ['安全合规扫描', '失败', 'danger', '今天 02:00 · 工具超时'], ['月度财务对账', '等待审批', 'info', '昨天 00:01']].map((r, i, arr) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 13, padding: '13px 18px', borderBottom: i < arr.length - 1 ? '1px solid var(--border)' : 0 }}>
              <MChip kind={r[2]} dot>{r[1]}</MChip>
              <span style={{ flex: 1, fontSize: 13.5 }}>{r[0]}</span>
              <span className="mono" style={{ fontSize: 11, color: 'var(--text-4)' }}>{r[3]}</span>
            </div>
          ))}
        </MCard>
      )}
    </div>
  );
}

/* ---------------- MEMORY ---------------- */
function MemoryPage({ navigate }) {
  const [tab, setTab] = muS('agent');
  return (
    <div style={{ padding: '30px 40px 60px', maxWidth: 1180, margin: '0 auto' }}>
      <MPageHead crumbs={['我的工作区', 'Memory']} title="记忆与知识" sub="查看数字员工记住了什么、公司共享了哪些知识，以及任务用到的来源。"
        actions={<MBtn variant="primary" icon="plus">添加记忆</MBtn>} />
      <MTabs tabs={[{ id: 'agent', label: '数字员工记忆' }, { id: 'company', label: '公司共享知识' }, { id: 'used', label: '本次任务使用' }]} active={tab} onChange={setTab} style={{ marginBottom: 20 }} />
      {tab === 'agent' && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2,1fr)', gap: 13 }}>
          {[
            ['偏好', '报告统一中文、结论先行', 'Atlas · 对话学习', false],
            ['事实', 'Q2 竞品：Nova / Drift / Beacon', 'Atlas · 任务沉淀', false],
            ['偏好', '财务数据精确到分', 'Ledger · 用户设定', false],
            ['公司知识', '《2026 产品策略》', 'Quill · 公司共享', true],
            ['经验', '招聘优先看项目经历', 'Pace · 对话学习', false],
            ['事实', '主要客户分布在华东', 'Relay · 任务沉淀', false],
          ].map((m, i) => (
            <MCard key={i} pad={15} hover>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 9 }}>
                <MChip kind={m[0] === '公司知识' ? 'info' : 'idle'}>{m[0]}</MChip>
                {m[3] && <span style={{ color: 'var(--text-4)' }} title="受权限限制"><MIcon name="lock" size={13} /></span>}
                <MIconBtn name="dots" size={26} iconSize={14} style={{ marginLeft: 'auto' }} />
              </div>
              <div style={{ fontSize: 13.5, marginBottom: 8, lineHeight: 1.5 }}>{m[1]}</div>
              <div className="mono" style={{ fontSize: 10, color: 'var(--text-4)' }}>来源 · {m[2]}</div>
            </MCard>
          ))}
        </div>
      )}
      {tab === 'company' && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 13 }}>
          {[['产品策略库', 'doc', '12 篇文档', '全员可读'], ['竞品情报', 'box', '8 份报告', '研究组'], ['品牌规范', 'star', '只读', '全员'], ['客户档案', 'users', '受限', '销售组'], ['技术架构', 'layers', '6 篇', '研发组'], ['合规手册', 'shield', '只读', '全员']].map((k, i) => (
            <MCard key={i} pad={16} hover>
              <span style={{ width: 36, height: 36, borderRadius: 9, background: 'var(--bg-sunk)', color: 'var(--text-2)', display: 'flex', alignItems: 'center', justifyContent: 'center', marginBottom: 12 }}><MIcon name={k[1]} size={18} /></span>
              <div style={{ fontSize: 14, fontWeight: 600 }}>{k[0]}</div>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 8 }}>
                <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>{k[2]}</span><MChip kind="idle">{k[3]}</MChip>
              </div>
            </MCard>
          ))}
        </div>
      )}
      {tab === 'used' && (
        <MCard pad={0}>
          {[['《2026 产品策略》', '公司知识', '用于市场定位分析'], ['Q2 竞品事实', '记忆', '提供竞品名单'], ['报告偏好', '偏好', '结论先行格式']].map((u, i, arr) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '14px 18px', borderBottom: i < arr.length - 1 ? '1px solid var(--border)' : 0 }}>
              <span style={{ color: 'var(--text-3)', display: 'flex' }}><MIcon name="link" size={16} /></span>
              <span style={{ fontSize: 13.5, fontWeight: 500, flex: 1 }}>{u[0]}</span>
              <MChip kind="idle">{u[1]}</MChip>
              <span style={{ fontSize: 12, color: 'var(--text-3)', width: 160, textAlign: 'right' }}>{u[2]}</span>
            </div>
          ))}
        </MCard>
      )}
    </div>
  );
}

/* ---------------- DOCUMENTS ---------------- */
function DocumentsPage({ navigate }) {
  const [sel, setSel] = muS(0);
  const docs = [
    ['市场定位摘要', 'Atlas', 'oklch(0.60 0.13 250)', '2 小时前', 'DOC'],
    ['竞品功能对比表', 'Atlas', 'oklch(0.60 0.13 250)', '2 小时前', 'XLSX'],
    ['用户访谈纪要 W23', 'Quill', 'oklch(0.60 0.12 200)', '昨天', 'DOC'],
    ['8 月对账报告', 'Ledger', 'oklch(0.62 0.12 150)', '3 天前', 'PDF'],
  ];
  return (
    <div style={{ display: 'flex', height: '100%' }}>
      <div style={{ width: 320, flex: '0 0 320px', borderRight: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }}>
        <div style={{ padding: '22px 22px 14px' }}>
          <h1 style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 22, margin: '0 0 4px' }}>文档与研究</h1>
          <p style={{ fontSize: 12.5, color: 'var(--text-3)', margin: 0 }}>数字员工生成的文档与报告</p>
        </div>
        <div className="thin-scroll" style={{ flex: 1, overflow: 'auto', padding: '0 12px 12px' }}>
          {docs.map((d, i) => (
            <div key={i} onClick={() => setSel(i)} style={{ display: 'flex', gap: 11, padding: '12px 12px', borderRadius: 9, cursor: 'pointer', background: sel === i ? 'var(--hover)' : 'transparent', marginBottom: 2 }}>
              <span style={{ width: 32, height: 32, borderRadius: 7, background: 'var(--bg-sunk)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-2)', flex: '0 0 auto' }}><MIcon name="doc" size={16} /></span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 600, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{d[0]}</div>
                <div className="mono" style={{ fontSize: 10, color: 'var(--text-4)', marginTop: 3 }}>{d[4]} · {d[3]}</div>
              </div>
            </div>
          ))}
        </div>
      </div>
      <div className="thin-scroll" style={{ flex: 1, overflow: 'auto' }}>
        <div style={{ maxWidth: 720, margin: '0 auto', padding: '40px 48px 80px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 18 }}>
            <MChip kind="idle">{docs[sel][4]}</MChip>
            <MHex size={22} bg={docs[sel][2]} fs={8}>{docs[sel][1].slice(0, 2).toUpperCase()}</MHex>
            <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>{docs[sel][1]} 生成 · {docs[sel][3]}</span>
            <div style={{ flex: 1 }} />
            <MBtn size="sm" variant="ghost" icon="refresh">继续研究</MBtn>
            <MBtn size="sm" variant="secondary" icon="download">导出</MBtn>
          </div>
          <h1 style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 30, margin: '0 0 8px', letterSpacing: '-.01em' }}>{docs[sel][0]}</h1>
          <p style={{ fontSize: 14, color: 'var(--text-2)', lineHeight: 1.7, marginBottom: 26 }}>本报告基于三家主要竞品的公开资料，结合公司产品策略库，分析 Q2 市场定位与差异机会点。</p>
          {[['核心结论', '在企业定价区间，Beacon 存在明显空档，是我们短期最具确定性的机会点。Nova 在中小客户侧形成壁垒，不建议正面竞争。'],
            ['竞品定价对比', '三家竞品的定价策略分化明显：Nova 以低价走量，Drift 主打中端，Beacon 缺乏企业级方案。'],
            ['机会点', '1. 企业定制方案；2. A2A 协作带来的自动化深度；3. 数据合规作为差异化卖点。']].map((s, i) => (
            <div key={i} style={{ marginBottom: 22 }}>
              <h2 style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 17, margin: '0 0 9px' }}>{s[0]}</h2>
              <p style={{ fontSize: 14, color: 'var(--text-1)', lineHeight: 1.75, margin: 0 }}>{s[1]}</p>
            </div>
          ))}
          <div style={{ display: 'flex', alignItems: 'center', gap: 9, padding: 14, background: 'var(--bg-sunk)', borderRadius: 10, marginTop: 10 }}>
            <span style={{ color: 'var(--text-3)', display: 'flex' }}><MIcon name="link" size={15} /></span>
            <span style={{ fontSize: 12, color: 'var(--text-2)' }}>资料来源：24 个公开网页 · 《2026 产品策略》 · Warden 可信度标注</span>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ---------------- APPROVALS (employee) ---------------- */
function ApprovalsPage({ navigate }) {
  const [items, setItems] = muS([
    { id: 1, agent: 'Ledger', abbr: 'FC', color: 'oklch(0.62 0.12 150)', action: '向外部邮箱发送对账报告', risk: '中', scope: '财务数据外发', from: '系统自动', when: '12 分钟前' },
    { id: 2, agent: 'Relay', abbr: 'OP', color: 'oklch(0.56 0.11 300)', action: '在飞书群批量发送通知', risk: '低', scope: '影响 38 人', from: '陈航 Hang', when: '1 小时前' },
  ]);
  const [done, setDone] = muS([]);
  const act = (id, verb) => { setItems(its => its.filter(i => i.id !== id)); setDone(d => [...d, verb]); };
  return (
    <div style={{ padding: '30px 40px 60px', maxWidth: 920, margin: '0 auto' }}>
      <MPageHead crumbs={['我的工作区', 'Approvals']} title="审批" meta={`${items.length} 待处理`} sub="数字员工执行高风险或受限动作前，会请求你的确认。" />
      {items.length === 0 ? (
        <MCard pad={0}><MEmpty title="没有待审批事项" sub="数字员工请求的高风险动作会出现在这里，等待你批准、拒绝或要求修改。" /></MCard>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {items.map((it) => (
            <MCard key={it.id} pad={0} style={{ overflow: 'hidden' }}>
              <div style={{ padding: '16px 18px', display: 'flex', alignItems: 'flex-start', gap: 13 }}>
                <MHex size={38} bg={it.color} fs={13}>{it.abbr}</MHex>
                <div style={{ flex: 1 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 5 }}>
                    <span style={{ fontSize: 14.5, fontWeight: 600 }}>{it.agent} 请求执行</span>
                    <MChip kind={it.risk === '中' ? 'warn' : 'idle'} dot>{it.risk}风险</MChip>
                  </div>
                  <div style={{ fontSize: 14, color: 'var(--text-1)', marginBottom: 10 }}>「{it.action}」</div>
                  <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap' }}>
                    {[['影响范围', it.scope], ['发起人', it.from], ['时间', it.when]].map((m, i) => (
                      <div key={i}><div className="eyebrow" style={{ marginBottom: 3 }}>{m[0]}</div><div style={{ fontSize: 12.5, color: 'var(--text-2)' }}>{m[1]}</div></div>
                    ))}
                  </div>
                </div>
              </div>
              <div style={{ display: 'flex', gap: 9, padding: '13px 18px', borderTop: '1px solid var(--border)', background: 'var(--surface-2)' }}>
                <MBtn size="sm" variant="primary" icon="checkPlain" onClick={() => act(it.id, 'approve')}>批准</MBtn>
                <MBtn size="sm" variant="secondary" icon="wand" onClick={() => act(it.id, 'changes')}>要求修改</MBtn>
                <MBtn size="sm" variant="ghost" style={{ marginLeft: 'auto', color: 'var(--text-3)' }} onClick={() => act(it.id, 'reject')}>拒绝</MBtn>
              </div>
            </MCard>
          ))}
        </div>
      )}
      {done.length > 0 && (
        <div style={{ marginTop: 26 }}>
          <MEye style={{ marginBottom: 12 }}>审批历史</MEye>
          <MCard pad={0}>
            {done.map((v, i) => (
              <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 18px', borderBottom: i < done.length - 1 ? '1px solid var(--border)' : 0 }}>
                <MChip kind={v === 'approve' ? 'ok' : v === 'reject' ? 'danger' : 'info'} dot>{v === 'approve' ? '已批准' : v === 'reject' ? '已拒绝' : '要求修改'}</MChip>
                <span style={{ fontSize: 13, color: 'var(--text-2)' }}>刚刚 · 已记录到审计</span>
              </div>
            ))}
          </MCard>
        </div>
      )}
    </div>
  );
}

window.HivePages = window.HivePages || {};
Object.assign(window.HivePages, { automations: AutomationsPage, memory: MemoryPage, documents: DocumentsPage, approvals: ApprovalsPage });
