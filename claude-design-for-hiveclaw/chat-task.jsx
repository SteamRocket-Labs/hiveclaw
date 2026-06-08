/* ============================================================
   Hive Prototype — Conversation & Task engine
   Live state machine: 交办 → 计划确认 → 执行(进度+A2A) → 产物
   Serves pages: tasks, plan
   ============================================================ */
const TU = window.HiveUI;
const { Icon: TIcon, Hex: THex, Chip: TChip, Btn: TBtn, IconBtn: TIconBtn, Card: TCard, Eyebrow: TEye,
        AGENTS: TAGENTS, ME: TME } = TU;
const { useState: tuS, useEffect: tuE, useRef: tuR, Fragment: TFrag } = TU;

function tAgent(id) { return TAGENTS.find(a => a.id === id) || TAGENTS[0]; }

const PLAN = {
  goal: '产出 Q2 竞品对标报告，覆盖定价、功能矩阵与差异机会点。',
  steps: ['抓取 Nova / Drift / Beacon 公开资料', '提取定价与功能矩阵', '委派 Warden 做合规与可信度核查', '分析差异与机会点', '生成结论与对比表'],
  assume: ['以三家主要竞品为范围', '数据截至本周公开信息', '报告语言为中文、结论先行'],
  question: '是否需要纳入价格区间外的企业定制方案？',
  risks: [['中', '部分竞品数据来自第三方，需标注来源'], ['低', '将调用外部检索工具，消耗模型预算约 ¥18']],
  scope: ['工具 · 网页检索', '技能 · 竞品分析', 'A2A · Warden', '渠道 · 无外发'],
  artifacts: ['竞品功能对比表 (XLSX)', '市场定位摘要 (DOC)'],
};

/* ----- message bubbles ----- */
function UserMsg({ text }) {
  return (
    <div style={{ display: 'flex', gap: 12, justifyContent: 'flex-end', marginBottom: 22 }}>
      <div style={{ maxWidth: '70%', background: 'var(--text-1)', color: '#fff', padding: '11px 15px', borderRadius: '12px 12px 3px 12px', fontSize: 14, lineHeight: 1.55 }}>{text}</div>
      <THex size={30} bg={TME.color} fs={11}>{TME.abbr}</THex>
    </div>
  );
}
function AgentLine({ agent, children }) {
  return (
    <div style={{ display: 'flex', gap: 12, marginBottom: 22 }}>
      <THex size={30} bg={agent.color} fs={11}>{agent.abbr}</THex>
      <div style={{ flex: 1, minWidth: 0, paddingTop: 1 }}>{children}</div>
    </div>
  );
}

/* ----- plan review card ----- */
function PlanCard({ agent, onConfirm, onReject, decided }) {
  return (
    <TCard pad={0} style={{ overflow: 'hidden', borderColor: decided ? 'var(--border)' : 'var(--honey-line)' }}>
      <div style={{ padding: '13px 17px', background: decided ? 'var(--surface-2)' : 'var(--warn-soft)', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 9 }}>
        <span style={{ color: 'var(--honey-deep)', display: 'flex' }}><TIcon name="check" size={17} /></span>
        <span style={{ fontWeight: 600, fontSize: 13.5 }}>执行计划 · 需要你确认</span>
        {decided === 'confirmed' && <TChip kind="ok" dot style={{ marginLeft: 'auto' }}>已确认</TChip>}
        {decided === 'rejected' && <TChip kind="danger" dot style={{ marginLeft: 'auto' }}>已拒绝</TChip>}
      </div>
      <div style={{ padding: 17 }}>
        <Field label="任务目标"><div style={{ fontSize: 13.5, lineHeight: 1.55 }}>{PLAN.goal}</div></Field>
        <Field label="执行步骤">
          <ol style={{ margin: 0, paddingLeft: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 10 }}>
            {PLAN.steps.map((s, i) => (
              <li key={i} style={{ display: 'flex', gap: 10, fontSize: 13, alignItems: 'flex-start' }}>
                <span className="mono" style={{ fontSize: 11, color: 'var(--text-4)', marginTop: 1, width: 16, flex: '0 0 auto' }}>{String(i + 1).padStart(2, '0')}</span>
                <span style={{ lineHeight: 1.5 }}>{s}{s.includes('Warden') && <TChip kind="purple" style={{ marginLeft: 7, height: 18 }}>A2A</TChip>}</span>
              </li>
            ))}
          </ol>
        </Field>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          <Field label="假设">
            <ul style={{ margin: 0, paddingLeft: 15, fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.65 }}>{PLAN.assume.map((a, i) => <li key={i}>{a}</li>)}</ul>
          </Field>
          <Field label="会使用的能力 / 渠道">
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>{PLAN.scope.map((s, i) => <TChip key={i} kind="idle" style={{ height: 22 }}>{s}</TChip>)}</div>
          </Field>
        </div>
        <Field label="需要你确认">
          <div style={{ fontSize: 13, background: 'var(--bg-sunk)', borderRadius: 8, padding: '10px 12px', display: 'flex', gap: 8 }}>
            <span style={{ color: 'var(--honey-deep)', display: 'flex', marginTop: 1 }}><TIcon name="alert" size={14} /></span>{PLAN.question}
          </div>
        </Field>
        <Field label="风险与影响">
          {PLAN.risks.map((r, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 9, fontSize: 12.5, padding: '5px 0' }}>
              <TChip kind={r[0] === '中' ? 'warn' : 'idle'}>{r[0]}风险</TChip><span style={{ color: 'var(--text-2)' }}>{r[1]}</span>
            </div>
          ))}
        </Field>
        <Field label="预期产物">
          <div style={{ display: 'flex', gap: 8 }}>{PLAN.artifacts.map((a, i) => <TChip key={i} kind="info" style={{ height: 22 }}><TIcon name="file" size={11} />{a}</TChip>)}</div>
        </Field>
      </div>
      {!decided && (
        <div style={{ display: 'flex', gap: 9, padding: '13px 17px', borderTop: '1px solid var(--border)', background: 'var(--surface-2)' }}>
          <TBtn variant="honey" icon="play" onClick={onConfirm}>确认执行</TBtn>
          <TBtn variant="secondary" icon="wand">修改计划</TBtn>
          <TBtn variant="ghost" onClick={onReject} style={{ marginLeft: 'auto', color: 'var(--text-3)' }}>拒绝</TBtn>
        </div>
      )}
    </TCard>
  );
}
function Field({ label, children }) {
  return (
    <div style={{ marginBottom: 15 }}>
      <div className="eyebrow" style={{ marginBottom: 7 }}>{label}</div>
      {children}
    </div>
  );
}

/* ----- live progress card ----- */
function ProgressCard({ progress }) {
  const steps = PLAN.steps;
  return (
    <TCard pad={0} style={{ overflow: 'hidden' }}>
      <div style={{ padding: '13px 17px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 9 }}>
        <span style={{ width: 8, height: 8, borderRadius: '50%', background: progress >= steps.length ? 'var(--ok)' : 'var(--honey)', boxShadow: `0 0 0 4px ${progress >= steps.length ? 'var(--ok-soft)' : 'var(--warn-soft)'}` }} />
        <span style={{ fontWeight: 600, fontSize: 13.5 }}>{progress >= steps.length ? '任务完成' : '正在执行'}</span>
        <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)', marginLeft: 'auto' }}>{Math.min(progress, steps.length)} / {steps.length}</span>
      </div>
      <div style={{ padding: '16px 17px' }}>
        {steps.map((s, i) => {
          const state = i < progress ? 'done' : i === progress ? 'active' : 'todo';
          const isA2A = s.includes('Warden');
          return (
            <div key={i}>
              <div style={{ display: 'flex', gap: 11, alignItems: 'flex-start' }}>
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', alignSelf: 'stretch' }}>
                  <span style={{ width: 17, height: 17, borderRadius: '50%', marginTop: 1, flex: '0 0 auto', display: 'flex', alignItems: 'center', justifyContent: 'center',
                    background: state === 'done' ? 'var(--ok)' : state === 'active' ? 'var(--honey)' : 'var(--bg-sunk)', color: '#fff', border: state === 'todo' ? '1.5px solid var(--border-2)' : 0 }}>
                    {state === 'done' && <TIcon name="checkPlain" size={10} />}
                    {state === 'active' && <span className="spin" style={{ width: 9, height: 9, border: '1.5px solid rgba(255,255,255,.5)', borderTopColor: '#fff', borderRadius: '50%' }} />}
                  </span>
                  {i < steps.length - 1 && <span style={{ width: 1.5, flex: 1, minHeight: 16, background: state === 'done' ? 'var(--ok)' : 'var(--border-2)' }} />}
                </div>
                <div style={{ paddingBottom: isA2A && state !== 'todo' ? 8 : 14, flex: 1 }}>
                  <div style={{ fontSize: 13, fontWeight: state === 'active' ? 600 : 500, color: state === 'todo' ? 'var(--text-3)' : 'var(--text-1)' }}>{s}</div>
                  {state === 'active' && !isA2A && <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 3 }}>处理中…</div>}
                  {isA2A && state !== 'todo' && (
                    <div style={{ marginTop: 9, background: 'var(--purple-soft)', borderRadius: 9, padding: '11px 13px', border: '1px solid transparent' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
                        <THex size={24} bg="oklch(0.52 0.05 60)" fs={9}>SE</THex>
                        <div style={{ flex: 1 }}><div style={{ fontSize: 12.5, fontWeight: 600 }}>委派 Warden · 安全审查</div>
                        <div style={{ fontSize: 11, color: 'var(--purple)', marginTop: 1 }}>{state === 'done' ? '✓ 返回 3 项可信度标注' : '执行子任务中…'}</div></div>
                        <TChip kind="purple" dot>{state === 'done' ? '已返回' : 'A2A'}</TChip>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </TCard>
  );
}

/* ----- artifacts result ----- */
function ResultCard({ navigate, ctx }) {
  return (
    <TCard pad={17}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 14 }}>
        <span style={{ color: 'var(--ok)', display: 'flex' }}><TIcon name="check" size={18} /></span>
        <span style={{ fontWeight: 600, fontSize: 14 }}>已生成 2 份产物</span>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 9, marginBottom: 15 }}>
        {[['竞品功能对比表', 'box', 'XLSX · 24 行 · 3 家竞品'], ['市场定位摘要', 'doc', 'DOC · 5 个机会点 · 含来源']].map((p, i) => (
          <div key={i} onClick={() => navigate('documents', {})} style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '11px 13px', border: '1px solid var(--border)', borderRadius: 9, cursor: 'pointer' }}
            onMouseEnter={(e) => e.currentTarget.style.background = 'var(--surface-2)'} onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}>
            <span style={{ width: 34, height: 34, borderRadius: 8, background: 'var(--bg-sunk)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-2)' }}><TIcon name={p[1]} size={17} /></span>
            <div style={{ flex: 1 }}><div style={{ fontSize: 13, fontWeight: 600 }}>{p[0]}</div><div className="mono" style={{ fontSize: 10, color: 'var(--text-4)', marginTop: 2 }}>{p[2]}</div></div>
            <TIconBtn name="download" size={30} iconSize={16} />
          </div>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 9 }}>
        <TBtn variant="secondary" size="sm" icon="flow" onClick={() => ctx.setModal('saveFlow')}>保存为流程</TBtn>
        <TBtn variant="ghost" size="sm" icon="download">导出</TBtn>
        <TBtn variant="ghost" size="sm" icon="refresh" style={{ marginLeft: 'auto' }}>继续修改</TBtn>
      </div>
    </TCard>
  );
}

/* ---------------- TASKS PAGE ---------------- */
function TasksPage({ navigate, params, ctx }) {
  const agent = tAgent(params.agentId || 'atlas');
  // phase: empty | planning | plan | running | done
  const startPlan = params.startPlan;
  const [phase, setPhase] = tuS(startPlan ? 'plan' : 'empty');
  const [decided, setDecided] = tuS(null);
  const [progress, setProgress] = tuS(0);
  const [input, setInput] = tuS('');
  const threadRef = tuR(null);

  const scrollDown = () => { setTimeout(() => { if (threadRef.current) threadRef.current.scrollTop = threadRef.current.scrollHeight; }, 80); };
  tuE(() => { if (threadRef.current && phase !== 'empty') threadRef.current.scrollTop = threadRef.current.scrollHeight; }, [phase, progress]);

  const sendTask = (text) => {
    setInput('');
    setPhase('planning'); scrollDown();
    setTimeout(() => { setPhase('plan'); scrollDown(); }, 1300);
  };
  const confirmPlan = () => {
    setDecided('confirmed'); setPhase('running'); setProgress(0); scrollDown();
  };
  tuE(() => {
    if (phase !== 'running') return;
    if (progress >= PLAN.steps.length) { setTimeout(() => { setPhase('done'); scrollDown(); }, 700); return; }
    const t = setTimeout(() => { setProgress(p => p + 1); scrollDown(); }, progress === 2 ? 1900 : 1300);
    return () => clearTimeout(t);
  }, [phase, progress]);

  const suggestions = ['做一份 Q2 竞品对标报告', '汇总上周用户访谈要点', '核对 8 月成本明细'];

  return (
    <div style={{ display: 'flex', height: '100%' }}>
      {/* conversation */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '14px 28px', borderBottom: '1px solid var(--border)' }}>
          <THex size={32} bg={agent.color} fs={12}>{agent.abbr}</THex>
          <div style={{ flex: 1 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}><span style={{ fontWeight: 600, fontSize: 14.5, whiteSpace: 'nowrap' }}>{agent.name}</span><TChip kind={phase === 'running' ? 'ok' : agent.status} dot>{phase === 'running' ? '工作中' : phase === 'done' ? '已完成' : agent.statusLabel}</TChip></div>
            <div style={{ fontSize: 12, color: 'var(--text-3)' }}>{agent.role}</div>
          </div>
          <TBtn size="sm" variant="ghost" icon="bot" onClick={() => navigate('workspace', { agentId: agent.id })}>工作台</TBtn>
        </div>

        <div ref={threadRef} className="thin-scroll" style={{ flex: 1, overflow: 'auto', padding: '28px 28px 20px' }}>
          <div style={{ maxWidth: 720, margin: '0 auto' }}>
            {phase === 'empty' ? (
              <div style={{ paddingTop: 40 }}>
                <div style={{ textAlign: 'center', marginBottom: 30 }}>
                  <THex size={48} bg={agent.color} fs={17} style={{ marginBottom: 16 }}>{agent.abbr}</THex>
                  <div style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 20 }}>交办任务给 {agent.name.split(' ')[1] || agent.name}</div>
                  <p style={{ fontSize: 13.5, color: 'var(--text-2)', marginTop: 7 }}>描述你想要的结果。{agent.name.split(' ')[1]} 会先给出计划，确认后再执行。</p>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 9, maxWidth: 460, margin: '0 auto' }}>
                  {suggestions.map((s, i) => (
                    <div key={i} onClick={() => sendTask(s)} style={{ display: 'flex', alignItems: 'center', gap: 11, padding: '13px 15px', border: '1px solid var(--border)', borderRadius: 10, cursor: 'pointer', background: 'var(--surface)' }}
                      onMouseEnter={(e) => { e.currentTarget.style.borderColor = 'var(--border-2)'; e.currentTarget.style.background = 'var(--surface-2)'; }} onMouseLeave={(e) => { e.currentTarget.style.borderColor = 'var(--border)'; e.currentTarget.style.background = 'var(--surface)'; }}>
                      <span style={{ color: 'var(--honey)', display: 'flex' }}><TIcon name="sparkle" size={16} /></span>
                      <span style={{ fontSize: 13.5, flex: 1 }}>{s}</span>
                      <span style={{ color: 'var(--text-4)' }}><TIcon name="arrowRight" size={15} /></span>
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <>
                <UserMsg text="帮我做一份 Q2 竞品对标报告，覆盖定价和功能矩阵，重点是差异机会点。" />
                {phase === 'planning' ? (
                  <AgentLine agent={agent}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 9, color: 'var(--text-3)', fontSize: 13 }}>
                      <span className="spin" style={{ width: 13, height: 13, border: '2px solid var(--border-2)', borderTopColor: 'var(--text-2)', borderRadius: '50%' }} />正在制定计划…
                    </div>
                  </AgentLine>
                ) : (
                  <>
                    <AgentLine agent={agent}>
                      <div style={{ fontSize: 14, lineHeight: 1.6, marginBottom: 13 }}>明白。这是一个需要外部检索和 A2A 协作的任务，我先给出计划，确认后再执行 👇</div>
                      <PlanCard agent={agent} decided={decided} onConfirm={confirmPlan} onReject={() => { setDecided('rejected'); }} />
                    </AgentLine>
                    {decided === 'confirmed' && (phase === 'running' || phase === 'done') && (
                      <AgentLine agent={agent}><ProgressCard progress={progress} /></AgentLine>
                    )}
                    {phase === 'done' && (
                      <AgentLine agent={agent}>
                        <div style={{ fontSize: 14, lineHeight: 1.6, marginBottom: 13 }}>完成了。三家竞品的对标已汇总，Warden 也返回了可信度标注。主要发现：Beacon 在企业定价上有空档，是我们的机会点。产物如下：</div>
                        <ResultCard navigate={navigate} ctx={ctx} />
                      </AgentLine>
                    )}
                    {decided === 'rejected' && (
                      <AgentLine agent={agent}><div style={{ fontSize: 14, color: 'var(--text-2)' }}>好的，已取消该计划。你可以调整需求后重新交办。</div></AgentLine>
                    )}
                  </>
                )}
              </>
            )}
          </div>
        </div>

        {/* composer */}
        <div style={{ padding: '14px 28px 20px', borderTop: '1px solid var(--border)' }}>
          <div style={{ maxWidth: 720, margin: '0 auto' }}>
            <div style={{ display: 'flex', alignItems: 'flex-end', gap: 10, background: 'var(--surface)', border: '1px solid var(--border-2)', borderRadius: 13, padding: '10px 12px', boxShadow: 'var(--shadow-1)' }}>
              <TIconBtn name="clip" size={32} iconSize={18} />
              <textarea value={input} onChange={(e) => setInput(e.target.value)} rows={1} placeholder={`给 ${agent.name.split(' ')[1] || ''} 发消息，或交办新任务…`}
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (input.trim()) sendTask(input); } }}
                style={{ flex: 1, border: 0, outline: 'none', resize: 'none', fontFamily: 'var(--sans)', fontSize: 14, lineHeight: 1.5, background: 'transparent', color: 'var(--text-1)', maxHeight: 120, padding: '5px 0' }} />
              <TBtn variant="primary" size="sm" icon="send" style={{ width: 36, padding: 0 }} onClick={() => input.trim() && sendTask(input)} />
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginTop: 9, fontSize: 11, color: 'var(--text-4)' }}>
              <TChip kind="idle" style={{ height: 19 }}>计划确认 · 已开启</TChip>
              <span className="mono">高风险动作会请求人工审批</span>
              {phase !== 'empty' && <span className="mono" style={{ marginLeft: 'auto', cursor: 'pointer', color: 'var(--honey-deep)' }} onClick={() => { setPhase('empty'); setDecided(null); setProgress(0); }}>↺ 重新演示</span>}
            </div>
          </div>
        </div>
      </div>

      {/* right rail: task detail */}
      <aside style={{ width: 280, flex: '0 0 280px', borderLeft: '1px solid var(--border)', padding: '20px 18px', overflow: 'auto', background: 'var(--bg)' }} className="thin-scroll">
        <TEye style={{ marginBottom: 13 }}>本次任务 · This task</TEye>
        <TCard pad={15} style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 13.5, fontWeight: 600, marginBottom: 4 }}>Q2 竞品对标报告</div>
          <TChip kind={phase === 'done' ? 'ok' : phase === 'running' ? 'warn' : phase === 'empty' ? 'idle' : 'info'} dot>
            {phase === 'empty' ? '未开始' : phase === 'planning' ? '制定计划中' : phase === 'plan' ? '待确认' : phase === 'running' ? '执行中' : '已完成'}
          </TChip>
          {(phase === 'running' || phase === 'done') && (
            <div style={{ marginTop: 12 }}>
              <div style={{ height: 6, borderRadius: 999, background: 'var(--bg-sunk)', overflow: 'hidden' }}>
                <div style={{ width: `${Math.min(progress / PLAN.steps.length * 100, 100)}%`, height: '100%', background: phase === 'done' ? 'var(--ok)' : 'var(--honey)', transition: 'width .5s var(--ease)' }} />
              </div>
              <div className="mono" style={{ fontSize: 10, color: 'var(--text-4)', marginTop: 6 }}>{Math.min(progress, PLAN.steps.length)} / {PLAN.steps.length} 步骤</div>
            </div>
          )}
        </TCard>

        <TEye style={{ marginBottom: 11 }}>使用的能力</TEye>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 7, marginBottom: 20 }}>
          {PLAN.scope.map((s, i) => <TChip key={i} kind="idle" style={{ height: 22 }}>{s.split(' · ')[1]}</TChip>)}
        </div>

        <TEye style={{ marginBottom: 11 }}>A2A 协作</TEye>
        <TCard pad={13} style={{ marginBottom: 20 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
            <THex size={26} bg="oklch(0.52 0.05 60)" fs={10}>SE</THex>
            <div style={{ flex: 1 }}><div style={{ fontSize: 12.5, fontWeight: 600 }}>Warden</div><div style={{ fontSize: 11, color: 'var(--text-3)' }}>安全审查</div></div>
            <TChip kind={progress > 2 || phase === 'done' ? 'ok' : phase === 'running' ? 'purple' : 'idle'} dot>{progress > 2 || phase === 'done' ? '已返回' : phase === 'running' && progress === 2 ? '执行中' : '待委派'}</TChip>
          </div>
        </TCard>

        {phase === 'done' && (
          <>
            <TEye style={{ marginBottom: 11 }}>产物</TEye>
            {['竞品功能对比表', '市场定位摘要'].map((a, i) => (
              <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '8px 0', borderTop: i ? '1px solid var(--border)' : 0, cursor: 'pointer' }} onClick={() => navigate('documents', {})}>
                <span style={{ color: 'var(--text-3)', display: 'flex' }}><TIcon name={i ? 'doc' : 'box'} size={15} /></span>
                <span style={{ fontSize: 12.5, flex: 1 }}>{a}</span>
                <TIcon name="arrowUpRight" size={13} style={{ color: 'var(--text-4)' }} />
              </div>
            ))}
          </>
        )}
      </aside>
    </div>
  );
}

/* ---------------- PLAN PAGE (deep-link to a pending plan) ---------------- */
function PlanPage(props) {
  return <TasksPage {...props} params={{ ...props.params, agentId: props.params.agentId || 'ledger', startPlan: true }} />;
}

window.HivePages = window.HivePages || {};
window.HivePages.tasks = TasksPage;
window.HivePages.plan = PlanPage;
