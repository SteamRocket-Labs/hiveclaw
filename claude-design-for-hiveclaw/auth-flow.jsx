/* ============================================================
   Hive Prototype — Entry flow: Auth + Workspace onboarding
   Gates the App. Stages: auth → space → create → (app)
   Exposes window.HiveAuth = { Gate }
   ============================================================ */
const NU = window.HiveUI;
const { Icon: NIcon, Hex: NHex, Chip: NChip, Btn: NBtn, IconBtn: NIconBtn, Card: NCard, Toggle: NToggle, ME: NME } = NU;
const { useState: nuS, useEffect: nuE, Fragment: NFrag } = NU;

/* ---------- honeycomb deco ---------- */
function Honeycomb({ style }) {
  const cells = [];
  const cols = 5, rows = 6, w = 46, h = 52;
  for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) {
    const x = c * w * 0.86, y = r * h + (c % 2 ? h / 2 : 0);
    const lit = (r + c) % 4 === 0;
    cells.push(
      <span key={r + '-' + c} className="hex" style={{
        position: 'absolute', left: x, top: y, width: w - 6, height: h - 6,
        background: lit ? 'rgba(255,255,255,.13)' : 'transparent',
        border: '1px solid rgba(255,255,255,.10)',
      }} />
    );
  }
  return <div style={{ position: 'absolute', ...style }}>{cells}</div>;
}

/* ---------- brand panel (left of auth) ---------- */
function BrandPanel() {
  return (
    <div style={{ position: 'relative', width: 460, flex: '0 0 460px', background: 'var(--text-1)', color: '#fff', overflow: 'hidden', display: 'flex', flexDirection: 'column', padding: 48 }}>
      <Honeycomb style={{ top: -30, right: -60, opacity: .9 }} />
      <Honeycomb style={{ bottom: -40, left: -80, opacity: .5 }} />
      <div style={{ position: 'relative', display: 'flex', alignItems: 'center', gap: 11 }}>
        <NHex size={34} bg="var(--honey)" fs={15}>H</NHex>
        <span style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 19 }}>Hive</span>
      </div>
      <div style={{ position: 'relative', marginTop: 'auto' }}>
        <div style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 33, lineHeight: 1.18, letterSpacing: '-.01em' }}>
          让每个人都<br />拥有一支<span style={{ color: 'var(--honey)' }}>数字员工</span>团队
        </div>
        <p style={{ fontSize: 14.5, lineHeight: 1.7, color: 'rgba(255,255,255,.6)', marginTop: 20, maxWidth: '36ch' }}>
          创建、配置、交办任务。Hive 的数字员工会先给出计划、经你确认后执行，并在需要时彼此协作。
        </p>
        <div style={{ display: 'flex', gap: 8, marginTop: 28 }}>
          {['先计划后执行', 'A2A 协作', '企业级治理'].map((t) => (
            <span key={t} style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'rgba(255,255,255,.75)', border: '1px solid rgba(255,255,255,.18)', borderRadius: 999, padding: '5px 11px', whiteSpace: 'nowrap' }}>{t}</span>
          ))}
        </div>
      </div>
      <div style={{ position: 'relative', marginTop: 40, fontFamily: 'var(--mono)', fontSize: 11, color: 'rgba(255,255,255,.4)' }}>
        © 2026 Hive · 企业 AI 数字员工平台
      </div>
    </div>
  );
}

/* ---------- text field ---------- */
function TField({ label, type = 'text', value, onChange, placeholder, suffix, autoFocus }) {
  const [focus, setFocus] = nuS(false);
  return (
    <label style={{ display: 'block', marginBottom: 15 }}>
      <span className="eyebrow" style={{ display: 'block', marginBottom: 7 }}>{label}</span>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, height: 42, padding: '0 13px', background: 'var(--surface)',
        border: `1.5px solid ${focus ? 'var(--text-1)' : 'var(--border-2)'}`, borderRadius: 9, transition: 'border-color .12s' }}>
        <input type={type} value={value} autoFocus={autoFocus} onFocus={() => setFocus(true)} onBlur={() => setFocus(false)}
          onChange={(e) => onChange(e.target.value)} placeholder={placeholder}
          style={{ flex: 1, border: 0, outline: 'none', background: 'transparent', fontFamily: 'var(--sans)', fontSize: 14, color: 'var(--text-1)' }} />
        {suffix}
      </div>
    </label>
  );
}

/* ---------- SSO row ---------- */
function SSOButtons() {
  const items = [['飞书', 'oklch(0.6 0.13 250)'], ['企业微信', 'oklch(0.58 0.12 150)'], ['SSO', 'var(--text-2)']];
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 9 }}>
      {items.map(([t, c]) => (
        <button key={t} style={{ height: 40, border: '1px solid var(--border-2)', borderRadius: 9, background: 'var(--surface)', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 7, fontFamily: 'var(--sans)', fontSize: 12.5, fontWeight: 500, color: 'var(--text-2)' }}
          onMouseEnter={(e) => e.currentTarget.style.background = 'var(--hover)'} onMouseLeave={(e) => e.currentTarget.style.background = 'var(--surface)'}>
          <span className="hex" style={{ width: 16, height: 16, background: c, display: 'inline-block' }} />{t}
        </button>
      ))}
    </div>
  );
}

/* ---------- AUTH screen ---------- */
function AuthScreen({ onAuthed }) {
  const [mode, setMode] = nuS('login');
  const [email, setEmail] = nuS('jen@acme.com');
  const [pw, setPw] = nuS('••••••••');
  const [name, setName] = nuS('');
  const [show, setShow] = nuS(false);
  const isReg = mode === 'register';
  const ok = email && pw && (!isReg || name);

  return (
    <div style={{ display: 'flex', height: '100vh', background: 'var(--bg)' }}>
      <BrandPanel />
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 40, overflow: 'auto' }} className="thin-scroll">
        <div style={{ width: 360 }}>
          <div style={{ display: 'inline-flex', background: 'var(--bg-sunk)', borderRadius: 9, padding: 3, marginBottom: 26 }}>
            {[['login', '登录'], ['register', '注册']].map(([m, t]) => (
              <button key={m} onClick={() => setMode(m)} style={{ height: 30, padding: '0 18px', border: 0, borderRadius: 6, cursor: 'pointer', fontFamily: 'var(--sans)', fontSize: 13, fontWeight: mode === m ? 600 : 500,
                background: mode === m ? 'var(--surface)' : 'transparent', color: mode === m ? 'var(--text-1)' : 'var(--text-3)', boxShadow: mode === m ? 'var(--shadow-1)' : 'none' }}>{t}</button>
            ))}
          </div>

          <h1 style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 25, margin: '0 0 6px', letterSpacing: '-.01em' }}>
            {isReg ? '创建你的账户' : '欢迎回来'}
          </h1>
          <p style={{ fontSize: 13.5, color: 'var(--text-2)', margin: '0 0 26px' }}>
            {isReg ? '注册后即可加入或创建一个 workspace。' : '登录以继续你的数字员工工作区。'}
          </p>

          {isReg && <TField label="姓名 · Name" value={name} onChange={setName} placeholder="你的名字" autoFocus />}
          <TField label="邮箱 · Email" type="email" value={email} onChange={setEmail} placeholder="you@company.com" autoFocus={!isReg} />
          <TField label="密码 · Password" type={show ? 'text' : 'password'} value={pw} onChange={setPw} placeholder="••••••••"
            suffix={<button onClick={() => setShow(!show)} style={{ border: 0, background: 'transparent', cursor: 'pointer', color: 'var(--text-3)', display: 'flex' }}><NIcon name="eye" size={16} /></button>} />

          {!isReg && (
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: -4, marginBottom: 18 }}>
              <span style={{ fontSize: 12.5, color: 'var(--honey-deep)', cursor: 'pointer' }}>忘记密码？</span>
            </div>
          )}

          <NBtn variant="primary" size="lg" icon="arrowRight" onClick={() => ok && onAuthed({ name: name || NME.name, email })} disabled={!ok}
            style={{ width: '100%', marginTop: isReg ? 8 : 0, flexDirection: 'row-reverse' }}>
            {isReg ? '创建账户' : '登录'}
          </NBtn>

          <div style={{ display: 'flex', alignItems: 'center', gap: 12, margin: '22px 0' }}>
            <span style={{ flex: 1, height: 1, background: 'var(--border)' }} />
            <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-4)' }}>或使用</span>
            <span style={{ flex: 1, height: 1, background: 'var(--border)' }} />
          </div>
          <SSOButtons />

          <p style={{ fontSize: 12, color: 'var(--text-3)', textAlign: 'center', marginTop: 26, lineHeight: 1.6 }}>
            {isReg ? <>注册即代表同意 <span style={{ color: 'var(--text-2)', textDecoration: 'underline' }}>服务条款</span> 与 <span style={{ color: 'var(--text-2)', textDecoration: 'underline' }}>隐私政策</span></>
              : <>还没有账户？ <span onClick={() => setMode('register')} style={{ color: 'var(--honey-deep)', fontWeight: 600, cursor: 'pointer' }}>立即注册</span></>}
          </p>
        </div>
      </div>
    </div>
  );
}

/* ---------- WORKSPACE picker ---------- */
function SpaceScreen({ user, onEnter, onCreate }) {
  const existing = [
    { name: 'Acme Inc.', domain: 'hive.acme.com', members: 128, role: '管理员', color: 'var(--honey)', abbr: 'H' },
  ];
  const invites = [
    { name: 'Nova Labs', domain: 'hive.nova.io', from: '王磊', members: 42, color: 'oklch(0.58 0.12 250)', abbr: 'N' },
    { name: '青柠设计', domain: 'hive.qingn.com', from: '李工', members: 16, color: 'oklch(0.58 0.12 150)', abbr: '青' },
  ];
  return (
    <div style={{ height: '100vh', overflow: 'auto', background: 'var(--bg)', display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '56px 24px' }} className="thin-scroll">
      <div style={{ position: 'absolute', top: 28, left: 32, display: 'flex', alignItems: 'center', gap: 10 }}>
        <NHex size={28} bg="var(--honey)" fs={13}>H</NHex><span style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 16 }}>Hive</span>
      </div>
      <div style={{ position: 'absolute', top: 28, right: 32, display: 'flex', alignItems: 'center', gap: 9 }}>
        <NHex size={26} bg={NME.color} fs={10}>{NME.abbr}</NHex>
        <span style={{ fontSize: 13, color: 'var(--text-2)' }}>{user.email}</span>
      </div>

      <div style={{ width: 540, maxWidth: '100%' }}>
        <div style={{ textAlign: 'center', marginBottom: 32 }}>
          <h1 style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 27, margin: '0 0 8px', letterSpacing: '-.01em' }}>选择一个 workspace</h1>
          <p style={{ fontSize: 14, color: 'var(--text-2)', margin: 0 }}>进入你已加入的团队，或创建一个新的 workspace。</p>
        </div>

        <div className="eyebrow" style={{ marginBottom: 11 }}>你的 workspace</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginBottom: 26 }}>
          {existing.map((w) => (
            <NCard key={w.domain} hover pad={16} onClick={() => onEnter(w)}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
                <NHex size={42} bg={w.color} fs={16}>{w.abbr}</NHex>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontSize: 15, fontWeight: 600, whiteSpace: 'nowrap' }}>{w.name}</span>
                    <NChip kind="warn">{w.role}</NChip>
                  </div>
                  <div className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 3, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{w.domain} · {w.members} 名成员</div>
                </div>
                <NBtn size="sm" variant="primary" icon="arrowRight" style={{ flexDirection: 'row-reverse', flex: '0 0 auto' }}>进入</NBtn>
              </div>
            </NCard>
          ))}
        </div>

        <div className="eyebrow" style={{ marginBottom: 11 }}>待接受的邀请 · {invites.length}</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginBottom: 26 }}>
          {invites.map((w) => (
            <NCard key={w.domain} pad={16}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
                <NHex size={42} bg={w.color} fs={16}>{w.abbr}</NHex>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 15, fontWeight: 600, whiteSpace: 'nowrap' }}>{w.name}</div>
                  <div className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 3, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{w.from} 邀请你加入 · {w.members} 名成员</div>
                </div>
                <div style={{ display: 'flex', gap: 8 }}>
                  <NBtn size="sm" variant="ghost" style={{ color: 'var(--text-3)' }}>忽略</NBtn>
                  <NBtn size="sm" variant="secondary" onClick={() => onEnter(w)}>接受并加入</NBtn>
                </div>
              </div>
            </NCard>
          ))}
        </div>

        <div style={{ display: 'flex', gap: 12 }}>
          <NCard hover pad={16} onClick={onCreate} style={{ flex: 1, borderStyle: 'dashed', display: 'flex', alignItems: 'center', gap: 12 }}>
            <div style={{ width: 38, height: 38, borderRadius: 10, background: 'var(--honey-soft)', color: 'var(--honey-deep)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><NIcon name="plus" size={19} /></div>
            <div><div style={{ fontSize: 14, fontWeight: 600 }}>创建新 workspace</div><div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 2 }}>为你的团队搭建数字员工</div></div>
          </NCard>
          <NCard hover pad={16} style={{ flex: 1, borderStyle: 'dashed', display: 'flex', alignItems: 'center', gap: 12 }}>
            <div style={{ width: 38, height: 38, borderRadius: 10, background: 'var(--bg-sunk)', color: 'var(--text-2)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><NIcon name="link" size={19} /></div>
            <div><div style={{ fontSize: 14, fontWeight: 600 }}>用邀请码加入</div><div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 2 }}>输入团队邀请码或链接</div></div>
          </NCard>
        </div>
      </div>
    </div>
  );
}

/* ---------- CREATE workspace (multi-step) ---------- */
function CreateScreen({ onBack, onDone }) {
  const [step, setStep] = nuS(0);
  const [name, setName] = nuS('');
  const [size, setSize] = nuS('11–50');
  const [role, setRole] = nuS('产品');
  const [invites, setInvites] = nuS(['', '', '']);
  const domain = name ? name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') : 'your-team';

  const steps = ['基本信息', '你的角色', '邀请团队'];
  const next = () => step < 2 ? setStep(step + 1) : onDone({ name: name || '我的团队', domain: `hive.${domain}.com` });

  return (
    <div style={{ height: '100vh', overflow: 'auto', background: 'var(--bg)', display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '52px 24px' }} className="thin-scroll">
      <div style={{ position: 'absolute', top: 28, left: 32, display: 'flex', alignItems: 'center', gap: 10 }}>
        <NHex size={28} bg="var(--honey)" fs={13}>H</NHex><span style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 16 }}>Hive</span>
      </div>

      <div style={{ width: 440, maxWidth: '100%' }}>
        {/* stepper */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 0, marginBottom: 34 }}>
          {steps.map((s, i) => (
            <NFrag key={i}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ width: 24, height: 24, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', fontFamily: 'var(--mono)', fontSize: 11, fontWeight: 600,
                  background: i < step ? 'var(--ok)' : i === step ? 'var(--text-1)' : 'var(--bg-sunk)', color: i <= step ? '#fff' : 'var(--text-3)', border: i > step ? '1px solid var(--border-2)' : 0 }}>
                  {i < step ? <NIcon name="checkPlain" size={12} /> : i + 1}</span>
                <span style={{ fontSize: 12.5, fontWeight: i === step ? 600 : 500, color: i === step ? 'var(--text-1)' : 'var(--text-3)' }}>{s}</span>
              </div>
              {i < 2 && <span style={{ flex: 1, height: 1.5, background: i < step ? 'var(--ok)' : 'var(--border-2)', margin: '0 12px' }} />}
            </NFrag>
          ))}
        </div>

        {step === 0 && (
          <>
            <h1 style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 24, margin: '0 0 6px' }}>创建你的 workspace</h1>
            <p style={{ fontSize: 13.5, color: 'var(--text-2)', margin: '0 0 24px' }}>给团队起个名字，稍后可随时修改。</p>
            <TField label="Workspace 名称" value={name} onChange={setName} placeholder="例如：Acme Inc." autoFocus />
            <label style={{ display: 'block', marginBottom: 18 }}>
              <span className="eyebrow" style={{ display: 'block', marginBottom: 7 }}>访问域名</span>
              <div style={{ display: 'flex', alignItems: 'center', height: 42, padding: '0 13px', background: 'var(--bg-sunk)', border: '1.5px solid var(--border)', borderRadius: 9 }}>
                <span className="mono" style={{ fontSize: 13.5, color: 'var(--text-1)' }}>hive.{domain}.com</span>
              </div>
            </label>
            <span className="eyebrow" style={{ display: 'block', marginBottom: 9 }}>团队规模</span>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {['1–10', '11–50', '51–200', '200+'].map((s) => (
                <button key={s} onClick={() => setSize(s)} style={{ height: 36, padding: '0 16px', borderRadius: 8, cursor: 'pointer', fontFamily: 'var(--sans)', fontSize: 13, fontWeight: size === s ? 600 : 500,
                  border: `1.5px solid ${size === s ? 'var(--text-1)' : 'var(--border-2)'}`, background: 'var(--surface)', color: size === s ? 'var(--text-1)' : 'var(--text-2)' }}>{s} 人</button>
              ))}
            </div>
          </>
        )}

        {step === 1 && (
          <>
            <h1 style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 24, margin: '0 0 6px' }}>你在团队里的角色</h1>
            <p style={{ fontSize: 13.5, color: 'var(--text-2)', margin: '0 0 24px' }}>这会帮我们为你推荐合适的数字员工模板。</p>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
              {[['产品', 'box'], ['研发', 'gear'], ['运营', 'flow'], ['财务', 'coins'], ['人事', 'users'], ['市场', 'sparkle']].map(([r, ic]) => (
                <div key={r} onClick={() => setRole(r)} style={{ display: 'flex', alignItems: 'center', gap: 11, padding: 14, borderRadius: 10, cursor: 'pointer',
                  border: `1.5px solid ${role === r ? 'var(--text-1)' : 'var(--border)'}`, background: 'var(--surface)' }}>
                  <span style={{ width: 32, height: 32, borderRadius: 8, background: role === r ? 'var(--text-1)' : 'var(--bg-sunk)', color: role === r ? '#fff' : 'var(--text-2)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}><NIcon name={ic} size={16} /></span>
                  <span style={{ fontSize: 13.5, fontWeight: 600 }}>{r}</span>
                  {role === r && <span style={{ marginLeft: 'auto', color: 'var(--honey)' }}><NIcon name="checkPlain" size={16} /></span>}
                </div>
              ))}
            </div>
          </>
        )}

        {step === 2 && (
          <>
            <h1 style={{ fontFamily: 'var(--display)', fontWeight: 600, fontSize: 24, margin: '0 0 6px' }}>邀请你的团队</h1>
            <p style={{ fontSize: 13.5, color: 'var(--text-2)', margin: '0 0 24px' }}>可以现在邀请，也可以稍后在控制中台添加。</p>
            {invites.map((v, i) => (
              <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8, height: 42, padding: '0 13px', background: 'var(--surface)', border: '1.5px solid var(--border-2)', borderRadius: 9, marginBottom: 9 }}>
                <NIcon name="users" size={15} style={{ color: 'var(--text-4)' }} />
                <input value={v} onChange={(e) => setInvites(invites.map((x, j) => j === i ? e.target.value : x))} placeholder="同事的邮箱"
                  style={{ flex: 1, border: 0, outline: 'none', background: 'transparent', fontFamily: 'var(--sans)', fontSize: 13.5, color: 'var(--text-1)' }} />
              </div>
            ))}
            <button onClick={() => setInvites([...invites, ''])} style={{ display: 'flex', alignItems: 'center', gap: 7, border: 0, background: 'transparent', cursor: 'pointer', color: 'var(--text-3)', fontFamily: 'var(--sans)', fontSize: 13, padding: '6px 0' }}>
              <NIcon name="plus" size={15} />添加更多
            </button>
            <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginTop: 16, padding: '12px 14px', background: 'var(--honey-soft)', borderRadius: 10 }}>
              <NIcon name="link" size={16} style={{ color: 'var(--honey-deep)' }} />
              <span style={{ flex: 1, fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--honey-deep)' }}>hive.{domain}.com/join/X8F2</span>
              <NBtn size="sm" variant="secondary" icon="copy">复制邀请链接</NBtn>
            </div>
          </>
        )}

        <div style={{ display: 'flex', gap: 10, marginTop: 30 }}>
          <NBtn variant="ghost" onClick={() => step === 0 ? onBack() : setStep(step - 1)} style={{ color: 'var(--text-2)' }}>{step === 0 ? '返回' : '上一步'}</NBtn>
          {step === 2 && <NBtn variant="secondary" onClick={() => onDone({ name: name || '我的团队', domain: `hive.${domain}.com` })} style={{ marginLeft: 'auto' }}>跳过</NBtn>}
          <NBtn variant="primary" icon="arrowRight" onClick={next} disabled={step === 0 && !name} style={{ marginLeft: step === 2 ? 0 : 'auto', flexDirection: 'row-reverse' }}>
            {step === 2 ? '完成并进入' : '继续'}
          </NBtn>
        </div>
      </div>
    </div>
  );
}

/* ---------- GATE ---------- */
function Gate() {
  const [stage, setStage] = nuS('auth'); // auth | space | create | app
  const [user, setUser] = nuS(null);

  nuE(() => { window.HiveReturnToEntry = (s) => setStage(s || 'space'); }, []);

  if (stage === 'app') return <window.HiveShell.App />;
  if (stage === 'auth') return <AuthScreen onAuthed={(u) => { setUser(u); setStage('space'); }} />;
  if (stage === 'space') return <SpaceScreen user={user} onEnter={() => setStage('app')} onCreate={() => setStage('create')} />;
  if (stage === 'create') return <CreateScreen onBack={() => setStage('space')} onDone={() => setStage('app')} />;
  return null;
}

window.HiveAuth = { Gate };
