/* ============================================================
   Hive Prototype — Create digital employee flow + modals
   ============================================================ */
const CU = window.HiveUI;
const { Icon: CIcon, Hex: CHex, Chip: CChip, Btn: CBtn, IconBtn: CIconBtn, Card: CCard, Toggle: CToggle,
        Eyebrow: CEye, CAP_TYPES: CCAPS } = CU;
const { useState: cuS, Fragment: CFrag } = CU;

const COLORS = ['oklch(0.60 0.13 250)', 'oklch(0.62 0.12 150)', 'oklch(0.60 0.12 25)', 'oklch(0.56 0.11 300)', 'oklch(0.60 0.12 200)', 'oklch(0.64 0.11 60)'];

function CreatePage({ navigate }) {
  const [step, setStep] = cuS(0);
  const [method, setMethod] = cuS(null);
  const [name, setName] = cuS('');
  const [role, setRole] = cuS('');
  const [color, setColor] = cuS(COLORS[0]);
  const [scope, setScope] = cuS('group');
  const [caps, setCaps] = cuS(['chat', 'file', 'memory']);
  const [creating, setCreating] = cuS(false);

  const abbr = (name || 'AI').replace(/[^A-Za-z\u4e00-\u9fa5]/g, '').slice(0, 2).toUpperCase() || 'AI';
  const steps = ['创建方式', '基本信息', '可见范围', '能力配置', '确认创建'];
  const canNext = step === 0 ? method : step === 1 ? (name && role) : true;

  const toggleCap = (id) => setCaps(cs => cs.includes(id) ? cs.filter(c => c !== id) : [...cs, id]);

  const finish = () => {
    setCreating(true);
    setTimeout(() => navigate('workspace', { agentId: 'atlas' }), 1600);
  };

  return (
    <div style={{ minHeight: '100%', background: 'var(--bg-sunk)' }}>
      <div style={{ maxWidth: 760, margin: '0 auto', padding: '24px 32px 80px' }}>
        {/* header */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 22 }}>
          <CIconBtn name="x" onClick={() => navigate('employees')} title="退出" />
          <div style={{ flex: 1 }}>
            <div style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 18 }}>创建数字员工</div>
          </div>
          <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>{step + 1} / {steps.length}</span>
        </div>

        {/* stepper */}
        <div style={{ display: 'flex', gap: 6, marginBottom: 30 }}>
          {steps.map((s, i) => (
            <div key={i} style={{ flex: 1 }}>
              <div style={{ height: 4, borderRadius: 999, background: i <= step ? 'var(--honey)' : 'var(--border-2)', transition: 'background .2s' }} />
              <div style={{ fontSize: 11, marginTop: 7, color: i === step ? 'var(--text-1)' : 'var(--text-3)', fontWeight: i === step ? 600 : 400 }}>{s}</div>
            </div>
          ))}
        </div>

        <CCard pad={26} style={{ minHeight: 340 }}>
          {step === 0 && (
            <div>
              <StepTitle t="选择创建方式" s="从零搭建、套用公司模板，或直接描述你想要什么。" />
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12, marginTop: 22 }}>
                {[
                  ['blank', 'plus', '从空白创建', '自己配置职责、能力与权限', false],
                  ['template', 'template', '从公司模板创建', '基于标准数字员工快速开始 · 6 个模板', false],
                  ['ai', 'sparkle', '自然语言助手', '描述需求，Hive 帮你生成配置', true],
                ].map(([id, ic, t, d, rec]) => (
                  <div key={id} onClick={() => setMethod(id)} style={{ display: 'flex', alignItems: 'center', gap: 14, padding: 17, borderRadius: 12, cursor: 'pointer', border: `1.5px solid ${method === id ? 'var(--text-1)' : 'var(--border)'}`, background: method === id ? 'var(--surface-2)' : 'var(--surface)', transition: 'all .12s' }}>
                    <span style={{ width: 40, height: 40, borderRadius: 10, background: id === 'ai' ? 'var(--honey-soft)' : 'var(--bg-sunk)', color: id === 'ai' ? 'var(--honey-deep)' : 'var(--text-1)', display: 'flex', alignItems: 'center', justifyContent: 'center', flex: '0 0 auto' }}><CIcon name={ic} size={20} /></span>
                    <div style={{ flex: 1 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}><span style={{ fontSize: 14.5, fontWeight: 600 }}>{t}</span>{rec && <CChip kind="warn">推荐</CChip>}</div>
                      <div style={{ fontSize: 12.5, color: 'var(--text-3)', marginTop: 3 }}>{d}</div>
                    </div>
                    <span style={{ width: 20, height: 20, borderRadius: '50%', border: `1.5px solid ${method === id ? 'var(--text-1)' : 'var(--border-2)'}`, display: 'flex', alignItems: 'center', justifyContent: 'center', background: method === id ? 'var(--text-1)' : 'transparent' }}>{method === id && <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#fff' }} />}</span>
                  </div>
                ))}
              </div>
              {method === 'ai' && (
                <div style={{ marginTop: 16, background: 'var(--bg-sunk)', borderRadius: 11, padding: 14 }}>
                  <div className="eyebrow" style={{ marginBottom: 8 }}>描述你想要的数字员工</div>
                  <textarea autoFocus rows={2} defaultValue="一个帮我做市场和竞品调研、能产出对比报告的助理"
                    style={{ width: '100%', border: '1px solid var(--border-2)', borderRadius: 8, padding: '10px 12px', fontFamily: 'var(--sans)', fontSize: 13.5, resize: 'none', outline: 'none', color: 'var(--text-1)' }} />
                </div>
              )}
            </div>
          )}

          {step === 1 && (
            <div>
              <StepTitle t="基本信息" s="给你的数字员工一个名字和清晰的职责。" />
              <div style={{ display: 'flex', gap: 18, marginTop: 22, alignItems: 'flex-start' }}>
                <div style={{ textAlign: 'center' }}>
                  <CHex size={64} bg={color} fs={24}>{abbr}</CHex>
                  <div className="eyebrow" style={{ marginTop: 12, marginBottom: 8 }}>形象</div>
                  <div style={{ display: 'flex', gap: 6, justifyContent: 'center', maxWidth: 80, flexWrap: 'wrap' }}>
                    {COLORS.map(c => <span key={c} onClick={() => setColor(c)} className="hex" style={{ width: 18, height: 18, background: c, cursor: 'pointer', outline: color === c ? '2px solid var(--text-1)' : 0, outlineOffset: 2 }} />)}
                  </div>
                </div>
                <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 16 }}>
                  <LabeledInput label="名称" value={name} onChange={setName} placeholder="例如：调研助理 Atlas" />
                  <LabeledInput label="职责" value={role} onChange={setRole} placeholder="例如：市场与竞品研究" />
                  <div>
                    <div className="eyebrow" style={{ marginBottom: 7 }}>工作说明 (可选)</div>
                    <textarea rows={3} placeholder="描述这个数字员工应该如何工作、遵循哪些原则…" style={{ width: '100%', border: '1px solid var(--border-2)', borderRadius: 8, padding: '10px 12px', fontFamily: 'var(--sans)', fontSize: 13.5, resize: 'none', outline: 'none', color: 'var(--text-1)' }} />
                  </div>
                </div>
              </div>
            </div>
          )}

          {step === 2 && (
            <div>
              <StepTitle t="可见范围" s="谁可以看到并使用这个数字员工？随时可在「权限与分享」修改。" />
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 11, marginTop: 22 }}>
                {[['me', 'lock', '仅自己可见', '只有你能使用'], ['picked', 'users', '指定员工', '选择具体成员'], ['group', 'users', '指定 Group', '研究组、财务组…'], ['all', 'globe', '全公司可见', 'workspace 全员']].map(([id, ic, t, d]) => (
                  <div key={id} onClick={() => setScope(id)} style={{ display: 'flex', gap: 12, padding: 15, borderRadius: 11, cursor: 'pointer', border: `1.5px solid ${scope === id ? 'var(--text-1)' : 'var(--border)'}` }}>
                    <span style={{ width: 32, height: 32, borderRadius: 8, background: scope === id ? 'var(--text-1)' : 'var(--bg-sunk)', color: scope === id ? '#fff' : 'var(--text-2)', display: 'flex', alignItems: 'center', justifyContent: 'center', flex: '0 0 auto' }}><CIcon name={ic} size={16} /></span>
                    <div><div style={{ fontSize: 13.5, fontWeight: 600 }}>{t}</div><div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 2 }}>{d}</div></div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {step === 3 && (
            <div>
              <StepTitle t="能力配置" s="选择基础能力。标记的能力由公司治理，创建后需走申请或审批。" />
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 9, marginTop: 20 }}>
                {CCAPS.map((c) => {
                  const on = caps.includes(c.id);
                  const gov = ['admin', 'approval'].includes(c.state);
                  return (
                    <div key={c.id} onClick={() => !gov && toggleCap(c.id)} style={{ display: 'flex', alignItems: 'center', gap: 11, padding: '12px 13px', borderRadius: 9, border: `1px solid ${on ? 'var(--text-1)' : 'var(--border)'}`, cursor: gov ? 'default' : 'pointer', opacity: gov ? .65 : 1, background: on ? 'var(--surface-2)' : 'var(--surface)' }}>
                      <span style={{ color: on ? 'var(--honey-deep)' : 'var(--text-3)', display: 'flex' }}><CIcon name={c.icon} size={17} /></span>
                      <span style={{ fontSize: 13, fontWeight: 500, flex: 1 }}>{c.name}</span>
                      {gov ? <CChip kind={c.state === 'approval' ? 'info' : 'warn'} style={{ height: 18 }}>{c.state === 'approval' ? '需审批' : '需管理员'}</CChip>
                        : <span style={{ width: 18, height: 18, borderRadius: 5, border: `1.5px solid ${on ? 'var(--text-1)' : 'var(--border-2)'}`, background: on ? 'var(--text-1)' : 'transparent', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>{on && <CIcon name="checkPlain" size={11} style={{ color: '#fff' }} />}</span>}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {step === 4 && !creating && (
            <div>
              <StepTitle t="确认创建" s="检查配置。创建后将进入工作台，可立即交办任务。" />
              <div style={{ display: 'flex', alignItems: 'center', gap: 14, padding: 16, background: 'var(--surface-2)', borderRadius: 11, marginTop: 20, marginBottom: 16 }}>
                <CHex size={48} bg={color} fs={18}>{abbr}</CHex>
                <div style={{ flex: 1 }}><div style={{ fontSize: 15, fontWeight: 600 }}>{name || '未命名数字员工'}</div><div style={{ fontSize: 12.5, color: 'var(--text-2)', marginTop: 2 }}>{role || '—'}</div></div>
                <CChip kind="idle"><CIcon name={scope === 'all' ? 'globe' : scope === 'me' ? 'lock' : 'users'} size={11} />{{ me: '仅自己', picked: '指定员工', group: '指定 Group', all: '全公司' }[scope]}</CChip>
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 7, marginBottom: 18 }}>
                {caps.map(id => { const c = CCAPS.find(x => x.id === id); return <CChip key={id} kind="idle" style={{ height: 24 }}>{c.name}</CChip>; })}
              </div>
              <div style={{ display: 'flex', gap: 10, padding: 14, background: 'var(--warn-soft)', borderRadius: 10 }}>
                <span style={{ color: 'var(--honey-deep)', display: 'flex', marginTop: 1 }}><CIcon name="shield" size={16} /></span>
                <div style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.55 }}><b style={{ color: 'var(--honey-deep)' }}>公司治理提示</b> · 「渠道」「工作流」等能力需管理员开放或审批后才能使用，已在能力配置中标记。</div>
              </div>
            </div>
          )}

          {creating && (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', minHeight: 300, gap: 18 }}>
              <CHex size={56} bg={color} fs={20}>{abbr}</CHex>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: 'var(--text-2)' }}>
                <span className="spin" style={{ width: 15, height: 15, border: '2px solid var(--border-2)', borderTopColor: 'var(--honey)', borderRadius: '50%' }} />
                <span style={{ fontSize: 14 }}>正在创建 {name || '数字员工'}…</span>
              </div>
            </div>
          )}
        </CCard>

        {/* footer */}
        {!creating && (
          <div style={{ display: 'flex', alignItems: 'center', marginTop: 20 }}>
            <CBtn variant="ghost" onClick={() => step === 0 ? navigate('employees') : setStep(step - 1)}>{step === 0 ? '取消' : '上一步'}</CBtn>
            <div style={{ flex: 1 }} />
            {step < steps.length - 1
              ? <CBtn variant="primary" icon="arrowRight" disabled={!canNext} onClick={() => setStep(step + 1)}>下一步</CBtn>
              : <CBtn variant="honey" icon="checkPlain" onClick={finish}>创建数字员工</CBtn>}
          </div>
        )}
      </div>
    </div>
  );
}

function StepTitle({ t, s }) {
  return <div><div style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 19 }}>{t}</div><p style={{ fontSize: 13.5, color: 'var(--text-2)', margin: '6px 0 0', lineHeight: 1.55 }}>{s}</p></div>;
}
function LabeledInput({ label, value, onChange, placeholder }) {
  return (
    <div>
      <div className="eyebrow" style={{ marginBottom: 7 }}>{label}</div>
      <input value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder}
        style={{ width: '100%', height: 38, border: '1px solid var(--border-2)', borderRadius: 8, padding: '0 12px', fontFamily: 'var(--sans)', fontSize: 14, outline: 'none', color: 'var(--text-1)' }}
        onFocus={(e) => e.target.style.borderColor = 'var(--text-2)'} onBlur={(e) => e.target.style.borderColor = 'var(--border-2)'} />
    </div>
  );
}

/* ---------------- Modals ---------------- */
function HiveModals({ ctx }) {
  if (ctx.modal === 'saveFlow') return <SaveFlowModal ctx={ctx} />;
  return null;
}
function SaveFlowModal({ ctx }) {
  const [submitted, setSubmitted] = cuS(false);
  return (
    <div onClick={() => ctx.setModal(null)} style={{ position: 'fixed', inset: 0, background: 'rgba(38,36,31,.3)', backdropFilter: 'blur(2px)', zIndex: 100, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div onClick={(e) => e.stopPropagation()} style={{ width: 480, background: 'var(--surface)', borderRadius: 16, boxShadow: 'var(--shadow-pop)', overflow: 'hidden' }}>
        {!submitted ? (
          <>
            <div style={{ padding: '20px 22px 0' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 11, marginBottom: 6 }}>
                <span style={{ width: 36, height: 36, borderRadius: 9, background: 'var(--honey-soft)', color: 'var(--honey-deep)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><CIcon name="flow" size={18} /></span>
                <div style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 17 }}>保存为流程</div>
                <CIconBtn name="x" onClick={() => ctx.setModal(null)} style={{ marginLeft: 'auto' }} />
              </div>
              <p style={{ fontSize: 13, color: 'var(--text-2)', margin: '0 0 18px', lineHeight: 1.55 }}>把这次成功的任务沉淀为可复用流程，下次一键运行，或提交为公司候选资产。</p>
            </div>
            <div style={{ padding: '0 22px 20px' }}>
              <div style={{ marginBottom: 14 }}><div className="eyebrow" style={{ marginBottom: 7 }}>流程名称</div>
                <input defaultValue="竞品对标报告 · Q2" style={{ width: '100%', height: 38, border: '1px solid var(--border-2)', borderRadius: 8, padding: '0 12px', fontFamily: 'var(--sans)', fontSize: 14, outline: 'none' }} /></div>
              <div style={{ marginBottom: 14 }}><div className="eyebrow" style={{ marginBottom: 7 }}>适用场景</div>
                <input defaultValue="季度竞品分析、市场定位更新" style={{ width: '100%', height: 38, border: '1px solid var(--border-2)', borderRadius: 8, padding: '0 12px', fontFamily: 'var(--sans)', fontSize: 14, outline: 'none' }} /></div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: 12, background: 'var(--bg-sunk)', borderRadius: 9 }}>
                <CToggle on={true} onChange={() => {}} />
                <div style={{ flex: 1 }}><div style={{ fontSize: 13, fontWeight: 500 }}>提交为公司候选资产</div><div style={{ fontSize: 11.5, color: 'var(--text-3)' }}>管理层审核通过后进入资产库，供他人复用</div></div>
              </div>
            </div>
            <div style={{ display: 'flex', gap: 9, padding: '14px 22px', borderTop: '1px solid var(--border)', background: 'var(--surface-2)' }}>
              <div style={{ flex: 1 }} />
              <CBtn variant="ghost" onClick={() => ctx.setModal(null)}>取消</CBtn>
              <CBtn variant="primary" icon="checkPlain" onClick={() => setSubmitted(true)}>保存流程</CBtn>
            </div>
          </>
        ) : (
          <div style={{ padding: '40px 30px', textAlign: 'center' }}>
            <span style={{ width: 52, height: 52, borderRadius: '50%', background: 'var(--ok-soft)', color: 'var(--ok)', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', marginBottom: 16 }}><CIcon name="checkPlain" size={26} /></span>
            <div style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 18, marginBottom: 7 }}>已保存为流程</div>
            <p style={{ fontSize: 13, color: 'var(--text-2)', margin: '0 0 20px', lineHeight: 1.55 }}>「竞品对标报告 · Q2」已加入你的流程，并提交为公司候选资产等待审核。</p>
            <CBtn variant="primary" onClick={() => ctx.setModal(null)}>完成</CBtn>
          </div>
        )}
      </div>
    </div>
  );
}

window.HivePages = window.HivePages || {};
window.HivePages.create = CreatePage;
window.HiveModals = HiveModals;
