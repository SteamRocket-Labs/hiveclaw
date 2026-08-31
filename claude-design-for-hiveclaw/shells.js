/* ============================================================
   Hive — Phase 1 shell mocks + shared mini-components
   Pure vanilla. Renders into the foundation page.
   ============================================================ */
(function () {
  'use strict';

  /* ---------- tiny helpers ---------- */
  const h = (html) => html;
  const hex = (label, bg, fg, size = 26, fs = 11) =>
    `<span class="hex" style="display:inline-flex;align-items:center;justify-content:center;
      width:${size}px;height:${size}px;background:${bg};color:${fg};
      font-family:var(--mono);font-weight:600;font-size:${fs}px;flex:0 0 auto;">${label}</span>`;

  const chip = (text, kind = 'idle') => {
    const map = {
      idle:   ['var(--bg-sunk)', 'var(--text-2)', 'var(--border-2)'],
      ok:     ['var(--ok-soft)', 'var(--ok)', 'transparent'],
      warn:   ['var(--warn-soft)', 'var(--honey-deep)', 'transparent'],
      danger: ['var(--danger-soft)', 'var(--danger)', 'transparent'],
      info:   ['var(--info-soft)', 'var(--info)', 'transparent'],
      purple: ['var(--purple-soft)', 'var(--purple)', 'transparent'],
    };
    const [bg, fg, bd] = map[kind] || map.idle;
    return `<span style="display:inline-flex;align-items:center;gap:5px;height:20px;padding:0 8px;
      border-radius:999px;background:${bg};color:${fg};border:1px solid ${bd};
      font-family:var(--mono);font-size:10.5px;font-weight:500;white-space:nowrap;">${text}</span>`;
  };
  const dot = (c) => `<span style="width:6px;height:6px;border-radius:50%;background:${c};display:inline-block;flex:0 0 auto;"></span>`;

  /* ---------- icon set (1.5px stroke, currentColor) ---------- */
  const I = (p, sz = 17) =>
    `<svg width="${sz}" height="${sz}" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" style="flex:0 0 auto;">${p}</svg>`;
  const icons = {
    home: I('<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/>'),
    bot: I('<rect x="4" y="8" width="16" height="11" rx="2.5"/><path d="M12 8V4M9 13h.01M15 13h.01"/><path d="M2 13h2M20 13h2"/>'),
    chat: I('<path d="M21 12a8 8 0 0 1-11.5 7.2L3 21l1.8-6.5A8 8 0 1 1 21 12Z"/>'),
    flow: I('<rect x="3" y="3" width="6" height="6" rx="1.5"/><rect x="15" y="15" width="6" height="6" rx="1.5"/><path d="M9 6h4a2 2 0 0 1 2 2v7"/>'),
    brain: I('<path d="M9 4a3 3 0 0 0-3 3 3 3 0 0 0-1 5 3 3 0 0 0 2 4 3 3 0 0 0 5 1 3 3 0 0 0 5-1 3 3 0 0 0 2-4 3 3 0 0 0-1-5 3 3 0 0 0-3-3 3 3 0 0 0-3.5-1A3 3 0 0 0 9 4Z"/><path d="M12 4v15"/>'),
    doc: I('<path d="M6 3h8l4 4v14H6Z"/><path d="M14 3v4h4M9 12h6M9 16h6"/>'),
    check: I('<path d="M4 12a8 8 0 1 0 16 0 8 8 0 0 0-16 0Z"/><path d="M9 12l2 2 4-4"/>'),
    gear: I('<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>'),
    users: I('<circle cx="9" cy="8" r="3"/><path d="M3 20a6 6 0 0 1 12 0M16 5.5a3 3 0 0 1 0 5.8M21 20a6 6 0 0 0-4-5.6"/>'),
    grid: I('<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>'),
    search: I('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>'),
    plus: I('<path d="M12 5v14M5 12h14"/>'),
    chevron: I('<path d="m9 6 6 6-6 6"/>', 14),
    box: I('<path d="M21 8 12 3 3 8l9 5 9-5Z"/><path d="M3 8v8l9 5 9-5V8"/>'),
    shield: I('<path d="M12 3 5 6v5c0 4 3 7 7 9 4-2 7-5 7-9V6Z"/><path d="M9 12l2 2 4-4"/>'),
    coins: I('<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/>'),
    plug: I('<path d="M9 7V3M15 7V3M7 7h10v4a5 5 0 0 1-10 0Z"/><path d="M12 16v5"/>'),
    audit: I('<path d="M6 3h9l3 3v15H6Z"/><path d="M9 8h6M9 12h6M9 16h4"/>'),
    bolt: I('<path d="M13 2 4 14h6l-1 8 9-12h-6Z"/>'),
  };

  /* ============================================================
     IA DATA
     ============================================================ */
  const iaEmployee = {
    title: '我的工作区', en: 'My Workspace',
    groups: [
      { items: [['home', '首页', 'Home']] },
      { items: [
        ['bot', '数字员工', 'Digital Employees', ['我创建的 · My agents', '公司推荐 · Recommended', '协作中 · Shared with me']],
        ['chat', '对话与任务', 'Conversations & Tasks'],
        ['check', '计划确认', 'Plan Review'],
      ]},
      { items: [
        ['flow', '自动化', 'Automations'],
        ['brain', '记忆与知识', 'Memory & Knowledge'],
        ['doc', '文档与研究', 'Documents & Research'],
        ['shield', '审批', 'Approvals'],
      ]},
    ],
  };
  const iaAdmin = {
    title: '公司控制中台', en: 'Control Plane',
    groups: [
      { items: [['grid', '公司总览', 'Overview']] },
      { items: [
        ['users', '成员与组织', 'Members & Org'],
        ['bot', '数字员工治理', 'Agent Governance'],
      ]},
      { items: [
        ['coins', '模型与预算', 'Models & Budget'],
        ['gear', '能力与工具', 'Capabilities & Tools'],
        ['brain', '记忆治理', 'Memory Governance'],
        ['plug', '渠道连接', 'Channels'],
      ]},
      { items: [
        ['shield', '审批中心', 'Approvals'],
        ['audit', '审计记录', 'Audit Log'],
        ['box', '自动化与资产库', 'Assets Library'],
      ]},
    ],
  };

  /* ---------- IA column renderer ---------- */
  function iaColumn(model, tone) {
    const accent = tone === 'admin' ? 'var(--text-1)' : 'var(--honey)';
    const mark = tone === 'admin'
      ? hex(icons.shield ? '' : '', 'var(--text-1)', '#fff', 30, 0).replace('></span>', `>${I('<path d="M12 3 5 6v5c0 4 3 7 7 9 4-2 7-5 7-9V6Z"/>', 15)}</span>`)
      : hex('H', 'var(--honey)', '#fff', 30, 13);
    let rows = '';
    model.groups.forEach((g, gi) => {
      g.items.forEach(([ic, cn, en, kids]) => {
        rows += `<div style="display:flex;align-items:center;gap:9px;padding:7px 10px;border-radius:6px;">
          <span style="color:var(--text-3);display:flex;">${icons[ic]}</span>
          <span style="font-size:13.5px;font-weight:500;color:var(--text-1);">${cn}</span>
          <span class="mono" style="font-size:10.5px;color:var(--text-4);margin-left:auto;">${en}</span>
        </div>`;
        if (kids) kids.forEach((k) => {
          rows += `<div style="display:flex;align-items:center;gap:8px;padding:4px 10px 4px 31px;">
            <span style="width:5px;height:5px;border-radius:50%;background:var(--border-2);"></span>
            <span style="font-size:12px;color:var(--text-2);">${k}</span></div>`;
        });
      });
      if (gi < model.groups.length - 1)
        rows += `<div style="height:1px;background:var(--border);margin:7px 8px;"></div>`;
    });
    return `<div style="flex:1;min-width:0;background:var(--surface);border:1px solid var(--border);
        border-radius:var(--r-lg);padding:16px;box-shadow:var(--shadow-1);">
      <div style="display:flex;align-items:center;gap:11px;margin-bottom:14px;padding:0 2px;">
        ${mark}
        <div><div style="font-family:var(--display);font-weight:600;font-size:15px;">${model.title}</div>
        <div class="mono" style="font-size:10.5px;color:var(--text-3);">${model.en}</div></div>
        <span style="margin-left:auto;">${chip(tone === 'admin' ? '管理层 · Admin' : '员工 · Employee', tone === 'admin' ? 'idle' : 'warn')}</span>
      </div>
      ${rows}
    </div>`;
  }

  /* ============================================================
     SAMPLE CONTENT — reused inside all 3 shells (employee: 数字员工列表)
     ============================================================ */
  const agents = [
    ['DR', '调研助理 Atlas', '市场与竞品研究', 'ok', '运行中', 'oklch(0.62 0.13 250)'],
    ['FC', '财务对账 Ledger', '月度对账与报表', 'warn', '待确认', 'oklch(0.66 0.12 145)'],
    ['HR', '招聘协调 Pace', '简历筛选与排期', 'idle', '空闲', 'oklch(0.62 0.12 25)'],
    ['OP', '运营自动化 Relay', '工单分发与跟进', 'info', 'A2A 协作', 'oklch(0.58 0.11 300)'],
    ['SE', '安全审查 Warden', '合规与风险扫描', 'danger', '最近失败', 'oklch(0.55 0.05 60)'],
    ['WR', '文档撰写 Quill', '报告与纪要生成', 'ok', '运行中', 'oklch(0.64 0.12 200)'],
  ];

  function employeeListPage(scale) {
    const cards = agents.map(([ab, name, role, st, stl, c]) => `
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--r-lg);
        padding:15px;box-shadow:var(--shadow-1);">
        <div style="display:flex;align-items:flex-start;gap:11px;">
          ${hex(ab, c, '#fff', 36, 13)}
          <div style="min-width:0;flex:1;">
            <div style="font-weight:600;font-size:14px;line-height:1.2;">${name}</div>
            <div style="font-size:12px;color:var(--text-2);margin-top:3px;">${role}</div>
          </div>
          <span style="color:var(--text-4);">${icons.chevron}</span>
        </div>
        <div style="display:flex;align-items:center;gap:7px;margin-top:13px;padding-top:12px;border-top:1px solid var(--border);">
          ${chip(stl, st)}
          ${chip('+4 能力', 'idle')}
        </div>
      </div>`).join('');

    return `<div style="padding:30px 38px;">
      <div class="mono" style="font-size:11px;color:var(--text-3);display:flex;align-items:center;gap:7px;margin-bottom:14px;">
        我的工作区 <span style="color:var(--text-4);">/</span> Digital Employees
      </div>
      <div style="display:flex;align-items:flex-end;gap:14px;margin-bottom:6px;">
        <h1 style="font-family:var(--display);font-weight:600;font-size:30px;margin:0;letter-spacing:-.01em;">数字员工</h1>
        <span class="mono" style="font-size:12px;color:var(--text-3);padding-bottom:6px;">6 active · 2 shared</span>
      </div>
      <p style="font-size:14px;color:var(--text-2);max-width:560px;margin:0 0 22px;">
        你的 AI 工作伙伴。创建、配置、交办任务，并查看它们的进度与产物。</p>
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:20px;flex-wrap:wrap;">
        <div style="display:flex;align-items:center;gap:8px;height:34px;padding:0 12px;background:var(--surface);
          border:1px solid var(--border-2);border-radius:var(--r);color:var(--text-3);min-width:210px;">
          ${icons.search}<span style="font-size:13px;color:var(--text-3);">搜索数字员工…</span>
          <span class="mono" style="margin-left:auto;font-size:10px;color:var(--text-4);border:1px solid var(--border-2);border-radius:4px;padding:1px 5px;">⌘K</span>
        </div>
        ${['状态', '职责', '能力', '可见范围'].map(f => `<button style="height:34px;padding:0 12px;background:var(--surface);
          border:1px solid var(--border-2);border-radius:var(--r);font-family:var(--sans);font-size:12.5px;
          color:var(--text-2);display:inline-flex;align-items:center;gap:6px;cursor:pointer;">${f}
          <span style="color:var(--text-4);">${icons.chevron}</span></button>`).join('')}
        <button style="margin-left:auto;height:34px;padding:0 14px;background:var(--text-1);color:#fff;border:0;
          border-radius:var(--r);font-family:var(--sans);font-weight:600;font-size:13px;display:inline-flex;
          align-items:center;gap:7px;cursor:pointer;box-shadow:var(--shadow-1);">
          <span style="display:flex;">${icons.plus}</span>创建数字员工</button>
      </div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:14px;">${cards}</div>
    </div>`;
  }

  /* ============================================================
     SHELL CHROME — three directions
     ============================================================ */

  // shared sidebar tree (Notion-style) for A & C
  function notionTree(activeAdmin) {
    const model = activeAdmin ? iaAdmin : iaEmployee;
    let html = '';
    model.groups.forEach((g, gi) => {
      g.items.forEach(([ic, cn, en, kids], idx) => {
        const active = gi === 1 && idx === 0 && !activeAdmin;
        html += `<div style="display:flex;align-items:center;gap:8px;height:30px;padding:0 8px;border-radius:6px;
          ${active ? 'background:var(--hover);' : ''}cursor:pointer;">
          <span style="color:${active ? 'var(--text-1)' : 'var(--text-3)'};display:flex;">${icons[ic]}</span>
          <span style="font-size:13.5px;font-weight:${active ? 600 : 500};color:${active ? 'var(--text-1)' : 'var(--text-2)'};">${cn}</span>
          ${kids ? `<span style="margin-left:auto;color:var(--text-4);transform:rotate(90deg);display:flex;">${icons.chevron}</span>` : ''}
        </div>`;
        if (kids && active) kids.forEach(k => {
          html += `<div style="padding:3px 8px 3px 30px;"><div style="padding:5px 8px;border-radius:5px;font-size:12.5px;color:var(--text-2);">${k.split(' · ')[0]}</div></div>`;
        });
      });
      if (gi < model.groups.length - 1) html += `<div style="height:11px;"></div>`;
    });
    return html;
  }

  function workspaceSwitcher() {
    return `<button style="display:flex;align-items:center;gap:10px;width:100%;padding:8px;border-radius:8px;
      border:0;background:transparent;cursor:pointer;text-align:left;">
      ${hex('H', 'var(--honey)', '#fff', 28, 13)}
      <div style="min-width:0;flex:1;">
        <div style="font-family:var(--display);font-weight:600;font-size:13.5px;line-height:1.1;">Acme Inc.</div>
        <div class="mono" style="font-size:10px;color:var(--text-3);">hive.acme.com</div>
      </div>
      <span style="color:var(--text-4);display:flex;flex-direction:column;margin-left:auto;">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m8 9 4-4 4 4M8 15l4 4 4-4"/></svg>
      </span>
    </button>`;
  }

  function userFooter() {
    return `<div style="display:flex;align-items:center;gap:9px;padding:8px;border-top:1px solid var(--border);">
      <span class="hex" style="display:inline-flex;width:26px;height:26px;background:oklch(0.6 0.1 280);"></span>
      <div style="min-width:0;flex:1;"><div style="font-size:12.5px;font-weight:500;">示例用户 A</div>
      <div class="mono" style="font-size:10px;color:var(--text-3);">产品 · Product</div></div>
      <span style="color:var(--text-4);">${icons.gear}</span>
    </div>`;
  }

  // ---- Direction A: Pure Notion Tree ----
  function shellA() {
    return `<div style="display:flex;height:100%;background:var(--bg);">
      <aside style="width:252px;flex:0 0 252px;background:var(--bg);border-right:1px solid var(--border);
        display:flex;flex-direction:column;">
        <div style="padding:10px 10px 6px;">${workspaceSwitcher()}</div>
        <div style="padding:2px 12px 10px;">
          <div style="display:flex;align-items:center;gap:8px;height:32px;padding:0 9px;background:var(--surface);
            border:1px solid var(--border);border-radius:7px;color:var(--text-3);">
            ${icons.search}<span style="font-size:12.5px;">搜索 Search</span>
            <span class="mono" style="margin-left:auto;font-size:10px;color:var(--text-4);">⌘K</span></div>
        </div>
        <div class="thin-scroll" style="flex:1;overflow:auto;padding:0 12px;">
          ${notionTree(false)}
          <div style="height:14px;"></div>
          <div style="display:flex;align-items:center;gap:8px;height:30px;padding:0 8px;border-radius:6px;cursor:pointer;">
            <span style="color:var(--text-3);display:flex;">${icons.shield}</span>
            <span style="font-size:13.5px;font-weight:500;color:var(--text-2);">公司控制中台</span>
            <span style="margin-left:auto;">${chip('Admin', 'idle')}</span></div>
        </div>
        ${userFooter()}
      </aside>
      <main class="thin-scroll" style="flex:1;overflow:auto;background:var(--bg);">${employeeListPage()}</main>
    </div>`;
  }

  // ---- Direction B: Dual Rail (icon rail + context sidebar) ----
  function shellB() {
    const railItems = [['home', 0], ['bot', 1], ['chat', 0], ['flow', 0], ['brain', 0], ['shield', 0]];
    const rail = railItems.map(([ic, act]) => `
      <div style="width:40px;height:40px;border-radius:10px;display:flex;align-items:center;justify-content:center;
        ${act ? 'background:var(--text-1);color:#fff;' : 'color:var(--text-3);'}cursor:pointer;">
        ${icons[ic]}</div>`).join('');
    // context sidebar content for "数字员工"
    const ctx = iaEmployee.groups[1].items.map(([ic, cn, en, kids], i) => `
      <div style="display:flex;align-items:center;gap:8px;height:30px;padding:0 9px;border-radius:6px;
        ${i === 0 ? 'background:var(--hover);' : ''}cursor:pointer;">
        <span style="color:${i === 0 ? 'var(--text-1)' : 'var(--text-3)'};display:flex;">${icons[ic]}</span>
        <span style="font-size:13.5px;font-weight:${i === 0 ? 600 : 500};color:${i === 0 ? 'var(--text-1)' : 'var(--text-2)'};">${cn}</span></div>
      ${kids && i === 0 ? kids.map(k => `<div style="padding:3px 8px 3px 30px;"><div style="padding:5px 8px;border-radius:5px;font-size:12.5px;color:var(--text-2);">${k.split(' · ')[0]}</div></div>`).join('') : ''}`).join('');
    return `<div style="display:flex;height:100%;background:var(--bg);">
      <div style="width:56px;flex:0 0 56px;background:var(--surface-2);border-right:1px solid var(--border);
        display:flex;flex-direction:column;align-items:center;padding:12px 0;gap:6px;">
        ${hex('H', 'var(--honey)', '#fff', 32, 14)}
        <div style="height:8px;"></div>
        ${rail}
        <div style="margin-top:auto;display:flex;flex-direction:column;align-items:center;gap:8px;">
          <div style="width:40px;height:40px;border-radius:10px;display:flex;align-items:center;justify-content:center;color:var(--text-3);">${icons.gear}</div>
          <span class="hex" style="display:inline-flex;width:28px;height:28px;background:oklch(0.6 0.1 280);"></span>
        </div>
      </div>
      <aside style="width:218px;flex:0 0 218px;background:var(--bg);border-right:1px solid var(--border);display:flex;flex-direction:column;">
        <div style="padding:16px 16px 12px;border-bottom:1px solid var(--border);">
          <div class="eyebrow" style="margin-bottom:4px;">Section</div>
          <div style="font-family:var(--display);font-weight:600;font-size:16px;">数字员工</div>
        </div>
        <div class="thin-scroll" style="flex:1;overflow:auto;padding:12px;">${ctx}</div>
      </aside>
      <main class="thin-scroll" style="flex:1;overflow:auto;">${employeeListPage()}</main>
    </div>`;
  }

  // ---- Direction C: Command Workspace (top command bar + space switch) ----
  function shellC() {
    return `<div style="display:flex;flex-direction:column;height:100%;background:var(--bg);">
      <header style="height:50px;flex:0 0 50px;display:flex;align-items:center;gap:14px;padding:0 16px;
        background:var(--surface);border-bottom:1px solid var(--border);">
        ${hex('H', 'var(--honey)', '#fff', 26, 12)}
        <div style="display:inline-flex;background:var(--bg-sunk);border-radius:8px;padding:3px;gap:2px;">
          <button style="height:26px;padding:0 12px;border:0;border-radius:6px;background:var(--surface);box-shadow:var(--shadow-1);
            font-family:var(--sans);font-size:12.5px;font-weight:600;cursor:pointer;">我的工作区</button>
          <button style="height:26px;padding:0 12px;border:0;border-radius:6px;background:transparent;
            font-family:var(--sans);font-size:12.5px;color:var(--text-2);cursor:pointer;">公司控制中台</button>
        </div>
        <div style="flex:1;max-width:440px;margin:0 auto;display:flex;align-items:center;gap:9px;height:34px;padding:0 12px;
          background:var(--bg);border:1px solid var(--border-2);border-radius:9px;color:var(--text-3);">
          ${icons.search}<span style="font-size:13px;">问 Hive 或搜索任何东西…</span>
          <span class="mono" style="margin-left:auto;font-size:10px;color:var(--text-4);border:1px solid var(--border-2);border-radius:4px;padding:1px 6px;">⌘K</span>
        </div>
        <button style="height:32px;padding:0 12px;background:var(--text-1);color:#fff;border:0;border-radius:8px;
          font-family:var(--sans);font-weight:600;font-size:12.5px;display:inline-flex;align-items:center;gap:6px;cursor:pointer;">
          ${icons.bolt}新任务</button>
        <span class="hex" style="display:inline-flex;width:28px;height:28px;background:oklch(0.6 0.1 280);"></span>
      </header>
      <div style="display:flex;flex:1;min-height:0;">
        <aside style="width:236px;flex:0 0 236px;background:var(--bg);border-right:1px solid var(--border);display:flex;flex-direction:column;">
          <div class="thin-scroll" style="flex:1;overflow:auto;padding:14px 12px;">
            <div class="eyebrow" style="padding:0 8px 8px;">Workspace</div>
            ${notionTree(false)}
          </div>
          ${userFooter()}
        </aside>
        <main class="thin-scroll" style="flex:1;overflow:auto;">${employeeListPage()}</main>
      </div>
    </div>`;
  }

  /* ---------- expose ---------- */
  window.HiveShells = { shellA, shellB, shellC, iaColumn, iaEmployee, iaAdmin, chip, hex, icons };
})();
