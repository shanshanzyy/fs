/* ===========================================================================
   Vibe Coding Agent · 桌面端主逻辑（IDE 布局，参照 Trae / VSCode）
   ---------------------------------------------------------------------------
   数据来源只有一个：后端推的 SSE 事件流。界面不自己推断 Agent 的状态，
   只忠实渲染收到的每一件事 —— 这样「界面显示的和 Agent 实际做的不一致」
   这类问题从结构上就不可能出现。

   断线/刷新时靠事件 id 补齐（见 host/protocol.py），因此刷新页面不会丢会话。

   布局分工（对齐 IDE 的肌肉记忆）：
   - 活动栏 + 侧栏：文件树 / 改动 / 工具 / 审计 / 历史会话 / Git
   - 编辑器区：标签页看文件（Markdown 可渲染预览），底部终端看命令输出
   - 右侧对话栏：与 Agent 的一切交流
   =========================================================================== */

import { renderDiff } from './diff.js';
import { render, isMarkdown } from './markdown.js';

/* ------------------------------------------------------------------ 基础 */
const $ = (id) => document.getElementById(id);

const TOKEN = new URLSearchParams(location.search).get('token') || '';

const api = {
  async get(path, params = {}) {
    const url = new URL(path, location.origin);
    url.searchParams.set('token', TOKEN);
    for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
    const res = await fetch(url, { headers: { 'X-Vibe-Token': TOKEN } });
    return handle(res);
  },
  async post(path, body) {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Vibe-Token': TOKEN },
      body: JSON.stringify(body ?? {}),
    });
    return handle(res);
  },
};

async function handle(res) {
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = {};
  }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

/* ------------------------------------------------------------------ 状态 */
const S = {
  session: null,
  running: false,
  awaiting: null,
  lastEventId: 0,
  booted: false,
  calls: new Map(),        // callId -> 聊天流里的工具卡片条目
  changed: new Set(),
  created: new Set(),
  treeNodes: new Map(),    // path -> 树节点元素
  streaming: null,         // 流式回复卡片
  editors: new Map(),      // path -> { tab, node, kind: 'file'|'diff', mode, data }
  activeEditor: 'welcome',
  activePanel: 'explorer',
  panel: 'explorer',
  iterations: 0,
  iterationTotal: 0,
  diffs: [],               // 本会话的改动（供改动面板与 diff 标签共用）
  changesMode: 'session',  // 改动面板视图：'session'（本会话）| 'git'（相对 git，P0-C）
  git: null,               // 最近一次 /api/git/status 的结果（Git 面板与改动面板共用）
  refs: [],                // 已选引用（P0-A）：[{type, value}]，随 /api/run 一起提交
  visionOn: false,         // 多模态输入（P1-10）：VIBE_VISION 非 off 时才有「选图」入口
  browsePath: '',          // 内置目录浏览器当前所在目录（'' = 盘符列表）
  browseParent: '',        // 它的上一级；'' 表示已到顶，再点「上级」回盘符列表
  plan: null,              // 计划（P2-12）：{steps:[{id,text,status}], revision}；null = 没有计划
  planCollapsed: false,    // 计划卡片是否收起（只影响这一次会话的视图）
  context: null,           // 上下文用量（P2-17）：见 context_stats() 的返回结构
  testTree: null,          // 最近一次 /api/tests 的结果（收集到的用例树，P2-14）
  testResults: new Map(),  // nodeId -> 最近一次结果 {status, ms, failureFile, failureLine, messageHead}
  testSummary: null,       // 最近一次 test.finished 的汇总 {passed, failed, skipped, ms}
  testsRunning: false,     // 是否有测试正在跑（后端同一时刻只跑一批）
  testRunningIds: new Set(),  // 正在跑的 node id（用于给对应用例行显示「运行中」）
};
const stream = $('stream');

/* 命令类工具：它们的输出进底部「终端」，其它工具只在聊天流里展示 */
const COMMAND_TOOLS = /command|shell|bash|exec/i;

/* ------------------------------------------------------------------ 工具函数 */
function el(tag, className, textContent) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (textContent != null) node.textContent = textContent;
  return node;
}

function toast(message, kind = '') {
  const node = el('div', 'toast ' + (kind ? `is-${kind}` : ''), message);
  $('toasts').appendChild(node);
  setTimeout(() => {
    node.style.opacity = '0';
    node.style.transition = 'opacity .25s';
    setTimeout(() => node.remove(), 260);
  }, 4200);
}

function nearBottom() {
  return stream.scrollHeight - stream.scrollTop - stream.clientHeight < 120;
}
function scrollToEnd(force = false) {
  if (force || nearBottom()) stream.scrollTop = stream.scrollHeight;
}

function hideChatWelcome() {
  const w = $('chat-welcome');
  if (w) w.remove();
}

function fmtSize(bytes) {
  if (!bytes && bytes !== 0) return '';
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}K`;
  return `${(bytes / 1048576).toFixed(1)}M`;
}

function shortPath(p) {
  if (!p) return '—';
  const parts = p.replace(/\\/g, '/').split('/').filter(Boolean);
  return parts.length <= 2 ? p : '…/' + parts.slice(-2).join('/');
}

function baseName(p) {
  const parts = String(p || '').replace(/\\/g, '/').split('/');
  return parts[parts.length - 1] || p;
}

/* ═══════════════════════════════ 标题栏 / 状态栏 ═══════════════════════════════ */
function applySession(info) {
  S.session = info;
  $('tb-workspace').textContent = shortPath(info.workspace);
  $('tb-workspace').title = info.workspace;
  $('chip-model').textContent = info.model;
  $('chip-model').title = `${info.model} @ ${info.baseUrl}`;
  const vibe = $('chip-vibe');
  if (info.projectInstructions) {
    vibe.hidden = false;
    vibe.textContent = `VIBE ${info.projectInstructions.chars}`;
    vibe.title = `已加载 ${info.projectInstructions.source}（${info.projectInstructions.chars} 字符）`
      + (info.projectInstructions.truncated ? ' · 超过 32KB 已截断' : '')
      + '，Agent 会遵守其中的团队约定';
  } else {
    vibe.hidden = true;
  }
  setState(info.state || 'idle');
  $('welcome-foot').textContent = `模型 ${info.model} · ${info.baseUrl} · 审计 ${info.auditPath}`;
  renderTools(info.tools || []);
  // 计划（P2-12）：快照是权威来源 —— 刷新页面 / 断线重连后计划卡片照旧在
  applyPlan(info.plan || null);
  // 上下文用量（P2-17）：同一份快照，首帧不必额外发一次请求
  applyContext(info.context || null);
  // 交互式终端（P1-7）：配置关闭（VIBE_TERM=0）时隐藏入口 —— 不留一个点了必报错的按钮
  const termOn = info.config ? info.config.terminalEnabled !== false : true;
  term.enabled = termOn;
  const termTab = document.querySelector('.bp-tab[data-bp="terminal"]');
  if (termTab) termTab.hidden = !termOn;
  if (!termOn) {
    termMarkClosed('交互式终端已被配置关闭（VIBE_TERM=0）');
    const activeTab = document.querySelector('.bp-tab.is-active');
    if (activeTab && activeTab.dataset.bp === 'terminal') switchBottom('output');
  }
  // 多模态输入（P1-10）：默认 off —— 隐藏「选图」入口，粘贴图片也不上传。
  // 不留一个点了必报错的按钮（与终端 VIBE_TERM=0 同一纪律）。
  const visionMode = info.config ? String(info.config.visionMode || 'off') : 'off';
  S.visionOn = visionMode !== 'off';
  $('btn-image').hidden = !S.visionOn;
  $('btn-image').title = S.visionOn
    ? '选一张图片（也可直接粘贴图片）'
    : '需 VIBE_VISION=auto/on 才能输入图片';
  // 跨会话记忆（P2-15）：VIBE_MEMORY=0 时隐藏整个入口（与终端/多模态同一纪律）
  const memOn = info.config ? info.config.memoryEnabled !== false : true;
  const memAct = document.querySelector('.act[data-panel="memory"]');
  if (memAct) memAct.hidden = !memOn;
  if (!memOn && S.activePanel === 'memory') switchPanel('explorer');
  if (info.pendingApproval && !S.awaiting) openApproval(info.pendingApproval);
  if (info.notices?.length) {
    for (const text of info.notices) { addNotice(text); termLine('output', text, 'is-dim'); }
  }
  applyGovernanceChip(info.governance);
  $('sb-session').textContent = info.sessionId ? `会话 ${info.sessionId}` : '';
  $('sb-session').title = `会话 ${info.sessionId || ''}`;
  $('sb-audit').textContent = info.auditPath || '';
  $('sb-audit').title = `审计日志：${info.auditPath || ''}`;
  $('sb-model').textContent = info.model || '';
}

/* ═══════════════════════════════ 治理面徽标（M3） ═══════════════════════════════
 * 未接入治理面时整个徽标不出现 —— 纯本地用法不该看到治理相关的噪音。
 * 已接入但未登录/降级时用告警色：Agent 照样能用，只是没被策略管住，
 * 这一点必须让人看得见，否则「以为被管着、其实没有」比明说不管更危险。 */
const GOV_LABELS = {
  governed: '已接入治理面',
  unauthenticated: '治理面未登录',
  degraded: '治理面降级',
};

/* 配额三态必须分开，不能合并成一句「没额度」：
 *   已知（quota 有值） / 未知（quotaError 有值） / 用尽（remaining 归零）。
 * 「未知」尤其不能显示成「不限」—— 那会让人以为随便用。 */
function quotaLine(governance) {
  if (governance.quotaError) return `配额：未能获取（${governance.quotaError}）`;
  const quota = governance.quota;
  if (!quota) return '';
  const usage = quota.usage || {};
  const limits = quota.limits || {};
  const remaining = quota.remaining || {};
  const day = quota.day || '';
  const used = usage.cloudTokens || 0;
  const left = remaining.cloudTokens;
  const head = `配额（${day}）：云端 token 已用 ${used}`;
  return left === null || left === undefined
    ? `${head}，未设上限`
    : `${head}，剩余 ${left} / ${limits.dailyCloudTokens}`;
}

/** 显式归零才算用尽：`null`/缺字段是「不限」，不是「没了」。 */
function quotaExhausted(governance) {
  const remaining = governance.quota && governance.quota.remaining;
  if (!remaining) return false;
  return remaining.cloudTokens === 0 || remaining.requests === 0;
}

function applyGovernanceChip(governance) {
  const chip = $('chip-gov');
  const status = governance && governance.status;
  if (!status || status === 'off') {
    chip.hidden = true;
    chip.textContent = ''; // 清掉可能残留的「去登录」入口
    return;
  }
  chip.hidden = false;
  chip.dataset.status = status;
  const exhausted = quotaExhausted(governance);
  chip.dataset.quota = exhausted ? 'exhausted' : '';
  chip.textContent = (GOV_LABELS[status] || '治理面') + (exhausted ? ' · 配额用尽' : '');
  // 「去登录」入口（Q 阶段）：已接入治理面但未登录/降级时，界面给一个通往
  // 服务端登录页的入口。**薄壳纪律**：这里只负责打开 `<serverUrl>/login`，
  // 登录逻辑仍在服务端（/login + /api/v1/auth/login），界面不实现任何认证。
  // 外链交给系统浏览器（Electron 壳的 setWindowOpenHandler 负责），不在应用内打开。
  const loginUrl = loginPageUrl(governance);
  if (loginUrl && (status === 'unauthenticated' || status === 'degraded')) {
    const link = document.createElement('a');
    link.className = 'chip-link';
    link.href = loginUrl;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = '去登录';
    link.title = `在浏览器打开治理面登录页：${loginUrl}`;
    chip.append(' ', link);
  }
  const lines = [
    governance.degradedReason ||
      '策略已下发：模型请求经网关，审计事件上报服务端（客户端不持有云端密钥）',
  ];
  const quota = quotaLine(governance);
  if (quota) lines.push(quota);
  if (exhausted) lines.push('今日云端额度已用尽，模型请求会自动降级为本地处理。');
  chip.title = lines.join('\n');
}

/** 治理面登录页地址：`<serverUrl>` 去掉尾斜杠后拼 `/login`；没配治理面则为空。 */
function loginPageUrl(governance) {
  const base = governance && governance.serverUrl;
  if (!base) return '';
  return String(base).replace(/\/+$/, '') + '/login';
}

function setState(state) {
  const chip = $('chip-state');
  chip.dataset.state = state;
  chip.textContent =
    { idle: '空闲', running: '执行中', awaiting_approval: '等待审批' }[state] || state;
  const sb = $('sb-state');
  sb.dataset.state = state;
  sb.textContent =
    { idle: '● 空闲', running: '● 执行中', awaiting_approval: '● 等待审批' }[state] || `● ${state}`;
  S.running = state !== 'idle';
  $('btn-interrupt').disabled = !S.running;
  $('btn-reset').disabled = S.running;
  $('btn-rollback').disabled = S.running;
  $('input').disabled = S.running;
  $('btn-send').disabled = S.running || !$('input').value.trim();
  $('composer-status').textContent = S.running ? 'Agent 正在工作…' : '就绪 · Enter 发送';
}

function updateStatusBarFiles() {
  const sb = $('sb-files');
  sb.hidden = S.changed.size === 0;
  sb.textContent = `改动 ${S.changed.size} 个文件`;
}

function updateStatusBarIter() {
  const sb = $('sb-iter');
  sb.hidden = !S.iterations;
  sb.textContent = S.iterations ? `第 ${S.iterations}/${S.iterationTotal || '?'} 轮` : '';
}

function updateStats(stats) {
  if (!stats) return;
  const parts = [`轮次 ${stats.iterations}`, `工具 ${stats.toolCalls}`];
  if (stats.toolFailures) parts.push(`失败 ${stats.toolFailures}`);
  if (stats.denials) parts.push(`被拒 ${stats.denials}`);
  if (stats.trimmed) parts.push(`裁剪 ${stats.trimmed} 条`);
  if (stats.promptTokens || stats.completionTokens) {
    parts.push(`tokens ↑${stats.promptTokens} ↓${stats.completionTokens}`);
  }
  $('composer-stats').textContent = parts.join(' · ');
}

/* ═══════════════════════ 上下文预算条（P2-17） ═══════════════════════
   数据源两处、同一份快照：首帧来自 session.ready / GET /api/state 的 `context`，
   之后每轮由 `run.context` 更新（都在**裁剪之后**发，所以看到的是模型这一轮
   真正读到的长度）。`budgetTokens=0` 时整条隐藏 —— 那套部署仍按字符口径裁剪，
   画一条按 token 的进度条只会误导。 */
const CTX_SEGS = [
  ['system', '系统'],
  ['rules', '规则'],
  ['history', '历史'],
  ['toolOutputs', '工具输出'],
];

function fmtTokens(n) {
  const v = Math.max(0, Math.round(Number(n) || 0));
  if (v < 1000) return String(v);
  if (v < 10000) return (v / 1000).toFixed(1) + 'k';
  return Math.round(v / 1000) + 'k';
}

function applyContext(ctx) {
  S.context = ctx && ctx.usedTokens != null ? ctx : null;
  renderContext();
}

function renderContext() {
  const bar = $('ctx-bar');
  if (!bar) return;
  const ctx = S.context;
  const budget = (ctx && Number(ctx.budgetTokens)) || 0;
  if (!ctx || !budget) {
    bar.hidden = true;
    $('ctx-track').replaceChildren();
    return;
  }
  const segs = ctx.segments || {};
  const used = Number(ctx.usedTokens) || 0;
  // 分母取 max(预算, 用量)：超限时按超出后的总长等比缩放，条始终占满而不溢出。
  const denom = Math.max(budget, used) || 1;
  $('ctx-track').replaceChildren(...CTX_SEGS.map(([key]) => {
    const node = el('div', `ctx-seg is-${key}`);
    node.style.width = `${(((Number(segs[key]) || 0) / denom) * 100).toFixed(2)}%`;
    return node;
  }));
  const ratio = used / budget;
  bar.classList.toggle('is-near', ratio >= 0.8 && ratio < 1);
  bar.classList.toggle('is-over', ratio >= 1);
  $('ctx-label').textContent =
    `上下文 ${fmtTokens(used)} / ${fmtTokens(budget)}`
    + (ctx.trimmedLastRun ? ' · 本轮已裁剪' : '');
  // hover 交代两件在数字上看不出来的事：各段各占多少、估算器是谁。
  // 估算器必须可见 —— tiktoken 与启发式的差值能到两成，不说清等于骗人。
  bar.title =
    CTX_SEGS.map(([key, label]) => `${label} ${fmtTokens(segs[key])}`).join(' · ')
    + `　估算器：${ctx.estimator === 'tiktoken' ? 'tiktoken' : '启发式'}`;
  const cache = $('ctx-cache');
  if (ctx.cache === 'on') {
    cache.hidden = false;
    cache.className = 'ctx-cache';
    cache.textContent = '前缀缓存已启用';
    cache.title = '同一会话的稳定前缀可被上游缓存，命中时更快也更省';
  } else if (ctx.cache === 'unsupported') {
    cache.hidden = false;
    cache.className = 'ctx-cache is-warn';
    cache.textContent = '上游不支持前缀缓存';
    cache.title = '探测到上游严格拒收该字段，已永久回退为不注入（功能不受影响）';
  } else {
    cache.hidden = true;
  }
  bar.hidden = false;
}

/* ═══════════════════════════════ 对话流 ═══════════════════════════════ */
function block(kind, extraClass = '') {
  const node = el('div', `block block-${kind} ${extraClass}`.trim());
  stream.appendChild(node);
  return node;
}

function addUser(text) {
  hideChatWelcome();
  const node = block('user');
  node.appendChild(document.createTextNode(text));
  scrollToEnd(true);
}

function addAnswer(text) {
  const node = block('answer');
  node.appendChild(document.createTextNode(text));
  addRememberButton(node, text);
  scrollToEnd();
}

/* ---- 流式回复（A1） ---- */
function appendDelta(text) {
  if (!text) return;
  if (!S.streaming) {
    hideChatWelcome();
    const node = block('answer', 'is-streaming');
    const textNode = document.createTextNode('');
    node.appendChild(textNode);
    S.streaming = { node, textNode, text: '' };
  }
  S.streaming.text += text;
  S.streaming.textNode.textContent = S.streaming.text;
  scrollToEnd();
}

function finishStreaming(finalText) {
  const entry = S.streaming;
  S.streaming = null;
  if (!entry) return null;
  entry.node.classList.remove('is-streaming');
  if (typeof finalText === 'string') {
    entry.text = finalText;
    entry.textNode.textContent = finalText;
  }
  addRememberButton(entry.node, entry.text);
  return entry;
}

function addIteration(index, total) {
  S.iterations = index;
  S.iterationTotal = total;
  updateStatusBarIter();
  const node = el('div', 'iteration-line', `第 ${index} / ${total} 轮`);
  stream.appendChild(node);
  scrollToEnd();
}

function addNotice(text, isError = false) {
  const node = el('div', 'notice' + (isError ? ' is-error' : ''), text);
  stream.appendChild(node);
  scrollToEnd();
}

/* ---- 工具卡片（聊天流内，可折叠） ---- */
function summarizeArgs(args) {
  if (!args) return '';
  for (const key of ['path', 'command', 'pattern', 'name']) {
    if (args[key] != null) {
      return String(args[key]).replace(/\n/g, '⏎').slice(0, 90);
    }
  }
  const first = Object.entries(args)[0];
  return first ? `${first[0]}=${String(first[1]).slice(0, 60)}` : '';
}

function addToolCall(ev) {
  hideChatWelcome();
  const card = el('div', 'tool-card');
  const head = el('div', 'tool-head');
  head.appendChild(el('span', 'tool-arrow', '▶'));
  head.appendChild(el('span', 'tool-name', ev.tool));
  if (ev.risk && ev.risk !== 'SAFE') {
    const pill = el('span', 'risk-pill', ev.risk);
    pill.dataset.r = ev.risk;
    head.appendChild(pill);
  }
  head.appendChild(el('span', 'tool-arg', summarizeArgs(ev.arguments)));
  const status = el('span', 'tool-status', '执行中…');
  status.dataset.s = 'run';
  head.appendChild(status);

  const body = el('div', 'tool-body');
  const out = el('pre', 'tool-output', '');
  body.appendChild(out);

  card.append(head, body);
  head.addEventListener('click', () => card.classList.toggle('is-open'));

  stream.appendChild(card);
  S.calls.set(ev.callId, { card, body, out, status, name: ev.tool });
  scrollToEnd();

  // 命令类工具同步进底部终端
  if (COMMAND_TOOLS.test(ev.tool)) {
    const cmd = ev.arguments && (ev.arguments.command || summarizeArgs(ev.arguments));
    if (cmd) termLine('output', `$ ${cmd}`, 'is-cmd');
  }
}

function completeToolCall(ev) {
  const entry = S.calls.get(ev.callId);
  if (!entry) {
    // 补齐历史时可能只拿到结果 —— 终端仍然要补上
    if (COMMAND_TOOLS.test(ev.tool || '') && ev.content) {
      termLine('output', ev.content, ev.ok ? '' : 'is-err');
    }
    return;
  }

  entry.status.textContent = ev.ok ? '完成' : '失败';
  entry.status.dataset.s = ev.ok ? 'ok' : 'fail';
  entry.out.textContent = ev.content || '（无输出）';
  if (!ev.ok) entry.out.classList.add('is-error');

  entry.body.querySelectorAll('.diff').forEach((n) => n.remove());

  if (ev.diff) {
    const node = renderDiff(ev.diff);
    if (node) {
      entry.body.insertBefore(node, entry.out);
      entry.card.classList.add('is-open'); // 编辑类工具默认展开
    }
  }
  if (ev.meta?.path) markChangedFile(ev.meta.path, ev.meta.created);
  if (COMMAND_TOOLS.test(entry.name)) {
    termLine('output', ev.content || '（无输出）', ev.ok ? '' : 'is-err');
  }
  scrollToEnd();
}

/* ---- 子 Agent 卡片（M3）--------------------------------------------------
 * 一次 `task` 委派会在父侧工具卡片里内嵌一个**可展开**的子列表：子 Agent 的
 * 迭代与只读工具调用逐条列出，收尾时给一行汇总（N 轮 · M 次只读工具 · 摘要 K 字）。
 * 按 `callId` 归位到对应卡片 —— 与 completeToolCall 同一套关联机制。
 * 事件里**没有**子 Agent 正文，界面也不展示正文（正文在父上下文里，不在 UI）。
 * 子 Agent 未启用时不会收到任何 run.subagent 事件，界面因此逐字节不变。 */
function applySubAgent(ev) {
  const entry = S.calls.get(ev.callId);
  if (!entry) return; // 历史重放缺了调用卡片时静默跳过，不抛错
  if (!entry.sub) {
    const root = el('div', 'subagent');
    const head = el('div', 'subagent-head', '');
    const list = el('div', 'subagent-list', '');
    root.append(head, list);
    // 正文（pre.tool-output）之前插入：过程在上、结论在下，符合阅读顺序
    entry.body.insertBefore(root, entry.out);
    entry.sub = { root, head, list, pending: null };
  }
  const { root, head, list } = entry.sub;
  const line = (cls, text) => {
    const node = el('div', 'subagent-line ' + cls, text);
    list.appendChild(node);
    return node;
  };

  switch (ev.phase) {
    case 'started':
      root.classList.add('is-running');
      head.textContent = `子 Agent 探索中…（任务 ${ev.task_chars || 0} 字）`;
      entry.card.classList.add('is-open');
      break;
    case 'iteration':
      line('is-dim', `第 ${ev.index}/${ev.total} 轮`);
      break;
    case 'tool':
      entry.sub.pending = line(
        '',
        `· ${ev.name || '?'}${ev.risk && ev.risk !== 'SAFE' ? ` [${ev.risk}]` : ''}`
      );
      break;
    case 'tool_result':
      if (entry.sub.pending) {
        entry.sub.pending.textContent += ev.ok ? ' ✓' : ' ✗';
        if (!ev.ok) entry.sub.pending.classList.add('is-error');
        entry.sub.pending = null;
      }
      break;
    case 'notice':
      line('is-dim', ev.text || '');
      break;
    case 'finished':
      root.classList.remove('is-running');
      root.classList.add('is-done');
      head.textContent =
        `子 Agent 完成：${ev.iterations || 0} 轮 · ${ev.tool_calls || 0} 次只读工具 · ` +
        `摘要 ${ev.answer_chars || 0} 字`;
      entry.card.classList.add('is-open');
      break;
    case 'failed':
      root.classList.remove('is-running');
      root.classList.add('is-failed');
      head.textContent = '子 Agent 失败（父任务继续）';
      line('is-error', ev.error || '未知错误');
      entry.card.classList.add('is-open');
      break;
  }
  scrollToEnd();
}

/* ═══════════════════════════════ 输出面板 ═══════════════════════════════
 * Agent 的命令输出统一进「输出」面板；「终端」是用户本人的交互式终端（P1-7），
 * 两条通道在界面上也不混。panel 参数保留是为了调用点可读，当前恒为 'output'。 */
function termLine(panel, text, cls = '') {
  const host = $('output');
  const line = el('div', 'term-line ' + cls, text);
  host.appendChild(line);
  const stick = host.scrollHeight - host.scrollTop - host.clientHeight < 60;
  if (stick) host.scrollTop = host.scrollHeight;
}

function toggleBottom(open) {
  const bp = $('bottom-panel');
  const collapsed = bp.classList.toggle('is-collapsed', !open);
  $('btn-bottom-toggle').textContent = collapsed ? '▴' : '▾';
}

function switchBottom(name) {
  document.querySelectorAll('.bp-tab').forEach((t) => t.classList.toggle('is-active', t.dataset.bp === name));
  document.querySelectorAll('.bp-body').forEach((b) => { b.hidden = b.dataset.bp !== name; });
}

/* ═══════════════════════════════ 交互式终端（P1-7）═══
 * 用户在 IDE 里直接敲命令（ADR-011 范式）：不走审批浮层，但后端记审计。
 * 输出经 SSE `term.data`（base64）/ `term.exit` 送达，与 run.* 事件流分离。
 * 渲染只实现最小 ANSI 子集：SGR 前景/背景色与加粗、\r、\n、\b、清屏/清行；
 * 不做完整 VT 仿真（见 P1-P2-FEATURES-PLAN §10「明确不做」）。
 * ─────────────────────────────────────────────────────── */
const term = {
  rows: 24, cols: 80,
  buf: [], curRow: 0, curCol: 0,
  open: false, closed: false, enabled: true, id: null, mode: 'pty',
  sgr: { fg: '', bg: '', bold: false }, sgrCss: '',
  state: 0, csi: '', decoder: null,
};

const TERM_SGR = {
  // 16 色标准调色板 → 显示色（前景；背景 + 10 同段）
  0: '#d8d8d8', 1: '#d54e53', 2: '#b9ca4a', 3: '#e7c547',
  4: '#7aa6da', 5: '#c397d8', 6: '#70c0b1', 7: '#fff',
  8: '#666', 9: '#ff6c6b', 10: '#b9ca4a', 11: '#ffd27f',
  12: '#7aa6da', 13: '#c397d8', 14: '#70c0b1', 15: '#fff',
};

function termRender() {
  const host = $('term');
  const stick = host.scrollHeight - host.scrollTop - host.clientHeight < 60;
  const cursorRow = term.open && !term.closed ? term.curRow : -1;
  const frag = document.createDocumentFragment();
  for (let r = 0; r < term.rows; r++) {
    const row = el('div', 'term-row');
    const cells = term.buf[r] || [];
    let run = null;
    let runSt = null;
    const flush = () => { if (run) { row.appendChild(run); run = null; } };
    for (let c = 0; c < term.cols; c++) {
      const cell = cells[c];
      const ch = cell ? cell.t : ' ';
      const st = cell ? cell.st : '';
      if (r === cursorRow && c === term.curCol) {
        flush();
        row.appendChild(el('span', 'term-cursor', ch));
        runSt = null;
        continue;
      }
      if (!run || st !== runSt) {
        flush();
        run = el('span');
        runSt = st;
        if (st) run.setAttribute('style', st);
      }
      run.textContent += ch;
    }
    flush();
    frag.appendChild(row);
  }
  host.replaceChildren(frag);
  if (stick) host.scrollTop = host.scrollHeight;
}

function termSgrCss() {
  let css = '';
  if (term.sgr.fg) css += `color:${term.sgr.fg};`;
  if (term.sgr.bg) css += `background:${term.sgr.bg};`;
  if (term.sgr.bold) css += 'font-weight:700;';
  return css;
}

function termSgr(codes) {
  for (const code of (codes.length ? codes : [0])) {
    if (code === 0) { term.sgr.fg = ''; term.sgr.bg = ''; term.sgr.bold = false; }
    else if (code === 1) term.sgr.bold = true;
    else if (code === 22) term.sgr.bold = false;
    else if (code === 39) term.sgr.fg = '';
    else if (code === 49) term.sgr.bg = '';
    else if (code >= 30 && code <= 37) term.sgr.fg = TERM_SGR[code - 30];
    else if (code >= 90 && code <= 97) term.sgr.fg = TERM_SGR[code - 90 + 8];
    else if (code >= 40 && code <= 47) term.sgr.bg = TERM_SGR[code - 40 + 10];
    else if (code >= 100 && code <= 107) term.sgr.bg = TERM_SGR[code - 100 + 18];
  }
  term.sgrCss = termSgrCss();
}

function termNewline() {
  term.curRow++;
  term.curCol = 0;
  if (term.curRow >= term.rows) {
    term.buf.shift();
    term.curRow = term.rows - 1;
  }
}

function termWrite(ch) {
  if (term.curCol >= term.cols) termNewline();
  const row = term.buf[term.curRow] || (term.buf[term.curRow] = []);
  row[term.curCol] = { t: ch, st: term.sgrCss };
  term.curCol++;
}

function termClamp(value, lo, hi) { return Math.max(lo, Math.min(hi, value)); }

function termEraseDisplay(mode) {
  if (mode === 2 || mode === 3) { term.buf = []; term.curRow = 0; term.curCol = 0; return; }
  if (mode === 0) {
    const row = term.buf[term.curRow] || (term.buf[term.curRow] = []);
    row.length = term.curCol;
    term.buf.length = term.curRow + 1;
  }
}

function termEraseLine(mode) {
  const row = term.buf[term.curRow] || (term.buf[term.curRow] = []);
  if (mode === 2) row.length = 0;
  else if (mode === 1) { for (let c = 0; c <= term.curCol; c++) row[c] = undefined; }
  else row.length = term.curCol;
}

function termCsi(params, final) {
  const nums = params.replace(/\?/g, '').split(';').map((n) => (n === '' ? 0 : parseInt(n, 10)));
  const n0 = nums[0] || 0;
  const arg = (i, fallback = 1) => nums[i] || fallback;
  switch (final) {
    case 'm': termSgr(nums.filter((n) => !Number.isNaN(n))); break;
    case 'J': termEraseDisplay(n0); break;
    case 'K': termEraseLine(n0); break;
    case 'H': case 'f':
      term.curRow = termClamp(arg(0, 1) - 1, 0, term.rows - 1);
      term.curCol = termClamp(arg(1, 1) - 1, 0, term.cols - 1);
      break;
    case 'A': term.curRow = Math.max(0, term.curRow - arg(0)); break;
    case 'B': term.curRow = Math.min(term.rows - 1, term.curRow + arg(0)); break;
    case 'C': term.curCol = Math.min(term.cols - 1, term.curCol + arg(0)); break;
    case 'D': term.curCol = Math.max(0, term.curCol - arg(0)); break;
    case 'G': term.curCol = termClamp(arg(0, 1) - 1, 0, term.cols - 1); break;
    case 'd': term.curRow = termClamp(arg(0, 1) - 1, 0, term.rows - 1); break;
    default: break; // 其余（光标显隐 / 滚动 / 私有模式）忽略 —— 最小子集
  }
}

/* 状态机跨 chunk 保持：转义序列可能被切成两半（见 term.decoder 的流式解码）。 */
function termProcess(text) {
  for (const ch of text) {
    if (term.state === 3) { if (ch === '\x07') term.state = 0; continue; }   // OSC
    if (term.state === 2) {
      if (ch >= '@' && ch <= '~') { termCsi(term.csi, ch); term.csi = ''; term.state = 0; }
      else term.csi += ch;
      continue;
    }
    if (term.state === 1) {
      if (ch === '[') { term.state = 2; term.csi = ''; }
      else if (ch === ']') term.state = 3;
      else term.state = 0;
      continue;
    }
    if (ch === '\x1b') { term.state = 1; continue; }
    if (ch === '\r') { term.curCol = 0; continue; }
    if (ch === '\n') { termNewline(); continue; }
    if (ch === '\b') { term.curCol = Math.max(0, term.curCol - 1); continue; }
    if (ch === '\t') { const n = 8 - (term.curCol % 8); for (let i = 0; i < n; i++) termWrite(' '); continue; }
    if (ch < ' ') continue;  // 其余控制字符（含 BEL）忽略
    termWrite(ch);
  }
}

function termFeed(b64) {
  if (!term.open) return;
  if (!term.decoder) term.decoder = new TextDecoder('utf-8');
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  termProcess(term.decoder.decode(bytes, { stream: true }));
  termRender();
}

function termReset() {
  term.buf = [];
  term.curRow = 0;
  term.curCol = 0;
  term.state = 0;
  term.csi = '';
  term.sgr = { fg: '', bg: '', bold: false };
  term.sgrCss = '';
  term.decoder = new TextDecoder('utf-8');
}

async function termOpen() {
  if (term.open) return;
  if (!term.enabled) { toast('交互式终端已被配置关闭（VIBE_TERM=0）', 'error'); return; }
  try {
    termResize();  // 用当前容器尺寸开一个大小合适的终端
    const res = await api.post('/api/term/open', { cols: term.cols, rows: term.rows });
    term.id = res.termId;
    term.mode = res.mode;
    term.open = true;
    // `closed` 只表示「当前这个会话已终止」，不是「终端入口作废」：新会话起来后必须
    // 清掉，否则第一次关闭就把入口闩死，之后点「开启终端」直接 return、什么都看不到。
    term.closed = false;
    termReset();
    const mode = $('term-mode');
    mode.hidden = false;
    mode.classList.toggle('is-warn', res.mode !== 'pty');
    mode.textContent = res.mode === 'pty' ? 'PTY' : '管道模式';
    $('term-note').textContent = res.fallbackReason
      ? `降级为管道模式：${res.fallbackReason}`
      : '交互式终端 · 与 Agent 通道隔离';
    $('btn-term-open').hidden = true;
    $('btn-term-close').hidden = false;
    termRender();
    $('term').focus();
  } catch (err) {
    toast(`终端启动失败：${err.message}`, 'error');
  }
}

function termMarkClosed(note) {
  term.open = false;
  term.closed = true;
  termRender();
  $('term-note').textContent = note;
  $('btn-term-open').hidden = false;
  $('btn-term-close').hidden = true;
  $('term-mode').hidden = true;
}

async function termClose() {
  if (!term.open) return;
  const id = term.id;
  term.closed = true;  // 先标记：避免后端 TERM_EXIT 再次触发收尾
  try { await api.post('/api/term/close', { termId: id }); } catch { /* 后端已清理 */ }
  termMarkClosed('终端已关闭 · 点击「开启终端」启动新会话');
}

/** term.exit 事件：用户敲 exit 或进程自己结束。 */
function termOnExit(exitCode) {
  if (!term.open) return;
  termMarkClosed(`终端已结束${exitCode ? `（退出码 ${exitCode}）` : ''} · 点击「开启终端」重启`);
}

function termInput(data) {
  if (!term.open || term.closed || !data) return;
  api.post('/api/term/input', { termId: term.id, data }).catch(() => {});
}

/** 容器尺寸变化 → 用等宽字符测度换算 cols/rows → 通知后端重排。 */
function termResize() {
  const host = $('term');
  if (!host) return;
  const probe = el('span', null, 'M'.repeat(50));
  probe.style.position = 'absolute';
  probe.style.visibility = 'hidden';
  probe.style.whiteSpace = 'pre';
  host.appendChild(probe);
  const charW = probe.getBoundingClientRect().width / 50 || 8;
  probe.remove();
  const lineH = parseFloat(getComputedStyle(host).lineHeight) || 18;
  term.cols = Math.max(20, Math.floor((host.clientWidth - 16) / charW));
  term.rows = Math.max(3, Math.floor((host.clientHeight - 8) / lineH));
  if (term.open && !term.closed) {
    api.post('/api/term/resize', { termId: term.id, cols: term.cols, rows: term.rows })
      .catch(() => {});
    termRender();
  }
}

function termKeydown(event) {
  if (!term.open || term.closed) return;
  const k = event.key;
  if (k === 'Enter') { termInput('\r'); event.preventDefault(); return; }
  if (k === 'Backspace') { termInput('\x7f'); event.preventDefault(); return; }
  if (k === 'Tab') { termInput('\t'); event.preventDefault(); return; }
  if (k === 'Escape') { termInput('\x1b'); event.preventDefault(); return; }
  if (event.ctrlKey && k.length === 1) {
    const code = k.toUpperCase().charCodeAt(0) - 64;
    if (code >= 1 && code <= 26) { termInput(String.fromCharCode(code)); event.preventDefault(); }
    return;
  }
  if (k.length === 1 && !event.metaKey && !event.altKey) { termInput(k); event.preventDefault(); return; }
  const seq = {
    ArrowUp: '\x1b[A', ArrowDown: '\x1b[B', ArrowRight: '\x1b[C', ArrowLeft: '\x1b[D',
    Home: '\x1b[H', End: '\x1b[F', Delete: '\x1b[3~', PageUp: '\x1b[5~', PageDown: '\x1b[6~',
  }[k];
  if (seq) { termInput(seq); event.preventDefault(); }
}

function termPaste(event) {
  if (!term.open || term.closed) return;
  const text = (event.clipboardData || window.clipboardData)?.getData('text') || '';
  if (text) { termInput(text); event.preventDefault(); }
}

/* ═══════════════════════════════ 编辑器标签 ═══════════════════════════════ */
function activateEditor(id) {
  S.activeEditor = id;
  document.querySelectorAll('.editor').forEach((n) => n.classList.toggle('is-active', n.dataset.ed === id));
  document.querySelectorAll('.edtab').forEach((t) => t.classList.toggle('is-active', t.dataset.ed === id));

  const entry = S.editors.get(id);
  const toolbar = $('edtoolbar');
  const mdToggle = $('btn-md-toggle');
  const editToggle = $('btn-edit-toggle');
  const saveBtn = $('btn-save');
  const previewBtn = $('btn-preview');
  if (entry && entry.kind === 'file') {
    toolbar.hidden = false;
    $('ed-path').textContent = entry.data.path;
    $('ed-meta').textContent = `${entry.data.lines} 行 · ${String(entry.data.encoding).toUpperCase()} · ${entry.data.newline}${entry.data.bom ? ' · BOM' : ''}`;
    // 编辑模式与 Markdown 预览互斥：要改就改原文，别在渲染视图里改
    mdToggle.hidden = !!entry.editing || !isMarkdown(entry.data.path);
    mdToggle.classList.toggle('on', entry.mode === 'preview');
    mdToggle.querySelector('span').textContent = entry.mode === 'preview' ? '原文' : '预览';
    // 网页预览（O）：只对可渲染的网页文件出现，纯静态服务，任何时候都可用
    previewBtn.hidden = !WEB_EXT.test(entry.data.path);
    editToggle.hidden = false;
    editToggle.classList.toggle('on', !!entry.editing);
    editToggle.textContent = entry.editing ? '只读' : '编辑';
    saveBtn.hidden = !entry.editing;
    saveBtn.disabled = !entry.dirty;
    setLspChip(entry);
  } else if (entry && entry.kind === 'diff') {
    toolbar.hidden = false;
    $('ed-path').textContent = `${entry.path}（改动）`;
    $('ed-meta').textContent = entry.created ? '新建' : '修改';
    mdToggle.hidden = true;
    previewBtn.hidden = true;
    editToggle.hidden = true;
    saveBtn.hidden = true;
    $('ed-lsp').hidden = true;
  } else if (entry && entry.kind === 'preview') {
    // 预览标签自带工具条（刷新/新窗口/设备宽度/自检），主工具条整条让位
    toolbar.hidden = true;
    mdToggle.hidden = true;
    previewBtn.hidden = true;
    editToggle.hidden = true;
    saveBtn.hidden = true;
    $('ed-lsp').hidden = true;
  } else {
    toolbar.hidden = true;
    mdToggle.hidden = true;
    previewBtn.hidden = true;
    editToggle.hidden = true;
    saveBtn.hidden = true;
    $('ed-lsp').hidden = true;
  }
}

function makeTab(id, icon, label, { dot = false, closeable = true } = {}) {
  const tab = el('div', 'edtab');
  tab.dataset.ed = id;
  tab.appendChild(el('span', 'ed-icon', icon));
  tab.appendChild(el('span', 'ed-label', label));
  if (dot) tab.appendChild(el('span', 'ed-dot'));
  if (closeable) {
    const x = el('span', 'ed-close', '✕');
    x.title = '关闭';
    x.addEventListener('click', (event) => { event.stopPropagation(); closeEditor(id); });
    tab.appendChild(x);
  }
  tab.addEventListener('click', () => activateEditor(id));
  return tab;
}

function openEditor(id, icon, label, node, opts = {}) {
  let entry = S.editors.get(id);
  if (!entry) {
    entry = {
      id,
      tab: makeTab(id, icon, label, opts),
      node,
      kind: opts.kind || 'file',
      mode: opts.mode || (opts.kind === 'file' && isMarkdown(id) ? 'preview' : 'code'),
      data: opts.data || { path: id, lines: '', encoding: '', newline: '', bom: false },
      path: opts.path || id,
      created: !!opts.created,
    };
    node.dataset.ed = id;
    node.classList.add('editor');
    $('editor-host').appendChild(node);
    $('edtabs').appendChild(entry.tab);
    S.editors.set(id, entry);
  }
  updateLayout();   // SOLO 下打开标签才把编辑器区夹出来
  activateEditor(id);
  return entry;
}

function closeEditor(id) {
  const entry = S.editors.get(id);
  if (!entry) return;
  entry.tab.remove();
  entry.node.remove();
  S.editors.delete(id);
  updateLayout();   // SOLO 下关掉最后一个标签则收回编辑器区
  if (S.activeEditor === id) {
    const rest = [...S.editors.keys()];
    activateEditor(rest.length ? rest[rest.length - 1] : 'welcome');
  }
}

/* 只读代码视图。`entry` 用来把 LSP 诊断标到行号上（E7）——
   行号 span 带 data-line，装饰只改样式，不动正文。 */
function buildCodeView(data, entry) {
  const view = el('div', 'file-view');
  const pre = document.createElement('pre');
  const lines = String(data.text ?? '').split('\n');
  const frag = document.createDocumentFragment();
  lines.forEach((line, i) => {
    const span = el('span', 'ln', String(i + 1));
    span.dataset.line = String(i + 1);
    applyDiagMark(span, entry, i + 1);
    frag.appendChild(span);
    frag.appendChild(document.createTextNode(line + (i < lines.length - 1 ? '\n' : '')));
  });
  pre.appendChild(frag);
  view.appendChild(pre);
  if (entry) bindViewPointing(entry, pre);
  return view;
}

/* 给一个行号节点按 LSP 诊断上色。同一行只保留最严重的一条 ——
   多个诊断叠在同一个 gutter 上只会互相盖掉。 */
function applyDiagMark(node, entry, lineNo) {
  const mark = entry && entry.diagByLine ? entry.diagByLine[lineNo] : null;
  if (!mark) return;
  node.classList.add(`ln-${mark.severity}`);
  node.title = mark.text;
}

/* 把后端来的文本统一成「\n」行尾再放进视图。
   编辑面只在规范化文本上工作：CRLF/BOM 的往返保真交给保存接口的 TextDoc，
   界面这一层不必（也不该）关心行尾，否则光标位置的字节偏移会跟行号对不上。 */
function normalizeNewlines(text) {
  return String(text ?? '').replace(/\r\n?/g, '\n');
}

/* 统一的「文件标签内容渲染」入口：预览 / 只读代码 / 编辑三种形态一处决定。
   之前 openFile 与 toggleMarkdownMode 各自 replaceChildren，加编辑模式后
   三处分支会互相打架（比如切到编辑再点预览），收敛成一个函数。 */
function renderFileEntry(entry) {
  if (!entry || entry.kind !== 'file') return;
  if (entry.editing) {
    entry.node.replaceChildren(buildEditView(entry));
  } else if (entry.mode === 'preview' && isMarkdown(entry.path)) {
    entry.node.replaceChildren(render(entry.data.text));
  } else {
    entry.node.replaceChildren(buildCodeView(entry.data, entry));
  }
  activateEditor(entry.id);
  maybeRefreshDiagnostics(entry);
}

async function openFile(path, row) {
  if (row) {
    document.querySelectorAll('.tree-node.is-active').forEach((n) => n.classList.remove('is-active'));
    row.classList.add('is-active');
  }
  const id = `file:${path}`;
  const existing = S.editors.get(id);
  if (existing) {
    // 有未保存的编辑时绝不重读：磁盘内容会把用户正在写的东西盖掉
    if (existing.editing && existing.dirty) { activateEditor(id); return; }
    // 已开着：重读一遍（Agent 可能刚改过它），保持「所见即当前内容」
    try {
      const data = await api.get('/api/file', { path });
      existing.data = data;
      existing.digest = data.digest || '';
      existing.savedText = normalizeNewlines(data.text);
      existing.dirty = false;
      resetDiagnostics(existing);   // 重读到了新内容，诊断也跟着重取（E7）
      renderFileEntry(existing);
    } catch { /* 复用旧内容 */ }
    activateEditor(id);
    return;
  }
  const host = el('div');
  host.appendChild(el('div', 'empty', '读取中…'));
  openEditor(id, '📄', baseName(path), host, { kind: 'file', data: { path, lines: '…' } });
  try {
    const data = await api.get('/api/file', { path });
    const entry = S.editors.get(id);
    entry.data = data;
    entry.digest = data.digest || '';
    entry.savedText = normalizeNewlines(data.text);
    entry.dirty = false;
    renderFileEntry(entry);
  } catch (err) {
    const entry = S.editors.get(id);
    entry.node.replaceChildren(el('div', 'empty', `无法打开：${err.message}`));
  }
}

function toggleMarkdownMode() {
  const entry = S.editors.get(S.activeEditor);
  if (!entry || entry.kind !== 'file' || !isMarkdown(entry.path)) return;
  entry.mode = entry.mode === 'preview' ? 'code' : 'preview';
  renderFileEntry(entry);
}

/* ═══════════════════════════ 网页预览（O/P1-11） ═══════════════════════════
   两件事分开，别混：

   - **静态预览**：host 的 `GET /preview/<relpath>` 把工作区里的网页文件原样
     送出（PathGuard + MIME 白名单 + 预览专用 CSP，`connect-src 'none'` 把
     网络出口焊死）。iframe 直指这个地址即可，零依赖、零风险。
   - **网页自检**：截图只能由 **Electron 壳**的离屏桥产生（host 是 Python 进程，
     反向去调 IPC 是不可能的），再由 `POST /api/visual/check` 与基线做**粗粒度**
     分块色差。浏览器直开没有桥，于是如实提示「需桌面端」—— 与 LSP/MCP 同范式，
     绝不留一个点了必报错的按钮。
   ========================================================================== */

// 可在编辑器内渲染预览的文件类型；其余走只读代码视图 / Markdown 渲染
const WEB_EXT = /\.(html?|xhtml)$/i;

const PREVIEW_DEVICES = [
  { label: '自适应', w: 0 },
  { label: '桌面 1280', w: 1280 },
  { label: '笔记本 1024', w: 1024 },
  { label: '平板 768', w: 768 },
  { label: '手机 375', w: 375 },
];
const PREVIEW_HEIGHT = 800;   // 自检截图高度；宽度取当前所选设备宽度

/* 工作区相对路径 → /preview/ 地址。逐段编码，别把路径里的 `/` 也编掉。 */
function previewPath(path) {
  const segs = String(path).replace(/\\/g, '/').split('/').filter(Boolean);
  return '/preview/' + segs.map(encodeURIComponent).join('/');
}

function openPreview(path) {
  const id = `preview:${path}`;
  const existing = S.editors.get(id);
  if (existing) { activateEditor(id); return existing; }

  const node = el('div', 'preview-wrap');
  const frame = document.createElement('iframe');
  frame.className = 'preview-frame';
  frame.setAttribute('title', `预览 ${path}`);
  // 刻意**不加** sandbox：无 allow-same-origin 会让文档变不透明源，预览响应的
  // CSP `'self'` 便不再匹配同源子资源，页面样式 / 脚本会被整片拦掉。出口安全
  // 交给预览响应自带的 CSP（connect-src 'none' / form-action 'none'）。
  frame.src = previewPath(path);

  const reload = el('button', 'tbtn', '刷新');
  reload.title = '重新加载预览';
  reload.addEventListener('click', () => { frame.src = frame.src; });
  const popout = el('button', 'tbtn', '新窗口');
  popout.title = '在浏览器新窗口打开这个本机地址';
  popout.addEventListener('click', () => window.open(frame.src, '_blank', 'noopener'));

  const device = document.createElement('select');
  device.className = 'preview-device';
  for (const d of PREVIEW_DEVICES) {
    const opt = document.createElement('option');
    opt.value = String(d.w);
    opt.textContent = d.label;
    device.appendChild(opt);
  }

  const status = el('span', 'chip preview-status', '未自检');
  const checkBtn = el('button', 'tbtn', '截图自检');
  checkBtn.title = '用桌面端离屏截图，与基线做粗粒度分块比对';
  const baseBtn = el('button', 'tbtn', '设为基线');
  baseBtn.title = '把当前截图存为该页面的基线，之后自检都以此为准';

  const bar = el('div', 'preview-bar');
  bar.append(reload, popout, device, el('span', 'preview-spacer'), status, checkBtn, baseBtn);

  const stage = el('div', 'preview-stage');
  stage.appendChild(frame);
  node.append(bar, stage);

  const entry = openEditor(id, '🌐', `${baseName(path)}（预览）`, node, {
    kind: 'preview', path, previewPath: previewPath(path),
  });
  entry.frame = frame;
  entry.stage = stage;
  entry.status = status;
  entry.device = device;

  const applyWidth = () => {
    const w = Number(device.value) || 0;
    frame.style.width = w ? `${w}px` : '100%';
    stage.classList.toggle('is-fixed', !!w);
  };
  device.addEventListener('change', applyWidth);
  applyWidth();

  checkBtn.addEventListener('click', () => previewSelfCheck(entry, false));
  baseBtn.addEventListener('click', () => previewSelfCheck(entry, true));
  return entry;
}

async function previewSelfCheck(entry, setBaseline) {
  const bridge = window.vibeShell;
  if (!bridge || typeof bridge.capturePage !== 'function') {
    entry.status.className = 'chip preview-status';
    entry.status.textContent = '需桌面端';
    entry.status.title = '浏览器直开拿不到屏幕截图，请用 Electron 桌面端';
    toast('截图自检需要桌面端：浏览器直开没有屏幕捕获能力', 'warn');
    return;
  }
  const width = Number(entry.device.value) || Math.round(entry.stage.clientWidth) || 1280;
  entry.status.className = 'chip preview-status';
  entry.status.textContent = '截图中…';
  try {
    const shot = await bridge.capturePage(location.origin + entry.previewPath, width, PREVIEW_HEIGHT);
    if (!shot || !shot.ok || !shot.image) throw new Error((shot && shot.reason) || '截图失败');
    const body = { url: entry.path, width, height: PREVIEW_HEIGHT, image: shot.image };
    if (setBaseline) body.setBaseline = true;
    const res = await api.post('/api/visual/check', body);
    applyVisualResult(entry, res, setBaseline);
  } catch (err) {
    entry.status.textContent = '自检失败';
    entry.status.title = err.message;
    toast(`截图自检失败：${err.message}`, 'warn');
  }
}

function applyVisualResult(entry, res, setBaseline) {
  if (res.available === false) {
    entry.status.className = 'chip preview-status';
    entry.status.textContent = '需桌面端';
    entry.status.title = res.reason || '';
    toast(res.reason || '当前环境不支持截图自检', 'warn');
    return;
  }
  if (!res.ok) {
    entry.status.className = 'chip preview-status';
    entry.status.textContent = '无法比对';
    entry.status.title = res.reason || '';
    toast(res.reason || '无法比对截图', 'warn');
    return;
  }
  if (setBaseline || res.saved) {
    entry.status.className = 'chip preview-status';
    entry.status.textContent = '基线已保存';
    entry.status.title = `已把当前截图存为基线（宽 ${entry.device.value || '自适应'}）`;
    toast('已设为基线，下次自检以此为准', 'ok');
    return;
  }
  if (res.baselineMissing) {
    entry.status.className = 'chip preview-status';
    entry.status.textContent = '尚无基线';
    entry.status.title = '这个页面还没有基线，点「设为基线」建立基准';
    toast('尚无基线：点「设为基线」建立基准后再自检', 'warn');
    return;
  }
  const pct = (Number(res.diffRatio) || 0) * 100;
  const blocks = `${res.changedBlocks}/${res.totalBlocks}`;
  const warn = pct > 5;
  entry.status.className = 'chip preview-status' + (warn ? ' is-warn' : '');
  entry.status.textContent = `差异 ${pct.toFixed(1)}%`;
  entry.status.title = `分块色差：${blocks} 块有变化（粗粒度提示，非像素级判定）`;
  toast(`网页自检：差异 ${pct.toFixed(1)}%（${blocks} 块变化）`, warn ? 'warn' : 'ok');
}

/* ═══════════════════════════ 语言服务器（E7） ═══════════════════════════════
   界面这一层只调后端，自己绝不起进程。三件事：

   - 诊断：打开文件后拉一次 /api/lsp/diagnostics，按行归并后在行号上着色
     （hover 看原文）。不可用时只是不标 —— 「本机没装 pyright」是常态不是故障。
   - 跳转定义：F12 / Ctrl+Click → /api/lsp/definition，跨文件就开新标签。
   - 重命名：F2 → /api/lsp/rename/preview 出逐文件 diff，hunk 卡片确认后
     /api/lsp/rename/apply 落盘。这是**用户本人动作**，与改写同一条范式。
   ========================================================================== */

// 只有这些后缀才去问语言服务器；Markdown/JSON 之类不打扰后端
const LSP_EXT = /\.(py|pyi|js|jsx|mjs|cjs|ts|tsx|mts|cts|go|rs)$/i;
const LSP_SEVERITY_RANK = { error: 0, warning: 1, info: 2, hint: 3 };
const LSP_SEVERITY_LABEL = { error: '错误', warning: '警告', info: '提示', hint: '建议' };

function lspSupported(path) {
  return LSP_EXT.test(String(path || ''));
}

function severityRank(severity) {
  const key = String(severity || 'info').toLowerCase();
  return key in LSP_SEVERITY_RANK ? LSP_SEVERITY_RANK[key] : LSP_SEVERITY_RANK.info;
}

/* 工具条的 LSP 状态片：有错标红、只警告标黄；不可用则给出原因。 */
function setLspChip(entry) {
  const chip = $('ed-lsp');
  if (!chip) return;
  if (!entry || !lspSupported(entry.path)) { chip.hidden = true; return; }
  const lsp = entry.lsp;
  if (!lsp) { chip.hidden = true; return; }   // 还在取数
  if (!lsp.available) {
    chip.hidden = false;
    chip.className = 'chip';
    chip.textContent = 'LSP 不可用';
    chip.title = lsp.reason || '未检测到可用的语言服务器';
    return;
  }
  const counts = lsp.counts || {};
  const errors = Number(counts.error || 0);
  const warnings = Number(counts.warning || 0);
  chip.hidden = false;
  chip.textContent = `${errors} 错误 / ${warnings} 警告`;
  chip.title = `语言服务器：${lsp.server || '未知'}`;
  chip.className = 'chip' + (errors ? ' is-error' : warnings ? ' is-warning' : '');
}

/* 正文变了（打开/保存/重命名后重读）就作废诊断，下次渲染重新取。 */
function resetDiagnostics(entry) {
  entry.lsp = undefined;
  entry.diagByLine = {};
}

/* 每个文件只问一次（失败也算一次），避免切标签反复打扰后端。 */
function maybeRefreshDiagnostics(entry) {
  if (!entry || entry.kind !== 'file') return;
  if (!lspSupported(entry.path)) {
    entry.lsp = { available: false, reason: '该文件类型不走语言服务器' };
    return;
  }
  if (entry.lsp !== undefined) return;
  entry.lsp = null;   // 占位：重复调用不再触发第二次请求
  refreshDiagnostics(entry);
}

async function refreshDiagnostics(entry) {
  let data;
  try {
    data = await api.get('/api/lsp/diagnostics', { path: entry.path });
  } catch (err) {
    entry.lsp = { available: false, reason: err.message };
    entry.diagByLine = {};
    afterDiagnostics(entry);
    return;
  }
  if (data.available === false) {
    entry.lsp = { available: false, reason: data.reason || '' };
    entry.diagByLine = {};
    afterDiagnostics(entry);
    return;
  }
  entry.lsp = {
    available: true,
    language: data.language || '',
    server: data.server || '',
    counts: data.counts || {},
  };
  const byLine = {};
  for (const item of data.diagnostics || []) {
    const line = Number(item.line);
    if (!line) continue;
    const severity = String(item.severity || 'info').toLowerCase();
    const prev = byLine[line];
    // 同一行只留最严重的一条：叠着画只会互相盖掉
    if (prev && severityRank(prev.severity) <= severityRank(severity)) continue;
    byLine[line] = { severity, text: diagText(item) };
  }
  entry.diagByLine = byLine;
  afterDiagnostics(entry);
}

function diagText(item) {
  const severity = String(item.severity || '').toLowerCase();
  const label = LSP_SEVERITY_LABEL[severity] || '诊断';
  const code = item.code ? ` [${item.code}]` : '';
  const source = item.source ? `（${item.source}）` : '';
  return `${label}${code}：${item.message || ''}${source}`;
}

/* 诊断到位后重画标记。编辑态只刷 gutter，不重建 textarea ——
   重建会丢光标、丢未保存内容。 */
function afterDiagnostics(entry) {
  if (S.activeEditor !== entry.id) return;
  if (entry.editing) { syncGutter(entry); setLspChip(entry); return; }
  if (entry.mode === 'preview' && isMarkdown(entry.path)) { setLspChip(entry); return; }
  renderFileEntry(entry);
}

/* ---- 光标 ↔ LSP 位置 ---- */

function isTextField(node) {
  if (!node || node.nodeType !== Node.ELEMENT_NODE) return false;
  const tag = node.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || node.isContentEditable;
}

/* 光标在 textarea 中的 (行 1 基, 列 0 基)。列按 UTF-16 code unit 计，
   与 LSP 的 character 口径一致。 */
function caretPosition(area) {
  const before = area.value.slice(0, area.selectionStart);
  const lines = before.split('\n');
  return { line: lines.length, char: lines[lines.length - 1].length };
}

/* 把 (行 1 基, 列 0 基) 换算成 textarea 的字符偏移。 */
function offsetOf(text, line, char) {
  const lines = text.split('\n');
  const index = Math.max(0, Math.min(Number(line) - 1, lines.length - 1));
  let offset = 0;
  for (let i = 0; i < index; i += 1) offset += lines[i].length + 1;
  return offset + Math.max(0, Math.min(Number(char) || 0, lines[index].length));
}

function flashLine(node) {
  if (!node) return;
  node.scrollIntoView({ block: 'center' });
  node.classList.add('is-flash');
  setTimeout(() => node.classList.remove('is-flash'), 900);
}

/* 把光标放到指定位置并滚到可见处（只读视图下改为高亮那一行）。 */
function placeCaret(entry, line, char) {
  if (entry.editing && entry.area) {
    const offset = offsetOf(entry.area.value, line, char);
    entry.area.focus();
    entry.area.setSelectionRange(offset, offset);
    if (entry.gutter) flashLine(entry.gutter.querySelectorAll('.edit-ln')[Number(line) - 1]);
    return;
  }
  flashLine(entry.node.querySelector(`.ln[data-line="${Number(line)}"]`));
}

/* 从点击坐标反推只读视图里的 (行 1 基, 列 0 基)。
   每行是「span.ln + 文本节点」交替，所以从命中节点往前找最近的 .ln 即可。 */
function rangeAtPoint(root, x, y) {
  const doc = root.ownerDocument;
  if (doc.caretRangeFromPoint) return doc.caretRangeFromPoint(x, y);
  if (doc.caretPositionFromPoint) {
    const spot = doc.caretPositionFromPoint(x, y);
    if (!spot) return null;
    const range = doc.createRange();
    range.setStart(spot.offsetNode, spot.offset);
    return range;
  }
  return null;
}

function positionAtPoint(root, x, y) {
  const range = rangeAtPoint(root, x, y);
  if (!range || !root.contains(range.startContainer)) return null;
  let node = range.startContainer;
  let char = range.startOffset;
  if (node.nodeType === Node.ELEMENT_NODE) {
    node = node.childNodes[char] || node.lastChild;
    char = 0;
  }
  while (node && !(node.nodeType === Node.ELEMENT_NODE && node.classList.contains('ln'))) {
    node = node.previousSibling;
  }
  if (!node || !node.dataset.line) return null;
  return { line: Number(node.dataset.line), char };
}

/* 只读视图的点按：记住位置（F12 用），带 Ctrl 直接跳定义。 */
function bindViewPointing(entry, pre) {
  pre.addEventListener('click', (event) => {
    const pos = positionAtPoint(pre, event.clientX, event.clientY);
    if (!pos) return;
    entry.lastPos = pos;
    if (event.ctrlKey || event.metaKey) {
      event.preventDefault();
      gotoDefinition(entry, pos);
    }
  });
}

/* F12 / Ctrl+Click：跳转定义。跨文件就开新标签，同文件就移动光标。 */
async function gotoDefinition(entry, pos) {
  if (!entry || entry.kind !== 'file') return;
  if (!lspSupported(entry.path)) { toast('该文件类型不走语言服务器', 'error'); return; }
  let spot = pos;
  if (!spot) {
    if (entry.editing && entry.area) spot = caretPosition(entry.area);
    else spot = entry.lastPos;
  }
  if (!spot) { toast('把光标放到符号上（编辑模式）或在只读视图里点一下那一行', 'error'); return; }
  let data;
  try {
    data = await api.get('/api/lsp/definition', {
      path: entry.path, line: spot.line, char: spot.char,
    });
  } catch (err) {
    toast(err.message, 'error');
    return;
  }
  if (data.available === false) { toast(data.reason || 'LSP 不可用', 'error'); return; }
  const locations = data.locations || [];
  if (!locations.length) { toast('没找到定义', ''); return; }
  const first = locations.find((item) => !item.outside) || locations[0];
  if (first.outside) { toast('定义在工作区之外，不打开', ''); return; }
  if (first.path === entry.path) { placeCaret(entry, first.line, first.char); return; }
  await openFile(first.path);
  const target = S.editors.get(`file:${first.path}`);
  if (target) placeCaret(target, first.line, first.char);
}

/* ---- 重命名（F2） ---- */

function renameSpot(entry) {
  if (entry.editing && entry.area) return caretPosition(entry.area);
  return entry.lastPos || null;
}

function openRenameBar(entry) {
  if (!entry || entry.kind !== 'file') return;
  if (!lspSupported(entry.path)) { toast('该文件类型不走语言服务器', 'error'); return; }
  const spot = renameSpot(entry);
  if (!spot) { toast('先把光标放到要重命名的符号上，或在只读视图里点一下那一行', 'error'); return; }
  entry.renamePos = spot;
  entry.renameToken = '';
  entry.renameFiles = [];
  $('rename-target').textContent = `${baseName(entry.path)} : 第 ${spot.line} 行`;
  $('rename-input').value = '';
  $('rename-list').replaceChildren(el('div', 'empty', '输入新名称，然后点「生成预览」。'));
  $('rename-hint').textContent = '';
  $('btn-rename-apply').disabled = true;
  $('btn-rename-apply').textContent = '应用重命名';
  $('rename-bar').hidden = false;
  $('rename-input').focus();
}

async function runRenamePreview() {
  const entry = S.editors.get(S.activeEditor);
  if (!entry || !entry.renamePos) return;
  const newName = $('rename-input').value.trim();
  if (!newName) { toast('先输入新名称', 'error'); return; }
  const btn = $('btn-rename-run');
  btn.disabled = true;
  $('rename-hint').textContent = '生成中…';
  try {
    const data = await api.post('/api/lsp/rename/preview', {
      path: entry.path,
      line: entry.renamePos.line,
      char: entry.renamePos.char,
      newName,
    });
    if (data.available === false) {
      $('rename-list').replaceChildren(el('div', 'empty', data.reason || 'LSP 不可用'));
      $('rename-hint').textContent = '';
      return;
    }
    entry.renameToken = data.token || '';
    entry.renameFiles = data.files || [];
    if (data.noChange || !entry.renameFiles.length) {
      $('rename-list').replaceChildren(el('div', 'empty', '语言服务器认为没有需要改的地方。'));
      $('rename-hint').textContent = '';
      return;
    }
    renderRenameFiles(entry);
    $('rename-hint').textContent =
      `共 ${data.edits} 处改动，涉及 ${entry.renameFiles.length} 个文件`;
  } catch (err) {
    $('rename-hint').textContent = '';
    toast(err.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

function renderRenameFiles(entry) {
  const host = $('rename-list');
  host.replaceChildren();
  for (const file of entry.renameFiles) {
    const card = el('div', 'hunk-card');
    const head = el('div', 'hunk-head');
    head.appendChild(el('span', 'hunk-title', file.path));
    head.appendChild(el('span', 'chip', `+${file.added} / -${file.removed}`));
    card.appendChild(head);
    card.appendChild(renderDiff(file.diff) || el('pre', 'hunk-diff', file.diff));
    host.appendChild(card);
  }
  const btn = $('btn-rename-apply');
  btn.disabled = false;
  btn.textContent = `应用重命名（${entry.renameFiles.length} 个文件）`;
}

async function applyRename() {
  const entry = S.editors.get(S.activeEditor);
  if (!entry || !entry.renameToken) return;
  const btn = $('btn-rename-apply');
  btn.disabled = true;
  try {
    const data = await api.post('/api/lsp/rename/apply', { token: entry.renameToken });
    const paths = data.paths || [];
    closeRenameBar();
    if (!data.changed) { toast(data.notice || '没有应用任何改动', ''); return; }
    toast(data.notice || `已重命名 ${paths.length} 个文件`, 'ok');
    for (const path of paths) markChangedFile(path, false);
    refreshChanges();
    // 被改到的、已打开的标签重读一遍：磁盘已是新内容，界面要跟上
    for (const path of paths) {
      const target = S.editors.get(`file:${path}`);
      if (!target) continue;
      resetDiagnostics(target);
      await reloadEntry(target);
    }
    activateEditor(S.activeEditor);
  } catch (err) {
    toast(err.message, 'error');
    btn.disabled = false;
  }
}

function closeRenameBar() {
  $('rename-bar').hidden = true;
  for (const entry of S.editors.values()) {
    entry.renameToken = '';
    entry.renameFiles = [];
  }
  $('rename-list').replaceChildren();
  $('rename-hint').textContent = '';
}

/* ═══════════════════════════ 编辑面（D1/D2/D9/D11） ═══════════════════════════
   只读仍是默认；点「编辑」才把代码视图换成 textarea。三件事在这一层闭环：

   - 补全（D5/D6）：输入停顿后拿光标前的 prefix 与光标后的 suffix 调
     /api/complete，结果以 ghost text 贴在光标处，Tab 接受、Esc 取消。
     补全**不走审批**（它只读文件、不改盘），但会记一条审计。
   - 改写（D8）：Ctrl+K 在选区上浮出改写条，模型给的新文本被切成 hunk，
     逐块勾选后调 /api/rewrite/apply —— 服务端只落勾中的块。
   - 保存（D11）：Ctrl+S 调 /api/edit/save，带 expectedDigest 做乐观锁，
     磁盘文件被别人改过就拒收（409），绝不用旧内容覆盖新内容。
   ========================================================================== */

const COMPLETION_DELAY = 400;   // 打字停顿多久才请求补全，避免逐字符打满模型
// 前端截断窗口兜底值。大文件不能整份上送，否则 token 不可控、首字变慢；
// 后端还有一道权威截断，这里先裁一遍能省掉上行带宽与序列化开销。
const DEFAULT_PREFIX_CHARS = 4000;
const DEFAULT_SUFFIX_CHARS = 2000;

/* 光标前后上下文的行对齐截断 —— 与后端 window_completion_context 同一算法：
   prefix 从窗口内第一个换行之后开始（丢弃被切断的首行残段），suffix 只保留
   窗口内最后一个完整行（丢弃尾部残行）。limit <= 0 表示不截断。 */
function trimPrefixWindow(text, limit) {
  if (!limit || limit <= 0 || text.length <= limit) return text;
  const start = text.length - limit;
  const newline = text.indexOf('\n', start);
  return newline === -1 ? text.slice(start) : text.slice(newline + 1);
}

function trimSuffixWindow(text, limit) {
  if (!limit || limit <= 0 || text.length <= limit) return text;
  const newline = text.lastIndexOf('\n', limit);
  return newline === -1 ? text.slice(0, limit) : text.slice(0, newline + 1);
}

/** 取当前会话的补全窗口设置（拿不到就退回默认值）。 */
function completionWindow(name, fallback) {
  const cfg = (S.session && S.session.config) || {};
  const value = cfg[name];
  return Number.isFinite(value) ? value : fallback;
}

/* 建编辑视图：gutter（行号）+ textarea + ghost 层。
   textarea 用 white-space:pre（不折行）——折行会让行号 gutter 与视觉行错位，
   而 gutter 是靠 scrollTop 同步的，只有「一逻辑行 = 一视觉行」才对得齐。 */
function buildEditView(entry) {
  const wrap = el('div', 'edit-wrap');
  const gutter = el('div', 'edit-gutter');
  const surface = el('div', 'edit-surface');
  const area = document.createElement('textarea');
  area.className = 'code-edit';
  area.spellcheck = false;
  area.wrap = 'off';
  area.value = normalizeNewlines(entry.data.text);
  const ghost = el('div', 'ghost');
  ghost.hidden = true;
  surface.appendChild(area);
  surface.appendChild(ghost);
  wrap.appendChild(gutter);
  wrap.appendChild(surface);

  entry.area = area;
  entry.gutter = gutter;
  entry.ghost = ghost;
  entry.ghostText = '';
  entry.completionSeq = 0;
  if (entry.savedText == null) entry.savedText = area.value;

  area.addEventListener('input', () => {
    entry.dirty = area.value !== entry.savedText;
    cancelGhost(entry);
    syncGutter(entry);
    updateSaveState(entry);
    scheduleCompletion(entry);
  });
  area.addEventListener('scroll', () => {
    gutter.scrollTop = area.scrollTop;
    cancelGhost(entry);   // 滚动后 ghost 的坐标失效，先收起来，下次输入再算
  });
  area.addEventListener('keydown', (event) => onEditKeydown(event, entry));
  // Ctrl+Click 跳定义（E7）：与只读视图同一入口，位置取当前光标
  area.addEventListener('click', (event) => {
    if (event.ctrlKey || event.metaKey) gotoDefinition(entry);
  });
  area.addEventListener('blur', () => cancelGhost(entry));

  syncGutter(entry);
  requestAnimationFrame(() => { if (entry.editing && entry.area === area) area.focus(); });
  return wrap;
}

function syncGutter(entry) {
  const total = entry.area.value.split('\n').length;
  const frag = document.createDocumentFragment();
  for (let i = 1; i <= total; i += 1) {
    const node = el('div', 'edit-ln', String(i));
    applyDiagMark(node, entry, i);   // 诊断要显示在 gutter 上（E7）
    frag.appendChild(node);
  }
  entry.gutter.replaceChildren(frag);
  entry.gutter.scrollTop = entry.area.scrollTop;
}

/* 量出光标在 textarea 里的像素位置。
   做法是把「光标前的文本」塞进一个与 textarea 同字体同内边距的隐藏 mirror，
   在末尾放一个零宽标记，读它的 offsetTop/offsetLeft。textarea 自己不给
   光标坐标，这是唯一不引入第三方库的可靠办法；量不出来就返回 null，
   调用方退回「贴角浮条」，功能不受影响。 */
function caretMirror(area, index) {
  const style = getComputedStyle(area);
  const mirror = el('div', 'caret-mirror');
  for (const prop of [
    'fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'letterSpacing',
    'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
    'borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth',
    'boxSizing', 'whiteSpace', 'wordBreak', 'overflowWrap', 'tabSize',
  ]) mirror.style[prop] = style[prop];
  mirror.style.width = `${area.offsetWidth}px`;
  mirror.appendChild(document.createTextNode(area.value.slice(0, index)));
  const marker = document.createElement('span');
  marker.textContent = '\u200b';
  mirror.appendChild(marker);
  area.parentNode.appendChild(mirror);
  const top = marker.offsetTop - area.scrollTop;
  const left = marker.offsetLeft - area.scrollLeft;
  const lineHeight = marker.offsetHeight || parseFloat(style.lineHeight) || 16;
  mirror.remove();
  if (!Number.isFinite(top) || !Number.isFinite(left)) return null;
  return { top, left, lineHeight };
}

function scheduleCompletion(entry) {
  clearTimeout(entry.completionTimer);
  entry.completionTimer = setTimeout(() => requestCompletion(entry), COMPLETION_DELAY);
}

async function requestCompletion(entry) {
  if (!entry.editing || !entry.area) return;
  const area = entry.area;
  const index = area.selectionStart;
  if (index !== area.selectionEnd) return;      // 有选区时是在选代码，不补全
  const value = area.value;
  const prefix = trimPrefixWindow(value.slice(0, index), completionWindow('completionPrefixChars', DEFAULT_PREFIX_CHARS));
  const suffix = trimSuffixWindow(value.slice(index), completionWindow('completionSuffixChars', DEFAULT_SUFFIX_CHARS));
  if (!prefix.trim()) return;                    // 空文件/行首空白没什么可接的
  entry.completionSeq += 1;
  const seq = entry.completionSeq;
  try {
    const data = await api.post('/api/complete', { path: entry.path, prefix, suffix });
    // 期间又打了一个字（seq 变了）就别再贴旧结果，否则 ghost 位置会错
    if (seq !== entry.completionSeq || !entry.editing || entry.area !== area) return;
    const text = String(data.text || '');
    if (!text.trim()) { cancelGhost(entry); return; }
    showGhost(entry, text, String(data.backend || ''));
  } catch {
    // 补全失败必须静默：弹 toast 会让人以为整个编辑器坏了
  }
}

function showGhost(entry, text, backend) {
  const spot = caretMirror(entry.area, entry.area.selectionStart);
  const ghost = entry.ghost;
  ghost.textContent = text;
  ghost.title = backend === 'chat' ? '补全（chat 回退）' : '补全';
  if (spot) {
    ghost.classList.remove('is-floating');
    ghost.style.left = `${spot.left}px`;
    ghost.style.top = `${spot.top}px`;
    ghost.style.lineHeight = `${spot.lineHeight}px`;
  } else {
    ghost.classList.add('is-floating');   // 量不出光标就退回右下角浮条
    ghost.style.left = '';
    ghost.style.top = '';
    ghost.style.lineHeight = '';
  }
  ghost.hidden = false;
  entry.ghostText = text;
}

function cancelGhost(entry) {
  if (!entry) return;
  entry.completionSeq += 1;   // 作废在途请求
  clearTimeout(entry.completionTimer);
  entry.ghostText = '';
  if (entry.ghost) entry.ghost.hidden = true;
}

function acceptGhost(entry) {
  if (!entry.ghost || entry.ghost.hidden || !entry.ghostText) return false;
  const area = entry.area;
  const index = area.selectionStart;
  const caret = index + entry.ghostText.length;
  area.value = area.value.slice(0, index) + entry.ghostText + area.value.slice(index);
  area.setSelectionRange(caret, caret);
  entry.dirty = area.value !== entry.savedText;
  const text = entry.ghostText;
  cancelGhost(entry);
  syncGutter(entry);
  updateSaveState(entry);
  return !!text;
}

function updateSaveState(entry) {
  entry.tab.classList.toggle('is-dirty', !!entry.dirty);
  if (entry.id === S.activeEditor) {
    const btn = $('btn-save');
    if (btn) btn.disabled = !entry.dirty;
  }
}

function onEditKeydown(event, entry) {
  if (event.key === 'Tab') {
    // Tab 一律吞掉：有 ghost 就接受，没有就当场求一次补全。
    // 不吞的话焦点会跳走，编辑模式就变成「打两个字焦点没了」。
    event.preventDefault();
    if (!entry.ghost.hidden && entry.ghostText) acceptGhost(entry);
    else requestCompletion(entry);
    return;
  }
  // 跳定义 / 重命名（E7）。全局 keydown 只管只读视图，两边条件互斥不重复触发。
  if (event.key === 'F12') {
    event.preventDefault();
    gotoDefinition(entry);
    return;
  }
  if (event.key === 'F2') {
    event.preventDefault();
    openRenameBar(entry);
    return;
  }
  if (event.key === 'Escape') {
    if (!entry.ghost.hidden) {
      event.preventDefault();
      event.stopPropagation();   // 别让全局 Esc 顺手拒掉正在等的审批
      cancelGhost(entry);
      return;
    }
    if (!$('rename-bar').hidden) {
      event.preventDefault();
      event.stopPropagation();
      closeRenameBar();
      return;
    }
    if (!$('rewrite-bar').hidden) {
      event.preventDefault();
      event.stopPropagation();
      closeRewriteBar();
    }
  }
}

function toggleEditMode() {
  const entry = S.editors.get(S.activeEditor);
  if (!entry || entry.kind !== 'file') return;
  if (entry.editing) {
    if (entry.dirty && !confirm('有未保存的改动，切回只读会丢掉它们。继续？')) return;
    entry.editing = false;
    cancelGhost(entry);
    closeRewriteBar();
    closeRenameBar();
    entry.area = null;
    entry.gutter = null;
    entry.ghost = null;
    entry.dirty = false;
    entry.tab.classList.remove('is-dirty');
  } else {
    entry.editing = true;
    entry.mode = 'code';   // 编辑原文，Markdown 预览让位（预览视图里改不了源）
  }
  renderFileEntry(entry);
}

/* ---- 逐 hunk 改写（D9） ---- */

/* 选区 → 1 基行号区间。选区结束恰好落在行首时不算下一行 —— 否则
   「选中第 3 行整行」会变成改写 3-4 行，把下面那行也吃掉。 */
function lineRangeOf(text, start, end) {
  if (end <= start) return null;
  const startLine = text.slice(0, start).split('\n').length;
  let endLine = text.slice(0, end).split('\n').length;
  if (text[end - 1] === '\n') endLine -= 1;
  return { startLine, endLine: Math.max(startLine, endLine) };
}

function openRewriteBar(entry) {
  if (!entry || !entry.editing || !entry.area) return;
  const area = entry.area;
  const range = lineRangeOf(area.value, area.selectionStart, area.selectionEnd);
  if (!range) { toast('先在编辑区选中要改写的代码', 'error'); return; }
  cancelGhost(entry);
  entry.rewriteRange = range;
  entry.rewriteToken = '';
  entry.hunks = [];
  entry.accepted = new Set();
  $('rewrite-range').textContent = `第 ${range.startLine}-${range.endLine} 行`;
  $('rewrite-input').value = '';
  $('hunk-list').replaceChildren(el('div', 'empty', '写一句要怎么改，然后点「生成」。'));
  $('rewrite-hint').textContent = '';
  $('btn-rewrite-apply').disabled = true;
  $('btn-rewrite-apply').textContent = '应用选中';
  $('rewrite-bar').hidden = false;
  $('rewrite-input').focus();
}

async function runRewrite() {
  const entry = S.editors.get(S.activeEditor);
  if (!entry || !entry.editing || !entry.rewriteRange) return;
  if (S.running) { toast('Agent 正在跑任务，等这轮结束再改写', 'error'); return; }
  const instruction = $('rewrite-input').value.trim();
  if (!instruction) { toast('先写一句改写指令', 'error'); return; }
  const btn = $('btn-rewrite-run');
  btn.disabled = true;
  $('rewrite-hint').textContent = '生成中…';
  try {
    const data = await api.post('/api/rewrite', {
      path: entry.path,
      startLine: entry.rewriteRange.startLine,
      endLine: entry.rewriteRange.endLine,
      instruction,
    });
    entry.rewriteToken = data.token || '';
    entry.hunks = data.hunks || [];
    if (data.noChange || !entry.hunks.length) {
      $('hunk-list').replaceChildren(el('div', 'empty', '模型认为这段不用改。'));
      $('rewrite-hint').textContent = '';
      return;
    }
    renderHunks(entry);
    $('rewrite-hint').textContent = `共 ${entry.hunks.length} 处改动，取消勾选可跳过某几块`;
  } catch (err) {
    $('rewrite-hint').textContent = '';
    toast(err.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

function renderHunks(entry) {
  const host = $('hunk-list');
  host.replaceChildren();
  entry.accepted = new Set(entry.hunks.map((h) => h.index));
  for (const hunk of entry.hunks) {
    const card = el('div', 'hunk-card');
    const head = el('label', 'hunk-head');
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = true;
    box.addEventListener('change', () => {
      if (box.checked) entry.accepted.add(hunk.index);
      else entry.accepted.delete(hunk.index);
      card.classList.toggle('is-off', !box.checked);
      updateApplyState(entry);
    });
    head.appendChild(box);
    head.appendChild(el('span', 'hunk-title', hunk.header));
    head.appendChild(el('span', 'chip', `+${hunk.added} / -${hunk.removed}`));
    card.appendChild(head);
    card.appendChild(renderDiff(hunk.diff) || el('pre', 'hunk-diff', hunk.diff));
    host.appendChild(card);
  }
  updateApplyState(entry);
}

function updateApplyState(entry) {
  const n = entry.accepted ? entry.accepted.size : 0;
  const btn = $('btn-rewrite-apply');
  btn.disabled = n === 0;
  btn.textContent = n ? `应用选中（${n}）` : '应用选中';
}

async function applyRewrite() {
  const entry = S.editors.get(S.activeEditor);
  if (!entry || !entry.rewriteToken) return;
  const accepted = [...(entry.accepted || [])];
  if (!accepted.length) return;
  const btn = $('btn-rewrite-apply');
  btn.disabled = true;
  try {
    const data = await api.post('/api/rewrite/apply', { token: entry.rewriteToken, accepted });
    closeRewriteBar();
    if (!data.changed) { toast('没有应用任何改动', ''); return; }
    toast(`已应用 ${accepted.length} 处改动`, 'ok');
    await reloadEntry(entry);
    markChangedFile(entry.path, false);
    refreshChanges();
  } catch (err) {
    // 409：磁盘上的这个文件已经不是你看到的那份了（多半是 Agent 刚改过）
    toast(err.message, 'error');
    btn.disabled = false;
  }
}

function closeRewriteBar() {
  $('rewrite-bar').hidden = true;
  for (const entry of S.editors.values()) {
    entry.rewriteToken = '';
    entry.hunks = [];
    entry.accepted = new Set();
    entry.rewriteRange = null;
  }
  $('hunk-list').replaceChildren();
  $('rewrite-hint').textContent = '';
}

/* 落盘后重读文件：磁盘是唯一真相，界面跟着它走（顺便刷新 digest）。 */
async function reloadEntry(entry) {
  try {
    const data = await api.get('/api/file', { path: entry.path });
    entry.data = data;
    entry.digest = data.digest || '';
    entry.savedText = normalizeNewlines(data.text);
    entry.dirty = false;
    resetDiagnostics(entry);   // 正文变了，诊断要重新取（E7）
    renderFileEntry(entry);
  } catch { /* 读不到就保持旧内容，不改动界面 */ }
}

async function saveActiveFile() {
  const entry = S.editors.get(S.activeEditor);
  if (!entry || !entry.editing || !entry.area) return;
  if (S.running) { toast('Agent 正在跑任务，等这轮结束再保存', 'error'); return; }
  const content = normalizeNewlines(entry.area.value);
  const btn = $('btn-save');
  btn.disabled = true;
  try {
    const data = await api.post('/api/edit/save', {
      path: entry.path,
      content,
      expectedDigest: entry.digest || '',
    });
    entry.data = { ...(entry.data || {}), text: content, lines: content.split('\n').length };
    entry.digest = data.digest || '';
    entry.savedText = content;
    entry.dirty = false;
    updateSaveState(entry);
    resetDiagnostics(entry);          // 存完立刻重算诊断：刚改的错有没有消掉（E7）
    maybeRefreshDiagnostics(entry);
    if (data.changed) {
      toast('已保存', 'ok');
      markChangedFile(entry.path, false);
      refreshChanges();
    } else {
      toast('内容没有变化', '');
    }
  } catch (err) {
    toast(err.message, 'error');
    btn.disabled = false;
  }
}

function openDiffTab(change) {
  const id = `diff:${change.path}`;
  if (S.editors.has(id)) { activateEditor(id); return; }
  const host = el('div');
  const head = el('div', 'change-meta');
  head.style.padding = '10px 14px 0';
  head.appendChild(el('strong', null, change.path));
  head.appendChild(el('span', 'tag ' + (change.created ? 'create' : 'edit'), change.created ? '新建' : '修改'));
  // 增删行数直接用后端算好的（/api/diffs 每条都带 added/removed）。
  // 之前这里把 diff 原文传给 diffStat()，而它要的是 parseUnifiedDiff 的产物
  // —— 会抛 "parsed.files is not iterable"，整个 diff 标签打不开（存量 bug，一并修掉）。
  if (change.added || change.removed) {
    head.appendChild(el('span', 'chip', `+${change.added || 0} / -${change.removed || 0}`));
  }
  host.appendChild(head);
  const wrap = el('div');
  wrap.style.padding = '8px 14px 60px';
  const diff = renderDiff(change.diff);
  if (diff) wrap.appendChild(diff);
  else wrap.appendChild(el('div', 'empty', '（diff 为空或无法解析）'));
  host.appendChild(wrap);
  openEditor(id, '±', baseName(change.path), host, { kind: 'diff', path: change.path, created: change.created });
}

/* ═══════════════════════════════ 活动栏 / 侧栏 ═══════════════════════════════ */
const PANEL_TITLES = {
  explorer: '资源管理器',
  changes: '改动',
  tools: '工具箱',
  audit: '审计',
  history: '历史会话',
  git: 'Git',
  tests: '测试',
  memory: '记忆',
  mcp: 'MCP',
  welcome: '开始',
};

function switchPanel(name) {
  S.activePanel = name;
  document.querySelectorAll('.act').forEach((a) => a.classList.toggle('is-active', a.dataset.panel === name));
  $('sb-title').textContent = PANEL_TITLES[name] || name;
  // welcome 指向欢迎标签而不是侧栏内容
  const showSidebar = name !== 'welcome';
  $('sidebar').style.display = showSidebar ? '' : 'none';
  $('sb-resizer').style.display = showSidebar ? '' : 'none';
  document.querySelectorAll('.sb-body').forEach((b) => { b.hidden = b.dataset.body !== name; });
  if (name === 'changes') refreshChanges();
  if (name === 'git') refreshGit();
  if (name === 'audit') refreshAudit();
  if (name === 'history') loadHistory();
  if (name === 'tests') refreshTestsTree();
  if (name === 'memory') refreshMemories();
  if (name === 'mcp') refreshMcpPanel();
  if (name === 'welcome') activateEditor('welcome');
}

function updateChangesBadge() {
  const n = S.changed.size;
  const badge = $('act-badge-changes');
  badge.hidden = n === 0;
  badge.textContent = n;
  const badge2 = $('badge-changes');
  badge2.textContent = n;
  badge2.classList.toggle('has', n > 0);
  updateStatusBarFiles();
}

function markChangedFile(path, created) {
  S.changed.add(path);
  if (created) S.created.add(path);
  const node = S.treeNodes.get(path);
  if (node) {
    node.classList.add('is-changed');
    if (created) node.classList.add('is-created');
  }
  updateChangesBadge();
  // 已打开的文件标签标记为「已修改」
  const id = `file:${path}`;
  const entry = S.editors.get(id);
  if (entry && !entry.tab.querySelector('.ed-dot')) {
    entry.tab.appendChild(el('span', 'ed-dot'));
  }
}

/* ------------------------------------------------------------------ 文件树 */
function renderTree(payload) {
  const host = $('tree');
  host.replaceChildren();
  S.treeNodes.clear();

  const build = (nodes) => {
    const ul = document.createElement('ul');
    for (const node of nodes) {
      const li = document.createElement('li');
      const row = el('div', 'tree-node');
      row.dataset.path = node.path;
      if (node.type === 'dir') row.classList.add('is-dir');
      if (node.sensitive) row.classList.add('is-sensitive');
      if (S.changed.has(node.path)) row.classList.add('is-changed');
      if (S.created.has(node.path)) row.classList.add('is-created');

      const twisty = el('span', 'twisty', node.type === 'dir' ? '▾' : '');
      row.appendChild(twisty);
      row.appendChild(el('span', 'name', node.name));
      if (node.type === 'file') row.appendChild(el('span', 'size', fmtSize(node.size)));

      row.addEventListener('click', () => {
        if (node.type === 'dir') {
          const child = li.querySelector('ul');
          if (child) {
            child.hidden = !child.hidden;
            twisty.textContent = child.hidden ? '▸' : '▾';
          }
          return;
        }
        openFile(node.path, row);
      });

      li.appendChild(row);
      S.treeNodes.set(node.path, row);

      if (node.children?.length) li.appendChild(build(node.children));
      ul.appendChild(li);
    }
    return ul;
  };

  if (!payload.nodes?.length) {
    host.appendChild(el('div', 'empty', '工作区是空的。'));
    return;
  }
  host.appendChild(build(payload.nodes));
  if (payload.truncated) {
    host.appendChild(el('div', 'empty', '文件过多，仅展示一部分。'));
  }
}

async function refreshTree() {
  try {
    renderTree(await api.get('/api/tree'));
  } catch (err) {
    $('tree').replaceChildren(el('div', 'empty', `读取文件树失败：${err.message}`));
  }
}

/* ------------------------------------------------------------------ 改动面板 */
/* 两种口径（P0-C）：
 *   session —— Agent 的账本（/api/diffs），可回滚、可审查；
 *   git     —— 相对 HEAD 的全部改动，含用户自己在编辑器/终端里的改动与未跟踪文件。
 * 两边共用 S.git（最近一次 /api/git/status 的结果），避免同一份状态取两遍。 */
function setChangesMode(mode) {
  if (mode === S.changesMode) return;
  S.changesMode = mode;
  document.querySelectorAll('#changes-mode .mode-btn').forEach((b) => {
    b.classList.toggle('is-active', b.dataset.cmode === mode);
  });
  $('changes-caption').textContent = mode === 'git' ? '相对 git 的改动' : '本次会话的改动';
  refreshChanges();
}

async function refreshChanges() {
  if (S.changesMode === 'git') {
    const host = $('changes-list');
    try {
      const data = await api.get('/api/git/status');
      S.git = data;
      renderGitEntries(host, data);
      const n = (data.entries || []).length;
      const badge = $('badge-changes');
      badge.textContent = n;
      badge.classList.toggle('has', n > 0);
    } catch (err) {
      host.replaceChildren(el('div', 'empty', `读取 git 状态失败：${err.message}`));
    }
    return;
  }
  try {
    const data = await api.get('/api/diffs');
    S.diffs = data.changes || [];
    S.changed = new Set(S.diffs.map((c) => c.path));
    S.created = new Set(S.diffs.filter((c) => c.created).map((c) => c.path));
    updateChangesBadge();

    const host = $('changes-list');
    host.replaceChildren();
    if (!S.diffs.length) {
      host.appendChild(el('div', 'empty', '本次会话还没有产生改动。'));
      return;
    }
    for (const change of S.diffs) {
      const item = el('button', 'history-item');
      item.type = 'button';
      const meta = el('span', 'change-meta');
      meta.appendChild(el('strong', null, baseName(change.path)));
      meta.appendChild(el('span', 'tag ' + (change.created ? 'create' : 'edit'), change.created ? '新建' : '修改'));
      meta.appendChild(el('span', 'history-time', change.path));
      item.appendChild(meta);
      item.addEventListener('click', () => openDiffTab(change));
      host.appendChild(item);
    }

    // 同步刷新树上的高亮
    for (const [path, node] of S.treeNodes) {
      node.classList.toggle('is-changed', S.changed.has(path));
      node.classList.toggle('is-created', S.created.has(path));
    }
  } catch (err) {
    toast(`读取改动失败：${err.message}`, 'error');
  }
}

/* ------------------------------------------------------------------ Git 面板（P0-C） */
const GIT_STATUS_LABEL = { M: '修改', A: '新增', D: '删除', R: '改名', C: '复制', '??': '未跟踪' };

async function refreshGit() {
  try {
    const data = await api.get('/api/git/status');
    S.git = data;
    renderGitPanel(data);
  } catch (err) {
    $('git-head').replaceChildren(el('div', 'empty', `读取 git 状态失败：${err.message}`));
  }
}

function renderGitPanel(data) {
  const head = $('git-head');
  const btn = $('btn-commit-msg');
  if (!data || !data.available) {
    btn.disabled = true;
    $('git-msg').hidden = true;
    head.replaceChildren(el('div', 'empty', (data && data.reason) || '当前工作区不是 git 仓库。'));
    $('git-list').replaceChildren(el('div', 'empty', '初始化 git 仓库后可在这里查看改动、起草提交信息。'));
    $('badge-git').textContent = '0';
    $('badge-git').classList.remove('has');
    return;
  }
  btn.disabled = false;
  const branch = el('span', 'git-branch',
    data.detached ? `@ ${data.branch || 'HEAD'}（游离头）` : (data.branch || '（未知分支）'));
  head.replaceChildren(branch);
  const sub = el('span', 'git-sub');
  if (data.upstream) {
    const ahead = data.ahead || 0;
    const behind = data.behind || 0;
    sub.textContent = `${data.upstream}${ahead || behind ? `（↑${ahead} ↓${behind}）` : ''}`;
  } else {
    sub.textContent = data.unborn ? '还没有任何提交' : '无上游分支';
  }
  head.appendChild(sub);
  // conflicts 是冲突文件路径数组（计数在 counts.conflicts）——
  // 空数组也是真值，必须看长度，否则无冲突的仓库会渲染出「⚠ 个文件…」
  const conflictPaths = Array.isArray(data.conflicts) ? data.conflicts : [];
  if (conflictPaths.length) {
    head.appendChild(el('span', 'git-sub git-conflict', `⚠ ${conflictPaths.length} 个文件处于冲突状态，需先解决`));
  }
  renderGitEntries($('git-list'), data);
  const n = (data.entries || []).length;
  const badge = $('badge-git');
  badge.textContent = n;
  badge.classList.toggle('has', n > 0);
}

/** git 条目列表 —— Git 面板与「改动·相对 Git」视图共用同一份渲染。 */
function renderGitEntries(host, data) {
  host.replaceChildren();
  if (!data || !data.available) {
    host.appendChild(el('div', 'empty', (data && data.reason) || '当前工作区不是 git 仓库。'));
    return;
  }
  const entries = data.entries || [];
  if (!entries.length) {
    host.appendChild(el('div', 'empty', '工作区干净，没有相对 HEAD 的改动。'));
    return;
  }
  for (const entry of entries) {
    const item = el('button', 'history-item' + (entry.conflicted ? ' is-conflict' : ''));
    item.type = 'button';
    item.title = entry.conflicted
      ? '该文件处于冲突状态，需要先解决再提交'
      : `相对 HEAD 的${GIT_STATUS_LABEL[entry.status] || '改动'}，点击查看 diff`;
    const meta = el('span', 'change-meta');
    const code = el('span', 'git-code', entry.status);
    code.dataset.c = entry.status;
    meta.appendChild(code);
    meta.appendChild(el('span', 'git-file', entry.path));
    item.appendChild(meta);
    if (entry.orig) {
      item.appendChild(el('span', 'history-time', `由 ${entry.orig} 改名而来`));
    }
    item.addEventListener('click', () => {
      // 未跟踪文件没有 diff 可看 —— 直接看文件本身，别给用户一个空标签
      if (entry.status === '??') { openFile(entry.path); return; }
      openGitDiffTab(entry.path);
    });
    host.appendChild(item);
  }
}

async function openGitDiffTab(path) {
  const id = `gitdiff:${path}`;
  if (S.editors.has(id)) { activateEditor(id); return; }
  const host = el('div');
  const head = el('div', 'change-meta');
  head.style.padding = '10px 14px 0';
  head.appendChild(el('strong', null, path));
  head.appendChild(el('span', 'tag edit', 'git'));
  host.appendChild(head);
  const wrap = el('div');
  wrap.style.padding = '8px 14px 60px';
  wrap.appendChild(el('div', 'empty', '读取中…'));
  host.appendChild(wrap);
  openEditor(id, '±', baseName(path), host, { kind: 'diff', path });
  try {
    const data = await api.get('/api/git/diff', { path });
    // gitinfo 已统计好增删行数；别在这里重复解析 diff（diffStat 要的是解析后的对象）
    if (data.added || data.removed) {
      head.appendChild(el('span', 'chip', `+${data.added || 0} / -${data.removed || 0}`));
    }
    const diff = renderDiff(data.diff);
    wrap.replaceChildren();
    if (diff) wrap.appendChild(diff);
    else wrap.appendChild(el('div', 'empty', data.truncated ? '（diff 过大已截断）' : '（相对 HEAD 没有可显示的差异）'));
  } catch (err) {
    wrap.replaceChildren(el('div', 'empty', `无法读取 diff：${err.message}`));
  }
}

async function generateCommitMessage() {
  if (S.running) {
    toast('任务执行中，等它结束再生成提交信息', 'error');
    return;
  }
  const btn = $('btn-commit-msg');
  btn.disabled = true;
  btn.textContent = '生成中…';
  try {
    const res = await api.post('/api/git/commit-message');
    $('git-msg-text').textContent = res.message;
    $('git-msg').hidden = false;
    toast('已起草提交信息 —— 只生成文本，不会自动提交', 'ok');
  } catch (err) {
    toast(`生成提交信息失败：${err.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '生成提交信息';
  }
}

/** 兜底复制：剪贴板 API 不可用/挂起时（自动化浏览器、非安全上下文）走 execCommand。 */
function legacyCopyText(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  return ok;
}

async function copyCommitMessage() {
  const text = $('git-msg-text').textContent || '';
  if (!text) return;
  let ok = false;
  if (navigator.clipboard) {
    // 个别环境里 clipboard 权限不拒绝也不通过，promise 一直挂起 ——
    // 不设超时按钮就永远没有反馈，所以赛个短超时再走兜底。
    // writeText 还可能同步抛（自动化/受限上下文），必须整体兜住，否则没有 toast。
    try {
      ok = await Promise.race([
        navigator.clipboard.writeText(text).then(() => true).catch(() => false),
        new Promise((resolve) => setTimeout(() => resolve(false), 1500)),
      ]);
    } catch {
      ok = false;
    }
  }
  if (!ok) ok = legacyCopyText(text);
  if (ok) {
    toast('已复制到剪贴板', 'ok');
    return;
  }
  // 全选文本让用户自己 Ctrl+C
  const range = document.createRange();
  range.selectNodeContents($('git-msg-text'));
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  toast('已全选，按 Ctrl+C 复制');
}

/* ═══════════════════════════ 测试面板（K，P2-14） ═══════════════════════════
   跑测试是**用户本人**发起的动作（ADR-011）：命令来自用户配置
   （`VIBE_TEST_COMMAND` 或 VIBE.md 的 `test:` 行），界面只做三件事，边界清晰：

   1. 收集与执行都交给后端（`/api/tests`、`/api/tests/run`）——界面不自己起进程，
      也就不存在绕过沙箱的执行路径；
   2. 结果通过 SSE（`test.started` / `test.finished`）回推，界面不猜状态；
   3. 失败项点击 → 开编辑器并**定位到 failureLine**（后端从 traceback 解析出的
      断言位置），复用现有的打开文件 / 定位能力（openFile + placeCaret）。 */
const TEST_STATUS_ICON = { passed: '✔', failed: '✕', error: '✕', skipped: '−', running: '◌' };
const TEST_STATUS_LABEL = { passed: '通过', failed: '失败', error: '错误', skipped: '跳过', running: '运行中' };

function testResultOf(nodeId) {
  return S.testResults.get(nodeId) || null;
}

async function refreshTestsTree(force = false) {
  const host = $('tests-tree');
  if (host) host.replaceChildren(el('div', 'empty', '收集用例中…'));
  try {
    S.testTree = await api.get('/api/tests', force ? { refresh: '1' } : {});
    renderTests();
  } catch (err) {
    if (host) host.replaceChildren(el('div', 'empty', `收集用例失败：${err.message}`));
  }
}

/* 拉最近一次的结果（后端内存态）。用它而不是从 `test.finished` 的 failures 反推：
   失败事件只带失败项，跑「全部」时无法据此标出哪些是 passed / skipped。 */
async function loadTestResults() {
  try {
    const data = await api.get('/api/tests/last');
    const result = data && data.result;
    if (result && Array.isArray(result.results)) {
      S.testResults = new Map(result.results.map((r) => [r.nodeId, r]));
      S.testSummary = {
        passed: result.passed, failed: result.failed,
        skipped: result.skipped, ms: result.ms,
      };
      updateTestsBadge();
    }
  } catch { /* 读不到就保留旧结果，不打扰用户 */ }
}

/* 换工作区后测试状态整体重置：命令与用例集都变了，旧结果会指向已不存在的用例。
   （与「新会话」区分：后者工作区没变，后端结果仍有效，不该清。） */
function resetTestView() {
  S.testTree = null;
  S.testResults = new Map();
  S.testSummary = null;
  S.testsRunning = false;
  S.testRunningIds = new Set();
  const host = $('tests-tree');
  if (host) host.replaceChildren(el('div', 'empty', '加载中…'));
  updateTestsBadge();
}

function renderTestSummary() {
  const summary = $('tests-summary');
  summary.replaceChildren();
  if (S.testsRunning) {
    summary.appendChild(el('span', 'test-chip is-running', '运行中…'));
    return;
  }
  const s = S.testSummary;
  if (!s) { summary.appendChild(el('span', 'test-chip', '尚未运行')); return; }
  summary.appendChild(el('span', 'test-chip is-pass', `✔ ${s.passed}`));
  summary.appendChild(el('span', 'test-chip is-fail', `✕ ${s.failed}`));
  summary.appendChild(el('span', 'test-chip', `− ${s.skipped}`));
  summary.appendChild(el('span', 'test-chip', `${s.ms} ms`));
}

function renderTestRow(item) {
  const row = el('div', 'test-case');
  const result = testResultOf(item.nodeId);
  const status = S.testRunningIds.has(item.nodeId)
    ? 'running'
    : (result ? result.status : 'idle');
  const icon = el('span', 'test-status', TEST_STATUS_ICON[status] || '○');
  icon.dataset.s = status;
  const label = result ? TEST_STATUS_LABEL[result.status] : status === 'running' ? '运行中' : '未运行';
  icon.title = label;
  row.appendChild(icon);
  row.appendChild(el('span', 'tname', item.name || item.nodeId));

  const runBtn = el('button', 'test-run', '▶');
  runBtn.type = 'button';
  runBtn.title = '跑这一个';
  runBtn.addEventListener('click', (event) => {
    event.stopPropagation();   // 别让它顺带触发「打开文件」
    runTests([item.nodeId]);
  });
  row.appendChild(runBtn);

  // 悬停提示：失败给一行断言信息，其余给状态；点击定位到失败行（没有则只开文件）
  row.title = result && result.messageHead ? result.messageHead : label;
  row.addEventListener('click', () => openTestLocation(item.file, result ? result.failureLine : 0));
  return row;
}

function renderTests() {
  const host = $('tests-tree');
  host.replaceChildren();
  const tree = S.testTree;
  if (!tree) return;
  updateTestsBadge();

  $('btn-tests-run-all').disabled = !tree.configured || S.testsRunning;
  $('btn-tests-run-failed').disabled = !tree.configured || S.testsRunning;

  if (!tree.configured) {
    $('tests-summary').replaceChildren();
    host.appendChild(el('div', 'empty',
      tree.error || '未配置测试命令（可通过 VIBE_TEST_COMMAND 或 VIBE.md 的 test: 行设置）'));
    return;
  }
  renderTestSummary();
  const cases = tree.cases || [];
  if (!cases.length) {
    host.appendChild(el('div', 'empty',
      tree.error ? `没有收集到用例：${tree.error}` : '没有收集到用例。'));
    return;
  }

  // 树：文件 → 用例。结果图标落在用例上（node id 与收集输出对齐）。
  const byFile = new Map();
  for (const item of cases) {
    if (!byFile.has(item.file)) byFile.set(item.file, []);
    byFile.get(item.file).push(item);
  }
  for (const [file, groupCases] of byFile) {
    const group = el('div', 'test-file');
    const head = el('div', 'test-file-head');
    head.appendChild(el('span', 'twisty', '▾'));
    head.appendChild(el('span', 'name', file));
    head.appendChild(el('span', 'test-file-count', String(groupCases.length)));
    group.appendChild(head);
    const list = el('div', 'test-cases');
    for (const item of groupCases) list.appendChild(renderTestRow(item));
    group.appendChild(list);
    host.appendChild(group);
  }
  if (tree.truncated) host.appendChild(el('div', 'empty', '用例过多，仅展示一部分。'));
}

function updateTestsBadge() {
  const badge = $('act-badge-tests');
  const failed = S.testSummary ? S.testSummary.failed : 0;
  badge.hidden = !failed;
  badge.textContent = String(failed);
}

/* 开编辑器并定位到失败行。line<=0（通过 / 跳过）时只打开文件不跳。 */
async function openTestLocation(file, line) {
  if (!file) return;
  await openFile(file);
  const entry = S.editors.get(`file:${file}`);
  if (entry && line > 0) placeCaret(entry, line, 0);
}

async function runTests(nodeIds) {
  if (S.testsRunning) { toast('已有测试正在运行，等它结束再点', 'error'); return; }
  // 乐观置位「运行中」：POST 到 test.started 之间有一小段空窗，
  // 不先占住的话连点两下会有第二下撞在 409 上。
  S.testsRunning = true;
  S.testRunningIds = new Set(nodeIds.length ? nodeIds : (S.testTree?.cases || []).map((c) => c.nodeId));
  renderTests();
  try {
    await api.post('/api/tests/run', { nodeIds });
  } catch (err) {
    // 起不来（未配置 400 / 并发 409）：把乐观置位退回去，并如实提示
    S.testsRunning = false;
    S.testRunningIds = new Set();
    renderTests();
    toast(`发起测试失败：${err.message}`, 'error');
  }
}

function runAllTests() {
  runTests([]);
}

function runFailedTests() {
  const ids = [];
  for (const [nodeId, result] of S.testResults) {
    if (result.status === 'failed' || result.status === 'error') ids.push(nodeId);
  }
  if (!ids.length) { toast('上一次没有失败 / 出错的用例', ''); return; }
  runTests(ids);
}

function renderTools(tools) {
  const host = $('tools-list');
  host.replaceChildren();
  if (!tools.length) {
    host.appendChild(el('div', 'empty', '没有可用工具。'));
    return;
  }
  for (const tool of tools) {
    const item = el('div', 'tool-item');
    const title = el('div', 'tn');
    title.appendChild(document.createTextNode(tool.name));
    const pill = el('span', 'risk-pill', tool.risk);
    pill.dataset.r = tool.risk;
    pill.style.marginLeft = '8px';
    title.appendChild(pill);
    item.appendChild(title);
    item.appendChild(el('div', 'td', tool.description));
    host.appendChild(item);
  }
}

async function refreshAudit() {
  const host = $('audit-view');
  host.replaceChildren(el('div', 'empty', '校验中…'));
  try {
    const data = await api.get('/api/audit');
    host.replaceChildren();

    host.appendChild(el('div', data.passed ? 'audit-ok' : 'audit-bad',
      (data.passed ? '✓ ' : '✗ ') + data.detail));
    host.appendChild(el('div', 'audit-path', data.path));

    host.appendChild(el('div', 'empty',
      '审计只记录「谁 / 何时 / 对哪个文件 / 用了哪些工具」等元数据，' +
      '不记录代码内容与模型输出原文 —— 因此下面的记录里不会出现源码。'));

    const list = el('div', null, '');
    list.style.marginTop = '10px';
    for (const entry of [...data.entries].reverse()) {
      const row = el('div', 'audit-entry');
      const ev = el('span', 'ev', entry.event);
      const ts = el('span', 'ts', ` ${entry.ts} `);
      row.append(ts, ev);
      const rest = Object.entries(entry)
        .filter(([k]) => !['ts', 'event', 'seq', 'session', 'pid', 'prev', 'hash'].includes(k))
        .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`)
        .join(' ');
      if (rest) row.appendChild(document.createTextNode(' ' + rest));
      list.appendChild(row);
    }
    host.appendChild(list);
  } catch (err) {
    host.replaceChildren(el('div', 'empty', `校验失败：${err.message}`));
  }
}

/* ═══════════════════════════════ 审批 ═══════════════════════════════ */
function openApproval(payload) {
  S.awaiting = payload;
  const risk = $('modal-risk');
  risk.textContent = payload.risk || 'REVIEW';
  risk.dataset.r = payload.risk || 'REVIEW';
  $('modal-tool').textContent = payload.tool;
  $('modal-reason').textContent = payload.reason || '';

  const preview = payload.preview || {};
  $('modal-summary').textContent = preview.summary || '';

  const box = $('modal-preview');
  box.replaceChildren();

  if (preview.kind === 'diff' && preview.body) {
    const node = renderDiff(preview.body);
    if (node) box.appendChild(node);
    else box.appendChild(el('div', 'diff-empty', '（diff 为空或无法解析）'));
  } else if (preview.kind === 'text') {
    box.appendChild(el('pre', 'raw-preview', preview.body || ''));
  } else if (preview.kind === 'error') {
    box.appendChild(el('div', 'preview-error', preview.error || '预演失败'));
  } else {
    box.appendChild(el('pre', 'raw-preview', preview.body || JSON.stringify(payload.arguments, null, 2)));
  }

  if (preview.clipped) {
    box.appendChild(el('div', 'empty', '内容过长，此处仅展示前一部分。'));
  }

  $('modal').hidden = false;
  $('approval-banner').hidden = true;
  $('btn-approve').focus();
}

function closeApproval() {
  $('modal').hidden = true;
  S.awaiting = null;
  $('approval-banner').hidden = true;
}

async function decide(decision) {
  if (!S.awaiting) return;
  const id = S.awaiting.approvalId;
  closeApproval();
  try {
    const res = await api.post('/api/approval', { approvalId: id, decision });
    if (!res.delivered) toast('该审批已失效（可能已超时或任务已结束）', 'error');
  } catch (err) {
    toast(`提交审批结果失败：${err.message}`, 'error');
  }
}

function minimizeApproval() {
  $('modal').hidden = true;
  $('approval-banner').hidden = false;
}

/* ═══════════════════════════════ 事件流 ═══════════════════════════════ */
const HANDLERS = {
  'session.ready': (ev) => {
    // 后端切换工作区后会重发一次 session.ready。它不是「新连接」，
    // 而是一整套上下文的更替 —— 视图必须跟着清空，否则会拿旧工作区的
    // 文件树与改动去对应新会话。
    const switched =
      S.booted && S.session && ev.workspace && S.session.workspace !== ev.workspace;
    // 顺序不能反：**先清视图，再铺权威状态**。反过来的话，applySession 刚把
    // 待审批的浮层打开（并设好 S.awaiting），紧接着的 clearSessionView 又把
    // S.awaiting 清空，留下一个「在、却点不动」的弹窗。页面刷新时事件流会重放
    // 一次 session.ready，这条路径每次都走，所以这个 bug 一刷新就必然复现。
    if (!S.booted) {
      clearSessionView();
      S.booted = true;
    } else if (switched) {
      clearSessionView({ closeEditors: true });
    }
    applySession(ev);
    if (switched) {
      refreshTree();
      refreshChanges();
      loadHistory();
      resetTestView();
      loadTestResults();
    }
  },

  'run.started': (ev) => {
    hideChatWelcome();
    setState('running');
    S.streaming = null;
    S.iterations = 0;
    updateStatusBarIter();
    addUser(ev.prompt);
    S.lastRun = ev.runId;
    termLine('output', `任务开始：${ev.prompt}`, 'is-dim');
  },

  'run.iteration': (ev) => addIteration(ev.index, ev.total),

  'run.assistant_delta': (ev) => appendDelta(ev.text),

  'run.tool_call': (ev) => {
    finishStreaming();
    addToolCall(ev);
  },

  'run.tool_result': (ev) => completeToolCall(ev),

  'run.subagent': (ev) => applySubAgent(ev),

  'run.approval': (ev) => openApproval(ev),

  'run.approval_resolved': (ev) => {
    if (S.awaiting && S.awaiting.approvalId === ev.approvalId) closeApproval();
    if (ev.timedOut) toast('审批超时，已按最安全的方式处理：拒绝', 'error');
  },

  'run.notice': (ev) => {
    addNotice(ev.text);
    termLine('output', ev.text, 'is-dim');
  },

  'run.plan': (ev) => applyPlan(ev),

  'run.context': (ev) => applyContext(ev),

  'run.finished': (ev) => {
    setState('idle');
    // 用 closeApproval() 而不是只清 S.awaiting：同一个不变式 —— 任务都结束了，
    // 留一个点不动的审批弹窗没有任何意义（同上，decide() 会见状态为空而 return）。
    closeApproval();
    const streamed = finishStreaming(ev.answer || '（模型返回了空内容）');
    if (!streamed) addAnswer(ev.answer || '（模型返回了空内容）');
    updateStats(ev.stats);
    refreshChanges();
    if (ev.checkpointId) addCheckpointBanner(ev.checkpointId);
    termLine('output', '任务完成。', 'is-ok');
  },

  'run.failed': (ev) => {
    setState('idle');
    finishStreaming();
    addNotice(`任务失败（${ev.kind}）：${ev.error}`, true);
    termLine('output', `任务失败（${ev.kind}）：${ev.error}`, 'is-err');
  },

  'run.interrupted': (ev) => {
    setState('idle');
    finishStreaming();
    addNotice(ev.answer || '已中断本次任务。');
    termLine('output', ev.answer || '已中断本次任务。', 'is-dim');
    refreshChanges();
  },

  // 测试（K/P2-14）。与 run.* 分开：跑测试是用户本人发起的动作，不是 Agent 一轮任务。
  'test.started': (ev) => {
    S.testsRunning = true;
    const ids = ev.nodeIds && ev.nodeIds.length
      ? ev.nodeIds
      : (S.testTree?.cases || []).map((c) => c.nodeId);
    S.testRunningIds = new Set(ids);
    renderTests();
    termLine('output', `开始跑测试${ev.total ? `（${ev.total} 个用例）` : ''}`, 'is-dim');
  },

  'test.finished': async (ev) => {
    S.testsRunning = false;
    S.testRunningIds = new Set();
    // 用 /api/tests/last 拿逐用例结果 —— 失败事件只带失败项，跑「全部」时
    // 无法据此标出哪些是 passed / skipped。
    await loadTestResults();
    renderTests();
    if (ev.error) {
      addNotice(`测试未跑起来：${ev.error}`, true);
      termLine('output', `测试未跑起来：${ev.error}`, 'is-err');
    } else {
      termLine('output',
        `测试完成：${ev.passed} 通过 · ${ev.failed} 失败 · ${ev.skipped} 跳过`,
        ev.failed ? 'is-err' : 'is-ok');
    }
  },

  // 交互式终端（P1-7）。与 run.* / test.* 都分开：这是用户本人在终端里敲命令，
  // 后端只把原始字节（base64）透传回来，前端做最小 ANSI 渲染。
  'term.data': (ev) => {
    if (term.open && ev.termId === term.id) termFeed(ev.data);
  },

  'term.exit': (ev) => {
    if (ev.termId === term.id) termOnExit(ev.exitCode);
  },
};

function connect() {
  const url = new URL('/api/events', location.origin);
  url.searchParams.set('token', TOKEN);
  if (S.lastEventId) url.searchParams.set('since', String(S.lastEventId));

  const es = new EventSource(url);

  for (const type of Object.keys(HANDLERS)) {
    es.addEventListener(type, (message) => {
      let data;
      try {
        data = JSON.parse(message.data);
      } catch {
        return;
      }
      if (typeof data.id === 'number') S.lastEventId = data.id;
      try {
        HANDLERS[type](data);
      } catch (err) {
        console.error('渲染事件失败', type, err);
      }
    });
  }

  es.onerror = () => {
    $('composer-status').textContent = '与后端断开，正在重连…';
  };
  es.onopen = () => {
    $('composer-status').textContent = S.running ? 'Agent 正在工作…' : '就绪 · Enter 发送';
  };
  return es;
}

/* ═══════════════════════════════ 交互 ═══════════════════════════════ */
/* ---- 引用（P0-A）：`@` 候选浮层 + 已选 chips ----
   两条硬约定：
   1) 界面只提交「意图」（{type, value}），正文一律由后端经 PathGuard 解析 ——
      否则前端自己读文件就成了绕过沙箱的通道；
   2) 选中后**不把 `@xxx` 留在正文里**，而是落成一个 chip、发送时独立带上。
      留在正文里会让模型把 `@` 当成普通文本再猜一遍，与结构化引用重复。 */
const REF_KINDS = [
  ['file', '文件'],
  ['symbol', '符号'],
  ['folder', '目录'],
];
// image 不进 `@` 候选浮层（它来自上传，不是扫描），但 chip 标题要认得它
const REF_LABEL = { ...Object.fromEntries(REF_KINDS), image: '图片' };
const MAX_REFS = 50;            // 与后端 MAX_REF_ITEMS 一致，超出后端会 400

const mention = {
  open: false, type: 'file', start: -1, q: '',
  items: [], index: 0, loading: false, token: 0, timer: 0,
};

function syncSendState() {
  $('btn-send').disabled = S.running || !$('input').value.trim();
}

function closeMention() {
  clearTimeout(mention.timer);
  mention.open = false;
  mention.type = 'file';        // 重开时回到「文件」，免得上次选过「符号」让人困惑
  mention.start = -1;
  mention.q = '';
  mention.items = [];
  mention.index = 0;
  mention.loading = false;
  $('mention-pop').hidden = true;
}

/* 光标前是否存在一个尚未结束的 `@词`；返回 {start, q} 或 null。
   `@` 前必须是行首或空白 —— 否则 `a@b.com` 这种文本会误触发浮层。 */
function mentionContext() {
  const input = $('input');
  const caret = input.selectionStart;
  const before = input.value.slice(0, caret);
  const at = before.lastIndexOf('@');
  if (at < 0) return null;
  if (at > 0 && !/\s/.test(before[at - 1])) return null;
  const q = before.slice(at + 1);
  if (/\s/.test(q)) return null;
  return { start: at, q };
}

function renderMention() {
  const head = $('mention-head');
  head.replaceChildren();
  for (const [type, label] of REF_KINDS) {
    const tab = el('button', 'mention-tab' + (mention.type === type ? ' is-active' : ''), label);
    tab.type = 'button';
    tab.dataset.type = type;
    head.appendChild(tab);
  }
  head.appendChild(el('span', 'mi-hint',
    mention.q ? `匹配 “${mention.q}”` : '↑↓ 选择 · Enter 引用 · Esc 取消'));

  const list = $('mention-list');
  list.replaceChildren();
  if (!mention.items.length) {
    list.appendChild(el('div', 'mention-empty', mention.loading ? '搜索中…' : '没有匹配项'));
    return;
  }
  mention.items.forEach((item, index) => {
    const row = el('div', 'mention-item' + (index === mention.index ? ' is-active' : ''));
    row.dataset.idx = String(index);
    row.appendChild(el('span', 'mi-label', item.label));
    row.appendChild(el('span', 'mi-detail', item.detail || ''));
    list.appendChild(row);
    if (index === mention.index) row.scrollIntoView({ block: 'nearest' });
  });
}

async function fetchMention() {
  const token = ++mention.token;
  const { q, type } = mention;
  mention.loading = true;
  try {
    const data = await api.get('/api/refs', { type, q, limit: 30 });
    if (token !== mention.token || !mention.open) return;  // 迟到的响应直接丢
    mention.items = data.items || [];
  } catch {
    if (token !== mention.token) return;
    mention.items = [];
  }
  mention.loading = false;
  mention.index = 0;
  renderMention();
}

function scheduleMention() {
  clearTimeout(mention.timer);
  mention.timer = setTimeout(fetchMention, 120);   // 候选要扫工作区，打字时先攒一下
}

function setMentionType(type) {
  if (mention.type === type) return;
  mention.type = type;
  mention.index = 0;
  fetchMention();
}

function addRef(item) {
  if (S.refs.some((r) => r.type === item.type && r.value === item.value)) {
    toast('这个引用已经在列表里了');
    return;
  }
  if (S.refs.length >= MAX_REFS) {
    toast(`一次最多引用 ${MAX_REFS} 项`, 'error');
    return;
  }
  S.refs.push({ type: item.type, value: item.value });
  renderRefs();
}

function renderRefs() {
  const bar = $('refs-bar');
  bar.replaceChildren();
  bar.hidden = !S.refs.length;
  S.refs.forEach((ref, index) => {
    const chip = el('span', `ref-chip is-${ref.type}`);
    chip.title = `${REF_LABEL[ref.type]}：${ref.value}`;
    chip.appendChild(el('span', 'ref-text', ref.value));
    const del = el('button', 'ref-x', '✕');
    del.type = 'button';
    del.title = '移除引用';
    del.addEventListener('click', () => {
      S.refs.splice(index, 1);
      renderRefs();
      syncSendState();
    });
    chip.appendChild(del);
    bar.appendChild(chip);
  });
}

/* 多模态输入（P1-10）：把图片读成 data URL，交给后端落盘 + 校验 -------
 * 前端不判断内容是否真是图片（只看 MIME 声明）：真伪与大小上限都由后端
 * `session.upload_image` 用魔数再核一遍 —— 前端说了不算。 */
function fileToDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error('读取图片失败'));
    reader.readAsDataURL(file);
  });
}

async function uploadImageFile(file) {
  if (!file) return;
  if (!S.visionOn) {
    toast('当前未开启图片输入（需 VIBE_VISION=auto/on）', 'error');
    return;
  }
  if (!/^image\/(png|jpeg|webp|gif)$/i.test(file.type || '')) {
    toast('只支持 PNG / JPEG / WebP / GIF 图片', 'error');
    return;
  }
  try {
    const data = await fileToDataURL(file);
    const res = await api.post('/api/upload-image', {
      name: file.name || 'paste.png',
      data,
      mediaType: file.type || '',
    });
    addRef({ type: 'image', value: res.ref.value });
  } catch (err) {
    toast(err.message, 'error');
  }
}

function acceptMention(index = mention.index) {
  const item = mention.items[index];
  if (!item) return;
  addRef(item);
  // 把正文里的 `@查询词` 抹掉：它已经变成 chip 了，留着只会变成重复信息
  const input = $('input');
  const start = mention.start;                       // closeMention() 会把它清掉，先留住
  const caret = Math.max(start + 1, input.selectionStart);
  input.value = input.value.slice(0, start) + input.value.slice(caret);
  closeMention();
  input.focus();
  input.setSelectionRange(start, start);
  autoGrow();
  syncSendState();
}

function autoGrow() {
  const input = $('input');
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 200) + 'px';
}

async function send() {
  const input = $('input');
  const text = input.value.trim();
  if (!text || S.running) return;
  const refs = S.refs.slice();     // 失败时要把引用一并还回去
  input.value = '';
  S.refs = [];
  renderRefs();
  closeMention();
  autoGrow();
  syncSendState();
  try {
    await api.post('/api/run', { text, refs });
  } catch (err) {
    toast(err.message, 'error');
    // 发送失败就把内容与引用都还回去，别让用户白打一遍
    input.value = text;
    S.refs = refs;
    renderRefs();
    autoGrow();
    syncSendState();
  }
}

async function interrupt() {
  try {
    const res = await api.post('/api/interrupt', {});
    toast(res.interrupted ? '已请求中断，将在当前步骤结束后停止' : '当前没有正在执行的任务');
  } catch (err) {
    toast(err.message, 'error');
  }
}

/* ---- 计划卡片（P2-12） ----
   计划是 Agent 用 update_plan 工具给出的**完整**步骤表，状态由模型驱动；
   人也可以点某一步手动标注（POST /api/plan/toggle）。

   两条约定：
   1) 版本号只用来**丢弃乱序**的旧事件（SSE 重放时旧计划可能后到）；
   2) 没有计划（steps 为空）时整张卡片隐藏 —— 不用这个工具的用户界面与
      以前完全一致，不会多出一块空白。 */
const PLAN_ICON = { pending: '○', in_progress: '◐', done: '●', skipped: '◌' };
const PLAN_LABEL = { pending: '待办', in_progress: '进行中', done: '完成', skipped: '跳过' };

function applyPlan(plan) {
  const steps = (plan && plan.steps) || [];
  const revision = (plan && plan.revision) || 0;
  if (S.plan && revision && revision < S.plan.revision) return;  // 乱序的旧版本
  S.plan = steps.length ? { steps, revision } : null;
  renderPlan();
}

function renderPlan() {
  const card = $('plan-card');
  if (!card) return;
  const plan = S.plan;
  if (!plan) {
    card.hidden = true;
    $('plan-list').replaceChildren();
    return;
  }
  const done = plan.steps.filter((s) => s.status === 'done' || s.status === 'skipped').length;
  $('plan-progress').textContent = `${done}/${plan.steps.length}`;
  $('plan-caret').textContent = S.planCollapsed ? '▸' : '▾';
  $('plan-list').hidden = S.planCollapsed;
  $('plan-list').replaceChildren(...plan.steps.map(planItem));
  card.hidden = false;
}

function planItem(step) {
  const item = el('li', `plan-item is-${step.status}`);
  item.appendChild(el('span', 'plan-icon', PLAN_ICON[step.status] || '○'));
  item.appendChild(el('span', 'plan-text', step.text || ''));
  // 点一下 = 手动标注「这步我自己做掉了」；已完成再点则撤销回待办。
  // 只在这两个状态间切换，不猜「进行中/跳过」—— 那两个是模型的活。
  const next = step.status === 'done' ? 'pending' : 'done';
  item.title = `${PLAN_LABEL[step.status] || step.status} · 点击标注为「${PLAN_LABEL[next]}」`;
  item.onclick = () => togglePlanStep(step.id, next);
  return item;
}

async function togglePlanStep(id, status) {
  try {
    await api.post('/api/plan/toggle', { id, status });
    // 不在这里改 S.plan：后端改完会广播 run.plan，渲染统一由事件驱动，
    // 免得「本地先改一次、事件回来再改一次」，看起来跳两下。
  } catch (err) {
    toast(err.message, 'error');
  }
}

function togglePlanCollapsed() {
  S.planCollapsed = !S.planCollapsed;
  renderPlan();
}

/* 清空「属于某个工作区」的视图状态：对话流、编辑器标签、改动计数。
   新建会话（同工作区）与切换工作区（换工作区）共用，差别只在参数：
   - 切换工作区时旧标签指向已离开的目录，必须关掉（closeEditors）；
   - 但 lastEventId **不能**归零 —— 它只用于断线补齐，归零会让重连时
     把整段事件历史重放一遍。 */
function clearSessionView({ closeEditors = false, resetEventId = false } = {}) {
  stream.replaceChildren();
  const welcome = el('div', 'chat-welcome', '');
  welcome.id = 'chat-welcome';
  welcome.appendChild(el('p', null, '把任务描述清楚，剩下的交给 Agent。'));
  welcome.appendChild(el('p', 'is-dim', '所有高危动作都会先弹审批。'));
  stream.appendChild(welcome);

  if (closeEditors) for (const id of [...S.editors.keys()]) closeEditor(id);
  S.calls.clear();
  S.changed.clear();
  S.created.clear();
  S.iterations = 0;
  S.streaming = null;
  S.awaiting = null;
  // 引用属于「这次提交」，新会话/换工作区都不该把它带过去（换工作区后
  // 旧引用指向已离开的目录，后端会一律解析失败）。
  S.refs = [];
  renderRefs();
  // 计划属于「这一次会话」：新会话 / 换工作区都不该把上一份计划留下来。
  // 顺序要紧 —— session.ready 是「先清视图、再铺快照」，这里清掉之后
  // applySession() 会用权威快照把它补回来（刷新页面时计划不丢）。
  S.plan = null;
  renderPlan();
  // 用量同属「这一次会话」：同理先清、再由 session.ready 的快照补回来。
  S.context = null;
  renderContext();
  closeMention();
  // 改写条挂的是「某个文件的某个选区」，会话一换选区就没了意义；
  // 它和审批浮层同理，必须随会话状态一起收掉。
  closeRewriteBar();
  // 审批浮层必须和 S.awaiting 同生共死。这里只清状态却留着弹窗，
  // 就会出现「弹窗明明在，四个决策键却全都没反应」—— decide() 一进门
  // 见 awaiting 为空就 return，而「先看看」不查状态，所以只剩它能动。
  $('modal').hidden = true;
  $('approval-banner').hidden = true;
  if (resetEventId) S.lastEventId = 0;
  updateChangesBadge();
  updateStatusBarIter();
  $('changes-list').replaceChildren(el('div', 'empty', '本次会话还没有产生改动。'));
}

async function reset() {
  if (!confirm('清空对话历史？（工作区里的改动和审计日志都不受影响）')) return;
  try {
    await api.post('/api/reset', {});
    clearSessionView({ resetEventId: true });
    refreshTree();
    loadHistory();
    toast('已开始新会话', 'ok');
  } catch (err) {
    toast(err.message, 'error');
  }
}

/* ---- 历史会话（A2） ---- */
async function loadHistory() {
  const list = $('history-list');
  try {
    const res = await api.get('/api/sessions');
    const current = S.session && S.session.sessionId;
    const sessions = (res.sessions || []).filter((s) => s.id !== current);
    if (!sessions.length) {
      list.replaceChildren(el('div', 'empty', '还没有历史会话。'));
      return;
    }
    list.replaceChildren(
      ...sessions.map((item) => {
        const row = el('button', 'history-item');
        row.type = 'button';
        row.appendChild(el('span', 'history-title', item.title || item.id));
        row.appendChild(el('span', 'history-time', String(item.updatedAt || '').replace('T', ' ')));
        row.addEventListener('click', () => resumeSession(item.id));
        return row;
      })
    );
  } catch {
    list.replaceChildren(el('div', 'empty', '会话持久化未开启。'));
  }
}

/* ---- 跨会话记忆（P2-15） ---- */
/* 写入都是「用户本人动作」：这里只发 /api/memories，不经过审批浮层；
   模型侧的写入只能走 remember 工具（恒人审）。两者共用同一个本地库。 */
async function refreshMemories() {
  const list = $('mem-list');
  if (!list) return;
  try {
    const res = await api.get('/api/memories');
    updateMemoryBadge(res.count || 0);
    if (res.enabled === false) {
      list.replaceChildren(el('div', 'empty', '记忆功能未启用（VIBE_MEMORY=0）。'));
      return;
    }
    const items = res.items || [];
    if (!items.length) {
      list.replaceChildren(el('div', 'empty', '还没有记忆。'));
      return;
    }
    list.replaceChildren(...items.map((item) => memoryRow(item)));
  } catch (err) {
    list.replaceChildren(el('div', 'empty', '加载失败：' + err.message));
  }
}

function updateMemoryBadge(count) {
  const badge = $('act-badge-memory');
  if (badge) { badge.hidden = !count; badge.textContent = String(count); }
  const badge2 = $('badge-memory');
  if (badge2) { badge2.textContent = String(count); badge2.classList.toggle('has', count > 0); }
}

function memoryRow(item) {
  const row = el('div', 'mem-item');
  row.appendChild(el('div', 'mem-text', item.text || ''));
  const meta = el('div', 'mem-meta');
  const tags = item.tags || [];
  if (tags.length) meta.appendChild(el('span', 'mem-tag', tags.map((t) => '#' + t).join(' ')));
  // scope 区分来源：agent = 模型提议后经人审入库，user = 用户自己添加
  meta.appendChild(el('span', 'mem-scope', item.scope === 'agent' ? '模型提议' : '我添加'));
  meta.appendChild(el('span', 'mem-time', String(item.createdAt || '').replace('T', ' ')));
  const del = el('button', 'icon-btn', '✕');
  del.type = 'button';
  del.title = '删除这条记忆';
  del.addEventListener('click', () => deleteMemory(item.id));
  meta.appendChild(del);
  row.appendChild(meta);
  return row;
}

async function addMemoryFromPanel() {
  const box = $('mem-input');
  const tagsBox = $('mem-tags');
  const text = (box.value || '').trim();
  if (!text) { toast('请先输入要记住的内容', 'error'); return; }
  const tags = (tagsBox.value || '').split(',').map((t) => t.trim()).filter(Boolean);
  try {
    await api.post('/api/memories', { text, tags });
    box.value = '';
    tagsBox.value = '';
    toast('已记住', 'ok');
    refreshMemories();
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function deleteMemory(id) {
  try {
    await api.post('/api/memories/delete', { id });
    refreshMemories();
  } catch (err) {
    toast(err.message, 'error');
  }
}

/* 「记住这条」：把回复正文填进记忆面板（**不直接落库**）——
   让人先看一眼、可裁剪超长内容，确认后再点「记住」。那一下才是写入动作。 */
function rememberAnswer(text) {
  switchPanel('memory');
  const box = $('mem-input');
  if (box) {
    box.value = String(text || '').slice(0, 2000);
    box.focus();
  }
  toast('已填入记忆面板，确认后点「记住」', 'ok');
}

function addRememberButton(node, text) {
  const btn = el('button', 'remember-btn', '记住这条');
  btn.type = 'button';
  btn.title = '把这条回复填入记忆面板（用户本人动作，确认后落库）';
  btn.addEventListener('click', () => rememberAnswer(text));
  node.appendChild(btn);
}

/* ---- 外部 MCP server（L 管理面） ----
   写入只落 <工作区>/.vibe/mcp.json：命令 / 参数只取自该受信配置文件，
   界面**从不**把它们拼进 argv（ADR-015）。外部工具恒 REVIEW、每次调用都经审批浮层。 */
async function refreshMcpPanel() {
  const list = $('mcp-list');
  if (!list) return;
  const addBtn = $('btn-mcp-add');
  try {
    const status = await api.get('/api/mcp/status');
    // 开关状态 / 环境变量锁定（复用既有设置接口，字段按显式存在提交）
    try {
      const info = await api.get('/api/settings');
      const box = $('mcp-enabled');
      box.checked = info.mcpEnabled === true;
      lockField('mcp-enabled', (info.envLocked || {}).mcp_enabled);
    } catch {
      /* 读不到设置不影响 status 的展示 */
    }
    addBtn.disabled = status.enabled === false;
    if (status.enabled === false) {
      list.replaceChildren(
        el('div', 'empty', 'MCP 未启用。勾选上方「启用 MCP」后即可管理 server。')
      );
      return;
    }
    const servers = status.servers || [];
    const nodes = [];
    if (status.configError) {
      nodes.push(el('div', 'empty', '配置文件有问题：' + status.configError));
    }
    if (!servers.length) {
      nodes.push(el('div', 'empty', '还没有配置 server。点「新增 server」添加一个。'));
    } else {
      nodes.push(...servers.map((s) => mcpRow(s)));
    }
    list.replaceChildren(...nodes);
  } catch (err) {
    list.replaceChildren(el('div', 'empty', '加载失败：' + err.message));
  }
}

function mcpRow(server) {
  const row = el('div', 'mcp-item');
  const head = el('div', 'mcp-head');
  head.appendChild(el('span', 'mcp-name', server.name || '(未命名)'));
  head.appendChild(el('span', 'mcp-transport', server.transport || 'stdio'));
  if (server.running) head.appendChild(el('span', 'mcp-state is-running', '运行中'));
  else if (server.available === false) head.appendChild(el('span', 'mcp-state is-bad', '不可用'));
  else head.appendChild(el('span', 'mcp-state is-idle', '未启动'));
  head.appendChild(el('span', 'mcp-tools', `${(server.tools || []).length} 工具`));
  row.appendChild(head);

  const target = server.transport === 'http' ? server.url : server.command;
  if (target) row.appendChild(el('div', 'mcp-target', target));
  if (server.available === false && server.reason) {
    row.appendChild(el('div', 'mcp-reason', server.reason));
  }

  const acts = el('div', 'mcp-actions');
  const test = el('button', 'btn btn-small', '测试');
  test.type = 'button';
  test.addEventListener('click', () => testMcpServer(server.name));
  const edit = el('button', 'btn btn-small', '编辑');
  edit.type = 'button';
  edit.addEventListener('click', () => openMcpModal(server));
  const del = el('button', 'btn btn-small', '删除');
  del.type = 'button';
  del.addEventListener('click', () => removeMcpServer(server.name));
  acts.append(test, edit, del);
  row.appendChild(acts);
  return row;
}

function openMcpModal(server) {
  $('mcp-modal').hidden = false;
  $('mcp-error').hidden = true;
  $('mcp-msg').textContent = '';
  $('mcp-modal-title').textContent = server ? `编辑 server：${server.name}` : '新增 server';
  $('mcp-f-name').value = server ? server.name || '' : '';
  // 编辑时名称只读：改名会被后端当作「新增」而留下旧的同名项，容易困惑。
  $('mcp-f-name').disabled = !!server;
  if (server) $('mcp-f-name').title = '编辑时不可改名（如需改名请先删除再新增）';
  else $('mcp-f-name').removeAttribute('title');
  $('mcp-f-transport').value = server ? server.transport || 'stdio' : 'stdio';
  $('mcp-f-command').value = server ? server.command || '' : '';
  $('mcp-f-args').value = server && Array.isArray(server.args) ? server.args.join('\n') : '';
  $('mcp-f-url').value = server ? server.url || '' : '';
  const env = (server && server.env) || {};
  $('mcp-f-env').value = Object.entries(env).map(([k, v]) => `${k}=${v}`).join('\n');
  syncMcpTransportFields();
  $('mcp-f-name').focus();
}

function closeMcpModal() {
  $('mcp-modal').hidden = true;
}

/** 按传输类型禁用无关字段：stdio 用不上 URL，http 用不上 command/args。 */
function syncMcpTransportFields() {
  const http = $('mcp-f-transport').value === 'http';
  $('mcp-f-url').disabled = !http;
  $('mcp-f-command').disabled = http;
  $('mcp-f-args').disabled = http;
}

function showMcpError(message) {
  const box = $('mcp-error');
  box.hidden = false;
  box.textContent = message || '';
}

async function saveMcpServer() {
  const name = $('mcp-f-name').value.trim();
  const transport = $('mcp-f-transport').value === 'http' ? 'http' : 'stdio';
  if (!name) { showMcpError('请填写 server 名称'); return; }
  const server = { name, transport };
  if (transport === 'http') {
    const url = $('mcp-f-url').value.trim();
    if (!url) { showMcpError('http 传输需要填写 URL'); return; }
    server.url = url;
  } else {
    const command = $('mcp-f-command').value.trim();
    if (!command) { showMcpError('stdio 传输需要填写命令'); return; }
    server.command = command;
    const args = $('mcp-f-args').value.split('\n').map((s) => s.trim()).filter(Boolean);
    if (args.length) server.args = args;
  }
  const env = {};
  for (const line of $('mcp-f-env').value.split('\n')) {
    const text = line.trim();
    const eq = text.indexOf('=');
    if (eq > 0) env[text.slice(0, eq).trim()] = text.slice(eq + 1).trim();
  }
  if (Object.keys(env).length) server.env = env;

  const btn = $('btn-mcp-save');
  btn.disabled = true;
  $('mcp-error').hidden = true;
  try {
    await api.post('/api/mcp/servers', { server });
    toast('已保存', 'ok');
    closeMcpModal();
    refreshMcpPanel();
  } catch (err) {
    // 409 = 任务执行中；400 = 后端校验原文（未启用 / 配置非法 / 治理面接管）
    showMcpError(err.message);
    toast(err.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

async function removeMcpServer(name) {
  if (!confirm(`删除 server「${name}」？`)) return;
  try {
    await api.post('/api/mcp/servers/remove', { name });
    toast('已删除', 'ok');
    refreshMcpPanel();
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function testMcpServer(name) {
  try {
    const res = await api.post('/api/mcp/test', { name });
    if (res.ok) toast(`连接成功，${res.count} 个工具`, 'ok');
    else toast(res.error || '连接失败', 'error');
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function reloadMcpServers() {
  try {
    await api.post('/api/mcp/reload', {});
    toast('已重载', 'ok');
    refreshMcpPanel();
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function toggleMcpEnabled(checked) {
  const box = $('mcp-enabled');
  try {
    // 只提交 mcpEnabled 一项（按显式存在，false 也要落盘为「关闭」）
    await api.post('/api/settings', { mcpEnabled: checked });
    toast(checked ? '已启用 MCP' : '已关闭 MCP', 'ok');
    refreshMcpPanel();
  } catch (err) {
    box.checked = !checked; // 失败回滚复选框状态
    toast(err.message, 'error');
  }
}

function replayHistory(messages) {
  for (const m of messages) {
    if (m.role === 'user') {
      addUser(m.content);
    } else if (m.role === 'assistant') {
      if (m.content) addAnswer(m.content);
      const calls = m.tool_calls || [];
      const names = calls.map((c) => c.function && c.function.name).filter(Boolean);
      if (names.length) addNotice(`[已调用工具：${names.join('、')}]`);
    }
  }
}

async function resumeSession(id) {
  if (S.running) {
    toast('当前有任务在执行，等它结束后再恢复', 'error');
    return;
  }
  try {
    await api.post(`/api/sessions/${encodeURIComponent(id)}/resume`, {});
    const detail = await api.get(`/api/sessions/${encodeURIComponent(id)}/messages`);
    stream.replaceChildren();
    S.calls.clear();
    S.changed.clear();
    S.created.clear();
    S.iterations = 0;
    S.streaming = null;
    updateChangesBadge();
    updateStatusBarIter();
    replayHistory(detail.messages || []);
    hideChatWelcome();
    refreshTree();
    refreshChanges();
    loadHistory();
    toast('会话已恢复', 'ok');
  } catch (err) {
    toast(err.message, 'error');
  }
}

/* ---- 切换工作区（W1） ---- */
/* 两条前端共用一套：真正干活的是后端的工作区接口 —— 它会重建一整套上下文
   （新会话 / 新审计链 / 新 jail），所以弹窗必须先讲清代价。
   目录选择有两条平行的路：
   - 桌面壳：preload 注入的 window.vibeShell.pickFolder()，调系统对话框；
   - 浏览器：没有那个桥（File System Access API 拿不到真实路径，用不上），
     改由下面的内置目录浏览器逐级点选，后端是只读的 GET /api/fs/dirs。 */
async function openWorkspaceModal() {
  $('ws-modal').hidden = false;
  $('ws-error').hidden = true;
  // 没有桥就隐藏，而不是留一个点不动的禁用键 —— 后者正是上一版被误当成 bug 的地方。
  $('btn-ws-pick').hidden = !shellPickFolder();
  $('ws-path').value = S.session && S.session.workspace ? S.session.workspace : '';
  // 浏览器从「当前工作区」起步：往上走到盘符根，再往下点进任意目录。
  await Promise.all([loadWorkspaceList(), browseDirs($('ws-path').value)]);
  $('ws-path').focus();
  $('ws-path').select();
}

/** 桌面壳提供的系统目录对话框；浏览器里返回 null。 */
function shellPickFolder() {
  const shell = window.vibeShell;
  return shell && typeof shell.pickFolder === 'function' ? shell.pickFolder.bind(shell) : null;
}

/** 从系统对话框挑一个目录，填进输入框 —— **不直接切换**：
    要不要重建上下文，由用户看清路径后自己按「切换并重启」决定。 */
async function pickWorkspaceFolder() {
  const pick = shellPickFolder();
  if (!pick) return;
  let picked = null;
  try {
    picked = await pick();
  } catch (err) {
    $('ws-error').hidden = false;
    $('ws-error').textContent = `打开目录对话框失败：${err.message}`;
    return;
  }
  if (!picked) return; // 用户取消
  $('ws-path').value = picked;
  $('ws-error').hidden = true;
  $('ws-path').focus();
  $('ws-path').select();
}

function closeWorkspaceModal() {
  $('ws-modal').hidden = true;
}

async function loadWorkspaceList() {
  const host = $('ws-list');
  host.replaceChildren(el('div', 'empty', '加载中…'));
  let items = [];
  try {
    const res = await api.get('/api/workspaces');
    items = res.workspaces || [];
  } catch (err) {
    host.replaceChildren(el('div', 'empty', `读取最近工作区失败：${err.message}`));
    return;
  }
  host.replaceChildren(
    ...items.map((item) => {
      const row = el('button', 'ws-item');
      row.type = 'button';
      if (item.current) row.classList.add('is-picked');
      row.appendChild(el('span', 'ws-path', item.path));
      row.appendChild(
        el('span', 'ws-time',
          item.current
            ? '当前工作区'
            : String(item.updatedAt || '').replace('T', ' '))
      );
      row.addEventListener('click', () => {
        $('ws-path').value = item.path;
        $('ws-error').hidden = true;
        host.querySelectorAll('.ws-item').forEach((n) => n.classList.remove('is-picked'));
        row.classList.add('is-picked');
      });
      return row;
    })
  );
}

/** 内置目录浏览器：列 `target` 的子目录；`target` 为空表示列盘符。
 *
 *  点击任意一行即「进入该目录」，同时把它填进 #ws-path —— 输入框与浏览器
 *  始终指向同一个目录，用户不必去想「我现在选的是哪个」。要不要真的重建
 *  上下文，仍由他自己按「切换并重启」决定。
 */
async function browseDirs(target) {
  const dirsHost = $('ws-dirs');
  dirsHost.replaceChildren(el('div', 'empty', '加载中…'));
  let data = null;
  try {
    data = await api.get('/api/fs/dirs', target ? { path: target } : {});
  } catch (err) {
    $('ws-cwd').textContent = '';
    S.browsePath = '';
    S.browseParent = '';
    $('btn-ws-up').disabled = true;
    dirsHost.replaceChildren(el('div', 'empty', `读取目录失败：${err.message}`));
    return;
  }

  const current = data.path || '';
  S.browsePath = current;
  S.browseParent = data.parent || '';
  $('ws-cwd').textContent = current || '选择磁盘';
  $('btn-ws-up').disabled = !current;

  const rows = [];
  if (current) {
    (data.dirs || []).forEach((dir) => rows.push(dirRow(dir.name, dir.path)));
    if (!rows.length) rows.push(el('div', 'empty', '这个目录下没有子目录'));
  } else {
    (data.roots || []).forEach((root) => rows.push(dirRow(root.name, root.path)));
    // 主目录是个高频起点，顺手给一个入口，省得从盘符一层层点下去。
    if (data.home) rows.push(dirRow('主目录', data.home));
  }
  dirsHost.replaceChildren(...rows);
}

/** 浏览器里的一行目录。 */
function dirRow(name, path) {
  const row = el('button', 'ws-dir');
  row.type = 'button';
  row.title = path;
  row.appendChild(el('span', 'ws-dir-name', name));
  row.addEventListener('click', () => {
    $('ws-path').value = path;
    $('ws-error').hidden = true;
    browseDirs(path);
  });
  return row;
}

/** 上一级：已到盘符根时 parent 为空，此时回盘符列表（路径框保持不变）。 */
async function browseUp() {
  if (!S.browsePath) return;
  if (S.browseParent) $('ws-path').value = S.browseParent;
  await browseDirs(S.browseParent || '');
}

async function switchWorkspace() {
  const path = $('ws-path').value.trim();
  if (!path) {
    $('ws-error').hidden = false;
    $('ws-error').textContent = '请选择或输入一个目录路径。';
    return;
  }
  const btn = $('btn-ws-open');
  btn.disabled = true;
  $('ws-error').hidden = true;
  try {
    const res = await api.post('/api/workspaces/open', { path });
    closeWorkspaceModal();
    if (res.switched === false) {
      toast('这已经是当前工作区。');
      return;
    }
    toast(`已切换到 ${shortPath(res.workspace)}，后端上下文已重启`, 'ok');
    // 界面主要由随后到达的 session.ready 事件刷新；这里主动刷一次兜底
    // （SSE 万一断着，也不能让用户对着旧文件树）。
    await refreshTree();
    await refreshChanges();
    await loadHistory();
  } catch (err) {
    $('ws-error').hidden = false;
    $('ws-error').textContent = err.message;
  } finally {
    btn.disabled = false;
  }
}

/* ---- 模型设置：改 model / baseUrl / apiKey（热生效、保留对话）+ 工作区 + 解释器 ----
   保存走 host 的 POST /api/settings：后端换掉 LLM 客户端引用并推一份新会话快照，
   界面据此刷新模型 chip —— 当前对话不受影响（后端不重建会话）。
   解释器（pythonPath / backendBin）属桌面壳专有：写 config.json 并重启应用，
   浏览器里没有那座桥（window.vibeShell.saveDesktopConfig），整段隐藏。 */
const PROVIDER_PRESETS = {
  ollama: { baseUrl: 'http://127.0.0.1:11434/v1', model: 'qwen2.5-coder:7b', apiKey: 'ollama' },
  openai: { baseUrl: 'https://api.openai.com/v1', model: 'gpt-4o-mini', apiKey: '' },
  deepseek: { baseUrl: 'https://api.deepseek.com/v1', model: 'deepseek-chat', apiKey: '' },
  custom: null, // 自定义：不动字段，让用户自己填
};
const PRESET_HINTS = {
  openai: 'OpenAI 需自备 API Key（留空则沿用当前密钥）',
  deepseek: 'DeepSeek 需自备 API Key（留空则沿用当前密钥）',
};

/** 桌面壳的配置桥；浏览器里返回 null（据此隐藏解释器段）。 */
function shellConfigBridge() {
  const shell = window.vibeShell;
  return shell && typeof shell.saveDesktopConfig === 'function' ? shell : null;
}

function setMsg(text, kind = '') {
  const node = $('set-msg');
  node.className = 'set-msg' + (kind ? ` is-${kind}` : '');
  node.textContent = text || '';
}

function lockField(id, isLocked) {
  const node = $(id);
  node.disabled = !!isLocked;
  if (isLocked) node.title = '该字段被环境变量锁定，界面修改不会生效';
  else node.removeAttribute('title');
}

/** 按当前接入地址高亮对应的服务商预设（识别不出来就落到「自定义」）。 */
function highlightPresetFor(baseUrl) {
  const url = String(baseUrl || '').toLowerCase();
  let match = 'custom';
  if (url.includes('11434')) match = 'ollama';
  else if (url.includes('api.openai.com')) match = 'openai';
  else if (url.includes('api.deepseek.com')) match = 'deepseek';
  document.querySelectorAll('#set-presets .set-preset').forEach((b) => {
    b.classList.toggle('is-active', b.dataset.preset === match);
  });
}

function applyProviderPreset(name) {
  document.querySelectorAll('#set-presets .set-preset').forEach((b) => {
    b.classList.toggle('is-active', b.dataset.preset === name);
  });
  const preset = PROVIDER_PRESETS[name];
  if (!preset) {
    setMsg('自定义：请自行填写接入地址与模型名');
    return;
  }
  if (!$('set-model').disabled) $('set-model').value = preset.model;
  if (!$('set-baseurl').disabled) $('set-baseurl').value = preset.baseUrl;
  if (preset.apiKey && !$('set-apikey').disabled) $('set-apikey').value = preset.apiKey;
  setMsg(PRESET_HINTS[name] || '');
}

function fillModelList(models) {
  const list = $('set-model-list');
  list.replaceChildren(
    ...(models || []).map((name) => {
      const opt = document.createElement('option');
      opt.value = name;
      return opt;
    })
  );
}

async function openSettingsModal() {
  $('settings-modal').hidden = false;
  $('set-error').hidden = true;
  setMsg('');
  // 解释器段只在桌面壳里出现（浏览器没有写 config.json 的桥）
  const bridge = shellConfigBridge();
  $('set-interp').hidden = !bridge;
  $('btn-set-restart').hidden = !bridge;
  $('btn-set-ws-pick').hidden = !shellPickFolder();
  try {
    const info = await api.get('/api/settings');
    $('set-path').textContent = info.settingsPath || '—';
    $('set-model').value = info.model || '';
    $('set-baseurl').value = info.baseUrl || '';
    $('set-apikey').value = '';
    $('set-apikey').placeholder = info.apiKeySet
      ? `留空保持不变（当前 ${info.apiKeyMasked}）`
      : '留空保持不变';
    $('set-workspace').value = '';
    $('set-workspace').placeholder = info.workspace
      ? `留空保持不变（当前 ${info.workspace}）`
      : '留空保持不变';
    const locked = info.envLocked || {};
    lockField('set-model', locked.model);
    lockField('set-baseurl', locked.base_url);
    lockField('set-apikey', locked.api_key);
    // 补全（FIM）参数回填：布尔用 checked，数值留空表示「保持当前」
    $('set-fim').checked = info.fimEnabled !== false;
    $('set-completion-tokens').value = info.completionMaxTokens ?? '';
    $('set-completion-prefix').value = info.completionPrefixChars ?? '';
    $('set-completion-suffix').value = info.completionSuffixChars ?? '';
    lockField('set-fim', locked.fim_enabled);
    lockField('set-completion-tokens', locked.completion_max_tokens);
    lockField('set-completion-prefix', locked.completion_prefix_chars);
    lockField('set-completion-suffix', locked.completion_suffix_chars);
    if (bridge) {
      try {
        const cfg = (await bridge.readDesktopConfig()) || {};
        $('set-python').value = cfg.pythonPath || '';
        $('set-backendbin').value = cfg.backendBin || '';
      } catch {
        /* 读不到就留空 —— 不影响模型字段的编辑 */
      }
    }
    highlightPresetFor(info.baseUrl);
    if (info.governance) {
      setMsg('治理面已接管模型接入，界面不支持修改模型配置', 'error');
    }
  } catch (err) {
    $('set-error').hidden = false;
    $('set-error').textContent = `读取设置失败：${err.message}`;
  }
  $('set-model').focus();
  $('set-model').select();
}

function closeSettingsModal() {
  $('settings-modal').hidden = true;
}

/** 桌面壳的系统目录对话框，结果填进设置浮层的工作区字段（不直接切换）。 */
async function pickSettingsWorkspace() {
  const pick = shellPickFolder();
  if (!pick) return;
  try {
    const picked = await pick();
    if (picked) {
      $('set-workspace').value = picked;
      $('set-error').hidden = true;
    }
  } catch (err) {
    setMsg(`打开目录对话框失败：${err.message}`, 'error');
  }
}

async function refreshModelList() {
  const btn = $('btn-set-models');
  btn.disabled = true;
  setMsg('拉取中…');
  try {
    const res = await api.post('/api/model/test', {
      baseUrl: $('set-baseurl').value.trim(),
      apiKey: $('set-apikey').value.trim(),
    });
    if (res.ok) fillModelList(res.models);
    setMsg(res.message || (res.ok ? '连接成功' : '连接失败'), res.ok ? 'ok' : 'error');
  } catch (err) {
    setMsg(`拉取失败：${err.message}`, 'error');
  } finally {
    btn.disabled = false;
  }
}

async function testModelConnection() {
  const btn = $('btn-set-test');
  btn.disabled = true;
  setMsg('测试中…');
  try {
    const res = await api.post('/api/model/test', {
      baseUrl: $('set-baseurl').value.trim(),
      apiKey: $('set-apikey').value.trim(),
    });
    if (res.ok && (res.models || []).length) fillModelList(res.models);
    setMsg(res.message || (res.ok ? '连接成功' : '连接失败'), res.ok ? 'ok' : 'error');
  } catch (err) {
    setMsg(`测试失败：${err.message}`, 'error');
  } finally {
    btn.disabled = false;
  }
}

async function saveSettings() {
  // 被环境变量锁定的字段不提交（后端也会照原样保留）
  const body = {
    model: $('set-model').disabled ? '' : $('set-model').value.trim(),
    baseUrl: $('set-baseurl').disabled ? '' : $('set-baseurl').value.trim(),
    apiKey: $('set-apikey').disabled ? '' : $('set-apikey').value.trim(),
    workspace: $('set-workspace').value.trim(),
  };
  // 补全（FIM）参数：置灰的字段跳过（被环境变量锁定），数值留空表示「不修改」。
  // 布尔按「是否可改」决定 —— 可改时始终提交当前勾选状态。
  const completionFields = [
    ['fimEnabled', $('set-fim'), false],
    ['completionMaxTokens', $('set-completion-tokens'), true],
    ['completionPrefixChars', $('set-completion-prefix'), true],
    ['completionSuffixChars', $('set-completion-suffix'), true],
  ];
  for (const [key, node, isNumber] of completionFields) {
    if (node.disabled) continue;
    if (!isNumber) { body[key] = node.checked; continue; }
    const raw = node.value.trim();
    if (!raw) continue;
    const parsed = Number(raw);
    if (!Number.isInteger(parsed) || parsed < 0) {
      $('set-error').hidden = false;
      $('set-error').textContent = '补全参数必须是非负整数。';
      return;
    }
    body[key] = parsed;
  }
  const hasCompletion = completionFields.some(([key]) => key in body);
  if (!body.model && !body.baseUrl && !body.apiKey && !body.workspace && !hasCompletion) {
    $('set-error').hidden = false;
    $('set-error').textContent = '没有需要修改的配置项。';
    return;
  }
  const btn = $('btn-set-save');
  btn.disabled = true;
  $('set-error').hidden = true;
  try {
    const res = await api.post('/api/settings', body);
    if (res.session) applySession(res.session);
    closeSettingsModal();
    toast(res.switched ? '模型设置已保存，工作区已切换' : '模型设置已保存并生效', 'ok');
    if (res.switched) {
      await refreshTree();
      await refreshChanges();
      await loadHistory();
    }
  } catch (err) {
    $('set-error').hidden = false;
    $('set-error').textContent = err.message;
  } finally {
    btn.disabled = false;
  }
}

async function saveInterpreterAndRestart() {
  const bridge = shellConfigBridge();
  if (!bridge) return;
  const patch = {
    pythonPath: $('set-python').value.trim(),
    backendBin: $('set-backendbin').value.trim(),
  };
  const btn = $('btn-set-restart');
  btn.disabled = true;
  try {
    const res = await bridge.saveDesktopConfig(patch);
    if (res && res.ok === false) throw new Error(res.error || '写入配置失败');
    toast('解释器设置已保存，正在重启应用…', 'ok');
    if (typeof bridge.relaunch === 'function') await bridge.relaunch();
  } catch (err) {
    $('set-error').hidden = false;
    $('set-error').textContent = `保存解释器失败：${err.message}`;
    btn.disabled = false;
  }
}

/* ---- 检查点 / 回滚（B1） ---- */
function addCheckpointBanner(checkpointId) {
  const banner = el('div', 'cp-banner');
  banner.appendChild(el('span', 'cp-text', '本轮改动已留检查点'));
  const btn = el('button', 'btn btn-small', '回滚');
  btn.title = '把工作区回滚到本轮改动之前（回滚本身可撤销）';
  btn.addEventListener('click', () => openCheckpoints(checkpointId));
  banner.appendChild(btn);
  stream.appendChild(banner);
  scrollToEnd();
}

let cpPick = null;

async function openCheckpoints(pickId = null) {
  if (S.running) {
    toast('任务执行中不能回滚，等任务结束或先中断', 'error');
    return;
  }
  $('cp-modal').hidden = false;
  cpPick = null;
  $('btn-cp-confirm').disabled = true;
  const host = $('cp-list');
  host.replaceChildren(el('div', 'empty', '加载中…'));
  try {
    const res = await api.get('/api/checkpoints');
    const items = (res.checkpoints || []).slice().reverse();
    if (!items.length) {
      host.replaceChildren(el('div', 'empty', '还没有检查点。Agent 改过文件之后这里会出现回滚点。'));
      return;
    }
    host.replaceChildren(
      ...items.map((cp) => {
        const row = el('button', 'cp-item');
        row.type = 'button';
        if (cp.kind === 'redo') row.classList.add('is-redo');
        row.appendChild(
          el('span', 'cp-time', String(cp.createdAt || '').replace('T', ' ') + (cp.kind === 'redo' ? ' · 回滚前快照' : ''))
        );
        row.appendChild(
          el('span', 'cp-files', `${cp.fileCount} 个文件：${(cp.files || []).slice(0, 3).join('、')}${cp.fileCount > 3 ? ' …' : ''}`)
        );
        row.addEventListener('click', () => {
          cpPick = cp;
          host.querySelectorAll('.cp-item').forEach((n) => n.classList.remove('is-picked'));
          row.classList.add('is-picked');
          $('btn-cp-confirm').disabled = false;
        });
        if (pickId && cp.id === pickId) row.click();
        return row;
      })
    );
  } catch (err) {
    host.replaceChildren(el('div', 'empty', `读取检查点失败：${err.message}`));
  }
}

function closeCheckpoints() {
  $('cp-modal').hidden = true;
  cpPick = null;
}

async function confirmRestore() {
  if (!cpPick) return;
  const target = cpPick;
  $('btn-cp-confirm').disabled = true;
  try {
    const res = await api.post(`/api/checkpoints/${encodeURIComponent(target.id)}/restore`, {});
    closeCheckpoints();
    addNotice(res.notice || `已回滚到检查点 ${target.id}`);
    refreshTree();
    refreshChanges();
    toast('回滚完成', 'ok');
  } catch (err) {
    toast(`回滚失败：${err.message}`, 'error');
    $('btn-cp-confirm').disabled = false;
  }
}

/* -------------------------------------------------------- 命令面板（F，P2-13）
   快捷键从「一串 if-else」收敛成一张键位表 KEYMAP：每条 {chord, id, label, when?, run}。
   keydown 只做两件事：查表 → 命中即 preventDefault + run；未命中直接 return（不吞键）。
   命令面板（Ctrl/Cmd+Shift+P）复用同一张表 + 若干动态项 —— 后段各阶段新增动作时，
   只需登记一条命令，不必再动那段越来越长的 keydown。

   chord 口径：Ctrl（含 Meta）+ 可选的 Shift + 归一化后的键名（字母转小写、Escape→Esc）。
   与改造前的旧实现逐字节对齐：旧代码对 Shift 不敏感的地方（Ctrl+K / Ctrl+S），
   这里同时登记 Ctrl+Shift+X 变体，保证行为不变。

   注意 KEYMAP 的**顺序即优先级**：Esc 的匹配顺序必须是
   「重命名条 → 工作区弹窗 → 回滚浮层 → 审批拒绝」，与改造前完全一致。 */
const KEYMAP = [
  // 侧栏面板（Ctrl/Cmd+1..6）
  { chord: 'Ctrl+1', id: 'panel.explorer', label: '打开：资源管理器', group: '面板', run: () => switchPanel('explorer') },
  { chord: 'Ctrl+2', id: 'panel.changes', label: '打开：改动', group: '面板', run: () => switchPanel('changes') },
  { chord: 'Ctrl+3', id: 'panel.tools', label: '打开：工具箱', group: '面板', run: () => switchPanel('tools') },
  { chord: 'Ctrl+4', id: 'panel.audit', label: '打开：审计', group: '面板', run: () => switchPanel('audit') },
  { chord: 'Ctrl+5', id: 'panel.history', label: '打开：历史会话', group: '面板', run: () => switchPanel('history') },
  { chord: 'Ctrl+6', id: 'panel.git', label: '打开：Git 面板', group: '面板', run: () => switchPanel('git') },
  { chord: 'Ctrl+7', id: 'panel.tests', label: '打开：测试面板', group: '面板', run: () => switchPanel('tests') },
  { chord: 'Ctrl+8', id: 'panel.memory', label: '打开：记忆面板', group: '面板', run: () => switchPanel('memory') },
  { chord: 'Ctrl+9', id: 'panel.mcp', label: '打开：MCP 面板', group: '面板', run: () => switchPanel('mcp') },
  { chord: 'Ctrl+`', id: 'panel.bottom', label: '收起/展开底部面板', group: '视图', run: () => toggleBottom($('bottom-panel').classList.contains('is-collapsed')) },
  // 编辑面：改写（Ctrl+K）与保存（Ctrl+S）。旧实现 Shift 不敏感 —— 两个变体都接住。
  { chord: 'Ctrl+k', id: 'edit.rewrite', label: '行内改写选区', group: '编辑', when: rewriteReady, run: () => openRewriteBar(S.editors.get(S.activeEditor)) },
  { chord: 'Ctrl+Shift+k', id: 'edit.rewrite', label: '行内改写选区', group: '编辑', when: rewriteReady, run: () => openRewriteBar(S.editors.get(S.activeEditor)) },
  { chord: 'Ctrl+s', id: 'edit.save', label: '保存当前文件', group: '编辑', when: saveReady, run: saveActiveFile },
  { chord: 'Ctrl+Shift+s', id: 'edit.save', label: '保存当前文件', group: '编辑', when: saveReady, run: saveActiveFile },
  // 语言服务器（E7）：只读视图下的跳转定义（F12）/ 重命名（F2）
  { chord: 'F12', id: 'lsp.definition', label: '跳转到定义', group: '语言', when: lspReady, run: () => gotoDefinition(S.editors.get(S.activeEditor)) },
  { chord: 'F2', id: 'lsp.rename', label: '重命名符号', group: '语言', when: lspReady, run: () => openRenameBar(S.editors.get(S.activeEditor)) },
  // Esc：优先级与改造前逐字节一致
  { chord: 'Esc', id: 'ui.close-rename', label: '关闭重命名条', palette: false, when: () => !$('rename-bar').hidden && !S.awaiting, run: closeRenameBar },
  { chord: 'Esc', id: 'ui.close-workspace', label: '关闭工作区弹窗', palette: false, when: () => !$('ws-modal').hidden, run: closeWorkspaceModal },
  { chord: 'Esc', id: 'ui.close-settings', label: '关闭模型设置', palette: false, when: () => !$('settings-modal').hidden, run: closeSettingsModal },
  { chord: 'Esc', id: 'ui.close-mcp', label: '关闭 MCP 浮层', palette: false, when: () => !$('mcp-modal').hidden, run: closeMcpModal },
  { chord: 'Esc', id: 'ui.close-checkpoints', label: '关闭回滚浮层', palette: false, when: () => !$('cp-modal').hidden && !S.awaiting, run: closeCheckpoints },
  { chord: 'Esc', id: 'approval.deny', label: '拒绝审批（默认动作）', palette: false, when: () => !!S.awaiting, run: () => decide('deny') },
  { chord: 'Ctrl+Enter', id: 'approval.approve', label: '批准审批', palette: false, when: () => !!S.awaiting, run: () => decide('approve') },
  // 命令面板自身
  { chord: 'Ctrl+Shift+p', id: 'palette.open', label: '命令面板', group: '视图', run: openPalette },
];

/* 命令面板里的「非快捷键动作」：与 KEYMAP 一起进列表，但没有键位。 */
const PALETTE_ACTIONS = [
  { id: 'settings.model', label: '模型设置…', group: '模型', run: openSettingsModal },
  { id: 'workspace.open', label: '切换工作区…', group: '工作区', run: openWorkspaceModal },
  { id: 'git.commit-message', label: '生成提交信息', group: 'Git', run: generateCommitMessage },
  { id: 'session.rollback', label: '回滚到检查点…', group: '会话', run: () => openCheckpoints() },
  { id: 'session.reset', label: '新建会话（清空对话）', group: '会话', run: reset },
  { id: 'session.interrupt', label: '中断当前任务', group: '会话', run: interrupt },
  { id: 'view.refresh-tree', label: '刷新文件树', group: '视图', run: refreshTree },
  { id: 'view.refresh-changes', label: '刷新改动', group: '视图', run: refreshChanges },
  { id: 'view.refresh-git', label: '刷新 Git 状态', group: '视图', run: refreshGit },
];

const PALETTE_GROUPS = ['面板', '视图', '编辑', '语言', '模型', '工作区', 'Git', '会话'];
const PALETTE_GROUP_FALLBACK = PALETTE_GROUPS.length;
const palette = { items: [], filtered: [], index: 0, open: false };

function activeFileEntry() {
  const entry = S.editors.get(S.activeEditor);
  return entry && entry.kind === 'file' ? entry : null;
}
function rewriteReady(event) {
  const entry = S.editors.get(S.activeEditor);
  return !!entry && !!entry.editing && !$('rewrite-bar').contains(event.target);
}
function saveReady() {
  const entry = S.editors.get(S.activeEditor);
  return !!entry && !!entry.editing;
}
function lspReady(event) {
  const entry = activeFileEntry();
  return !isTextField(event.target) && !!entry && !entry.editing;
}

/* 把一次 keydown 归一成 chord 串。Ctrl 与 Meta 视为同一修饰键（跨平台）。 */
function chordOf(event) {
  const parts = [];
  if (event.ctrlKey || event.metaKey) parts.push('Ctrl');
  if (event.shiftKey) parts.push('Shift');
  let key = event.key;
  if (key && key.length === 1 && /[a-z]/i.test(key)) key = key.toLowerCase();
  else if (key === 'Escape') key = 'Esc';
  parts.push(key);
  return parts.join('+');
}

/* 查表：返回第一条 chord 命中且 when 通过的项；没有则 null（调用方据此不吞键）。 */
function matchCommand(event) {
  const chord = chordOf(event);
  for (const item of KEYMAP) {
    if (item.chord !== chord) continue;
    if (item.when && !item.when(event)) continue;
    return item;
  }
  return null;
}

/* 命令清单 = KEYMAP 动作（去重，Esc 等纯语境键不进面板）∪ 动态动作。 */
function buildCommandList() {
  const out = [];
  const seen = new Set();
  const push = (item) => {
    if (seen.has(item.id)) return;
    seen.add(item.id);
    out.push(item);
  };
  KEYMAP.forEach((item) => { if (item.palette !== false) push(item); });
  PALETTE_ACTIONS.forEach(push);
  const rank = (group) => {
    const i = PALETTE_GROUPS.indexOf(group);
    return i < 0 ? PALETTE_GROUP_FALLBACK : i;
  };
  return out.sort((a, b) => rank(a.group) - rank(b.group));
}

/* 子序列模糊匹配：query 的字符按序出现在 text 里即命中（不引第三方库）。 */
function fuzzyMatch(query, text) {
  const q = String(query).toLowerCase();
  if (!q) return true;
  const t = String(text).toLowerCase();
  let i = 0;
  for (let k = 0; k < t.length && i < q.length; k++) {
    if (t[k] === q[i]) i++;
  }
  return i === q.length;
}

function filterCommands(query) {
  const q = String(query || '').trim();
  return palette.items.filter((c) => fuzzyMatch(q, c.label) || fuzzyMatch(q, c.id));
}

function displayChord(chord) {
  return chord.split('+').map((p) => (p.length === 1 && /[a-z]/.test(p) ? p.toUpperCase() : p)).join('+');
}

function openPalette() {
  palette.items = buildCommandList();
  palette.filtered = palette.items;
  palette.index = 0;
  palette.open = true;
  const input = $('palette-input');
  input.value = '';
  $('palette').hidden = false;
  renderPalette();
  input.focus();
}

function closePalette() {
  palette.open = false;
  $('palette').hidden = true;
}

function paletteMove(step) {
  const n = palette.filtered.length;
  if (!n) return;
  palette.index = (palette.index + step + n) % n;
  renderPalette();
}

function renderPalette() {
  const list = $('palette-list');
  list.replaceChildren();
  if (!palette.filtered.length) {
    list.append(el('div', 'palette-empty', '没有匹配的命令'));
    return;
  }
  let lastGroup = null;
  palette.filtered.forEach((item, index) => {
    if (item.group && item.group !== lastGroup) {
      lastGroup = item.group;
      list.append(el('div', 'palette-group', item.group));
    }
    const row = el('div', 'palette-item' + (index === palette.index ? ' is-active' : ''));
    row.append(el('span', 'palette-label', item.label));
    if (item.chord) row.append(el('span', 'palette-chord', displayChord(item.chord)));
    row.addEventListener('mousedown', (event) => {
      event.preventDefault();   // 不让输入框先失焦
      runCommand(index);
    });
    list.append(row);
  });
  const active = list.querySelector('.palette-item.is-active');
  if (active) active.scrollIntoView({ block: 'nearest' });
}

/* 执行前先关浮层：动作里可能又要开别的浮层（如「切换工作区…」）。 */
function runCommand(index) {
  const item = palette.filtered[index];
  if (!item) return;
  closePalette();
  item.run();
}

/* ------------------------------------------------------------------ 拖拽分栏 */
/* resolve() 在「按下的那一刻」才求值，因此同一根把手在两种布局下能给出
   不同的目标与方向语义（IDE 里调对话宽度，SOLO 里调编辑器宽度）。 */
function bindResizer(handleId, resolve) {
  const handle = $(handleId);
  handle.addEventListener('mousedown', (event) => {
    event.preventDefault();
    const { id, min = 170, max = 560, dir = 1 } = resolve();
    const target = $(id);
    const startX = event.clientX;
    const startWidth = target.getBoundingClientRect().width;
    const move = (e) => {
      const width = Math.min(max, Math.max(min, startWidth + (e.clientX - startX) * dir));
      target.style.width = `${width}px`;
    };
    const up = () => {
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
    };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
  });
}

/* ------------------------------------------------------------------ 布局模式 */
const MODE_KEY = 'vibe.layout-mode';

function isSolo() {
  return document.body.dataset.mode === 'solo';
}

/* 只有「打开了真实文件/diff 标签」时 SOLO 才夹出编辑器区 */
function updateLayout() {
  document.body.classList.toggle('has-open-tab', S.editors.size > 0);
}

function setMode(mode, { persist = true } = {}) {
  const next = mode === 'solo' ? 'solo' : 'ide';
  if (document.body.dataset.mode === next) return;
  document.body.dataset.mode = next;
  document.querySelectorAll('.mode-btn').forEach((b) => b.classList.toggle('is-active', b.dataset.mode === next));
  // 两种模式各管一套列宽：切过去时清掉上一种模式留下的内联宽度，避免串味
  for (const id of ['chatpane', 'editor-stack', 'sidebar']) $(id).style.width = '';
  updateLayout();
  if (persist) {
    try { localStorage.setItem(MODE_KEY, next); } catch { /* 隐私模式下忽略 */ }
  }
}

function restoreMode() {
  let saved = null;
  try { saved = localStorage.getItem(MODE_KEY); } catch { /* 隐私模式下忽略 */ }
  setMode(saved === 'solo' ? 'solo' : 'ide', { persist: false });
  updateLayout();
}

/* ------------------------------------------------------------------ 绑定 */
function bind() {
  const input = $('input');

  input.addEventListener('input', () => {
    autoGrow();
    syncSendState();
    // `@` 引用（P0-A）：光标落在 `@词` 上就开浮层，否则关掉
    const ctx = mentionContext();
    if (!ctx) {
      if (mention.open) closeMention();
      return;
    }
    const fresh = !mention.open;
    mention.open = true;
    mention.start = ctx.start;
    mention.q = ctx.q;
    if (fresh) {
      mention.type = 'file';
      mention.index = 0;
      mention.items = [];
    }
    $('mention-pop').hidden = false;
    renderMention();
    scheduleMention();
  });
  input.addEventListener('keydown', (event) => {
    if (mention.open) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (mention.items.length) {
          const step = event.key === 'ArrowDown' ? 1 : -1;
          mention.index = (mention.index + step + mention.items.length) % mention.items.length;
          renderMention();
        }
        return;
      }
      if ((event.key === 'Enter' && !event.shiftKey) || event.key === 'Tab') {
        if (mention.items.length) {
          event.preventDefault();
          acceptMention();
          return;
        }
        closeMention();   // 没有候选就别卡住发送
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();   // 别让全局 Esc 顺手拒掉正在等的审批
        closeMention();
        return;
      }
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  });
  // 失焦就收浮层。浮层内部的 mousedown 已 preventDefault，所以点它不会触发这条
  input.addEventListener('blur', () => { if (mention.open) closeMention(); });

  // 多模态输入（P1-10）：直接粘贴图片 → 上传成 image 引用。
  // 关掉时**不拦截**：让浏览器按默认行为处理（至少不会把图片静默吞掉）。
  input.addEventListener('paste', (event) => {
    if (!S.visionOn) return;
    const files = event.clipboardData ? Array.from(event.clipboardData.files || []) : [];
    const images = files.filter((f) => /^image\//i.test(f.type || ''));
    if (!images.length) return;
    event.preventDefault();
    images.forEach((f) => { uploadImageFile(f); });
  });
  $('btn-image').addEventListener('click', () => $('image-input').click());
  $('image-input').addEventListener('change', (event) => {
    const files = Array.from(event.target.files || []);
    files.forEach((f) => { uploadImageFile(f); });
    event.target.value = '';   // 复位，同一文件可再选
  });

  const pop = $('mention-pop');
  pop.addEventListener('mousedown', (event) => {
    event.preventDefault();   // 不让输入框失焦，否则 blur 会抢先把浮层关掉
    const tab = event.target.closest('.mention-tab');
    if (tab) {
      setMentionType(tab.dataset.type);
      return;
    }
    const row = event.target.closest('.mention-item');
    if (row) acceptMention(Number(row.dataset.idx));
  });

  $('btn-send').addEventListener('click', send);
  $('btn-interrupt').addEventListener('click', interrupt);
  $('btn-reset').addEventListener('click', reset);
  $('btn-rollback').addEventListener('click', () => openCheckpoints());
  $('btn-tree-refresh').addEventListener('click', refreshTree);

  // 切换工作区（W1）：标题栏的工作区名就是入口
  $('tb-workspace').addEventListener('click', openWorkspaceModal);
  $('btn-ws-close').addEventListener('click', closeWorkspaceModal);
  $('btn-ws-open').addEventListener('click', switchWorkspace);
  $('btn-ws-pick').addEventListener('click', pickWorkspaceFolder);
  $('btn-ws-up').addEventListener('click', browseUp);
  $('ws-modal').addEventListener('click', (event) => {
    if (event.target === $('ws-modal')) closeWorkspaceModal();
  });
  $('ws-path').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      switchWorkspace();
    }
  });

  // 模型设置：标题栏的模型 chip 就是入口（键盘可达：Enter / Space）
  $('chip-model').addEventListener('click', openSettingsModal);
  $('chip-model').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      openSettingsModal();
    }
  });
  $('btn-set-close').addEventListener('click', closeSettingsModal);
  $('btn-set-save').addEventListener('click', saveSettings);
  $('btn-set-test').addEventListener('click', testModelConnection);
  $('btn-set-models').addEventListener('click', refreshModelList);
  $('btn-set-ws-pick').addEventListener('click', pickSettingsWorkspace);
  $('btn-set-restart').addEventListener('click', saveInterpreterAndRestart);
  // 服务商预设：事件委托（浮层里是动态按钮组，逐个绑会随 DOM 变化失效）
  $('set-presets').addEventListener('click', (event) => {
    const btn = event.target.closest('.set-preset');
    if (btn) applyProviderPreset(btn.dataset.preset);
  });
  $('settings-modal').addEventListener('click', (event) => {
    if (event.target === $('settings-modal')) closeSettingsModal();
  });
  // 浮层内回车保存（与工作区弹窗同一套交互）
  $('settings-modal').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && event.target.tagName !== 'BUTTON') {
      event.preventDefault();
      saveSettings();
    }
  });

  // 活动栏
  document.querySelectorAll('.act').forEach((a) => {
    a.addEventListener('click', () => switchPanel(a.dataset.panel));
  });

  // 布局模式（IDE / SOLO）—— 只认标题栏那一组；
  // 改动面板里的 .mode-btn（data-cmode）是另一组，不能被这条绑到
  document.querySelectorAll('#mode-switch .mode-btn').forEach((b) => {
    b.addEventListener('click', () => setMode(b.dataset.mode));
  });

  // 改动面板视图（本会话 / 相对 Git，P0-C）
  document.querySelectorAll('#changes-mode .mode-btn').forEach((b) => {
    b.addEventListener('click', () => setChangesMode(b.dataset.cmode));
  });

  // Git 面板（P0-C）：起草提交信息 / 刷新 / 复制
  $('btn-commit-msg').addEventListener('click', generateCommitMessage);
  $('btn-git-refresh').addEventListener('click', refreshGit);
  $('btn-copy-msg').addEventListener('click', copyCommitMessage);

  // 测试面板（K/P2-14）：跑全部 / 只跑失败 / 重新收集
  $('btn-tests-run-all').addEventListener('click', runAllTests);
  $('btn-tests-run-failed').addEventListener('click', runFailedTests);
  $('btn-tests-refresh').addEventListener('click', () => refreshTestsTree(true));

  // 记忆面板（P2-15）：用户本人写入（模型侧写入另走 remember 工具的审批闸门）
  $('btn-mem-add').addEventListener('click', addMemoryFromPanel);

  // MCP 面板（L 管理面）：开关 / 新增 / 重载 / 浮层按钮（写入只落配置文件）
  $('mcp-enabled').addEventListener('change', (event) => toggleMcpEnabled(event.target.checked));
  $('btn-mcp-add').addEventListener('click', () => openMcpModal(null));
  $('btn-mcp-reload').addEventListener('click', reloadMcpServers);
  $('btn-mcp-cancel').addEventListener('click', closeMcpModal);
  $('btn-mcp-save').addEventListener('click', saveMcpServer);
  $('mcp-f-transport').addEventListener('change', syncMcpTransportFields);
  $('mcp-modal').addEventListener('click', (event) => {
    if (event.target === $('mcp-modal')) closeMcpModal();
  });
  // 浮层内回车保存（文本域保留换行，不触发保存）
  $('mcp-modal').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && event.target.tagName !== 'BUTTON' && event.target.tagName !== 'TEXTAREA') {
      event.preventDefault();
      saveMcpServer();
    }
  });

  // 编辑器工具条
  $('btn-md-toggle').addEventListener('click', toggleMarkdownMode);
  // 网页预览（O/P1-11）：对当前打开的网页文件开一个 preview 标签
  $('btn-preview').addEventListener('click', () => {
    const entry = S.editors.get(S.activeEditor);
    if (entry && entry.kind === 'file' && WEB_EXT.test(entry.data.path)) openPreview(entry.data.path);
  });
  // 编辑面（D）：编辑模式开关 / 保存 / 改写条
  $('btn-edit-toggle').addEventListener('click', toggleEditMode);
  $('btn-save').addEventListener('click', saveActiveFile);
  $('btn-rewrite-run').addEventListener('click', runRewrite);
  $('btn-rewrite-apply').addEventListener('click', applyRewrite);
  $('btn-rewrite-close').addEventListener('click', closeRewriteBar);
  $('rewrite-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      runRewrite();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closeRewriteBar();
    }
  });
  // 重命名条（E7）：与改写条同一套交互
  $('btn-rename-run').addEventListener('click', runRenamePreview);
  $('btn-rename-apply').addEventListener('click', applyRename);
  $('btn-rename-close').addEventListener('click', closeRenameBar);
  $('rename-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      runRenamePreview();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closeRenameBar();
    }
  });

  // 底部面板
  document.querySelectorAll('.bp-tab').forEach((t) => {
    t.addEventListener('click', () => switchBottom(t.dataset.bp));
  });
  $('btn-bottom-toggle').addEventListener('click', () => {
    toggleBottom($('bottom-panel').classList.contains('is-collapsed'));
  });
  $('btn-term-clear').addEventListener('click', () => {
    const active = document.querySelector('.bp-tab.is-active').dataset.bp;
    // 「输出」面板是本会话的日志，直接清 DOM；「终端」是交互终端，清的是本地回滚缓冲。
    if (active === 'term') { termReset(); termRender(); } else { $('output').replaceChildren(); }
  });

  // 交互式终端（P1-7）：入口按钮 + 键盘 / 焦点 / 粘贴 + 尺寸自适应
  $('btn-term-open').addEventListener('click', () => termOpen());
  $('btn-term-close').addEventListener('click', () => termClose());
  const termEl = $('term');
  termEl.addEventListener('keydown', termKeydown);
  termEl.addEventListener('mousedown', () => termEl.focus());
  termEl.addEventListener('paste', termPaste);
  termEl.addEventListener('focus', () => { if (!term.open && !term.closed) termOpen(); });
  if (typeof ResizeObserver === 'function') new ResizeObserver(() => termResize()).observe(termEl);

  // 分栏拖拽（方向/目标随布局模式变化，见 bindResizer 注释）
  bindResizer('sb-resizer', () => isSolo()
    // SOLO：文件树在最右，把手在它左侧 —— 往右拖是「把树拖窄」
    ? { id: 'sidebar', min: 170, max: 480, dir: -1 }
    : { id: 'sidebar', min: 170, max: 480, dir: 1 });
  bindResizer('chat-resizer', () => isSolo()
    // SOLO：把手夹在「对话 | 编辑器」之间，调的是中间编辑器列的宽度
    ? { id: 'editor-stack', min: 320, max: 1200, dir: 1 }
    : { id: 'chatpane', min: 280, max: 640, dir: -1 });

  // 回滚浮层
  $('btn-cp-close').addEventListener('click', closeCheckpoints);
  $('btn-cp-confirm').addEventListener('click', confirmRestore);
  $('cp-modal').addEventListener('click', (event) => {
    if (event.target === $('cp-modal')) closeCheckpoints();
  });

  // 审批
  $('btn-approve').addEventListener('click', () => decide('approve'));
  $('btn-approve-all').addEventListener('click', () => decide('approve_all'));
  $('btn-deny').addEventListener('click', () => decide('deny'));
  $('btn-deny-all').addEventListener('click', () => decide('deny_all'));
  $('btn-minimize').addEventListener('click', minimizeApproval);
  $('btn-goto-approval').addEventListener('click', () => {
    $('approval-banner').hidden = true;
    $('modal').hidden = false;
  });
  $('btn-chat-clear').addEventListener('click', () => {
    stream.replaceChildren(el('div', 'empty', '（仅清除了视图；对话历史仍在后端）'));
  });
  $('plan-head').addEventListener('click', togglePlanCollapsed);

  // 快捷键：查 KEYMAP（见下方「命令面板」段）。命中即 preventDefault + run；未命中不吞键。
  // 编辑面快捷键挂在 document 而非 textarea 上：Ctrl+K/Ctrl+S 的默认行为
  // （浏览器保存页、部分输入法的组合键）必须在冒泡到 document 时被拦掉。
  document.addEventListener('keydown', (event) => {
    const command = matchCommand(event);
    if (!command) return;
    event.preventDefault();
    command.run(event);
  });

  // 命令面板（F，P2-13）：入口按钮 + 输入框交互
  $('btn-palette').addEventListener('click', openPalette);
  $('palette').addEventListener('click', (event) => {
    if (event.target === $('palette')) closePalette();
  });
  const paletteInput = $('palette-input');
  paletteInput.addEventListener('input', () => {
    palette.filtered = filterCommands(paletteInput.value);
    palette.index = 0;
    renderPalette();
  });
  paletteInput.addEventListener('keydown', (event) => {
    // 审批态优先：这时按键交给 document 的 KEYMAP（Esc 拒绝 / Ctrl+Enter 批准），面板不吞
    if (S.awaiting) return;
    if ((event.ctrlKey || event.metaKey) && event.shiftKey && (event.key === 'p' || event.key === 'P')) {
      event.preventDefault();
      event.stopPropagation();
      closePalette();    // 面板开着时再按一次 = 收起
      return;
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      paletteMove(1);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      paletteMove(-1);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      runCommand(palette.index);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();   // 别让全局 Esc 顺手拒掉正在等的审批
      closePalette();
    }
  });

  document.querySelectorAll('.hint').forEach((hint) => {
    hint.addEventListener('click', () => {
      input.value = hint.dataset.fill;
      autoGrow();
      $('btn-send').disabled = false;
      input.focus();
    });
  });
}

/* ------------------------------------------------------------------ 启动 */
async function boot() {
  restoreMode();
  bind();
  setState('idle');
  switchPanel('explorer');
  switchBottom('output');
  activateEditor('welcome');

  try {
    const state = await api.get('/api/state');
    applySession(state);
  } catch (err) {
    toast(`无法读取会话状态：${err.message}`, 'error');
    $('chip-state').textContent = '连接失败';
  }

  await refreshTree();
  await refreshChanges();
  // 刷新页面后把最近一次测试结果补回来（后端内存态），否则失败计数徽标会是空的
  await loadTestResults();
  connect();
}

boot();
