const C = {
  ink: "#17202A",
  ink2: "#26384E",
  paper: "#F4F0E7",
  paper2: "#E8E1D5",
  rule: "#9EA7A1",
  teal: "#0E7C66",
  green: "#77A68A",
  blue: "#2E72B8",
  sky: "#71A9D6",
  amber: "#C88A2E",
  coral: "#D95F43",
  rose: "#AE3E5B",
  violet: "#6E5AA8",
  white: "#FFFFFF",
  muted: "#6C746F",
  darkMuted: "#B7C0B8",
};

const W = 1280;
const H = 720;
const FONT_ZH = "PingFang SC";
const FONT_EN = "Aptos";
const FONT_DISPLAY = "Aptos Display";
const FONT_MONO = "Aptos Mono";

function bg(slide, ctx, mode = "light") {
  const dark = mode === "dark";
  ctx.addShape(slide, { x: 0, y: 0, w: W, h: H, fill: dark ? C.ink : C.paper, line: ctx.line() });
  if (dark) {
    ctx.addShape(slide, { x: 0, y: 0, w: W, h: 86, fill: "#141E2A", line: ctx.line() });
    ctx.addShape(slide, { x: 0, y: 636, w: W, h: 84, fill: "#121A24", line: ctx.line() });
    ctx.addShape(slide, { x: 760, y: 0, w: 6, h: H, fill: C.teal, line: ctx.line() });
  } else {
    ctx.addShape(slide, { x: 0, y: 0, w: W, h: 72, fill: "#EEE7DB", line: ctx.line() });
    ctx.addShape(slide, { x: 0, y: 644, w: W, h: 76, fill: "#EAE2D6", line: ctx.line() });
    ctx.addShape(slide, { x: 54, y: 90, w: 2, h: 500, fill: "#C8BDAE", line: ctx.line() });
  }
}

function txt(slide, ctx, text, x, y, w, h, opts = {}) {
  return ctx.addText(slide, {
    x,
    y,
    w,
    h,
    text,
    fontSize: opts.size ?? 24,
    color: opts.color ?? C.ink,
    bold: opts.bold ?? false,
    align: opts.align ?? "left",
    valign: opts.valign ?? "top",
    typeface: opts.face ?? FONT_ZH,
    fill: opts.fill ?? "#00000000",
    line: opts.line ?? ctx.line(),
    insets: opts.insets ?? { left: 0, right: 0, top: 0, bottom: 0 },
    name: opts.name,
  });
}

function rect(slide, ctx, x, y, w, h, fill, lineFill = "#00000000", lineWidth = 0, name) {
  return ctx.addShape(slide, {
    x,
    y,
    w,
    h,
    fill,
    line: ctx.line(lineFill, lineWidth),
    name,
  });
}

function hline(slide, ctx, x, y, w, color = C.rule, width = 1) {
  rect(slide, ctx, x, y, w, width, color);
}

function vline(slide, ctx, x, y, h, color = C.rule, width = 1) {
  rect(slide, ctx, x, y, width, h, color);
}

function dot(slide, ctx, x, y, color, size = 12) {
  return ctx.addShape(slide, {
    x,
    y,
    w: size,
    h: size,
    geometry: "ellipse",
    fill: color,
    line: ctx.line(color, 0),
  });
}

function footer(slide, ctx, n, source = "") {
  hline(slide, ctx, 70, 646, 1140, "#C8BDAE", 1);
  txt(slide, ctx, `Hive technical roadshow / ${String(n).padStart(2, "0")}`, 70, 660, 330, 20, {
    size: 10,
    color: C.muted,
    face: FONT_MONO,
  });
  if (source) {
    txt(slide, ctx, source, 630, 660, 580, 26, {
      size: 9,
      color: C.muted,
      face: FONT_MONO,
      align: "right",
    });
  }
}

function darkFooter(slide, ctx, n, source = "") {
  hline(slide, ctx, 70, 646, 1140, "#506070", 1);
  txt(slide, ctx, `Hive technical roadshow / ${String(n).padStart(2, "0")}`, 70, 660, 330, 20, {
    size: 10,
    color: C.darkMuted,
    face: FONT_MONO,
  });
  if (source) {
    txt(slide, ctx, source, 610, 660, 600, 26, {
      size: 9,
      color: C.darkMuted,
      face: FONT_MONO,
      align: "right",
    });
  }
}

function kicker(slide, ctx, label, x, y, color = C.teal, dark = false) {
  const ky = y >= 54 ? 38 : y;
  rect(slide, ctx, x, ky + 8, 9, 9, color);
  txt(slide, ctx, label, x + 20, ky, 420, 28, {
    size: 11,
    color: dark ? C.darkMuted : C.muted,
    face: FONT_MONO,
    bold: true,
    valign: "middle",
  });
}

function arrow(slide, ctx, x1, y, x2, color = C.ink2) {
  hline(slide, ctx, x1, y, x2 - x1 - 14, color, 2);
  txt(slide, ctx, ">", x2 - 18, y - 12, 22, 24, { size: 18, color, face: FONT_MONO, bold: true });
}

function tag(slide, ctx, label, x, y, w, color = C.teal, dark = false) {
  rect(slide, ctx, x, y, w, 30, dark ? "#00000000" : "#FFFFFF55", color, 1);
  txt(slide, ctx, label, x + 10, y + 5, w - 20, 18, {
    size: 10,
    color: dark ? C.paper : C.ink,
    face: FONT_MONO,
    align: "center",
    valign: "middle",
  });
}

function box(slide, ctx, x, y, w, h, title, body, accent = C.teal, opts = {}) {
  rect(slide, ctx, x, y, w, h, opts.fill ?? "#FFFFFF77", "#C9C1B5", 1);
  rect(slide, ctx, x, y, 5, h, accent);
  txt(slide, ctx, title, x + 18, y + 18, w - 36, 30, {
    size: opts.titleSize ?? 19,
    color: opts.dark ? C.paper : C.ink,
    bold: true,
  });
  txt(slide, ctx, body, x + 18, y + 56, w - 36, h - 68, {
    size: opts.bodySize ?? 13,
    color: opts.dark ? C.darkMuted : C.muted,
    face: FONT_ZH,
  });
}

function smallMetric(slide, ctx, label, value, x, y, w, dark = false) {
  txt(slide, ctx, label, x, y, w, 20, { size: 10, color: dark ? C.darkMuted : C.muted, face: FONT_MONO });
  txt(slide, ctx, value, x, y + 24, w, 46, {
    size: 30,
    color: dark ? C.paper : C.ink,
    face: FONT_DISPLAY,
    bold: true,
  });
}

export async function slide01(presentation, ctx) {
  const slide = presentation.slides.add();
  bg(slide, ctx, "dark");
  kicker(slide, ctx, "TECHNICAL ROADSHOW / 2026-06-06", 72, 56, C.amber, true);
  txt(slide, ctx, "Hive", 72, 120, 480, 118, {
    size: 82,
    color: C.paper,
    face: FONT_DISPLAY,
    bold: true,
  });
  txt(slide, ctx, "自进化数字员工基础设施\n+ 企业级控制中台", 72, 242, 650, 140, {
    size: 42,
    color: C.paper,
    bold: true,
  });
  txt(slide, ctx, "不是聊天机器人包装层，而是让 agent 具备身份、记忆、执行、治理和可审计演化的 runtime platform。", 74, 404, 650, 74, {
    size: 19,
    color: C.darkMuted,
  });
  rect(slide, ctx, 820, 92, 310, 458, "#0F1721", "#506070", 1);
  txt(slide, ctx, "CURRENT CHECKOUT", 848, 126, 260, 22, { size: 10, color: C.darkMuted, face: FONT_MONO });
  smallMetric(slide, ctx, "version", "1.7.0", 848, 170, 210, true);
  smallMetric(slide, ctx, "tool decorators", "107", 848, 250, 210, true);
  smallMetric(slide, ctx, "backend services", "158", 848, 330, 210, true);
  smallMetric(slide, ctx, "governance", "RLS + audit", 848, 410, 240, true);
  rect(slide, ctx, 1128, 92, 10, 458, C.teal);
  darkFooter(slide, ctx, 1, "source: current repo scan + README.md");
  return slide;
}

export async function slide02(presentation, ctx) {
  const slide = presentation.slides.add();
  bg(slide, ctx, "light");
  kicker(slide, ctx, "WHY THIS NEEDS A PLATFORM", 78, 54, C.coral);
  txt(slide, ctx, "企业 agent 的瓶颈不是模型本身，\n而是缺少可持续运行的生命体征。", 78, 106, 820, 102, {
    size: 33,
    bold: true,
  });
  txt(slide, ctx, "模型越强，越需要平台把身份、记忆、授权和副作用边界补齐。否则 agent 只能停留在一次性聊天或脚本自动化。", 82, 222, 900, 44, {
    size: 17,
    color: C.muted,
  });

  const items = [
    ["01", "状态断裂", "上下文存在窗口里，身份和偏好不成为可演化资产。", "需要 soul.md、T0/T2/T3、动态记忆激活。", C.rose],
    ["02", "执行脆弱", "长任务和后台任务依赖页面/会话，失败后难以恢复。", "需要 RuntimeTask、journal、resume、durable broker。", C.blue],
    ["03", "治理缺席", "工具、数据、公司边界、外部动作混在一个 prompt 里。", "需要 RLS、capability policy、preflight、checkpoint。", C.teal],
  ];
  items.forEach((it, i) => {
    const x = 96 + i * 370;
    rect(slide, ctx, x, 334, 312, 198, "#FFFFFF88", "#D2C7BA", 1);
    txt(slide, ctx, it[0], x + 20, 350, 64, 34, { size: 24, color: it[4], face: FONT_DISPLAY, bold: true });
    txt(slide, ctx, it[1], x + 86, 352, 190, 28, { size: 22, bold: true });
    hline(slide, ctx, x + 20, 398, 262, "#C8BDAE", 1);
    txt(slide, ctx, it[2], x + 20, 418, 258, 45, { size: 14, color: C.muted });
    txt(slide, ctx, it[3], x + 20, 474, 258, 42, { size: 13, color: C.ink2, bold: true });
  });
  footer(slide, ctx, 2, "source: README.md lines 16-29");
  return slide;
}

export async function slide03(presentation, ctx) {
  const slide = presentation.slides.add();
  bg(slide, ctx, "light");
  kicker(slide, ctx, "AI-NATIVE DESIGN LAW", 78, 54, C.teal);
  txt(slide, ctx, "Hive 的设计法则：释放模型能力，\n把约束放在行动边界。", 78, 104, 760, 95, { size: 32, bold: true });
  txt(slide, ctx, "智能步骤交给 LLM；harness 负责权限、预算、审计和副作用。这样既保持模型可替换，又不牺牲企业治理。", 82, 214, 820, 42, {
    size: 16,
    color: C.muted,
  });

  const layers = [
    ["L1", "Unleash the model first", "总结、规划、提取、综合、判断都给模型完整输入和足够输出预算。", C.coral],
    ["L2", "Harness constrains, never replaces", "治理层限制 agent 能做什么，而不是削弱它怎么思考。", C.amber],
    ["L3", "Neutral enterprise control plane", "模型平等、组织权限、预算、审计和协作治理在平台层统一。", C.teal],
  ];
  layers.forEach((l, i) => {
    const y = 322 + i * 86;
    rect(slide, ctx, 132, y, 1000, 58, i === 0 ? "#F8E8DF" : i === 1 ? "#F3E9CE" : "#DFEEE8", "#C8BDAE", 1);
    txt(slide, ctx, l[0], 158, y + 13, 58, 28, { size: 22, face: FONT_DISPLAY, color: l[3], bold: true });
    txt(slide, ctx, l[1], 238, y + 11, 296, 28, { size: 20, face: FONT_EN, color: C.ink, bold: true });
    txt(slide, ctx, l[2], 558, y + 12, 520, 30, { size: 14, color: C.ink2 });
  });
  vline(slide, ctx, 206, 330, 222, C.ink2, 2);
  footer(slide, ctx, 3, "source: AGENTS.md / AI-Native Design Law");
  return slide;
}

export async function slide04(presentation, ctx) {
  const slide = presentation.slides.add();
  bg(slide, ctx, "dark");
  kicker(slide, ctx, "SYSTEM ARCHITECTURE", 72, 54, C.sky, true);
  txt(slide, ctx, "一套统一 runtime 承载聊天、渠道、触发、办公、workflow 和 agent 协作。", 72, 98, 860, 72, {
    size: 30,
    color: C.paper,
    bold: true,
  });

  const cols = [
    { x: 70, w: 205, title: "Entry surfaces", items: ["Web chat", "Feishu / Slack / Teams", "Schedule / webhook", "Office workbench"], color: C.sky },
    { x: 318, w: 210, title: "FastAPI backend", items: ["61 API routers", "RuntimeTask APIs", "Channel webhooks", "Admin surfaces"], color: C.amber },
    { x: 574, w: 230, title: "Agent runtime", items: ["invoker.py", "AgentKernel", "LoopGuard", "Tool budgets"], color: C.teal },
    { x: 852, w: 206, title: "Governed tools", items: ["ToolRuntimeService", "ActionPreflight", "CapabilityGate", "107 @tool hooks"], color: C.coral },
    { x: 1090, w: 124, title: "Stores", items: ["Postgres RLS", "Redis", "Agent FS", "Audit"], color: C.green },
  ];
  cols.forEach((c, i) => {
    rect(slide, ctx, c.x, 220, c.w, 300, "#111A26", "#506070", 1);
    rect(slide, ctx, c.x, 220, c.w, 7, c.color);
    txt(slide, ctx, c.title, c.x + 16, 244, c.w - 30, 32, { size: 16, color: C.paper, bold: true });
    c.items.forEach((item, j) => {
      dot(slide, ctx, c.x + 18, 300 + j * 43, c.color, 7);
      txt(slide, ctx, item, c.x + 36, 292 + j * 43, c.w - 48, 28, { size: 12, color: C.darkMuted });
    });
    if (i < cols.length - 1) arrow(slide, ctx, c.x + c.w + 14, 370, cols[i + 1].x - 14, "#708397");
  });
  rect(slide, ctx, 318, 558, 896, 42, "#182538", "#506070", 1);
  txt(slide, ctx, "Memory Control Plane + enterprise control plane wrap the whole path: owner/company context, RLS, audit, budgets, approvals.", 340, 568, 840, 20, {
    size: 13,
    color: C.darkMuted,
    face: FONT_EN,
  });
  darkFooter(slide, ctx, 4, "source: README.md architecture + live repo counts");
  return slide;
}

export async function slide05(presentation, ctx) {
  const slide = presentation.slides.add();
  bg(slide, ctx, "light");
  kicker(slide, ctx, "INVOCATION PATH", 78, 54, C.blue);
  txt(slide, ctx, "每次 agent 调用都走同一个 kernel，工具副作用统一过治理层。", 78, 104, 860, 78, {
    size: 31,
    bold: true,
  });

  const steps = [
    ["Entry", "chat / webhook / cron / delegation"],
    ["invoker.py", "resolve model, tenant, tools, dynamic memory"],
    ["AgentKernel", "multi-round LLM loop, LoopGuard, compaction"],
    ["ToolRuntime", "execute() is the side-effect gate"],
    ["Handlers", "files, search, office, Feishu, MCP, DR"],
  ];
  steps.forEach((s, i) => {
    const x = 76 + i * 235;
    rect(slide, ctx, x, 262, 185, 106, i === 2 ? "#DDEBE5" : "#FFFFFF88", "#C8BDAE", 1);
    txt(slide, ctx, s[0], x + 16, 284, 154, 26, { size: 18, bold: true, color: i === 2 ? C.teal : C.ink });
    txt(slide, ctx, s[1], x + 16, 318, 150, 36, { size: 12, color: C.muted });
    if (i < steps.length - 1) arrow(slide, ctx, x + 190, 315, x + 232, C.ink2);
  });

  rect(slide, ctx, 206, 432, 454, 128, "#F4E6CF", "#C8BDAE", 1);
  txt(slide, ctx, "Memory Control Plane", 232, 456, 250, 28, { size: 20, bold: true, color: C.amber });
  txt(slide, ctx, "动态注入 owner/company/goal/open-loop 相关记忆；敏感内容按访问权限剥离。", 232, 492, 372, 38, { size: 13, color: C.ink2 });
  arrow(slide, ctx, 420, 432, 420, C.amber);
  rect(slide, ctx, 736, 432, 336, 128, "#E8DDE7", "#C8BDAE", 1);
  txt(slide, ctx, "Preflight / Checkpoint / Audit", 762, 456, 280, 28, { size: 16, bold: true, color: C.rose });
  txt(slide, ctx, "外部可见、敏感、不可逆或公司边界动作在工具执行前被拦截或升级。", 762, 492, 260, 44, { size: 13, color: C.muted });
  footer(slide, ctx, 5, "source: backend/app/runtime/invoker.py; backend/app/kernel/engine.py; backend/app/tools/service.py");
  return slide;
}

export async function slide06(presentation, ctx) {
  const slide = presentation.slides.add();
  bg(slide, ctx, "light");
  kicker(slide, ctx, "MEMORY CONTROL PLANE", 78, 54, C.teal);
  txt(slide, ctx, "记忆不是一个向量库；它是 agent 身份、证据和权限共同治理的资产层。", 78, 104, 910, 72, {
    size: 30,
    bold: true,
  });

  const pyramid = [
    ["soul.md", "永久身份：角色、边界、质量标准", 176, 218, 300, C.rose],
    ["T3 semantic", "Heartbeat curation: feedback / knowledge / strategies", 136, 292, 380, C.amber],
    ["T2 learnings", "extract_agent: atomic learnings with evidence", 96, 366, 460, C.teal],
    ["T0 raw logs", "session hooks: cursor logs, tool calls, tool results", 56, 440, 540, C.blue],
  ];
  pyramid.forEach((p) => {
    rect(slide, ctx, p[2], p[3], p[4], 54, "#FFFFFF90", p[5], 2);
    txt(slide, ctx, p[0], p[2] + 18, p[3] + 11, 142, 26, { size: 18, bold: true, color: p[5], face: FONT_EN });
    txt(slide, ctx, p[1], p[2] + 175, p[3] + 14, p[4] - 200, 22, { size: 12, color: C.muted });
  });
  vline(slide, ctx, 676, 214, 290, "#C8BDAE", 1);
  const rails = [
    ["Principal stack", "company / direct owner / current user / delegation context"],
    ["Write safety", "privacy classification, sensitivity labels, PL4 credential rejection"],
    ["Dynamic activation", "goal relevance, retention, open-loop pressure, sensitivity access"],
    ["Decision trace", "why the agent acted, asked, refused, or escalated"],
    ["Steward loop", "low-risk preparation; external-visible actions require checkpoint"],
  ];
  rails.forEach((r, i) => {
    const y = 218 + i * 57;
    dot(slide, ctx, 724, y + 7, [C.teal, C.amber, C.blue, C.rose, C.green][i], 10);
    txt(slide, ctx, r[0], 746, y, 210, 24, { size: 16, bold: true });
    txt(slide, ctx, r[1], 962, y + 2, 245, 36, { size: 11, color: C.muted, face: FONT_EN });
  });
  footer(slide, ctx, 6, "source: README.md Memory Pyramid + backend/app/memory/*");
  return slide;
}

export async function slide07(presentation, ctx) {
  const slide = presentation.slides.add();
  bg(slide, ctx, "dark");
  kicker(slide, ctx, "SELF-EVOLUTION FLYWHEEL", 72, 54, C.green, true);
  txt(slide, ctx, "自我进化不是直接改长期记忆；\n它先形成可验证、可回滚的候选资产。", 72, 98, 820, 88, {
    size: 30,
    color: C.paper,
    bold: true,
  });

  const nodes = [
    ["Response\nComplete", C.sky],
    ["Fast\nReflection", C.amber],
    ["Session\nProjection", C.teal],
    ["Verification\nEval", C.rose],
    ["Candidate /\nPromotion", C.green],
  ];
  nodes.forEach((n, i) => {
    const x = 82 + i * 154;
    rect(slide, ctx, x, 292, 126, 78, "#111A26", n[1], 2);
    txt(slide, ctx, n[0], x + 12, 312, 102, 38, { size: 14, color: C.paper, bold: true, align: "center", valign: "middle" });
    if (i < nodes.length - 1) arrow(slide, ctx, x + 134, 330, x + 154, n[1]);
  });
  rect(slide, ctx, 274, 438, 318, 74, "#1C2C40", C.teal, 2);
  txt(slide, ctx, "Next-turn learning without unreviewed durable writes", 300, 462, 266, 24, {
    size: 17,
    color: C.paper,
    bold: true,
    align: "center",
  });
  vline(slide, ctx, 436, 370, 68, C.teal, 2);

  rect(slide, ctx, 872, 230, 286, 326, "#0F1721", "#506070", 1);
  txt(slide, ctx, "DATED EVIDENCE", 900, 256, 220, 20, { size: 10, color: C.darkMuted, face: FONT_MONO });
  txt(slide, ctx, "P0-P7", 900, 286, 220, 46, { size: 34, color: C.paper, face: FONT_DISPLAY, bold: true });
  txt(slide, ctx, "completed self-evolution substrate phases", 900, 336, 210, 32, { size: 13, color: C.darkMuted });
  hline(slide, ctx, 900, 390, 212, "#506070", 1);
  txt(slide, ctx, "6 bakeoff scenarios\nscores 90-96\npassed=true", 900, 414, 210, 74, { size: 18, color: C.paper, bold: true });
  txt(slide, ctx, "Proof is deterministic repo checks, generated 2026-05-24.", 900, 496, 210, 38, { size: 11, color: C.darkMuted, face: FONT_EN });
  darkFooter(slide, ctx, 7, "source: docs/self-evolution-sota-plan.md; docs/self-evolution-bakeoff-report.json");
  return slide;
}

export async function slide08(presentation, ctx) {
  const slide = presentation.slides.add();
  bg(slide, ctx, "light");
  kicker(slide, ctx, "PLAN MODE x WORKFLOW", 78, 54, C.violet);
  txt(slide, ctx, "两个并列底座：Plan Mode 管确认边界，Workflow 管确定性执行控制流。", 78, 104, 920, 70, {
    size: 30,
    bold: true,
  });

  rect(slide, ctx, 82, 236, 424, 260, "#F1E7EF", C.violet, 2);
  txt(slide, ctx, "Plan Mode", 120, 262, 300, 34, { size: 26, color: C.violet, bold: true, face: FONT_EN });
  txt(slide, ctx, "回答：能不能开始？要不要人确认？确认的计划是什么？", 120, 312, 330, 46, { size: 15, color: C.ink2, bold: true });
  txt(slide, ctx, "关键对象：confirmed plan、action artifact、permission boundary、human approval。", 120, 372, 330, 56, { size: 14, color: C.muted });
  tag(slide, ctx, "CONFIRMATION FOUNDATION", 120, 444, 250, C.violet);

  rect(slide, ctx, 774, 236, 424, 260, "#DDEBE5", C.teal, 2);
  txt(slide, ctx, "Workflow", 804, 262, 300, 34, { size: 26, color: C.teal, bold: true, face: FONT_EN });
  txt(slide, ctx, "回答：开始之后怎么推进、暂停、恢复、审计？", 804, 312, 330, 46, { size: 15, color: C.ink2, bold: true });
  txt(slide, ctx, "关键对象：structured definition、journal、quota、gate/wait、definition_hash。", 804, 372, 330, 56, { size: 14, color: C.muted });
  tag(slide, ctx, "EXECUTION FOUNDATION", 804, 444, 240, C.teal);

  rect(slide, ctx, 540, 320, 194, 74, "#FFFFFFAA", "#C8BDAE", 1);
  txt(slide, ctx, "组合\n不混成一个概念", 566, 334, 142, 44, { size: 18, color: C.ink, bold: true, align: "center", valign: "middle" });
  arrow(slide, ctx, 506, 352, 540, C.ink2);
  arrow(slide, ctx, 734, 374, 774, C.ink2);
  rect(slide, ctx, 214, 548, 852, 42, "#FFFFFF88", "#C8BDAE", 1);
  txt(slide, ctx, "共享底座：RuntimeTask · tenant boundary · budget envelope · audit · resume · checkpoints", 248, 558, 788, 20, {
    size: 14,
    color: C.ink2,
    face: FONT_EN,
    align: "center",
  });
  footer(slide, ctx, 8, "source: docs/workflow-source-capability.md");
  return slide;
}

export async function slide09(presentation, ctx) {
  const slide = presentation.slides.add();
  bg(slide, ctx, "light");
  kicker(slide, ctx, "DETERMINISTIC WORKFLOW RUNTIME", 78, 54, C.teal);
  txt(slide, ctx, "Workflow 的核心不是多一个任务功能，而是给高风险 agent 工作一条可恢复的执行轨道。", 78, 104, 980, 70, {
    size: 29,
    bold: true,
  });

  const rowY = 272;
  const flow = [
    ["definition data", "sequence / bounded fanout / condition / gate / wait", C.teal],
    ["compile + admission", "schema forbid extra, capability binding, risk grade", C.amber],
    ["RuntimeTask", "task_type=workflow, metadata mirrors tenant/hash", C.blue],
    ["journal", "WorkflowStep + WorkflowLeafCall, input_hash replay", C.rose],
    ["resume / trigger", "daemon drain, workflow_ref, version/hash pin", C.green],
  ];
  flow.forEach((f, i) => {
    const x = 70 + i * 232;
    rect(slide, ctx, x, rowY, 182, 118, "#FFFFFF88", f[2], 2);
    txt(slide, ctx, f[0], x + 16, rowY + 18, 150, 28, { size: 17, bold: true, color: f[2], face: FONT_EN });
    txt(slide, ctx, f[1], x + 16, rowY + 54, 150, 46, { size: 11, color: C.muted, face: FONT_EN });
    if (i < flow.length - 1) arrow(slide, ctx, x + 188, rowY + 58, x + 226, C.ink2);
  });

  const boundaries = [
    ["No arbitrary code execution", "definition 是数据，引擎解释执行"],
    ["Gate irreversible steps", "对外/不可逆步骤必须 checkpoint"],
    ["Leaf-level replay", "8 叶跑完 7 叶后恢复只补 1 叶"],
    ["Feature flags", "runtime/trigger enabled; DR workflow path gated"],
  ];
  boundaries.forEach((b, i) => {
    const x = 96 + i * 280;
    rect(slide, ctx, x, 470, 220, 72, i % 2 === 0 ? "#EEE7DB" : "#E3ECEE", "#C8BDAE", 1);
    txt(slide, ctx, b[0], x + 16, 486, 188, 22, { size: 15, bold: true, color: i % 2 === 0 ? C.amber : C.blue, face: FONT_EN });
    txt(slide, ctx, b[1], x + 16, 516, 188, 18, { size: 11, color: C.muted });
  });
  footer(slide, ctx, 9, "source: backend/app/runtime/workflow_definition.py; workflow_engine.py; workflow_daemon.py");
  return slide;
}

export async function slide10(presentation, ctx) {
  const slide = presentation.slides.add();
  bg(slide, ctx, "dark");
  kicker(slide, ctx, "ENTERPRISE TRUST BOUNDARY", 72, 54, C.coral, true);
  txt(slide, ctx, "Hive 的治理不是 prompt 免责声明；它在工具执行、数据库会话和审计对象上 fail-closed。", 72, 100, 930, 72, {
    size: 29,
    color: C.paper,
    bold: true,
  });

  rect(slide, ctx, 472, 286, 336, 96, "#1C2C40", C.teal, 2);
  txt(slide, ctx, "ToolRuntimeService.execute()", 500, 312, 280, 28, { size: 21, color: C.paper, bold: true, face: FONT_EN, align: "center" });
  txt(slide, ctx, "single side-effect gate", 530, 348, 220, 20, { size: 12, color: C.darkMuted, face: FONT_MONO, align: "center" });

  const gates = [
    ["Tenant / RLS", 110, 236, C.sky, "PostgreSQL RLS + explicit tenant context"],
    ["Capability policy", 370, 188, C.amber, "agent policy, tenant defaults, approval escalation"],
    ["Action preflight", 866, 188, C.coral, "external-visible, sensitive, irreversible"],
    ["Checkpoint", 984, 408, C.rose, "human-in-the-loop metadata and approval cards"],
    ["Audit + decision trace", 652, 508, C.green, "why acted, asked, refused, escalated"],
    ["Secrets boundary", 190, 438, C.violet, "encrypted provider and channel credentials"],
  ];
  gates.forEach((g) => {
    rect(slide, ctx, g[1], g[2], 210, 86, "#0F1721", g[3], 2);
    txt(slide, ctx, g[0], g[1] + 16, g[2] + 16, 178, 24, { size: 16, color: C.paper, bold: true });
    txt(slide, ctx, g[4], g[1] + 16, g[2] + 46, 178, 30, { size: 10, color: C.darkMuted, face: FONT_EN });
  });
  hline(slide, ctx, 320, 280, 152, C.sky, 2);
  hline(slide, ctx, 580, 250, 120, C.amber, 2);
  hline(slide, ctx, 808, 250, 58, C.coral, 2);
  hline(slide, ctx, 808, 448, 176, C.rose, 2);
  hline(slide, ctx, 652, 520, 96, C.green, 2);
  hline(slide, ctx, 400, 480, 120, C.violet, 2);
  darkFooter(slide, ctx, 10, "source: backend/app/tools/governance.py; action_preflight.py; capability_gate.py");
  return slide;
}

export async function slide11(presentation, ctx) {
  const slide = presentation.slides.add();
  bg(slide, ctx, "light");
  kicker(slide, ctx, "PRODUCT PROOF SURFACE", 78, 54, C.blue);
  txt(slide, ctx, "这不是库，而是带 UI、渠道、办公和扩展治理的多 agent 平台。", 78, 104, 880, 70, {
    size: 30,
    bold: true,
  });

  const surfaces = [
    ["App", "Agent Detail\nAgent Circle\nMessages / Chat", C.blue],
    ["Company Admin", "users / org\nmodels / memory\ntools / quotas / audit", C.teal],
    ["Platform Admin", "companies\nsystem config\noperator controls", C.amber],
  ];
  surfaces.forEach((s, i) => {
    const x = 96 + i * 360;
    rect(slide, ctx, x, 230, 300, 154, "#FFFFFF88", s[2], 2);
    txt(slide, ctx, s[0], x + 22, 254, 250, 28, { size: 22, bold: true, color: s[2], face: FONT_EN });
    txt(slide, ctx, s[1], x + 22, 298, 250, 70, { size: 15, color: C.ink2 });
  });

  rect(slide, ctx, 96, 424, 1020, 126, "#E9EEF0", "#C8BDAE", 1);
  txt(slide, ctx, "Channels", 124, 446, 120, 24, { size: 18, bold: true, face: FONT_EN, color: C.blue });
  txt(slide, ctx, "Feishu / Lark · Slack · DingTalk · WeCom · WeChat Personal · Teams · Telegram · Email", 262, 446, 810, 24, {
    size: 15,
    color: C.ink2,
    face: FONT_EN,
  });
  hline(slide, ctx, 124, 486, 948, "#C8BDAE", 1);
  txt(slide, ctx, "Extensions", 124, 508, 120, 24, { size: 18, bold: true, face: FONT_EN, color: C.teal });
  txt(slide, ctx, "Skills · MCP servers · Office document tools · subagents · workflows · deep research tools", 262, 508, 810, 24, {
    size: 15,
    color: C.ink2,
    face: FONT_EN,
  });
  footer(slide, ctx, 11, "source: README.md Product surfaces / Channels / Extension model");
  return slide;
}

export async function slide12(presentation, ctx) {
  const slide = presentation.slides.add();
  bg(slide, ctx, "light");
  kicker(slide, ctx, "DEEP RESEARCH + SUBAGENTS", 78, 54, C.rose);
  txt(slide, ctx, "Deep Research 是复杂 agent 工作的压力测试：fanout、证据、合成、质量门都要进统一 runtime。", 78, 104, 980, 70, {
    size: 29,
    bold: true,
  });

  const cols = [
    ["当前可见能力", ["deep_research_start/check/export", "spawn_subagent", "Work Ledger events", "source / artifact contracts"], C.blue],
    ["已识别风险", ["worker context 污染", "final writer 拼接感", "busy-poll / duplicated export", "新 workflow path 默认 gated"], C.rose],
    ["合流路径", ["Leaf Preset Registry", "governed subagent leaves", "workflow journal + artifacts", "critic + report composer"], C.teal],
  ];
  cols.forEach((c, i) => {
    const x = 98 + i * 360;
    rect(slide, ctx, x, 238, 300, 268, "#FFFFFF88", c[2], 2);
    txt(slide, ctx, c[0], x + 22, 262, 250, 28, { size: 20, bold: true, color: c[2] });
    c[1].forEach((item, j) => {
      dot(slide, ctx, x + 24, 314 + j * 42, c[2], 8);
      txt(slide, ctx, item, x + 42, 304 + j * 42, 230, 28, { size: 13, color: C.ink2, face: FONT_EN });
    });
  });
  rect(slide, ctx, 212, 546, 858, 42, "#F3E9CE", C.amber, 1);
  txt(slide, ctx, "路演表达边界：可以讲“Deep Research 是统一 runtime 的高价值试点”，不要讲成“新 workflow 路径已经默认生产打开”。", 242, 556, 800, 20, {
    size: 14,
    color: C.ink2,
    align: "center",
  });
  footer(slide, ctx, 12, "source: docs/deep-research-workflow-unification.md; backend/app/tools/handlers/deep_research.py");
  return slide;
}

export async function slide13(presentation, ctx) {
  const slide = presentation.slides.add();
  bg(slide, ctx, "dark");
  kicker(slide, ctx, "CURRENT ENGINEERING EVIDENCE", 72, 54, C.amber, true);
  txt(slide, ctx, "当前 checkout 已经是一个完整平台体量，不是原型 prompt demo。", 72, 100, 760, 70, {
    size: 31,
    color: C.paper,
    bold: true,
  });

  const bars = [
    ["API routers", 61, C.sky],
    ["Models", 41, C.green],
    ["Services", 158, C.amber],
    ["Migrations", 64, C.coral],
    ["Frontend API domains", 30, C.violet],
    ["@tool decorators", 107, C.teal],
  ];
  const max = 170;
  bars.forEach((b, i) => {
    const y = 226 + i * 54;
    txt(slide, ctx, b[0], 100, y + 8, 190, 20, { size: 13, color: C.darkMuted, face: FONT_EN });
    rect(slide, ctx, 312, y + 8, 520, 17, "#26384E");
    rect(slide, ctx, 312, y + 8, Math.round((b[1] / max) * 520), 17, b[2]);
    txt(slide, ctx, String(b[1]), 852, y + 2, 76, 28, { size: 21, color: C.paper, face: FONT_DISPLAY, bold: true });
  });

  rect(slide, ctx, 970, 222, 214, 300, "#0F1721", "#506070", 1);
  txt(slide, ctx, "STACK", 994, 248, 160, 20, { size: 10, color: C.darkMuted, face: FONT_MONO });
  txt(slide, ctx, "FastAPI / Python 3.12\nReact 19 / TypeScript 5\nPostgreSQL 15 + Redis 7\nDocker / Railway\n14+ LLM providers", 994, 286, 160, 136, {
    size: 15,
    color: C.paper,
    face: FONT_EN,
  });
  hline(slide, ctx, 994, 438, 150, "#506070", 1);
  txt(slide, ctx, "Version: backend 1.7.0 / frontend 1.7.0", 994, 456, 160, 44, { size: 11, color: C.darkMuted, face: FONT_EN });
  darkFooter(slide, ctx, 13, "source: live repo scan on 2026-06-05");
  return slide;
}

export async function slide14(presentation, ctx) {
  const slide = presentation.slides.add();
  bg(slide, ctx, "light");
  kicker(slide, ctx, "ROADSHOW CLOSE", 78, 54, C.teal);
  txt(slide, ctx, "明天现场要讲清楚：Hive 的价值在 runtime + governance，而不是又一个 agent UI。", 78, 104, 980, 70, {
    size: 30,
    bold: true,
  });

  rect(slide, ctx, 92, 224, 450, 292, "#FFFFFF88", C.blue, 2);
  txt(slide, ctx, "建议演示顺序", 122, 250, 300, 28, { size: 22, bold: true, color: C.blue });
  const demo = [
    "1. HR Agent 对话创建数字员工，展示 soul.md 与 workspace",
    "2. Web chat durable run：刷新页面不取消执行",
    "3. Memory Control Plane：动态记忆 + 敏感写入 gate",
    "4. Plan Mode / Workflow preview：高风险动作需要确认",
    "5. Office / Feishu / MCP：生产工作入口而非玩具聊天",
  ];
  demo.forEach((d, i) => txt(slide, ctx, d, 122, 296 + i * 40, 372, 32, { size: 13, color: C.ink2 }));

  rect(slide, ctx, 620, 224, 514, 292, "#E3ECEE", C.teal, 2);
  txt(slide, ctx, "90 天技术路线", 650, 250, 300, 28, { size: 22, bold: true, color: C.teal });
  const road = [
    ["01", "Self-evolution benchmark", "把 Hermes 体感优势转成可重复 eval 与 next-turn learning"],
    ["02", "Workflow v1 hardening", "registered / ephemeral 统一，trigger/UI/ops 全链路收口"],
    ["03", "Deep Research unification", "旧 DR 执行链迁到 leaf preset + workflow runtime"],
    ["04", "Enterprise pilot boundary", "RLS、审计、审批、预算、channel credentials 做试点验收"],
  ];
  road.forEach((r, i) => {
    const y = 300 + i * 48;
    txt(slide, ctx, r[0], 650, y, 44, 24, { size: 18, color: [C.blue, C.amber, C.rose, C.teal][i], face: FONT_DISPLAY, bold: true });
    txt(slide, ctx, r[1], 704, y, 200, 22, { size: 15, bold: true, color: C.ink });
    txt(slide, ctx, r[2], 704, y + 23, 370, 20, { size: 11, color: C.muted });
  });

  rect(slide, ctx, 260, 560, 760, 44, C.ink, C.ink, 1);
  txt(slide, ctx, "Ask: 技术合作 / 试点 / 融资支持，把 Hive 做成企业数字员工基础设施。", 288, 571, 704, 20, {
    size: 16,
    color: C.paper,
    align: "center",
    bold: true,
  });
  footer(slide, ctx, 14, "source: synthesized from current repo + docs roadmap");
  return slide;
}
