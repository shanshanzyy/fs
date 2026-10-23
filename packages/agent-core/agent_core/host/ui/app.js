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
import { render, isMarkdown, highlight, langForPath } from './markdown.js';

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
  if (!res.ok) {
    // 状态码跟着错误一起抛：远程工作面要区分「远端已被改过」（409，可重加载）
    // 与其它失败（400/403/502），只能靠它。err.message 仍是后端给的 error 文本。
    const err = new Error(data.error || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

/* 本页面开出去的远端转发（P3）。标签关闭时逐条释放；页面卸载时整批释放。
   为什么需要这个集合：`closeEditor` 只在「点 ✕」时执行，而**刷新页面**会直接
   销毁 JS 上下文，任何 onClose 都不会跑。不补这一手，转发会一直活到会话结束 ——
   用户看不到它，却会撞上「最多 N 条转发」的上限，而且界面上没有任何入口能收掉。 */
const OPEN_TUNNELS = new Set();

window.addEventListener('pagehide', () => {
  for (const id of OPEN_TUNNELS) {
    try {
      // keepalive：卸载后请求仍会发出（普通 fetch 会被随后的销毁取消）。
      fetch('/api/preview/tunnel/close', {
        method: 'POST',
        keepalive: true,
        headers: { 'Content-Type': 'application/json', 'X-Vibe-Token': TOKEN },
        body: JSON.stringify({ id }),
      }).catch(() => { /* 页面正在卸载，失败无从处置也无所谓 */ });
    } catch (err) {
      /* 同上：收尾阶段不再制造噪音 */
    }
  }
  OPEN_TUNNELS.clear();
});

/* ------------------------------------------------------------------ 状态 */
const S = {
  session: null,
  running: false,
  // 已请求中断、尚未收到结束事件（交互增强一）：暂停按钮置灰防重复点击。
  // 结束事件（run.finished / run.failed / run.interrupted）与 session.ready 必须复位，
  // 否则按钮会永久卡在「停止中…」。
  interrupting: false,
  awaiting: null,
  lastEventId: 0,
  booted: false,
  calls: new Map(),        // callId -> 聊天流里的工具卡片条目
  changed: new Set(),
  created: new Set(),
  treeNodes: new Map(),    // path -> 树节点元素
  streaming: null,         // 流式回复卡片
  reasoning: null,         // 思考流卡片（reasoning_content 实时滚动）
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
  treePayload: null,       // 最近一次 /api/tree 的结果（远端根展开时重渲染要用）
  // 已加入工作区的远端根（M12）：GET /api/remote/roots 的 roots。
  // 工作区树把它们渲染成本地根之后的顶级节点，右键「加入 / 移出工作区」改的就是它。
  workspaceRoots: [],
  wsRootCache: new Map(),  // `${connection}\0${相对 conn.root 的路径}` -> {entries} | {error}
  wsRootExpanded: new Set(), // 已展开的工作区树远端节点（同一把键）
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
  update: null,            // 客户端更新（M9）：最近一次 check 视图 + 事件增量，驱动 chip 与弹窗
  skills: [],              // 已加载的 Skill 工作流（M9-A）：[{name, description}]，空 = 未启用
  tasks: [],               // 后台任务（M9-D）：[{taskId, state, summary, chars}]，随 task.state 更新
  schedules: [],           // 定时任务（M10）：[{jobId, name, schedule, enabled, nextRunAt, …}]
  scheduleStatus: null,    // 调度器状态（M10）：{enabled, tick, maxPerTick, maxJobs, catchUp, jobCount, …}
  // 远程设备文件工作面（M11-E）：只镜像后端权威视图，界面不自己推断。
  remote: {
    enabled: false,
    connections: [],       // GET /api/remote/connections 的 connections
    configPath: '',
    default: '',           // 默认连接名
    active: '',            // 当前打开（活动）的连接名
    ssh: null,             // 本机 ssh 可用性 {available, path}
    state: null,           // 最近一次 remote.state / GET state 的载荷
    browsePath: '',        // 当前浏览路径（界面面包屑，'' = 根）
    currentPath: '',       // 右栏当前打开文件的相对路径（'' = 没打开文件）
    currentFile: null,     // {text, digest, encoding, newline, bom, size, lines}
    dirty: false,          // 编辑文本域内容是否已偏离打开时的内容
    stale: false,          // 是否收到了针对该文件的 remote.edit（远端已改）
    changes: [],           // GET /api/remote/changes 的 changes
    treeCache: new Map(),  // 已展开目录 path -> entries
    treeDegraded: new Set(), // 树接口报出来的能力降级项（与 state 的合并展示）
    expanded: new Set(),   // 已展开目录 path（含 '' = 根）
    tests: new Map(),      // 连接名 -> 最近一次 test 结果
    editing: '',           // 表单正在编辑的连接名（'' = 新增）
    rtab: 'view',          // 右栏当前页签
    node: null,            // #remote-view 节点缓存：关页签只摘出 DOM，重开要原位放回
    prevEditor: '',        // 打开远程设备之前的活动页签（关闭时回到它）
    degradedOpen: false,   // 能力降级摘要是否展开
  },
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
/** 顶栏的工作区标签：远程工作区时**必须让人一眼看出代码不在这台机器上**。
 *
 *  这里改的只有标签与 title：文件是否允许读写由远端自己的 allow_write 决定，
 *  而本机 Git / 诊断这类能力的降级原因由后端 `remoteDegraded` 流到各自的面板
 *  （Git 面板本来就有 `available:false + reason` 的关闭态），前端不再做第二份判断。
 *
 *  `remoteWorkspace` 缺省时（本机模式 / 未启用）标签与改造前逐字一致。
 */
function applyWorkspaceLabel(info) {
  const chip = $('tb-workspace');
  const remote = info.remoteWorkspace;
  if (!remote) {
    chip.textContent = shortPath(info.workspace);
    chip.title = info.workspace;
    chip.classList.remove('is-remote');
    return;
  }
  const conn = info.remoteConnection || '';
  const sub = info.remoteSub || '';
  chip.textContent = `远端 ${conn}:${sub || '/'}`;
  chip.classList.add('is-remote');
  const lines = [
    `当前工作区在远端设备 ${conn} 的 /${sub || ''} 上 —— 代码不在本机。`,
    `本机锚点（只放审计链与回滚留底）：${info.workspace}`,
  ];
  if (info.remoteError) lines.push(`连接异常：${info.remoteError}`);
  chip.title = lines.join('\n');
}

function applySession(info) {
  S.session = info;
  applyWorkspaceLabel(info);
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
  // Skill 提示（M9-A）：VIBE_SKILLS=1 且加载了工作流时显示「N 个工作流」，
  // 否则恒 hidden（与 chip-vibe 同一纪律：不配就不占标题栏）。
  const skillChip = $('chip-skill');
  const skillNames = (info.skills || []).map((s) => s.name);
  if (skillNames.length) {
    skillChip.hidden = false;
    skillChip.textContent = `${skillNames.length} 个工作流`;
    skillChip.title = `已加载工作流：${skillNames.join('、')}（命令面板可运行）`;
  } else {
    skillChip.hidden = true;
  }
  // 恢复历史会话 / 重连时清零「停止中」标记：那是上一段运行态，带到新会话里
  // 只会让暂停按钮永久卡灰（交互增强一）。
  S.interrupting = false;
  setState(info.state || 'idle');
  $('welcome-foot').textContent = `模型 ${info.model} · ${info.baseUrl} · 审计 ${info.auditPath}`;
  renderTools(info.tools || []);
  // 计划（P2-12）：快照是权威来源 —— 刷新页面 / 断线重连后计划卡片照旧在
  applyPlan(info.plan || null);
  // 上下文用量（P2-17）：同一份快照，首帧不必额外发一次请求
  applyContext(info.context || null);
  // 交互式终端（P1-7）：配置关闭（VIBE_TERM=0）时隐藏入口 —— 不留一个点了必报错的按钮
  const termOn = info.config ? info.config.terminalEnabled !== false : true;
  const termWasOn = termEnabled;
  termEnabled = termOn;
  const termTab = document.querySelector('.bp-tab[data-bp="terminal"]');
  if (termTab) termTab.hidden = !termOn;
  if (termOn) {
    // 跟后端对账（幂等）：刷新页面后把还活着的终端接回来，同时撤掉已经不在的标签。
    // 从关到开也要走一次 —— 那通常意味着换了配置或连了另一套后端。
    loadTermSessions();
  }
  if (termOn && !termWasOn) termNote('点「新建终端」开始；可同时开多个，各自独立');
  if (!termOn) {
    // 配置关掉时不能只藏入口：已经跑着的终端进程要收掉，否则用户连关闭的按钮都看不到。
    if (TERMS.size) termCloseAll();
    termNote('交互式终端已被配置关闭（VIBE_TERM=0）');
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
  // Skill（M9-A）：VIBE_SKILLS=0 时清空列表（命令面板不出现 Skill 动作）。
  // 列表只存 name + description，正文与来源不出机（后端 describe 已裁掉）。
  S.skills = info.skills || [];
  // 后台任务（M9-D）：VIBE_ASYNC_AGENT=0 时隐藏「任务」页签入口。
  const asyncOn = info.config ? info.config.asyncAgentEnabled !== false : false;
  const taskAct = document.querySelector('.act[data-panel="tasks"]');
  if (taskAct) taskAct.hidden = !asyncOn;
  if (!asyncOn && S.activePanel === 'tasks') switchPanel('explorer');
  // 定时任务（M10）：VIBE_SCHEDULER=0（或未开后台 Agent）时隐藏「定时任务」页签入口。
  const schedOn = info.config ? info.config.schedulerEnabled === true : false;
  const schedAct = document.querySelector('.act[data-panel="schedules"]');
  if (schedAct) schedAct.hidden = !schedOn;
  if (!schedOn && S.activePanel === 'schedules') switchPanel('explorer');
  // 远程设备文件工作面（M11）：VIBE_REMOTE=0 时隐藏「远程设备」入口（连按钮都不渲染）。
  // 未开启的部署不该看到一个点进去只有空壳的面板 —— 与终端/定时任务同一纪律。
  const remoteOn = info.config ? info.config.remoteEnabled === true : false;
  S.remote.enabled = remoteOn;
  const remoteAct = document.querySelector('.act[data-panel="remote"]');
  if (remoteAct) remoteAct.hidden = !remoteOn;
  if (!remoteOn && S.activePanel === 'remote') switchPanel('explorer');
  // 切换工作区弹层的「远程目录」页签（M13）同理：未启用时连页签都不渲染。
  // 点进去只能看到「未启用」的拒绝 —— 那不是入口，只是一次徒劳的点击。
  const wsRemoteTab = $('ws-tab-remote');
  if (wsRemoteTab) wsRemoteTab.hidden = !remoteOn;
  if (info.pendingApproval && !S.awaiting) openApproval(info.pendingApproval);
  if (info.notices?.length) {
    for (const text of info.notices) { addNotice(text); termLine('output', text, 'is-dim'); }
  }
  applyGovernanceChip(info.governance);
  // 客户端更新（M9）：快照是权威来源 —— 重连 / 刷新后不必先发一次 check 就能恢复 chip。
  // 只在字段存在时才覆盖：治理面登录回包等场景可能不带 update 段，别把已有结论清掉。
  if (info.update !== undefined) S.update = info.update;
  applyUpdateChip(S.update);
  // 启动登录门（强制登录）：未接入治理面时遮住工作区，登录后才放行
  renderLoginGate(info.governance);
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
  // 「去登录」入口：已接入治理面但未登录/降级时，界面给一个登录入口。
  // M3+ 起改为在应用内打开登录弹窗（登录 / 邀请码注册 / 退出闭环），
  // 不再强制跳到浏览器。`loginPageUrl` 仍保留 —— 弹窗里用它显示可外开的地址，
  // 且浏览器直开 host 时弹窗同样可用（走本机 /api/governance/* 端点）。
  const loginUrl = loginPageUrl(governance);
  if (status === 'unauthenticated' || status === 'degraded') {
    const link = document.createElement('a');
    link.className = 'chip-link';
    link.href = loginUrl || '#';
    link.rel = 'noopener noreferrer';
    link.textContent = '去登录';
    link.title = loginUrl
      ? `登录治理面（应用内弹窗；服务端登录页 ${loginUrl}）`
      : '登录治理面';
    link.addEventListener('click', (event) => {
      event.preventDefault();
      openGovModal();
    });
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

/* ═══════════════════════════════ 客户端自动更新（M9） ═══════════════════════════════
 * 内核已把「双通道检查（治理面 S1 → 本地镜像 S2）+ 逐源 RSA 验签 + sha256 校验 +
 * 原子落盘 + 反降级 seq」做完；界面只做三件事：
 *   1) 把结论画成 `#chip-update`；2) 把待安装包路径交给桌面壳；3) 把下载进度如实显示。
 * 视图有**两处来源**，归一成同一份：会话快照里的 `update` 段（重连即恢复）与 `update.*` 事件。
 *
 * chip 为什么默认隐藏：没有待办动作时（idle / 刚更新完）常驻一个「已是最新」徽标只会变成
 * 噪音，盖掉真正要看的信号 —— 与治理面徽标同一条纪律（不该看到的东西不出现）。
 * 为什么下载完不自动安装：安装会重启进程、中断当前工作，必须由用户点「安装」。
 * 为什么浏览器直开 host 时不给「安装」按钮：安装要走桌面壳的 `vibeShell.applyUpdate`
 * 这座桥，浏览器里没有它，按钮点了也无处可去 —— 只显示「请在桌面端完成安装」。 */

const UPDATE_LABELS = {
  idle: '已是最新',
  available: '有可用更新',
  downloading: '更新下载中',
  staged: '更新待安装',
  applied: '已更新',
  failed: '更新检查失败',
};

// chip 只在这些「有待办动作」的状态下出现；其余状态收起来，不占标题栏。
const UPDATE_CHIP_STATES = new Set(['available', 'downloading', 'staged', 'failed']);

/** 桌面壳的更新桥；浏览器里返回 null（据此隐藏「安装」按钮）。 */
function updateBridge() {
  const shell = window.vibeShell;
  return shell && typeof shell.applyUpdate === 'function' ? shell : null;
}

function updateView() {
  return S.update || (S.session && S.session.update) || null;
}

/** 把事件增量并进当前视图（事件字段比快照更全，比如 release / mandatory / notes）。 */
function setUpdateView(patch) {
  S.update = Object.assign({}, updateView() || {}, patch);
  applyUpdateChip(S.update);
  return S.update;
}

function updateChipText(status, version) {
  if (status === 'failed') return '更新失败';
  const suffix = version ? ` v${version}` : '';
  if (status === 'available') return `有可用更新${suffix}`;
  if (status === 'downloading') return `更新下载中${suffix}`;
  if (status === 'staged') return `更新待安装${suffix}`;
  return '更新';
}

function updateChipTitle(view, status, version) {
  const desc = {
    available: '发现可安装的新版本',
    downloading: '安装包下载中（逐源 RSA 验签 + sha256 校验通过才用）',
    staged: '安装包已下载并校验，等待安装',
    failed: '更新检查或下载失败',
  }[status] || '客户端更新';
  const lines = [desc];
  if (view.currentVersion) lines.push(`当前版本 v${view.currentVersion}`);
  if (view.channel) lines.push(`更新通道 ${view.channel}`);
  if (version && status !== 'staged') lines.push(`目标版本 v${version}`);
  if (view.mandatory) lines.push('该版本为强制更新，必须安装后才能继续使用');
  if (view.reason) lines.push(`原因：${view.reason}`);
  if (view.source) lines.push(`来源：${view.source}`);
  lines.push('点此查看详情');
  return lines.join('\n');
}

function applyUpdateChip(view) {
  const chip = $('chip-update');
  if (!chip) return;
  const enabled = !!view && view.enabled !== false && view.disabled !== true;
  const status = (view && view.status) || 'idle';
  if (!enabled || !UPDATE_CHIP_STATES.has(status)) {
    chip.hidden = true;
    chip.textContent = '';
    chip.classList.remove('is-error', 'is-warning');
    delete chip.dataset.state;
    return;
  }
  chip.hidden = false;
  // 「按状态着色」：styles.css 只有 .chip.is-error / .chip.is-warning 两档颜色，没有
  // update 专属规则，所以映射到这两个既有类；dataset.state 仍如实记下状态本身（语义标记）。
  chip.dataset.state = status;
  chip.classList.toggle('is-error', status === 'failed');
  chip.classList.toggle('is-warning', status !== 'failed');
  const version = view.availableVersion || view.version || '';
  chip.textContent = updateChipText(status, version);
  chip.title = updateChipTitle(view, status, version);
}

/* ---- 更新弹窗 --------------------------------------------------------- */

function fmtUpdateSize(bytes) {
  const n = Number(bytes) || 0;
  if (!n) return '—';
  // 安装包一般几百 KiB 到几十 MiB，按 KiB / MiB 报更好读。
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / 1048576).toFixed(2)} MiB`;
}

function updateMsg(text) {
  const node = $('update-error');
  node.hidden = !text;
  node.textContent = text || '';
}

function openUpdateModal() {
  $('update-modal').hidden = false;
  updateMsg('');
  renderUpdateModal();
}

function closeUpdateModal() {
  $('update-modal').hidden = true;
}

function renderUpdateProgress(prog) {
  const bar = $('update-progress');
  const total = (prog && Number(prog.total)) || 0;
  if (!total) { bar.hidden = true; return; }
  const done = Number(prog.done) || 0;
  const pct = Math.max(0, Math.min(100, (done / total) * 100));
  bar.hidden = false;
  $('update-progress-fill').style.width = `${pct.toFixed(1)}%`;
  $('update-progress-label').textContent =
    `${fmtUpdateSize(done)} / ${fmtUpdateSize(total)}（${pct.toFixed(0)}%）`;
}

function renderUpdateStaged(view, status) {
  const path = view.stagedPath || '';
  const ready = status === 'staged' && !!path;
  const info = $('update-staged');
  info.hidden = !ready;
  const bridge = updateBridge();
  if (ready) {
    // 没有壳桥（浏览器直开 host）时不给「安装」按钮，只提示去桌面端装。
    info.textContent = bridge
      ? `安装包已就绪：${path}`
      : `安装包已就绪：${path}（请在桌面端完成安装）`;
  }
  $('btn-update-apply').hidden = !(ready && bridge);
}

function renderUpdateModal() {
  const view = updateView() || {};
  const status = view.status || 'idle';
  $('update-status').textContent = UPDATE_LABELS[status] || '客户端更新';
  $('update-current').textContent = view.currentVersion ? `v${view.currentVersion}` : '—';
  $('update-channel').textContent = view.channel || '—';
  const target = view.availableVersion || view.version || '';
  $('update-target').textContent = target ? `v${target}` : '—';
  const release = view.release || null;
  const size = (release && release.size) || view.stagedSize || (view.progress && view.progress.total) || 0;
  $('update-size').textContent = fmtUpdateSize(size);
  // 发布说明是 release.notes（多行纯文本）；快照视图不含 release 时退化为「—」。
  $('update-notes').textContent = (release && release.notes) || '—';
  $('update-mandatory').hidden = !view.mandatory;
  renderUpdateProgress(view.progress);
  renderUpdateStaged(view, status);
  const dl = $('btn-update-download');
  // mandatory（release.minVersion 约束导致）时文案必须明确「必须更新」，且不给「稍后」跳过。
  dl.textContent = view.mandatory ? '必须更新' : '下载';
  $('btn-update-later').hidden = !!view.mandatory;
  dl.hidden = status !== 'available' && status !== 'failed';
}

/** 下载成功（事件或下载响应）后的统一收尾：绝不自动安装，等用户点「安装」。 */
function applyUpdateStaged(info) {
  const version = String((info && (info.version || info.availableVersion)) || '');
  setUpdateView({
    status: 'staged',
    availableVersion: version,
    stagedPath: String((info && info.stagingPath) || ''),
    stagedSize: Number((info && info.size) || 0),
    progress: null,
  });
  if (!$('update-modal').hidden) renderUpdateModal();
}

async function downloadUpdate() {
  const view = updateView() || {};
  const version = view.availableVersion || view.version || '';
  const btn = $('btn-update-download');
  btn.disabled = true;
  updateMsg('');
  try {
    // 带上已知的目标版本：避免下载时再挑一次源（「稍后」只是关窗，不改状态）。
    const res = await api.post('/api/update/download', version ? { version } : {});
    if (res && res.stagingPath) applyUpdateStaged(res);
    toast(`安装包已下载并通过校验（${fmtUpdateSize(res && res.size)}）`, 'ok');
  } catch (err) {
    updateMsg(`下载失败：${err.message}`);
    toast(`下载更新失败：${err.message}`, 'error');
  } finally {
    btn.disabled = false;
    renderUpdateModal();
  }
}

function applyStagedUpdate() {
  const view = updateView() || {};
  const path = view.stagedPath || '';
  const bridge = updateBridge();
  if (!bridge || !path) return;
  // 安装交给桌面壳（Part C）：它做路径形态校验后 spawn 安装器并重启应用。
  bridge.applyUpdate(path);
  toast('已交给桌面端安装，安装过程中应用会重启', 'ok');
  closeUpdateModal();
}

/* ═══════════════════════════════ 治理面登录弹窗（M3+） ═══════════════════════════════
 * 把「填地址 → 密码登录 / 邀请码注册 → 热接入」做成桌面端闭环：不必再「先用 CLI
 * 登录、再重启」。登录是「用户本人动作」（ADR-011）—— 不走审批浮层，但执行中后端
 * 返回 409（这里如实显示）。**令牌全程只在后端与用户级凭证文件里**，界面只读回
 * `describe()` 的掩码视图，绝不持有明文。
 *
 * 地址双重用途：本次登录把它随请求下发（serverUrl），后端据此即时热接入、无需重启；
 * 「保存地址」则经桌面壳配置桥写进 config.json，下次启动作为 VIBE_SERVER_URL 生效。 */

function govMsg(text, kind = '', id = 'gov-msg') {
  const node = $(id);
  node.className = 'set-msg' + (kind ? ` is-${kind}` : '');
  node.textContent = text || '';
}

function governanceView() {
  return (S.session && S.session.governance) || null;
}

function openGovModal() {
  $('gov-modal').hidden = false;
  $('gov-error').hidden = true;
  govMsg('');
  govMsg('', '', 'gov-reg-msg');
  $('gov-password').value = '';
  $('gov-token').value = '';
  // 「保存地址」只在桌面壳里可用（浏览器没有写 config.json 的桥）
  const bridge = shellConfigBridge();
  $('btn-gov-save-server').hidden = !bridge;
  const gov = governanceView();
  if (gov && gov.serverUrl) $('gov-server').value = gov.serverUrl;
  // 后端这次启动没带治理面地址时，回填上一次保存进 config.json 的地址。
  if (bridge && !(gov && gov.serverUrl)) {
    bridge
      .readDesktopConfig()
      .then((cfg) => {
        if (cfg && cfg.serverUrl && !$('gov-server').value) {
          $('gov-server').value = cfg.serverUrl;
        }
      })
      .catch(() => {});
  }
  renderGovStatus(gov);
  $('gov-email').focus();
}

function closeGovModal() {
  $('gov-modal').hidden = true;
}

function renderGovStatus(gov) {
  const status = gov && gov.status;
  $('gov-status').textContent = status ? GOV_LABELS[status] || status : '未接入治理面';
  // 已登录 / 已降级都允许退出；纯未登录没有可退的东西
  $('btn-gov-logout').hidden = !(status === 'governed' || status === 'degraded');
  const lines = [];
  if (gov && gov.degradedReason) lines.push(gov.degradedReason);
  if (gov && gov.credentials) {
    const c = gov.credentials;
    lines.push(
      `当前用户：${c.email || c.userId || '—'}` +
        (c.deviceId ? ` · 设备 ${c.deviceId}` : '')
    );
  }
  const quota = gov ? quotaLine(gov) : '';
  if (quota) lines.push(quota);
  if (lines.length) $('gov-hint').textContent = lines.join('\n');
}

function govShowError(err) {
  $('gov-error').hidden = false;
  $('gov-error').textContent = err && err.message ? err.message : String(err);
}

/** 登录 / 注册 / 登出后的统一收尾：应用新快照 + 回显状态。 */
function applyGovernanceResult(res, msgId, okText) {
  if (res && res.session) applySession(res.session);
  govMsg(okText, 'ok', msgId);
  renderGovStatus(res && res.governance ? res.governance : governanceView());
  toast(okText, 'ok');
}

async function governanceLogin() {
  const email = $('gov-email').value.trim();
  const password = $('gov-password').value;
  if (!email || !password) {
    $('gov-error').hidden = false;
    $('gov-error').textContent = '请填写邮箱与密码。';
    return;
  }
  const btn = $('btn-gov-login');
  btn.disabled = true;
  $('gov-error').hidden = true;
  govMsg('登录中…');
  try {
    const res = await api.post('/api/governance/login', {
      email,
      password,
      serverUrl: $('gov-server').value.trim(),
    });
    $('gov-password').value = '';
    applyGovernanceResult(res, 'gov-msg', '已登录治理面，当前会话已热接入');
  } catch (err) {
    govShowError(err);
    govMsg('');
  } finally {
    btn.disabled = false;
  }
}

async function governanceRegister() {
  const token = $('gov-token').value.trim();
  const email = $('gov-email').value.trim();
  const password = $('gov-password').value;
  if (!token || !email || !password) {
    $('gov-error').hidden = false;
    $('gov-error').textContent = '邀请码、邮箱、密码都必须填写。';
    return;
  }
  const btn = $('btn-gov-register');
  btn.disabled = true;
  $('gov-error').hidden = true;
  govMsg('注册中…', '', 'gov-reg-msg');
  try {
    const res = await api.post('/api/governance/register', {
      enrollmentToken: token,
      email,
      password,
      displayName: $('gov-name').value.trim(),
      serverUrl: $('gov-server').value.trim(),
    });
    $('gov-token').value = '';
    $('gov-password').value = '';
    applyGovernanceResult(res, 'gov-reg-msg', '注册成功并已登录治理面');
  } catch (err) {
    govShowError(err);
    govMsg('', '', 'gov-reg-msg');
  } finally {
    btn.disabled = false;
  }
}

async function governanceLogout() {
  const btn = $('btn-gov-logout');
  btn.disabled = true;
  $('gov-error').hidden = true;
  try {
    const res = await api.post('/api/governance/logout', {});
    applyGovernanceResult(res, 'gov-msg', '已退出治理面，当前会话回到本地直连');
    // 退出后重新落回登录门（applySession 内已调 renderLoginGate，这里只需聚焦）
    setTimeout(() => { const n = $('gate-email'); if (n && !$('login-gate').hidden) n.focus(); }, 50);
  } catch (err) {
    govShowError(err);
  } finally {
    btn.disabled = false;
  }
}

/** 把治理面地址写进桌面壳 config.json；重启应用后作为 VIBE_SERVER_URL 传给后端。 */
async function saveGovernanceServer() {
  const bridge = shellConfigBridge();
  if (!bridge) return;
  const btn = $('btn-gov-save-server');
  btn.disabled = true;
  try {
    const res = await bridge.saveDesktopConfig({ serverUrl: $('gov-server').value.trim() });
    if (res && res.ok === false) throw new Error(res.error || '写入配置失败');
    toast('治理面地址已保存，重启应用后生效', 'ok');
  } catch (err) {
    govShowError(new Error(`保存地址失败：${err.message}`));
  } finally {
    btn.disabled = false;
  }
}

/* ═══════════════════════════════ 启动登录门（强制登录） ═══════════════════════════════
 * 「一进软件先登录，每个人自己的账号密码」：未接入治理面（status 不是 governed）时
 * 全屏遮住工作区，只给「登录 / 注册」两个动作。登录成功后热接入并隐藏 —— 会话、
 * 检查点、审计链原样保留（见 Session._install_governance）。这是**界面层**的强制；
 * 令牌仍只在后端与用户级凭证文件，界面只拿 describe() 的掩码视图。
 * 服务器地址可「记住」进桌面壳 config.json，下次启动即由 VIBE_SERVER_URL 注入。 */

function gateRequired(governance) {
  return !governance || governance.status !== 'governed';
}

function renderLoginGate(governance) {
  const gate = $('login-gate');
  if (!gate) return;
  const need = gateRequired(governance);
  gate.hidden = !need;
  if (!need) return;
  const gov = governance || governanceView();
  if (gov && gov.serverUrl && !$('gate-server').value) $('gate-server').value = gov.serverUrl;
  const bridge = shellConfigBridge();
  $('btn-gate-save-server').hidden = !bridge;
  // 后端这次启动没带地址时，回填上一次保存进 config.json 的地址
  if (bridge && !$('gate-server').value) {
    bridge
      .readDesktopConfig()
      .then((cfg) => {
        if (cfg && cfg.serverUrl && !$('gate-server').value) {
          $('gate-server').value = cfg.serverUrl;
          probeGateRegistration();
        }
      })
      .catch(() => {});
  }
  probeGateRegistration();
}

function gateTab(which) {
  const login = which !== 'register';
  $('gate-tab-login').classList.toggle('is-active', login);
  $('gate-tab-register').classList.toggle('is-active', !login);
  $('gate-login-form').hidden = !login;
  $('gate-register-form').hidden = login;
  gateErr('');
  $('gate-msg').textContent = '';
}

function gateErr(text) {
  const node = $('gate-error');
  node.hidden = !text;
  node.textContent = text || '';
}

/** 探针：服务器是否开放自助注册 —— 决定「邀请码」是否必填。 */
async function probeGateRegistration() {
  const server = $('gate-server').value.trim();
  const note = $('gate-server-note');
  if (!server) { note.textContent = ''; return; }
  try {
    const res = await api.get('/api/governance/config', { serverUrl: server });
    if (res && res.ok) {
      note.textContent = res.registrationOpen
        ? '该服务器已开放自助注册，可直接注册（邀请码可留空）。'
        : '该服务器需邀请码注册 —— 请向管理员索取。';
    } else {
      note.textContent = `未连上服务器：${(res && res.reason) || '未知原因'}`;
    }
  } catch (err) {
    note.textContent = `未连上服务器：${err.message}`;
  }
}

/** 登录成功后把地址记进桌面壳 config.json，下次启动即自动带 server_url。 */
async function persistGateServer() {
  const bridge = shellConfigBridge();
  const server = $('gate-server').value.trim();
  if (!bridge || !server) return;
  try {
    await bridge.saveDesktopConfig({ serverUrl: server });
  } catch (_) { /* 记不住地址不影响本次登录 */ }
}

async function gateLogin() {
  const email = $('gate-email').value.trim();
  const password = $('gate-password').value;
  if (!email || !password) { gateErr('请填写邮箱与密码。'); return; }
  const btn = $('btn-gate-login');
  btn.disabled = true;
  gateErr('');
  $('gate-msg').textContent = '登录中…';
  try {
    const res = await api.post('/api/governance/login', {
      email,
      password,
      serverUrl: $('gate-server').value.trim(),
    });
    $('gate-password').value = '';
    await persistGateServer();
    if (res && res.session) applySession(res.session);
    toast('已登录，欢迎回来', 'ok');
  } catch (err) {
    gateErr(err.message);
    $('gate-msg').textContent = '';
  } finally {
    btn.disabled = false;
  }
}

async function gateRegister() {
  const email = $('gate-reg-email').value.trim();
  const password = $('gate-reg-password').value;
  if (!email || !password) { gateErr('请填写邮箱与密码。'); return; }
  const btn = $('btn-gate-register');
  btn.disabled = true;
  gateErr('');
  $('gate-msg').textContent = '注册中…';
  try {
    const res = await api.post('/api/governance/register', {
      enrollmentToken: $('gate-token').value.trim(),
      email,
      password,
      displayName: $('gate-name').value.trim(),
      serverUrl: $('gate-server').value.trim(),
    });
    $('gate-reg-password').value = '';
    $('gate-token').value = '';
    await persistGateServer();
    if (res && res.session) applySession(res.session);
    toast('注册成功，已登录', 'ok');
  } catch (err) {
    gateErr(err.message);
    $('gate-msg').textContent = '';
  } finally {
    btn.disabled = false;
  }
}

async function saveGateServer() {
  const bridge = shellConfigBridge();
  if (!bridge) return;
  try {
    const res = await bridge.saveDesktopConfig({ serverUrl: $('gate-server').value.trim() });
    if (res && res.ok === false) throw new Error(res.error || '写入配置失败');
    toast('服务器地址已保存，下次启动自动带上', 'ok');
  } catch (err) {
    gateErr(`保存地址失败：${err.message}`);
  }
}

function setState(state) {
  S.state = state;
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
  syncRollbackButtons();
  $('input').disabled = S.running;
  $('btn-send').disabled = S.running || !$('input').value.trim();
  syncPauseButton();
  $('composer-status').textContent = S.interrupting && S.running
    ? '正在停止…'
    : (S.running ? 'Agent 正在工作…' : '就绪 · Enter 发送');
  // 远程工作面（M11）：执行中禁用「保存到远端」与「改连接」，与后端 409 同一口径。
  syncRemoteRunning();
}

/* 暂停按钮（交互增强一）：常驻、按状态置灰。
   - idle：置灰，提示「当前没有执行中的任务」；
   - running / awaiting_approval：可用（等待审批时点它 = 立即释放审批为拒绝）；
   - 已点过、还没收到结束事件：置灰并显示「停止中…」，防重复点击。
   与顶部「■ 中断」共用同一个 interrupt()，这里只负责呈现。 */
function syncPauseButton() {
  const btn = $('btn-pause');
  if (!btn) return;
  const running = !!S.running;
  if (!running) S.interrupting = false;  // 任务已结束：必须复位，否则下次执行永远卡灰
  btn.disabled = !running || S.interrupting;
  btn.textContent = S.interrupting ? '停止中…' : '⏸';
  btn.title = S.interrupting
    ? '正在停止：收到本轮结束事件后恢复'
    : (running
      ? '暂停当前任务（生成立即中断，工具执行完当前这一步后停止）'
      : '当前没有执行中的任务');
  btn.classList.toggle('is-stopping', S.interrupting);
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

function addUser(text, seq = 0) {
  hideChatWelcome();
  const node = block('user');
  node.appendChild(document.createTextNode(text));
  // 逐条回退（P3）：seq 来自消息表，只有 UI 通路带它（Agent.messages 里没有）。
  // 没有 seq（本页刚发出的提问在 run.started 之前、或历史未持久化）就不渲染按钮。
  if (seq > 0) {
    const foot = el('div', 'user-foot');
    const btn = el('button', 'btn-rollback-inline', '↩ 回退到这条之前');
    btn.type = 'button';
    btn.disabled = S.running;
    btn.title = '回到这条提问被发送之前：撤销本轮的文件改动，并删除这条及之后的对话';
    btn.addEventListener('click', () => openRollbackConfirm(seq));
    foot.appendChild(btn);
    node.appendChild(foot);
  }
  scrollToEnd(true);
}

/* 执行中一律置灰：与顶部「回滚」按钮同一口径（后端也会回 409）。 */
function syncRollbackButtons() {
  stream.querySelectorAll('.btn-rollback-inline').forEach((btn) => {
    btn.disabled = S.running;
  });
}

/** 助手正文按 Markdown 渲染：与编辑器预览共用零依赖渲染器（DOM 拼装，无 innerHTML）。 */
function renderAnswerBody(node, text) {
  node.replaceChildren(render(text || ''));
}

function addAnswer(text) {
  const node = block('answer');
  const body = el('div', 'msg-md');
  renderAnswerBody(body, text);
  node.appendChild(body);
  addRememberButton(node, text);
  scrollToEnd();
}

/* ---- 流式回复（A1） ---- */
function appendDelta(text) {
  if (!text) return;
  if (!S.streaming) {
    hideChatWelcome();
    const node = block('answer', 'is-streaming');
    const body = el('div', 'msg-md');
    node.appendChild(body);
    S.streaming = { node, body, text: '', timer: 0 };
  }
  S.streaming.text += text;
  // 每来一个 delta 就全量重排长文会把主线程拖住；按 100ms 节流，肉眼看仍是连续输出。
  const entry = S.streaming;
  if (!entry.timer) {
    entry.timer = setTimeout(() => {
      entry.timer = 0;
      renderAnswerBody(entry.body, entry.text);
      scrollToEnd();
    }, 100);
  }
  scrollToEnd();
}

function finishStreaming(finalText) {
  const entry = S.streaming;
  S.streaming = null;
  if (!entry) return null;
  if (entry.timer) { clearTimeout(entry.timer); entry.timer = 0; }
  entry.node.classList.remove('is-streaming');
  if (typeof finalText === 'string') entry.text = finalText;
  renderAnswerBody(entry.body, entry.text);
  addRememberButton(entry.node, entry.text);
  return entry;
}

/* ---- 思考流（reasoning_content）：模型边想边滚动 ----
   与回答块分开：思考是过程、回答是结论。流式期间自动展开跟随滚动，收口后保留
   （点标题可折叠）。纯呈现 —— 不进会话历史、不落盘（上游不返回思考时整块不出现）。 */
function appendReasoning(text) {
  if (!text) return;
  if (!S.reasoning) {
    hideChatWelcome();
    const card = block('reasoning', 'is-open is-streaming');
    const head = el('div', 'reasoning-head');
    head.appendChild(el('span', 'reasoning-arrow', '▶'));
    head.appendChild(el('span', 'reasoning-title', '思考中…'));
    const body = el('div', 'reasoning-body');
    const textNode = document.createTextNode('');
    body.appendChild(textNode);
    card.append(head, body);
    head.addEventListener('click', () => card.classList.toggle('is-open'));
    stream.appendChild(card);
    S.reasoning = { card, textNode, text: '' };
  }
  S.reasoning.text += text;
  S.reasoning.textNode.textContent = S.reasoning.text;
  scrollToEnd();
}

function finishReasoning() {
  const entry = S.reasoning;
  S.reasoning = null;
  if (!entry) return;
  entry.card.classList.remove('is-streaming');
  const title = entry.card.querySelector('.reasoning-title');
  if (title) title.textContent = '思考';
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
  // 提交前自审（M9-H）：review_changes 恒 SAFE，但语义上值得单列一个「自审」徽标，
  // 让用户一眼看出这是一次只读自审（不产出补丁、不自动改代码，ADR-008 不变）。
  if (ev.tool === 'review_changes') {
    const pill = el('span', 'risk-pill', '自审');
    pill.dataset.r = 'SAFE';
    pill.title = '提交前的只读自审：意见见下方模型回复，不会自动改代码';
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
  S.calls.set(ev.callId, {
    card, body, out, status, name: ev.tool, streaming: null,
    // 底部「输出」面板实时滚动的行缓冲：跨块的半行先攒着，凑满一行才推。
    termBuf: '', termErr: false, termStreamed: false,
  });
  scrollToEnd();

  // 命令类工具同步进底部终端
  if (COMMAND_TOOLS.test(ev.tool)) {
    const cmd = ev.arguments && (ev.arguments.command || summarizeArgs(ev.arguments));
    if (cmd) termLine('output', `$ ${cmd}`, 'is-cmd');
  }
}

/* ---- 命令流式回显（run_command 边跑边看）----------------------------------
 * run.tool_output 事件把子进程 stdout/stderr 的增量片段实时推到这里。
 * 它只是「打字机」：命令结束时 completeToolCall 用 run.tool_result 的完整
 * content **覆盖式收口**，并把这块临时区域移除 —— 模型侧与刷新后的视图都不受影响。 */
function applyToolOutput(ev) {
  const entry = S.calls.get(ev.callId);
  if (!entry) return; // 历史重放或刷新后卡片可能不在，静默跳过
  if (!entry.streaming) {
    const live = el('pre', 'tool-live', '');
    entry.body.insertBefore(live, entry.out);
    entry.card.classList.add('is-open'); // 命令有输出时自动展开，用户能立刻看到
    entry.streaming = live;
  }
  if (ev.stream === 'stderr') {
    // stderr 单独着色：一行一段，避免与 stdout 交错时染色错乱
    const span = el('span', 'live-err', ev.text || '');
    entry.streaming.appendChild(span);
  } else {
    entry.streaming.appendChild(document.createTextNode(ev.text || ''));
  }
  // 跟随滚动：只在用户没往上翻时自动滚到底
  const box = entry.streaming;
  if (box.scrollHeight - box.scrollTop - box.clientHeight < 40) {
    box.scrollTop = box.scrollHeight;
  }

  // 底部「输出」面板同步实时滚动：按行推进，跨块的半行先攒进 entry.termBuf。
  // 与卡片内的实时区是两条独立渲染路径，但都只影响「看」，不影响模型侧。
  const text = ev.text || '';
  if (text) {
    entry.termStreamed = true;
    // 流切换（stdout↔stderr）时先把上一个流的半行冲掉，避免两个流的残句拼进同一行。
    if (entry.termErr !== (ev.stream === 'stderr') && entry.termBuf) {
      termLine('output', entry.termBuf, entry.termErr ? 'is-err' : '');
      entry.termBuf = '';
    }
    entry.termErr = ev.stream === 'stderr';
    entry.termBuf += text;
    let nl;
    while ((nl = entry.termBuf.indexOf('\n')) >= 0) {
      const line = entry.termBuf.slice(0, nl);
      entry.termBuf = entry.termBuf.slice(nl + 1);
      termLine('output', line, entry.termErr ? 'is-err' : '');
    }
  }
  scrollToEnd();
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

  // 命令流式回显收口：以 run.tool_result 的完整 content 覆盖，移除临时实时区
  if (entry.streaming) {
    entry.streaming.remove();
    entry.streaming = null;
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
    if (entry.termStreamed) {
      // 流式已实时滚过：冲掉残留半行，再补一行退出状态收口（不重复写完整输出）。
      if (entry.termBuf) {
        termLine('output', entry.termBuf, entry.termErr ? 'is-err' : '');
        entry.termBuf = '';
      }
      const code = ev.meta && ev.meta.exit_code;
      termLine(
        'output',
        `（结束，退出码 ${code == null ? '?' : code}）`,
        ev.ok ? 'is-ok' : 'is-err'
      );
    } else {
      termLine('output', ev.content || '（无输出）', ev.ok ? '' : 'is-err');
    }
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
  // 命令输出里出现本机服务地址（dev server 启动横幅）→ 露出「浏览器」入口
  maybeLocalUrl(text);
}

function toggleBottom(open) {
  const bp = $('bottom-panel');
  // 用户拖出来的高度是 inline height，会压过 `.is-collapsed { height: 30px }`；
  // 所以收起时先把它存起来、展开时再还回去。
  if (!open) {
    if (bp.style.height) { bp.dataset.height = bp.style.height; bp.style.height = ''; }
  } else if (bp.dataset.height) {
    bp.style.height = bp.dataset.height;
  }
  const collapsed = bp.classList.toggle('is-collapsed', !open);
  $('btn-bottom-toggle').textContent = collapsed ? '▴' : '▾';
}

function switchBottom(name) {
  document.querySelectorAll('.bp-tab').forEach((t) => t.classList.toggle('is-active', t.dataset.bp === name));
  document.querySelectorAll('.bp-body').forEach((b) => { b.hidden = b.dataset.bp !== name; });
  // 切到终端页签：面板刚可见时量不出尺寸（同一帧内还没布局），下一帧再重算一次。
  // 不重算的话「在别的页签时改过窗口大小」会让终端停在旧列宽上，右侧内容看着缺一截。
  if (name === 'terminal') requestAnimationFrame(() => termResizeActive());
}

/* ═══════════════ 中间区三视图（浏览器 / 终端 / 编辑器） ═══════════════
 * 三者在同一列里切换：编辑器用既有的标签区；终端**复用**底部那块终端并展开到
 * 全高（DOM 只有一份，缓冲与滚动位置不会丢）；浏览器是独立面板，用来内嵌本机
 * 跑起来的界面 —— 走的是「本机应用」预览同一条门控与跨源隔离。
 * 门控与地址校验都在服务端：这里只在探测到 enabled 之后才允许输入并打开。 */
let browserEnabled = false;   // 能力探测没回来之前按关处理（fail closed）
let browserUrl = '';          // 当前已打开的本机应用地址（'' = 还没有）
let browserFrame = null;      // 惰性创建：第一次打开时才插 iframe
let browserBound = false;     // 事件是否已绑定（只绑一次）
let lastLocalUrl = '';        // 最近一次从终端 / 命令输出里识别到的本机地址

function switchStage(name) {
  const next = name === 'browser' || name === 'terminal' ? name : 'editor';
  $('editor-stack').dataset.stage = next;
  document.querySelectorAll('.stage-tab').forEach((b) => {
    const on = b.dataset.stage === next;
    b.classList.toggle('is-active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  if (next === 'terminal') {
    // 终端视图只做两件事：确保面板展开、把页签切到终端 —— 不搬 DOM。
    toggleBottom(true);
    switchBottom('terminal');
    requestAnimationFrame(() => termResizeActive());
  }
}

/* 服务端只收 127.0.0.1（刻意不收 localhost）；dev server 打印的多是 localhost，
   这里只做等价的字面替换，端口 / 路径 / 查询原样保留。 */
function browserNormalize(raw) {
  const text = String(raw == null ? '' : raw).trim();
  return text.replace(/^http:\/\/localhost(?=[:/]|$)/i, 'http://127.0.0.1');
}

/* 桌面壳里优先用 webview：本机应用常带 X-Frame-Options / CSP frame-ancestors，
   iframe 会被浏览器直接拒成白屏；webview 是独立 WebContents 的顶层导航，不受这两条
   限制。它只允许挂 127.0.0.1，且壳里已剥掉 preload / Node 能力（will-attach-webview）。
   纯浏览器打开 host 页面时没有 webview，自动退回 iframe。 */
function browserUsesWebview() {
  return typeof customElements !== 'undefined' && !!customElements.get('webview');
}

function browserReload() {
  if (!browserFrame || !browserUrl) return;
  if (browserFrame.tagName === 'WEBVIEW') {
    if (typeof browserFrame.reload === 'function') browserFrame.reload();
    else browserFrame.setAttribute('src', browserUrl);
  } else {
    browserFrame.src = browserUrl;
  }
}

function browserEnsureFrame() {
  if (browserFrame) return browserFrame;
  if (browserUsesWebview()) {
    const view = document.createElement('webview');
    view.className = 'browser-frame';
    view.setAttribute('title', '浏览器');
    view.setAttribute('webpreferences', 'contextIsolation=yes,nodeIntegration=no,sandbox=yes');
    view.addEventListener('did-start-loading', () => { $('browser-empty').hidden = true; });
    view.addEventListener('did-fail-load', (ev) => {
      if (ev.errorCode === -3) return;   // 主动取消 / 重定向，不算失败
      const why = ev.errorDescription || `错误码 ${ev.errorCode}`;
      toast(`页面加载失败：${why}`, 'error');
      const empty = $('browser-empty');
      empty.hidden = false;
      empty.textContent = `页面没有加载成功（${why}）。确认该端口上的服务已启动，或点「新窗口」在系统浏览器里打开。`;
    });
    browserFrame = view;
  } else {
    browserFrame = document.createElement('iframe');
    browserFrame.className = 'browser-frame';
    browserFrame.setAttribute('title', '浏览器');
    // 与「本机应用」预览同一模型：跨源 + allow-same-origin 指的是它**自己的**源，
    // 不含 allow-top-navigation / allow-popups / allow-modals / allow-downloads。
    browserFrame.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms');
    browserFrame.setAttribute('referrerpolicy', 'no-referrer');
  }
  $('browser-stage').appendChild(browserFrame);
  return browserFrame;
}

async function browserOpen(raw) {
  const input = $('browser-url');
  const url = browserNormalize(raw != null ? raw : input.value);
  if (!url) { input.focus(); return; }
  if (!browserEnabled) {
    toast('浏览器视图未启用（需 VIBE_PREVIEW_EXTERNAL=1）', 'error');
    return;
  }
  const btn = $('btn-browser-open');
  btn.disabled = true;
  try {
    // 校验在服务端（唯一口径）：它接受什么，页面的 frame-src 就放行什么。
    const res = await api.post('/api/preview/external', { url });
    browserUrl = res.url;
    input.value = res.url;
    const frame = browserEnsureFrame();
    if (frame.tagName === 'WEBVIEW') {
      // webview 走属性赋值；地址没变时直接 reload，避免属性相同不触发导航。
      if (frame.getAttribute('src') === res.url) browserReload();
      else frame.setAttribute('src', res.url);
    } else {
      frame.src = res.url;
    }
    $('browser-empty').hidden = true;
    $('btn-browser-reload').disabled = false;
    $('btn-browser-popout').disabled = false;
    const chip = $('browser-chip');
    chip.hidden = false;
    chip.textContent = `本机应用 :${res.port}`;
    chip.title = '内容来自你自己启动的本机服务；跨源隔离，读不到本工作区的接口与文件';
  } catch (err) {
    toast(`无法打开该地址：${err.message}`, 'error');
    input.focus();
    input.select();
  } finally {
    btn.disabled = false;
  }
}

/** 把「终端里认出来的地址」露成一个入口，点一下就在中间区打开。 */
function browserDetect(url) {
  const btn = $('btn-browser-detected');
  if (!btn) return;
  btn.hidden = false;
  btn.textContent = `打开 ${url.replace(/^http:\/\//, '')}`;
  btn.title = `在中间区打开终端里跑起来的 ${url}`;
  btn.onclick = () => { switchStage('browser'); browserOpen(url); };
}

/* 从终端 / 命令输出里认出本机地址：dev server 的启动横幅大多会打印
 * `Local: http://localhost:5173/`。只认本机回环 + 显式端口（与服务端校验口径一致），
 * 认出后把入口露出来，但**不自动切视图** —— 不打断用户正在看的输出。 */
function maybeLocalUrl(text) {
  if (!text || !browserEnabled) return;
  const found = String(text).match(/http:\/\/(?:localhost|127\.0\.0\.1):\d{1,5}(?:[^\s"'<>)]*)?/i);
  if (!found) return;
  const url = browserNormalize(found[0].replace(/[.,;]+$/, ''));
  if (!url || url === lastLocalUrl) return;
  lastLocalUrl = url;
  const input = $('browser-url');
  if (input && !input.value.trim()) input.value = url;
  browserDetect(url);
  toast(`检测到本机服务 ${url}，顶部「浏览器」可直接打开`, '');
}

function initBrowser() {
  if (browserBound) return;
  browserBound = true;
  document.querySelectorAll('.stage-tab').forEach((b) => {
    b.addEventListener('click', () => switchStage(b.dataset.stage));
  });
  $('btn-browser-open').addEventListener('click', () => browserOpen());
  $('btn-browser-reload').addEventListener('click', () => { browserReload(); });
  $('btn-browser-popout').addEventListener('click', () => {
    if (browserUrl) window.open(browserUrl, '_blank', 'noopener');
  });
  $('browser-url').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); browserOpen(); }
  });
  // 探测失败一律按关处理：不让人先点进去再撞一个 403（与预览工具条同一口径）。
  api.get('/api/preview/external').then((st) => {
    browserEnabled = !!(st && st.enabled);
    if (!browserEnabled) {
      $('btn-browser-open').disabled = true;
      $('browser-empty').textContent = '浏览器视图未启用：启动内核时设置 VIBE_PREVIEW_EXTERNAL=1 后可用。';
    }
  }).catch(() => {
    $('btn-browser-open').disabled = true;
    $('browser-empty').textContent = '无法确认浏览器视图是否启用（能力探测失败），已按关闭处理。';
  });
}


/* ═══════════════════════════════ 交互式终端（P1-7）═══
 * 用户在 IDE 里直接敲命令（ADR-011 范式）：不走审批浮层，但后端记审计。
 * 输出经 SSE `term.data`（base64）/ `term.exit` 送达，与 run.* 事件流分离。
 * 渲染只实现最小 ANSI 子集：SGR 前景/背景色与加粗、\r、\n、\b、清屏/清行；
 * 不做完整 VT 仿真（见 P1-P2-FEATURES-PLAN §10「明确不做」）。
 * ─────────────────────────────────────────────────────── */
/* 回滚缓冲上限（行）：屏幕顶部滚出去的行推进 history，不再直接丢弃，滚轮可回看。 */
const TERM_SCROLLBACK = 1000;

/* 每个终端一个会话对象。
 *
 * 这里原来是模块级单例（`const term = {...}`），于是界面只能存在一个终端 ——
 * 尽管后端一直是多会话进程池（`TerminalManager`）。改成「对象 + 注册表」后，
 * 渲染/输入/重排全部接收自己那个 `t`，不再读全局状态：同一份代码驱动任意多个终端。
 */
function createTerm({ id = null, seq = 0, shell = 'shell', mode = 'pty' } = {}) {
  return {
    id, seq, shell, mode,
    createdAt: Date.now(),
    rows: 24, cols: 80,
    buf: [], curRow: 0, curCol: 0,
    open: true, closed: false,
    sgr: { fg: '', bg: '', bold: false }, sgrCss: '',
    state: 0, csi: '', decoder: null,
    // 历史行（只读）+ 两个 DOM 宿主。历史只增量追加（见 termRender），不每个 chunk 重建。
    history: [], histEl: null, scrEl: null, histRendered: 0,
    el: null,           // 该终端的滚动容器（每个会话一个，同屏只显示激活的那个）
    // 输入法（中文等）的落脚点：普通 div 收不到 composition 事件，必须有一个
    // 可编辑元素持有焦点（见 attachTermEl）。composing 期间按键全部让给 IME。
    ime: null, composing: false,
    // 管道模式的本地行（见 termPipeKey）：后端管道没有 TTY 线路规程，行编辑只能在前端做。
    line: '', lineCur: 0, lineStart: null,
    // 最近一段输出（见 termFeed）：用来识别被分块拆开的 dev server 地址。
    scanBuf: '',
  };
}

/** termId → 会话对象。键是后端给的 id，`term.data` / `term.exit` 按它分发。 */
const TERMS = new Map();
/** 当前激活的终端 id（null = 一个都没有）。 */
let termActive = null;
/** 配置开关（VIBE_TERM）：关掉时整个入口隐藏，行为与未实现前一致。 */
let termEnabled = true;
/** 前端自增序号，只用于标签上的「1 / 2 / 3」——不随后端 id 复用而跳号。 */
let termSeq = 0;
/** 后端配置的并发上限。**0 = 不限**（默认）；只有显式配了 VIBE_TERM_MAX 才 > 0。 */
let termMax = 0;

function activeTerm() {
  return termActive ? TERMS.get(termActive) || null : null;
}

const TERM_SGR = {
  // 16 色标准调色板 → 显示色（前景；背景 + 10 同段）
  0: '#d8d8d8', 1: '#d54e53', 2: '#b9ca4a', 3: '#e7c547',
  4: '#7aa6da', 5: '#c397d8', 6: '#70c0b1', 7: '#fff',
  8: '#666', 9: '#ff6c6b', 10: '#b9ca4a', 11: '#ffd27f',
  12: '#7aa6da', 13: '#c397d8', 14: '#70c0b1', 15: '#fff',
};

/** 一行单元格 → DOM。cursorRow 为 -1 表示这行没有光标（历史行）。 */
function termRow(cells, r, cursorRow, t) {
  const row = el('div', 'term-row');
  let run = null;
  let runSt = null;
  const flush = () => { if (run) { row.appendChild(run); run = null; } };
  for (let c = 0; c < t.cols; c++) {
    const cell = cells[c];
    const ch = cell ? cell.t : ' ';
    const st = cell ? cell.st : '';
    if (r === cursorRow && c === t.curCol) {
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
  return row;
}

function termRender(t) {
  const host = t && t.el;
  if (!host) return;
  // 两个子容器：history（滚出去的行，只读）+ screen（当前屏，含光标）。
  if (!t.histEl || !t.histEl.isConnected || !t.scrEl || !t.scrEl.isConnected) {
    host.replaceChildren();
    t.histEl = el('div', 'term-hist');
    t.scrEl = el('div', 'term-scr');
    host.append(t.histEl, t.scrEl);
    // 重建宿主时别把输入法宿主（隐藏 textarea）弄丢，否则中文输入会静默失效。
    if (t.ime) host.appendChild(t.ime);
    t.histRendered = 0;
  }
  // 贴底时才跟随新输出；用户滚上去回看时不能抢他的滚动位置。
  const stick = host.scrollHeight - host.scrollTop - host.clientHeight < 60;
  const prevTop = host.scrollTop;
  // 历史行只读：只有「宿主重建过 / 前端裁剪过」才整块重来，其余增量追加 ——
  // 否则每来一个 chunk 都要重建上千个 div。
  if (t.histRendered !== t.histEl.childElementCount) {
    t.histEl.replaceChildren();
    t.histRendered = 0;
  }
  while (t.histRendered < t.history.length) {
    t.histEl.appendChild(termRow(t.history[t.histRendered], -1, -1, t));
    t.histRendered++;
  }
  const cursorRow = t.open && !t.closed ? t.curRow : -1;
  const frag = document.createDocumentFragment();
  for (let r = 0; r < t.rows; r++) frag.appendChild(termRow(t.buf[r] || [], r, cursorRow, t));
  t.scrEl.replaceChildren(frag);
  host.scrollTop = stick ? host.scrollHeight : prevTop;
}

function termSgrCss(t) {
  let css = '';
  if (t.sgr.fg) css += `color:${t.sgr.fg};`;
  if (t.sgr.bg) css += `background:${t.sgr.bg};`;
  if (t.sgr.bold) css += 'font-weight:700;';
  return css;
}

function termSgr(t, codes) {
  for (const code of (codes.length ? codes : [0])) {
    if (code === 0) { t.sgr.fg = ''; t.sgr.bg = ''; t.sgr.bold = false; }
    else if (code === 1) t.sgr.bold = true;
    else if (code === 22) t.sgr.bold = false;
    else if (code === 39) t.sgr.fg = '';
    else if (code === 49) t.sgr.bg = '';
    else if (code >= 30 && code <= 37) t.sgr.fg = TERM_SGR[code - 30];
    else if (code >= 90 && code <= 97) t.sgr.fg = TERM_SGR[code - 90 + 8];
    else if (code >= 40 && code <= 47) t.sgr.bg = TERM_SGR[code - 40 + 10];
    else if (code >= 100 && code <= 107) t.sgr.bg = TERM_SGR[code - 100 + 18];
  }
  t.sgrCss = termSgrCss(t);
}

function termNewline(t) {
  t.curRow++;
  t.curCol = 0;
  if (t.curRow >= t.rows) {
    // 屏幕顶部那一行不是丢掉，而是推进 history 供滚轮回看（上限 TERM_SCROLLBACK）。
    const gone = t.buf.shift();
    if (gone) {
      t.history.push(gone);
      const over = t.history.length - TERM_SCROLLBACK;
      if (over > 0) {
        t.history.splice(0, over);
        t.histRendered = -1;  // 前端裁剪过：DOM 与数组不再一一对应，下一帧整块重建
      }
    }
    t.curRow = t.rows - 1;
  }
}

function termWrite(t, ch) {
  if (t.curCol >= t.cols) termNewline(t);
  const row = t.buf[t.curRow] || (t.buf[t.curRow] = []);
  row[t.curCol] = { t: ch, st: t.sgrCss };
  t.curCol++;
}

function termClamp(value, lo, hi) { return Math.max(lo, Math.min(hi, value)); }

function termEraseDisplay(t, mode) {
  if (mode === 2 || mode === 3) { t.buf = []; t.curRow = 0; t.curCol = 0; return; }
  if (mode === 0) {
    const row = t.buf[t.curRow] || (t.buf[t.curRow] = []);
    row.length = t.curCol;
    t.buf.length = t.curRow + 1;
  }
}

function termEraseLine(t, mode) {
  const row = t.buf[t.curRow] || (t.buf[t.curRow] = []);
  if (mode === 2) row.length = 0;
  else if (mode === 1) { for (let c = 0; c <= t.curCol; c++) row[c] = undefined; }
  else row.length = t.curCol;
}

function termCsi(t, params, final) {
  const nums = params.replace(/\?/g, '').split(';').map((n) => (n === '' ? 0 : parseInt(n, 10)));
  const n0 = nums[0] || 0;
  const arg = (i, fallback = 1) => nums[i] || fallback;
  switch (final) {
    case 'm': termSgr(t, nums.filter((n) => !Number.isNaN(n))); break;
    case 'J': termEraseDisplay(t, n0); break;
    case 'K': termEraseLine(t, n0); break;
    case 'H': case 'f':
      t.curRow = termClamp(arg(0, 1) - 1, 0, t.rows - 1);
      t.curCol = termClamp(arg(1, 1) - 1, 0, t.cols - 1);
      break;
    case 'A': t.curRow = Math.max(0, t.curRow - arg(0)); break;
    case 'B': t.curRow = Math.min(t.rows - 1, t.curRow + arg(0)); break;
    case 'C': t.curCol = Math.min(t.cols - 1, t.curCol + arg(0)); break;
    case 'D': t.curCol = Math.max(0, t.curCol - arg(0)); break;
    case 'G': t.curCol = termClamp(arg(0, 1) - 1, 0, t.cols - 1); break;
    case 'd': t.curRow = termClamp(arg(0, 1) - 1, 0, t.rows - 1); break;
    default: break; // 其余（光标显隐 / 滚动 / 私有模式）忽略 —— 最小子集
  }
}

/* 状态机跨 chunk 保持：转义序列可能被切成两半（见 t.decoder 的流式解码）。 */
function termProcess(t, text) {
  for (const ch of text) {
    if (t.state === 3) { if (ch === '\x07') t.state = 0; continue; }   // OSC
    if (t.state === 2) {
      if (ch >= '@' && ch <= '~') { termCsi(t, t.csi, ch); t.csi = ''; t.state = 0; }
      else t.csi += ch;
      continue;
    }
    if (t.state === 1) {
      if (ch === '[') { t.state = 2; t.csi = ''; }
      else if (ch === ']') t.state = 3;
      else t.state = 0;
      continue;
    }
    if (ch === '\x1b') { t.state = 1; continue; }
    if (ch === '\r') { t.curCol = 0; continue; }
    if (ch === '\n') { termNewline(t); continue; }
    if (ch === '\b') { t.curCol = Math.max(0, t.curCol - 1); continue; }
    if (ch === '\t') { const n = 8 - (t.curCol % 8); for (let i = 0; i < n; i++) termWrite(t, ' '); continue; }
    if (ch < ' ') continue;  // 其余控制字符（含 BEL）忽略
    termWrite(t, ch);
  }
}

function termFeed(t, b64) {
  if (!t || t.closed) return;
  if (!t.decoder) t.decoder = new TextDecoder('utf-8');
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const text = t.decoder.decode(bytes, { stream: true });
  termProcess(t, text);
  // 终端里跑起 dev server 后，启动横幅会打印本机地址；按后端分块可能把一行拆开，
  // 所以在本终端最近 4KB 文本里找，而不是只看这一块。
  t.scanBuf = ((t.scanBuf || '') + text).slice(-4096);
  maybeLocalUrl(t.scanBuf);
  // 非激活的终端只更新缓冲、不碰 DOM：切回来时 termRender 会整屏画出来。
  if (t.id === termActive) termRender(t);
}

function termReset(t) {
  t.buf = [];
  t.curRow = 0;
  t.curCol = 0;
  t.state = 0;
  t.csi = '';
  t.sgr = { fg: '', bg: '', bold: false };
  t.sgrCss = '';
  t.decoder = new TextDecoder('utf-8');
  // 回滚缓冲一并清掉（「清空」按钮与新会话都从这里走）。
  t.history = [];
  t.histRendered = 0;
  if (t.histEl) t.histEl.replaceChildren();
  termLineReset(t);
}

/* ── 多终端：容器 / 标签条 / 激活切换 ─────────────────────────────────
 * 每个终端一个 `.term-interactive`，都挂在 #term-stage 里，只显示激活的那个。
 * 这样切标签是「换显示」而不是「重建 DOM」—— 缓冲、滚动位置、光标全都留着。
 * ─────────────────────────────────────────────────────────────────── */

function attachTermEl(t) {
  const host = el('div', 'term term-interactive');
  host.tabIndex = 0;
  host.dataset.term = t.id;
  host.hidden = true;
  host.addEventListener('keydown', (ev) => termKeydown(t, ev));
  // 直点终端时焦点会落到宿主 div 上（tabIndex=0，保证 Ctrl+C 这类快捷键能收到）。
  // 但中文输入法只对「可编辑元素」生效，所以真正持有焦点的是下面这个隐藏 textarea：
  // 松开鼠标且没有拖选时把焦点移过去，拖选时不动它（否则选区会被打断）。
  host.addEventListener('mouseup', () => { if (!termSelectionText(t)) termFocus(t); });
  const ime = el('textarea', 'term-ime');
  ime.setAttribute('autocomplete', 'off');
  ime.setAttribute('autocorrect', 'off');
  ime.setAttribute('autocapitalize', 'off');
  ime.setAttribute('spellcheck', 'false');
  ime.addEventListener('compositionstart', () => { t.composing = true; });
  ime.addEventListener('compositionend', (ev) => {
    t.composing = false;
    // 组合结束才会拿到最终文本；有的输入法 event.data 为空，退回读 textarea 的值。
    termImeCommit(t, ev.data != null ? ev.data : ime.value);
  });
  // 非组合输入（表情面板 / 手写 / 语音）走 input；组合提交已由 compositionend
  // 送过，这里靠「值已被清空」自然去重，不会重复插入。
  ime.addEventListener('input', () => {
    if (t.composing) return;
    termImeCommit(t, ime.value);
  });
  ime.addEventListener('blur', () => { t.composing = false; });
  host.appendChild(ime);
  t.ime = ime;
  host.addEventListener('paste', (ev) => termPaste(t, ev));
  // 右键菜单（复制 / 剪切 / 粘贴）：终端的输出是只读历史，浏览器默认菜单在这里
  // 只有「全选」可用；给一个与 Ctrl 快捷键同源的菜单，鼠标党也有路。
  host.addEventListener('contextmenu', (ev) => {
    ev.preventDefault();
    openTermMenu(t, ev);
  });
  t.el = host;
  $('term-stage').appendChild(host);
  return host;
}

/** 把焦点交给终端的输入法宿主（没有 textarea 时退回宿主 div）。 */
function termFocus(t) {
  if (!t || t.closed) return;
  if (t.composing) return;   // 组合进行中别抢焦点，否则会把未上屏的拼音弄丢
  const target = t.ime || t.el;
  if (!target || document.activeElement === target) return;
  target.focus({ preventScroll: true });
}

/** IME / input 提交的一段文本：清空暂存区后按终端模式送进去。 */
function termImeCommit(t, text) {
  const value = String(text || '');
  if (t.ime) t.ime.value = '';
  if (!value || !t.open || t.closed) return;
  termImePlace(t);
  termInsertText(t, value);
}

/** 把隐藏 textarea 挪到光标格附近：浏览器的输入法候选框锚在它身上。 */
function termImePlace(t) {
  const ime = t && t.ime;
  if (!ime || !t.el) return;
  const cur = t.el.querySelector('.term-cursor');
  const base = t.el.getBoundingClientRect();
  const box = cur ? cur.getBoundingClientRect() : null;
  if (box) {
    ime.style.left = `${Math.max(0, Math.min(base.width - 1, box.left - base.left))}px`;
    ime.style.top = `${Math.max(0, Math.min(base.height - 1, box.top - base.top))}px`;
  } else {
    ime.style.left = '8px';
    ime.style.top = '4px';
  }
}


function renderTermTabs() {
  const bar = $('term-tabs');
  if (!bar) return;
  const list = [...TERMS.values()].sort((a, b) => a.seq - b.seq);
  paintTermOpenBtn();
  // 只有一个终端时不显示标签条（没有「你在哪个」可言）；关闭按钮在 term-bar 上。
  bar.hidden = list.length < 2;
  bar.replaceChildren();
  if (bar.hidden) return;
  for (const t of list) {
    const tab = el('div', 'term-tab');
    tab.dataset.term = t.id;
    tab.title = `${t.shell || '终端'}${t.closed ? '（已退出）' : ''} · 点击切换`;
    if (t.id === termActive) tab.classList.add('is-active');
    if (t.closed) tab.classList.add('is-dead');
    tab.append(
      el('span', 'term-tab-seq', String(t.seq)),
      el('span', 'term-tab-name', t.shell || '终端'),
    );
    const x = el('button', 'term-tab-x', '✕');
    x.title = '关闭这个终端';
    x.addEventListener('click', (ev) => { ev.stopPropagation(); termClose(t.id); });
    tab.appendChild(x);
    tab.addEventListener('click', () => setActiveTerm(t.id));
    bar.appendChild(tab);
  }
  // 开得多时逐个关太累，给一个总闸（只在 ≥2 个时出现，单终端不需要）。
  const all = el('button', 'term-tabs-clear', '全部关闭');
  all.title = '关闭所有终端并收掉它们的进程';
  all.addEventListener('click', () => termCloseAll());
  bar.appendChild(all);
}

/** 激活某个终端（null = 一个都不激活）。切标签只改显示，不重建 DOM。 */
function setActiveTerm(termId) {
  const t = termId ? TERMS.get(termId) || null : null;
  termActive = t ? t.id : null;
  for (const other of TERMS.values()) {
    if (other.el) other.el.hidden = other.id !== termActive;
  }
  const empty = $('term-empty');
  if (empty) empty.hidden = TERMS.size > 0;
  renderTermTabs();
  const closeBtn = $('btn-term-close');
  if (!t) {
    if (closeBtn) closeBtn.hidden = true;
    $('term-mode').hidden = true;
    termNote('点「新建终端」开始；可同时开多个，各自独立');
    return;
  }
  if (closeBtn) closeBtn.hidden = false;
  paintTermMode(t);
  termRender(t);
  if (t.el && !t.closed) termFocus(t);
}

/** 终端说明行（没有激活终端时也要能写字）。 */
function termNote(text) {
  const note = $('term-note');
  if (note) note.textContent = text;
}

/* ── 终端剪贴板（复制 / 剪切 / 粘贴）─────────────────────────────────
 * 桌面壳里走窄桥 `vibeShell.clipboardRead/Write`（只渡纯文本，见 preload.js）；
 * 浏览器里退回标准 `navigator.clipboard`。两条路都失败时，复制再用隐藏 textarea +
 * execCommand 兜底 —— 宁可多一层，也不要让高频动作时灵时不灵。
 * ─────────────────────────────────────────────────────────────────── */

/** 当前终端里用户选中的文本；没有选区 / 选区不在这个终端里 → 空串。 */
function termSelectionText(t) {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !t || !t.el) return '';
  const node = sel.anchorNode;
  if (!node || !t.el.contains(node)) return '';
  // 每行尾部是为了补满列宽而填的空格，不属于用户选中的内容，进剪贴板只会变脏。
  return sel.toString()
    .split('\n')
    .map((line) => line.replace(/[ \t]+$/, ''))
    .join('\n')
    .replace(/\n+$/, '');
}

async function termClipboardWrite(text) {
  if (!text) return false;
  const shell = window.vibeShell;
  if (shell && shell.clipboardWrite) {
    try {
      const res = await shell.clipboardWrite(text);
      if (res && res.ok) return true;
    } catch { /* 落到 navigator / execCommand */ }
  }
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* 落到 execCommand */ }
  const ta = el('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  return ok;
}

/** 读剪贴板；**失败返回 null**（区别于「剪贴板是空的」）。 */
async function termClipboardRead() {
  const shell = window.vibeShell;
  if (shell && shell.clipboardRead) {
    try {
      const text = await shell.clipboardRead();
      if (typeof text === 'string') return text;
    } catch { /* 落到 navigator */ }
  }
  try {
    if (navigator.clipboard && navigator.clipboard.readText) {
      return await navigator.clipboard.readText();
    }
  } catch { /* 权限被拒 */ }
  return null;
}

/** 复制（cut=true 时额外收起选区）。返回是否真的写进了剪贴板。 */
async function termCopySelection(t, cut = false) {
  const text = termSelectionText(t);
  if (!text) return false;
  if (!(await termClipboardWrite(text))) {
    toast('复制失败：剪贴板不可用', 'error');
    return false;
  }
  if (cut) {
    // 终端输出是只读历史，不能像文本框那样真的「剪掉」。剪切的语义到此为止：
    // 内容进剪贴板、选区收起（与 Windows Terminal 一致）。
    const sel = window.getSelection();
    if (sel) sel.removeAllRanges();
  }
  return true;
}

/** 把文本送进终端：PTY 原样送；管道模式沿用本地行编辑（见 termPipeKey）。 */
function termInsertText(t, text) {
  if (!t || !t.open || t.closed || !text) return;
  if (t.mode !== 'pty') {
    // 管道模式：粘进本地行；换行视作回车逐行提交，别把控制字符塞进 shell 的行里。
    const parts = text.replace(/\r\n?/g, '\n').split('\n');
    for (let i = 0; i < parts.length; i++) {
      for (const ch of parts[i]) termLineEdit(t, ch);
      if (i < parts.length - 1) termPipeCommit(t);
    }
    return;
  }
  // PTY：剪贴板里的换行统一成回车（Windows 复制来的多为 CRLF；原样发会多按一次回车）。
  termInput(t, text.replace(/\r\n/g, '\r').replace(/\n/g, '\r'));
}

async function termPasteClipboard(t) {
  const text = await termClipboardRead();
  if (text === null) {
    toast('粘贴失败：读不到剪贴板（浏览器权限）', 'error');
    return;
  }
  termInsertText(t, text);
  termFocus(t);
}

/** Ctrl+C / Ctrl+X / Ctrl+V 的终端语义。返回 true 表示事件已被消费。
 *
 * Ctrl+C 的两种含义按「有没有选区」区分：选中了就是复制，没选中才是中断（\x03）。
 * 一律复制会让用户没法打断命令；一律中断则复制没法用 —— 只能二选一，取舍写在这里。
 */
function termClipboardKey(t, event) {
  if (!event.ctrlKey || event.altKey || event.shiftKey || event.metaKey) return false;
  const k = event.key;
  if (k === 'c' || k === 'C') {
    if (!termSelectionText(t)) return false;  // 没有选区 → 交回原有的中断逻辑
    termCopySelection(t, false);
    event.preventDefault();
    return true;
  }
  if (k === 'x' || k === 'X') {
    if (!termSelectionText(t)) return false;
    termCopySelection(t, true);
    event.preventDefault();
    return true;
  }
  if (k === 'v' || k === 'V') {
    termPasteClipboard(t);
    event.preventDefault();
    return true;
  }
  return false;
}

/* 右键菜单直接复用文件树那套 `openTreeMenu`（定位 / 点外面关闭 / Esc 全共用），
 * 只是这里多了两项能力：禁用的条目与右侧快捷键提示。 */
function openTermMenu(t, ev) {
  const hasSelection = !!termSelectionText(t);
  openTreeMenu(ev.clientX, ev.clientY, [
    { label: '复制', hint: 'Ctrl+C', disabled: !hasSelection, run: () => termCopySelection(t, false) },
    { label: '剪切', hint: 'Ctrl+X', disabled: !hasSelection, run: () => termCopySelection(t, true) },
    { label: '粘贴', hint: 'Ctrl+V', disabled: t.closed, run: () => termPasteClipboard(t) },
  ]);
}

/** 当前终端的模式 chip 与说明行（PTY / 管道 / 已退出）。 */
function paintTermMode(t) {
  const chip = $('term-mode');
  chip.hidden = false;
  chip.classList.toggle('is-warn', t.mode !== 'pty' || t.closed);
  chip.textContent = t.closed ? '已退出' : (t.mode === 'pty' ? 'PTY' : '管道模式');
  $('term-note').textContent = t.note
    || (t.mode === 'pty' ? '交互式终端 · 与 Agent 通道隔离' : '降级为管道模式：交互能力受限');
}

async function termOpen() {
  if (!termEnabled) { toast('交互式终端已被配置关闭（VIBE_TERM=0）', 'error'); return; }
  if (termMax > 0 && TERMS.size >= termMax) {
    toast(`终端数量已达配置上限（VIBE_TERM_MAX=${termMax}）。改成 0 即可不限。`, 'error');
    return;
  }
  const btn = $('btn-term-open');
  // 连点保护：一次只发起一个（否则会起两个进程、只留一个 id）。
  if (btn.disabled) return;
  btn.disabled = true;
  try {
    // 先量尺寸再开：新会话的 DOM 还没挂上，但 stage 本身已经撑满终端区。
    const size = termMeasure($('term-stage')) || { cols: 80, rows: 24 };
    const res = await api.post('/api/term/open', { cols: size.cols, rows: size.rows });
    const t = createTerm({
      id: res.termId,
      seq: ++termSeq,
      shell: res.shell || 'shell',
      mode: res.mode || 'pty',
    });
    t.cols = res.cols || size.cols;
    t.rows = res.rows || size.rows;
    if (res.fallbackReason) t.note = `降级为管道模式：${res.fallbackReason}`;
    else if (res.kind === 'remote') t.note = `远程终端 · ${res.connection || res.shell}（跟随当前工作区）`;
    TERMS.set(t.id, t);
    attachTermEl(t);
    setActiveTerm(t.id);
    termRender(t);
    // 新终端拿到真实尺寸后再训一次后端（open 时量的是 stage 的尺寸，通常一致）。
    termResize(t);
  } catch (err) {
    toast(`终端启动失败：${err.message}`, 'error');
  } finally {
    paintTermOpenBtn();
  }
}

/** 从界面上摘掉一个终端（删表项 + 删 DOM），**不碰后端进程**。 */
function dropTerm(t) {
  TERMS.delete(t.id);
  if (t.el) { t.el.remove(); t.el = null; }
}

/** 关闭一个终端：摘标签、删 DOM、收后端进程。对「已退出」的会话只做前两步。 */
async function termClose(termId) {
  const t = TERMS.get(termId);
  if (!t) return;
  // 先从表里摘掉：否则后端回推的 `term.exit` 会把它当成「还活着的会话」再收尾一次。
  const wasActive = termActive === t.id;
  dropTerm(t);
  if (wasActive) {
    // 关掉当前标签后落到最后一个还开着的终端（没有就清空视图）。
    const rest = [...TERMS.values()].sort((a, b) => a.seq - b.seq);
    setActiveTerm(rest.length ? rest[rest.length - 1].id : null);
  } else {
    renderTermTabs();
    const empty = $('term-empty');
    if (empty) empty.hidden = TERMS.size > 0;
  }
  if (t.closed) return;   // 进程早就没了，不用再请求一次
  try { await api.post('/api/term/close', { termId: t.id }); } catch { /* 后端已清理 */ }
}

/** 全部关闭。并发发出：后端每关一个都要收进程树，串行会明显变慢。 */
async function termCloseAll() {
  const ids = [...TERMS.keys()];
  await Promise.all(ids.map((id) => termClose(id)));
}

/** 刷新页面 / 切换工作区后跟后端对账：恢复还活着的会话，撤掉已经不在的标签。
 *
 * 为什么要做这件事：终端进程活在**后端进程**里，页面刷新不会杀掉它们。不恢复的话，
 * 它们就成了「看不见却还在跑」的进程，用户只能去任务管理器里找。反过来，切换工作区
 * 会重建 Session（终端被一并收掉），不撤销的话界面上会留一个点进去什么都不发生的死标签。
 */
async function loadTermSessions() {
  if (!termEnabled) return;
  let res;
  try { res = await api.get('/api/term'); } catch { return; }
  if (!res || !res.enabled) return;
  termMax = Number(res.maxSessions) || 0;
  paintTermOpenBtn();
  const alive = new Set();
  for (const row of res.sessions || []) {
    const id = String(row.termId || '');
    if (!id) continue;
    alive.add(id);
    if (TERMS.has(id)) continue;
    const t = createTerm({
      id,
      seq: ++termSeq,
      shell: row.shell || 'shell',
      mode: row.mode || 'pty',
    });
    t.cols = Number(row.cols) || 80;
    t.rows = Number(row.rows) || 24;
    t.note = row.kind === 'remote'
      ? `远程终端 · ${row.connection || row.shell}（刷新页面后接回来的终端，之前的滚动输出不会保留）`
      : '刷新页面后接回来的终端（之前的滚动输出不会保留）';
    TERMS.set(id, t);
    attachTermEl(t);
  }
  // 对账（跳过 3 秒内新建的：那条 open 请求可能还没被这次 GET 看到）。
  const now = Date.now();
  for (const t of [...TERMS.values()]) {
    if (alive.has(t.id) || now - t.createdAt < 3000) continue;
    const wasActive = termActive === t.id;
    dropTerm(t);
    if (wasActive) termActive = null;
  }
  const list = [...TERMS.values()].sort((a, b) => a.seq - b.seq);
  if (list.length) setActiveTerm(termActive && TERMS.has(termActive) ? termActive : list[list.length - 1].id);
  else setActiveTerm(null);
}

/** 「新建终端」按钮的可用性。只有用户显式配了 `VIBE_TERM_MAX` 才会被禁用。 */
function paintTermOpenBtn() {
  const btn = $('btn-term-open');
  if (!btn) return;
  const full = termMax > 0 && TERMS.size >= termMax;
  btn.disabled = full;
  btn.title = full
    ? `已达配置上限（VIBE_TERM_MAX=${termMax}）。设 VIBE_TERM_MAX=0 可解除。`
    : '新建一个终端（可以开任意多个）';
}

/** 进程自己退出了（敲 exit / 被 kill）：标签留着 —— 最后的输出往往才是要看的东西。 */
function termMarkClosed(t, note, exitCode = 0) {
  t.open = false;
  t.closed = true;
  t.exitCode = exitCode;
  t.note = note;
  if (t.id === termActive) { paintTermMode(t); termRender(t); }
  renderTermTabs();
}

function termOnExit(t, exitCode) {
  if (!t || t.closed) return;
  termMarkClosed(
    t,
    `终端已结束${exitCode ? `（退出码 ${exitCode}）` : ''} · 点标签可回看输出，✕ 收掉它`,
    exitCode,
  );
}

function termInput(t, data) {
  if (!t || t.closed || !data) return;
  api.post('/api/term/input', { termId: t.id, data }).catch(() => {});
}

/** 列宽变化后按新宽度重排（软折行）。
 *
 * 不重排的话，缩小窗口后每行多出来的格子会被 `termRow` 的 `c < term.cols`
 * 直接裁掉 —— 右侧内容整块消失，就是实测反馈的「放大再缩小显示不全」。
 * 历史行同样要重排，否则滚回去看的内容只剩左半截。
 */
function termReflow(t, newCols) {
  const splitRow = (row) => {
    const line = row || [];
    let len = line.length;
    while (len > 0 && !line[len - 1]) len--;  // 尾部的空格单元不参与折行
    const chunks = Math.max(1, Math.ceil(len / newCols));
    const out = [];
    for (let i = 0; i < chunks; i++) out.push(line.slice(i * newCols, (i + 1) * newCols));
    return out;
  };
  // 光标映射：先数旧缓冲里它前面有多少行，重排后再落到对应的新行 / 新列。
  let cursorRow = 0;
  for (let r = 0; r < t.curRow && r < t.buf.length; r++) {
    cursorRow += splitRow(t.buf[r]).length;
  }
  const curChunks = splitRow(t.buf[t.curRow]).length;
  const chunk = Math.min(curChunks - 1, Math.floor(t.curCol / newCols));
  const buf = [];
  for (const row of t.buf) buf.push(...splitRow(row));
  t.buf = buf;
  t.curRow = Math.min(cursorRow + Math.max(0, chunk), Math.max(0, buf.length - 1));
  t.curCol = Math.min(newCols - 1, t.curCol % newCols);
  const hist = [];
  for (const row of t.history) hist.push(...splitRow(row));
  const over = hist.length - TERM_SCROLLBACK;
  if (over > 0) hist.splice(0, over);
  t.history = hist;
  t.histRendered = 0;  // 行内容变了，DOM 必须整块重建
}

/** 量一个容器能放下多少列 / 行（等宽字符测度）。容器不可见时返回 null。 */
function termMeasure(host) {
  if (!host) return null;
  // 切到别的页签 / 折叠面板 / 非激活终端时尺寸为 0：此时绝不能重算 —— 按 0 宽折出来的行
  // 切回来也还原不回去（软折行不可逆）。等真正可见时 ResizeObserver 会再触发。
  if (host.clientWidth < 40 || host.clientHeight < 20) return null;
  const probe = el('span', null, 'M'.repeat(50));
  probe.style.position = 'absolute';
  probe.style.visibility = 'hidden';
  probe.style.whiteSpace = 'pre';
  host.appendChild(probe);
  const charW = probe.getBoundingClientRect().width / 50 || 8;
  probe.remove();
  const lineH = parseFloat(getComputedStyle(host).lineHeight) || 18;
  return {
    cols: Math.max(20, Math.floor((host.clientWidth - 16) / charW)),
    rows: Math.max(3, Math.floor((host.clientHeight - 8) / lineH)),
  };
}

/** 容器尺寸变化 → 重排并通知后端。 */
function termResize(t) {
  const size = termMeasure(t && t.el);
  if (!size) return;
  // 先重排再改尺寸：termReflow 要按「新列宽」切行，但光标映射依赖旧缓冲。
  if (size.cols !== t.cols) termReflow(t, size.cols);
  t.cols = size.cols;
  t.rows = size.rows;
  // 行数变少：把屏幕顶部溢出的行推进 history —— 否则光标和最新输出会跑到可视区之外，
  // 界面上看着就是「内容缺了一截」。
  while (t.buf.length > t.rows) {
    const gone = t.buf.shift();
    if (gone) {
      t.history.push(gone);
      const over = t.history.length - TERM_SCROLLBACK;
      if (over > 0) {
        t.history.splice(0, over);
        t.histRendered = 0;
      }
    }
    t.curRow = Math.max(0, t.curRow - 1);
  }
  if (t.curRow > t.rows - 1) t.curRow = t.rows - 1;
  if (t.curCol > t.cols - 1) t.curCol = t.cols - 1;
  if (!t.closed) {
    api.post('/api/term/resize', { termId: t.id, cols: t.cols, rows: t.rows })
      .catch(() => {});
  }
  termRender(t);
}

/** 面板尺寸变了：只重算**当前可见**的那个（隐藏的终端量不出尺寸）。 */
function termResizeActive() {
  const t = activeTerm();
  if (t) termResize(t);
}

/* ── 管道模式的本地行编辑 ──────────────────────────────────────────────
 * 后端降级成管道后没有 TTY 线路规程：shell（实测 PowerShell）不做任何行编辑，还会把我们
 * 写进去的每个字节原样回显。于是「退格」那一颗 0x7F 既删不掉字符、又被回显成一个控制字符
 * 渲染成空白格，看着就像插了个空格；它还会被当成命令正文一起交给解析器。
 * 所以管道模式的行编辑放在前端：本地维护当前行 + 本地回显，只把「整行 + 回车」交给后端。
 * 后端 shell 仍会回显这一行 —— 回车前把光标对回行首，回显即写到同一批格子上，不产生重影。
 * ─────────────────────────────────────────────────────────────────── */
function termLineReset(t) {
  t.line = '';
  t.lineCur = 0;
  t.lineStart = null;
}

function termLineDraw(t) {
  const st = t.lineStart;
  if (!st) return;
  t.buf.length = st.row + 1;
  const row = t.buf[st.row] || (t.buf[st.row] = []);
  row.length = st.col;
  t.curRow = st.row;
  t.curCol = st.col;
  for (const ch of t.line) termWrite(t, ch);
  t.curCol = Math.min(t.cols - 1, st.col + t.lineCur);  // 光标停在逻辑插入点
  termRender(t);
}

/** kind: 'BS' 删插入点前一个字符 | 'DEL' 删插入点处字符 | 其它为要插入的字符。 */
function termLineEdit(t, kind) {
  if (!t.lineStart) t.lineStart = { row: t.curRow, col: t.curCol };
  if (kind === 'BS') {
    if (t.lineCur === 0) return;
    t.line = t.line.slice(0, t.lineCur - 1) + t.line.slice(t.lineCur);
    t.lineCur--;
  } else if (kind === 'DEL') {
    if (t.lineCur >= t.line.length) return;
    t.line = t.line.slice(0, t.lineCur) + t.line.slice(t.lineCur + 1);
  } else {
    t.line = t.line.slice(0, t.lineCur) + kind + t.line.slice(t.lineCur);
    t.lineCur++;
  }
  termLineDraw(t);
}

/** 提交当前本地行：整行 + 回车交给后端；光标对回行首让 shell 的回显覆盖同位置。 */
function termPipeCommit(t) {
  const line = t.line;
  const st = t.lineStart;
  if (st) { t.curRow = st.row; t.curCol = st.col; }
  termLineReset(t);
  termRender(t);
  termInput(t, line + '\r');
}

function termPipeKey(t, event) {
  const k = event.key;
  if (k === 'Enter') { termPipeCommit(t); event.preventDefault(); return; }
  if (k === 'Backspace') { termLineEdit(t, 'BS'); event.preventDefault(); return; }
  if (k === 'Delete') { termLineEdit(t, 'DEL'); event.preventDefault(); return; }
  if (k === 'ArrowLeft' || k === 'ArrowRight' || k === 'Home' || k === 'End') {
    if (k === 'ArrowLeft') t.lineCur = Math.max(0, t.lineCur - 1);
    else if (k === 'ArrowRight') t.lineCur = Math.min(t.line.length, t.lineCur + 1);
    else if (k === 'Home') t.lineCur = 0;
    else t.lineCur = t.line.length;
    termLineDraw(t);
    event.preventDefault();
    return;
  }
  if (k === 'Tab') { termInput(t, '\t'); event.preventDefault(); return; }
  if (k === 'Escape') { termInput(t, '\x1b'); event.preventDefault(); return; }
  if (termClipboardKey(t, event)) return;
  if (event.ctrlKey && k.length === 1) {
    const code = k.toUpperCase().charCodeAt(0) - 64;
    if (code >= 1 && code <= 26) {
      if (code === 3) termLineReset(t);  // Ctrl-C：丢弃当前本地行（管道下后端不会清行）
      termInput(t, String.fromCharCode(code));
      event.preventDefault();
    }
    return;
  }
  if (k.length === 1 && !event.metaKey && !event.altKey) { termLineEdit(t, k); event.preventDefault(); return; }
  const seq = { ArrowUp: '\x1b[A', ArrowDown: '\x1b[B', PageUp: '\x1b[5~', PageDown: '\x1b[6~' }[k];
  if (seq) { termInput(t, seq); event.preventDefault(); }
}

function termKeydown(t, event) {
  if (!t || !t.open || t.closed) return;
  // 输入法组合期间的按键属于 IME（拼音字母、选词、上屏的回车），一律不拦、
  // 不发终端控制字符；等 compositionend 拿到最终文本再整段送进去。
  if (t.composing || event.isComposing || event.keyCode === 229) return;
  termImePlace(t);
  if (t.mode !== 'pty') { termPipeKey(t, event); return; }
  const k = event.key;
  if (k === 'Enter') { termInput(t, '\r'); event.preventDefault(); return; }
  if (k === 'Backspace') { termInput(t, '\x7f'); event.preventDefault(); return; }
  if (k === 'Tab') { termInput(t, '\t'); event.preventDefault(); return; }
  if (k === 'Escape') { termInput(t, '\x1b'); event.preventDefault(); return; }
  if (termClipboardKey(t, event)) return;
  if (event.ctrlKey && k.length === 1) {
    const code = k.toUpperCase().charCodeAt(0) - 64;
    if (code >= 1 && code <= 26) { termInput(t, String.fromCharCode(code)); event.preventDefault(); }
    return;
  }
  if (k.length === 1 && !event.metaKey && !event.altKey) { termInput(t, k); event.preventDefault(); return; }
  const seq = {
    ArrowUp: '\x1b[A', ArrowDown: '\x1b[B', ArrowRight: '\x1b[C', ArrowLeft: '\x1b[D',
    Home: '\x1b[H', End: '\x1b[F', Delete: '\x1b[3~', PageUp: '\x1b[5~', PageDown: '\x1b[6~',
  }[k];
  if (seq) { termInput(t, seq); event.preventDefault(); }
}

function termPaste(t, event) {
  if (!t || !t.open || t.closed) return;
  const text = (event.clipboardData || window.clipboardData)?.getData('text') || '';
  if (!text) return;
  termInsertText(t, text);
  event.preventDefault();
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
    // 网页预览（O）：只对可渲染的网页文件出现，纯静态服务，任何时候都可用。
    // 远端文件不走本机静态预览（那是本机 jail 里的同一路径），不亮这个按钮。
    previewBtn.hidden = !!entry.remote || !WEB_EXT.test(entry.data.path);
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
  } else if (entry && (entry.kind === 'preview' || entry.kind === 'external' || entry.kind === 'tunnel')) {
    // 预览类标签自带工具条（刷新/新窗口/设备宽度/自检），主工具条整条让位
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
  // 收尾钩子：远端预览标签靠它释放 `ssh -L` 转发（P3）。
  // 关标签 = 不再看那个远端服务，转发就没有继续存在的理由；
  // 钩子里的异常不让它挡住关闭动作 —— 关不掉的标签比泄漏一条转发更难受。
  if (typeof entry.onClose === 'function') {
    try { entry.onClose(entry); } catch (err) { /* 收尾失败不影响关闭 */ }
  }
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
   行号 span 带 data-line，装饰只改样式，不动正文。

   正文按文件后缀做语法着色（与对话区代码块同一套 highlighter）。着色后的
   节点会被按换行重新切成「行 → 片段」，再与行号 gutter 交错 —— 这样跨行的
   字符串/块注释也能保持同一颜色。超大文件直接跳过着色：几万个 span 会把
   打开标签的动作拖成卡顿，而纯文本至少还能读。 */
const MAX_HIGHLIGHT_CHARS = 200_000;

/** 把整段文本着色后按行拆开，返回 rows[i] = 第 i 行的节点数组。 */
function highlightedRows(text, lang) {
  const code = document.createElement('code');
  highlight(code, text, lang);
  const rows = [];
  let row = [];
  for (const node of code.childNodes) {
    const isText = node.nodeType === Node.TEXT_NODE;
    const cls = isText ? '' : node.className;
    const parts = String(node.textContent).split('\n');
    parts.forEach((part, idx) => {
      if (idx > 0) { rows.push(row); row = []; }
      if (!part) return;
      if (isText) {
        row.push(document.createTextNode(part));
      } else {
        const span = document.createElement('span');
        span.className = cls;
        span.textContent = part;
        row.push(span);
      }
    });
  }
  rows.push(row);
  return rows;
}

function buildCodeView(data, entry) {
  const view = el('div', 'file-view');
  const pre = document.createElement('pre');
  const text = String(data.text ?? '');
  const lang = text.length <= MAX_HIGHLIGHT_CHARS ? langForPath(data.path) : '';
  const rows = highlightedRows(text, lang);
  const frag = document.createDocumentFragment();
  rows.forEach((nodes, i) => {
    const span = el('span', 'ln', String(i + 1));
    span.dataset.line = String(i + 1);
    applyDiagMark(span, entry, i + 1);
    frag.appendChild(span);
    for (const node of nodes) frag.appendChild(node);
    if (i < rows.length - 1) frag.appendChild(document.createTextNode('\n'));
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
// SVG 也走 iframe —— 它是浏览器原生可渲染文档，且已在预览 MIME 白名单里。
const WEB_EXT = /\.(html?|xhtml|svg)$/i;

// 预览页**间接依赖**的资源类型：同目录下这些文件变了，页面渲染结果也会变。
// 只按「同目录」放宽，不做依赖图分析 —— 够用，且不会把无关目录的改动算进来。
const PREVIEW_ASSET = /\.(css|js|mjs|cjs|json|map|svg|png|jpe?g|gif|webp|ico|woff2?|ttf|otf)$/i;

const PREVIEW_AUTO_KEY = 'vibe.preview.auto';

function previewAutoOn() {
  try { return localStorage.getItem(PREVIEW_AUTO_KEY) !== '0'; } catch { return true; }
}

function previewSetAuto(on) {
  try { localStorage.setItem(PREVIEW_AUTO_KEY, on ? '1' : '0'); } catch { /* 隐私模式忽略 */ }
}

/* 工作区相对路径统一成 posix 形式，才能和事件里的路径逐字符比对。 */
function previewRel(p) {
  return String(p == null ? '' : p).replace(/\\/g, '/').replace(/^\.\//, '');
}

function previewDir(p) {
  const s = previewRel(p);
  const i = s.lastIndexOf('/');
  return i < 0 ? '' : s.slice(0, i);
}

/* 这次变化是否会影响这个预览页：自己变了，或同目录的资源变了。 */
function previewTouched(path, changed) {
  const target = previewRel(path);
  // 空路径不该匹配任何东西：`''` 的目录也是 `''`，否则会被当成「根目录资源」误刷。
  if (!target) return false;
  if (changed.has(target)) return true;
  const dir = previewDir(target);
  for (const p of changed) {
    if (previewDir(p) === dir && PREVIEW_ASSET.test(p)) return true;
  }
  return false;
}

/* 重载一个预览标签。`force=false` 时尊重「自动刷新」开关（关着只标记「有更新」）。 */
function reloadPreview(entry, force) {
  const frame = entry && entry.frame;
  if (!frame) return;
  if (!force && !entry.autoOn) {
    entry.stale = true;
    entry.status.className = 'chip preview-status is-warn';
    entry.status.textContent = '有更新';
    entry.status.title = '文件已变化；点「刷新」或开启自动刷新即可看到最新内容';
    return;
  }
  entry.stale = false;
  entry.status.className = 'chip preview-status';
  entry.status.textContent = '未自检';
  entry.status.title = '';
  // 同源可读滚动位置，重载后还原 —— 否则每次自动刷新都把人弹回页首。
  let scroll = null;
  try {
    scroll = { x: frame.contentWindow.scrollX, y: frame.contentWindow.scrollY };
  } catch { /* 尚未加载 / 跨源：放弃恢复滚动，不影响刷新本身 */ }
  if (scroll && (scroll.x || scroll.y)) {
    frame.addEventListener('load', () => {
      try { frame.contentWindow.scrollTo(scroll.x, scroll.y); } catch { /* ignore */ }
    }, { once: true });
  }
  frame.src = frame.src;
}

/* 收到 `preview.changed` / `run.finished` 后，挑出受影响的预览标签重载。
   `truncated` 表示路径清单被截断（一次改了太多文件），此时只能全刷。 */
function refreshPreviewTabs(paths, truncated, force) {
  const list = Array.isArray(paths) ? paths : [];
  if (!list.length && !truncated) return;
  const changed = truncated ? null : new Set(list.map(previewRel));
  for (const entry of S.editors.values()) {
    if (!entry || entry.kind !== 'preview') continue;
    if (changed && !previewTouched(entry.path, changed)) continue;
    reloadPreview(entry, !!force);
  }
}

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
  reload.addEventListener('click', () => reloadPreview(entry, true));
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

  const auto = el('button', 'tbtn is-toggle', '自动刷新');
  // 「本机应用」（P2）：把用户自己起的 dev server 嵌进来，看真跑起来的应用。
  // 它是**另一个来源**，不是同一份文件的另一种渲染 —— 所以单独一个入口，
  // 不复用「设备宽度」那套静态预览的身份。
  const externalBtn = el('button', 'tbtn', '本机应用');
  externalBtn.title = '嵌入本机 dev server（http://127.0.0.1:端口），看真实跑起来的应用';
  // 「远端」（P3）：把远端设备上的环回端口经 `ssh -L` 搬到本机环回再看。
  // 它落地在 127.0.0.1，所以复用同一套跨源帧与同一条 CSP 放宽（ADR-039/040）。
  const remoteBtn = el('button', 'tbtn', '远端');
  remoteBtn.title = '把远端设备的回环端口转发到本机，看远端跑着的应用';
  const bar = el('div', 'preview-bar');
  bar.append(reload, popout, device, auto, externalBtn, remoteBtn, el('span', 'preview-spacer'), status, checkBtn, baseBtn);

  // 地址输入条：默认收起，点「本机应用」才展开。
  // 不用 window.prompt —— 那会在 Electron 里抢焦点且无法就地报错；
  // 也不用通用弹窗 —— 全 UI 至今没有模态基建（P0 的右键菜单同理）。
  const srcRow = el('div', 'preview-src');
  srcRow.hidden = true;
  const srcInput = document.createElement('input');
  srcInput.className = 'preview-src-input';
  srcInput.type = 'text';
  srcInput.spellcheck = false;
  srcInput.placeholder = 'http://127.0.0.1:5173/';
  const srcGo = el('button', 'tbtn', '打开');
  srcRow.append(srcInput, srcGo);

  // 远端转发条（P3）：选一条连接 + 填远端端口。
  // 刻意**不提供**「本地端口」「远端主机」两个输入框 —— 那两处一旦可填，
  // `-L` 的规格串就变成用户可控，等于把远端内网的任意服务拽到本机。
  const remoteRow = el('div', 'preview-src');
  remoteRow.hidden = true;
  const connSel = document.createElement('select');
  connSel.className = 'preview-device';
  const remotePort = document.createElement('input');
  remotePort.className = 'preview-src-input';
  remotePort.type = 'number';
  remotePort.min = '1';
  remotePort.max = '65535';
  remotePort.placeholder = '远端端口，如 5173';
  const remoteGo = el('button', 'tbtn', '转发并预览');
  remoteRow.append(connSel, remotePort, remoteGo);

  const stage = el('div', 'preview-stage');
  stage.appendChild(frame);
  node.append(bar, srcRow, remoteRow, stage);

  const entry = openEditor(id, '🌐', `${baseName(path)}（预览）`, node, {
    kind: 'preview', path, previewPath: previewPath(path),
  });
  entry.frame = frame;
  entry.stage = stage;
  entry.status = status;
  entry.device = device;
  entry.auto = auto;
  entry.autoOn = previewAutoOn();
  entry.stale = false;

  const paintAuto = () => {
    auto.classList.toggle('on', entry.autoOn);
    auto.title = entry.autoOn
      ? '已开启：文件变化后自动重载（点击关闭）'
      : '已关闭：文件变化后只标记「有更新」（点击开启）';
  };
  auto.addEventListener('click', () => {
    entry.autoOn = !entry.autoOn;
    previewSetAuto(entry.autoOn);
    paintAuto();
    // 重新打开时立刻补一次，免得还挂着「有更新」却要再点一下。
    if (entry.autoOn && entry.stale) reloadPreview(entry, true);
  });
  paintAuto();

  // 自动刷新依赖文件监听（VIBE_INDEX_WATCH）。关着时如实禁用并说明原因，
  // 而不是留一个「点了没反应」的开关 —— 与截图自检在浏览器直开时的处置一致。
  api.get('/api/index/status').then((st) => {
    if (st && st.watching === false) {
      entry.autoOn = false;
      entry.watchOff = true;
      auto.disabled = true;
      auto.textContent = '自动刷新（未启用）';
      paintAuto();
      auto.title = '文件监听已关闭（VIBE_INDEX_WATCH=0），预览不会自动更新';
    }
  }).catch(() => { /* 拿不到状态就按可用处理，不影响手动刷新 */ });

  const applyWidth = () => {
    const w = Number(device.value) || 0;
    frame.style.width = w ? `${w}px` : '100%';
    stage.classList.toggle('is-fixed', !!w);
  };
  device.addEventListener('change', applyWidth);
  applyWidth();

  /* ── 真实应用预览（P2 / ADR-039）────────────────────────────────────────
     门控是 `VIBE_PREVIEW_EXTERNAL`，**默认关**（开它要放宽界面 CSP 的 frame-src）。
     探测结果没回来之前一律按"关"处理（fail closed）：宁可晚 100ms 才亮，
     也不能让人先点进去再撞一个 403。 */
  let externalOn = false;
  function paintExternal() {
    externalBtn.disabled = !externalOn;
    externalBtn.title = externalOn
      ? '嵌入本机 dev server（http://127.0.0.1:端口），看真实跑起来的应用'
      : '真实应用预览已关闭（需 VIBE_PREVIEW_EXTERNAL=1）';
    if (!externalOn) srcRow.hidden = true;
  }
  paintExternal();
  api.get('/api/preview/external').then((st) => {
    externalOn = !!(st && st.enabled);
    paintExternal();
  }).catch(() => { /* 探测失败保持禁用：不知道开没开，就当没开 */ });

  externalBtn.addEventListener('click', () => {
    if (!externalOn) return;
    srcRow.hidden = !srcRow.hidden;
    if (!srcRow.hidden) srcInput.focus();
  });

  const submitExternal = async () => {
    const raw = srcInput.value.trim();
    if (!raw) { srcInput.focus(); return; }
    srcGo.disabled = true;
    try {
      // 地址校验在服务端（唯一口径）：它能接受的集合与 CSP 放行的集合必须相同，
      // 否则就会出现"填得进、却是一片白"的预览。
      const res = await api.post('/api/preview/external', { url: raw });
      srcInput.value = res.url;
      srcRow.hidden = true;
      openExternalPreview(res.url, res.port);
    } catch (err) {
      toast(`无法预览该地址：${err.message}`, 'error');
      srcInput.focus();
      srcInput.select();
    } finally {
      srcGo.disabled = false;
    }
  };
  srcGo.addEventListener('click', submitExternal);
  srcInput.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); submitExternal(); }
    else if (ev.key === 'Escape') { srcRow.hidden = true; }
  });

  /* ── 远端应用预览（P3 / ADR-040）────────────────────────────────────────
     双门控：`VIBE_PREVIEW_REMOTE`（这一档功能）+ `VIBE_REMOTE`（连接清单与
     既有 SSH 加固）。两者都开，服务端才回 `enabled:true`。
     与 P2 一样 fail closed：探测没回来之前按"关"处理，不留"点了才知道不行"的按钮。 */
  let remoteOn = false;
  let connectionsLoaded = false;
  function paintRemote() {
    remoteBtn.disabled = !remoteOn;
    remoteBtn.title = remoteOn
      ? '把远端设备的回环端口转发到本机，看远端跑着的应用'
      : '远端预览已关闭（需 VIBE_PREVIEW_REMOTE=1 且 VIBE_REMOTE=1）';
    if (!remoteOn) remoteRow.hidden = true;
  }
  paintRemote();
  api.get('/api/preview/tunnel').then((st) => {
    remoteOn = !!(st && st.enabled);
    paintRemote();
  }).catch(() => { /* 探测失败保持禁用：不知道开没开，就当没开 */ });

  const loadConnections = async () => {
    if (connectionsLoaded) return;
    try {
      const res = await api.get('/api/remote/connections');
      const list = (res && res.connections) || [];
      connSel.replaceChildren(...list.map((conn) => {
        const opt = document.createElement('option');
        opt.value = conn.name;
        // 显示连接名而不是整串 `user@host:22 → /root`：下拉要回答的是
        // 「用哪条连接」，名字是用户自己起的，也是审计里记的那个。
        opt.textContent = conn.name;
        opt.title = conn.display || conn.name;
        return opt;
      }));
      // 已经打开的那条直接选中：绝大多数场景就是「我正在用的这台」。
      if (res && res.active) connSel.value = res.active;
      connectionsLoaded = true;
    } catch (err) {
      toast(`读取远程连接失败：${err.message}`, 'error');
    }
  };

  remoteBtn.addEventListener('click', () => {
    if (!remoteOn) return;
    remoteRow.hidden = !remoteRow.hidden;
    if (!remoteRow.hidden) {
      loadConnections();
      remotePort.focus();
    }
  });

  const submitRemote = async () => {
    const connection = connSel.value;
    const raw = remotePort.value.trim();
    const port = Number(raw);
    if (!connection) { toast('请先选择一条远程连接', 'error'); return; }
    // 空输入只把焦点放回输入框；**填了但不合法**必须说清楚 ——
    // 静默不动会让人以为是按钮坏了（0 / 70000 / 小数都走这条）。
    if (!raw) { remotePort.focus(); return; }
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      toast('远端端口需为 1-65535 的整数', 'error');
      remotePort.focus();
      remotePort.select();
      return;
    }
    remoteGo.disabled = true;
    remoteGo.textContent = '转发中…';
    try {
      // 建转发是服务端动作（本地监听 + ssh 子进程），地址也由服务端给：
      // 界面不拼 `http://127.0.0.1:<port>`，免得两边对"能开什么"理解不一致。
      const res = await api.post('/api/preview/tunnel', { connection, remotePort: port });
      remoteRow.hidden = true;
      openExternalPreview(res.url, res.localPort, {
        kind: 'tunnel',
        id: `tunnel:${res.id}`,
        tunnelId: res.id,
        connection: res.connection,
        remotePort: res.remotePort,
      });
    } catch (err) {
      toast(`无法转发该端口：${err.message}`, 'error');
      remotePort.focus();
      remotePort.select();
    } finally {
      remoteGo.disabled = false;
      remoteGo.textContent = '转发并预览';
    }
  };
  remoteGo.addEventListener('click', submitRemote);
  remotePort.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); submitRemote(); }
    else if (ev.key === 'Escape') { remoteRow.hidden = true; }
  });

  checkBtn.addEventListener('click', () => previewSelfCheck(entry, false));
  baseBtn.addEventListener('click', () => previewSelfCheck(entry, true));
  return entry;
}

/* ── 真实应用预览（P2 / ADR-039）────────────────────────────────────────────
   与上面的静态预览是**两种来源**，安全模型也完全不同：

     · 静态预览：**同源**（`/preview/…`），靠响应自带的 CSP `connect-src 'none'`
       焊死出口。因此**不能**加 sandbox —— 无 `allow-same-origin` 会让文档变成
       不透明源，`'self'` 子资源被整片拦掉（见 openPreview 里的注释）。
     · 外部预览：**跨源**（`http://127.0.0.1:<另一个端口>`），隔离由同源策略本身
       提供：它读不到 `/api/*`、读不到父页 DOM、也拿不到我们的令牌。正因为跨源，
       这里**可以**加 sandbox —— 跨源帧的 `allow-same-origin` 指的是它**自己的**
       源，碰不到我们的。

   刻意不做的事（都是"做不到"或"不该做"，不是漏了）：
     · **不参与自动刷新** —— dev server 自己有 HMR，强行 reload 只会打断它的状态；
     · **不做截图自检** —— 一个跑在别人进程里、随时在变的页面不该成为比对基准，
       而且跨源读不到它的 `onerror`，"自检通过"给不出任何承诺；
     · **不读它的任何内容** —— 连标题都读不到（`contentDocument` 为 null）。 */
function openExternalPreview(url, port, opts = {}) {
  // 两种来源共用同一个帧实现：
  //   · 本机应用（P2）：用户自己起的 dev server，地址是他填的；
  //   · 远端应用（P3）：`ssh -L` 把远端环回端口搬到本机，地址是服务端给的。
  // 二者的安全模型**完全相同**（跨源、读不到我们的接口与 DOM），差别只在
  // 「谁在维持那个端口」—— 所以这里按 kind 分叉，而不是复制一份 openPreview。
  const isTunnel = opts.kind === 'tunnel';
  const id = opts.id || `external:${url}`;
  const existing = S.editors.get(id);
  if (existing) { activateEditor(id); return existing; }

  const node = el('div', 'preview-wrap');
  const frame = document.createElement('iframe');
  frame.className = 'preview-frame';
  frame.setAttribute('title', isTunnel ? `远端应用 ${url}` : `本机应用 ${url}`);
  // 含 allow-same-origin（它自己的源，不是我们的）+ allow-scripts（应用要跑）+ allow-forms；
  // 不含 allow-top-navigation（防把整个工作台跳走）、allow-popups（防钓鱼弹窗）、
  // allow-modals（防 alert/confirm 卡住界面）、allow-downloads。
  frame.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms');
  // 我们的页面 URL 带 token：任何 Referer 都不该带给 dev server。
  frame.setAttribute('referrerpolicy', 'no-referrer');
  frame.src = url;

  const reload = el('button', 'tbtn', '刷新');
  reload.title = '重新加载这个本机应用';
  reload.addEventListener('click', () => { frame.src = frame.src; });
  const popout = el('button', 'tbtn', '新窗口');
  popout.title = '在浏览器新窗口打开（调试时更好用：有 DevTools）';
  popout.addEventListener('click', () => window.open(url, '_blank', 'noopener'));

  const device = document.createElement('select');
  device.className = 'preview-device';
  for (const d of PREVIEW_DEVICES) {
    const opt = document.createElement('option');
    opt.value = String(d.w);
    opt.textContent = d.label;
    device.appendChild(opt);
  }

  const portText = Number(port) ? `:${Number(port)}` : '';
  const badge = isTunnel
    ? el('span', 'chip preview-status is-tunnel',
        `远端 ${opts.connection || ''}${opts.remotePort ? `:${opts.remotePort}` : ''}`)
    : el('span', 'chip preview-status is-external', `外部应用${portText}`);
  badge.title = isTunnel
    ? `内容来自 ${opts.connection || '远端设备'} 的 127.0.0.1:${opts.remotePort}，`
      + `经 SSH 转发到本机 ${port}；跨源隔离，读不到本工作区的接口与文件`
    : '内容来自你自己启动的本机服务；跨源隔离，读不到本工作区的接口与文件';

  const bar = el('div', 'preview-bar');
  bar.append(reload, popout, device, el('span', 'preview-spacer'), badge);

  const stage = el('div', 'preview-stage');
  stage.appendChild(frame);
  node.append(bar, stage);

  const where = url.replace(/^http:\/\//, '');
  const entry = openEditor(
    id,
    isTunnel ? '🔗' : '🖥',
    isTunnel
      ? `${opts.connection || '远端'}:${opts.remotePort}（远端应用）`
      : `${where}（本机应用）`,
    node,
    { kind: isTunnel ? 'tunnel' : 'external', path: url, previewPath: url },
  );
  entry.frame = frame;
  entry.stage = stage;
  entry.device = device;
  entry.externalUrl = url;
  if (isTunnel) {
    entry.tunnelId = opts.tunnelId || '';
    if (entry.tunnelId) OPEN_TUNNELS.add(entry.tunnelId);
    // 关标签即收转发：`ssh -L` 是**本机监听口**，让它跟着标签一起消失，
    // 而不是等会话结束 —— 否则用户会留下一堆「看不见的端口」。
    entry.onClose = () => {
      const tunnelId = entry.tunnelId;
      if (!tunnelId) return;
      OPEN_TUNNELS.delete(tunnelId);
      api.post('/api/preview/tunnel/close', { id: tunnelId }).catch(() => {
        /* 已经断了 / 门控被关：关闭动作本身不该再报错打扰用户 */
      });
    };
  }

  const applyWidth = () => {
    const w = Number(device.value) || 0;
    frame.style.width = w ? `${w}px` : '100%';
    stage.classList.toggle('is-fixed', !!w);
  };
  device.addEventListener('change', applyWidth);
  applyWidth();
  return entry;
}

/* ── 审批期草稿预览（P1-2）────────────────────────────────────────────────
   与上面的 openPreview 是**两条独立链路**，刻意不复用同一个 kind：
     · 草稿不参与自动刷新（它的内容不随磁盘变化，刷新也只是同一份内存正文）；
     · 草稿不做截图自检/基线（一份尚未落盘的内容不该成为比对基准）；
     · 草稿有一个必须显眼的身份提示 —— 它看起来和真页面一模一样，
       如果不标注，人会以为改动已经生效。
   工具条只留「刷新 / 新窗口 / 设备宽度」，其余全部省略。 */
function openDraftPreview(url, primary, paths, approvalId) {
  const id = `draft:${url}`;
  const existing = S.editors.get(id);
  if (existing) { activateEditor(id); return existing; }

  const node = el('div', 'preview-wrap');
  const frame = document.createElement('iframe');
  frame.className = 'preview-frame';
  frame.setAttribute('title', `草稿预览 ${primary}`);
  frame.src = url;

  const reload = el('button', 'tbtn', '刷新');
  reload.title = '重新加载草稿（内容仍是同一份，未落盘）';
  reload.addEventListener('click', () => { frame.src = frame.src; });
  const popout = el('button', 'tbtn', '新窗口');
  popout.title = '在浏览器新窗口打开这个草稿地址';
  popout.addEventListener('click', () => window.open(frame.src, '_blank', 'noopener'));

  const device = document.createElement('select');
  device.className = 'preview-device';
  for (const d of PREVIEW_DEVICES) {
    const opt = document.createElement('option');
    opt.value = String(d.w);
    opt.textContent = d.label;
    device.appendChild(opt);
  }

  const badge = el('span', 'chip preview-status is-draft', '草稿 · 未落盘');
  badge.title = '这是「批准后会写入」的内容；磁盘上现在还是旧版本';

  const bar = el('div', 'preview-bar');
  bar.append(reload, popout, device, el('span', 'preview-spacer'), badge);

  const stage = el('div', 'preview-stage');
  stage.appendChild(frame);
  node.append(bar, stage);

  const title = paths.length > 1 ? `${baseName(primary)} 等 ${paths.length} 个文件（草稿）` : `${baseName(primary)}（草稿预览）`;
  const entry = openEditor(id, '🧪', title, node, {
    kind: 'draft', path: primary, previewPath: url,
  });
  entry.frame = frame;
  entry.stage = stage;
  entry.device = device;
  entry.approvalId = approvalId || '';

  const applyWidth = () => {
    const w = Number(device.value) || 0;
    frame.style.width = w ? `${w}px` : '100%';
    stage.classList.toggle('is-fixed', !!w);
  };
  device.addEventListener('change', applyWidth);
  applyWidth();
  return entry;
}

/* 审批弹窗里的「预览效果」：换 token → 开草稿标签 → 收起弹窗让人能看见它。
   收起的理由：弹窗是模态的，挡着整个工作区；而这一步的意图恰恰是「先看看再决定」，
   与「先看看」按钮同一个去向。审批横幅仍留在顶部，点它即可回到弹窗。 */
async function openDraftFromApproval() {
  if (!S.awaiting) return;
  const approvalId = S.awaiting.approvalId;
  const btn = $('btn-draft-preview');
  btn.disabled = true;
  try {
    const res = await api.post('/api/preview/draft', { approvalId });
    if (!res || !res.url) throw new Error('服务端没有返回草稿地址');
    openDraftPreview(res.url, res.primary, Array.isArray(res.paths) ? res.paths : [], approvalId);
    minimizeApproval();
  } catch (err) {
    toast(`无法生成草稿预览：${err.message}`, 'error');
  } finally {
    btn.disabled = false;
  }
}

/* 审批一了结（放行/拒绝/超时），草稿就作废 —— 服务端同步撤销 token。
   界面这里把标签一并收掉：留着一个注定 404 的标签，下次刷新只会弹一条
   看不懂的错误，不如不给它存在的机会。 */
function closeDraftTabs(approvalId) {
  if (!approvalId) return;
  for (const [id, entry] of [...S.editors.entries()]) {
    if (entry && entry.kind === 'draft' && entry.approvalId === approvalId) closeEditor(id);
  }
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
  if (!entry || entry.remote || !lspSupported(entry.path)) { chip.hidden = true; return; }
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
  // 远端文件由远端机器上的工具链负责，本机 LSP 读到的是同名的本地路径 ——
  // 宁可不出诊断，也不能把别的文件的错误标上来。
  if (entry.remote) return;
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
  if (!range) return null;
  let node = range.startContainer;
  let offset = range.startOffset;
  if (node === root) {
    node = root.childNodes[offset] || root.lastChild;
    offset = 0;
  }
  if (!node || !root.contains(node)) return null;
  // 语法着色把一行切成了「多个 token span + 换行节点」，命中点可能在任意 span 里。
  // 先回到 pre 的直接子节点（这一行的内容），再从行号 gutter 之后量到命中点，
  // 这样 char 始终是「整行内的字符偏移」，LSP 跳转定义的列号才不会漂。
  let row = node;
  while (row && row.parentNode !== root) row = row.parentNode;
  if (!row || (row.nodeType === Node.ELEMENT_NODE && row.classList.contains('ln'))) return null;
  let gutter = row.previousSibling;
  while (gutter && !(gutter.nodeType === Node.ELEMENT_NODE && gutter.classList.contains('ln'))) {
    gutter = gutter.previousSibling;
  }
  if (!gutter || !gutter.dataset.line) return null;
  const span = document.createRange();
  span.setStartAfter(gutter);
  span.setEnd(node, offset);
  return { line: Number(gutter.dataset.line), char: span.toString().length };
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
  if (entry.remote) { toast('远端文件暂不支持跳转定义', 'error'); return; }
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
  if (entry.remote) { toast('远端文件暂不支持重命名符号', 'error'); return; }
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
  // 补全要读本机同名文件做上下文；远端文件不发这个请求，免得把本地内容
  // 当成远端上下文接进来。
  if (entry.remote) return;
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
  if (entry.remote) { toast('远端文件暂不支持行内改写，请直接编辑后保存', 'error'); return; }
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
    // 远端文件（工作区树里的 @remote/… 标签）落点换到远端：同一套乐观锁纪律，
    // 远端被改过就 409，绝不静默覆盖。
    if (entry.remote) {
      const data = await api.post('/api/remote/save', {
        path: entry.remotePath,
        content,
        expectedDigest: entry.digest || '',
      });
      entry.data = { ...(entry.data || {}), text: content, lines: content.split('\n').length };
      entry.digest = data.digest || '';
      entry.savedText = content;
      entry.dirty = false;
      updateSaveState(entry);
      resetDiagnostics(entry);
      if (data.changed === false) {
        toast('内容没有变化', '');
      } else {
        toast('已保存到远端', 'ok');
        refreshRemoteChanges();
      }
      return;
    }
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
  tasks: '后台任务',
  schedules: '定时任务',
  remote: '远程设备',
  welcome: '开始',
};

function switchPanel(name) {
  // 「远程设备」是编辑器页签，不是侧栏内容：活动栏按钮是开/关切换。
  // 已经是活动页签时再点 = 关闭并回到打开前的页签（无则 welcome）。
  if (name === 'remote') {
    if (!S.remote.enabled) return;   // 门控关闭时按钮已隐藏，这里兜底
    if (S.editors.has('remote') && S.activeEditor === 'remote') {
      closeRemotePanel();
      return;
    }
    S.activePanel = 'remote';
    document.querySelectorAll('.act').forEach((a) => a.classList.toggle('is-active', a.dataset.panel === 'remote'));
    $('sb-title').textContent = PANEL_TITLES.remote;
    $('sidebar').style.display = 'none';
    $('sb-resizer').style.display = 'none';
    document.querySelectorAll('.sb-body').forEach((b) => { b.hidden = true; });
    openRemotePanel();
    return;
  }
  // 不留隐形占位：切到别的活动栏时，正开着的远程设备页签一并关掉。
  if (S.editors.has('remote') && S.activeEditor === 'remote') closeRemotePanel();
  S.activePanel = name;
  document.querySelectorAll('.act').forEach((a) => a.classList.toggle('is-active', a.dataset.panel === name));
  $('sb-title').textContent = PANEL_TITLES[name] || name;
  // welcome 指向编辑器区的独立视图而不是侧栏内容
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
  if (name === 'tasks') refreshTasks();
  if (name === 'schedules') refreshSchedules();
  if (name === 'welcome') activateEditor('welcome');
}

/* ── 远程设备页签的开/关（与普通文件页签同权）──────────────────────────
   关页签只是把它从编辑器区摘出来，节点本身留在 S.remote.node：
   连接、展开态、当前文件都在 S 里，重开时原位放回即可「保留现场」。 */
function openRemotePanel() {
  const node = S.remote.node || $('remote-view');
  if (!node) return;
  S.remote.node = node;
  S.remote.prevEditor = S.activeEditor && S.activeEditor !== 'remote' ? S.activeEditor : '';
  let entry = S.editors.get('remote');
  if (!entry) {
    entry = {
      id: 'remote',
      tab: makeTab('remote', '🖧', '远程设备'),
      node,
      kind: 'remote',
      mode: 'code',
      data: { path: 'remote', lines: '', encoding: '', newline: '', bom: false },
      path: 'remote',
      created: false,
    };
    node.dataset.ed = 'remote';
    node.classList.add('editor');
    $('editor-host').appendChild(node);
    $('edtabs').appendChild(entry.tab);
    S.editors.set('remote', entry);
  } else {
    // 之前关过（节点被摘出 DOM）或从未插入：确保它回到编辑器区
    if (!node.isConnected) $('editor-host').appendChild(node);
    if (!entry.tab.isConnected) $('edtabs').appendChild(entry.tab);
  }
  updateLayout();
  activateEditor('remote');
  refreshRemotePanel();
}

function closeRemotePanel() {
  closeRemoteDrawer();
  const prev = S.remote.prevEditor;
  closeEditor('remote');
  if (prev && prev !== 'remote' && (S.editors.has(prev) || prev === 'welcome')) activateEditor(prev);
  // 活动栏取消高亮：远程设备已经不在了，不该留一个「按下去」的图标
  const act = document.querySelector('.act[data-panel="remote"]');
  if (act) act.classList.remove('is-active');
  if (S.activePanel === 'remote') S.activePanel = '';
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

/* 目录首页预览（P0-2）与「加入 / 移出工作区」（M12）：右键文件树里的目录。
   菜单支持**多项**（M12 起）：传数组 `[{label, run}]`；仍兼容旧的三参写法
   `openTreeMenu(x, y, label, run)`（既有「预览首页」调用点零改动）。
   旧写法内部转成单项数组，因此两条路径走的是同一份渲染代码。 */
let treeMenu = null;

function closeTreeMenu() {
  if (treeMenu) {
    treeMenu.remove();
    treeMenu = null;
  }
}

function openTreeMenu(x, y, labelOrItems, run) {
  const items = Array.isArray(labelOrItems)
    ? labelOrItems
    : [{ label: labelOrItems, run }];
  closeTreeMenu();
  const menu = el('div', 'tree-menu');
  for (const entry of items) {
    if (!entry || !entry.label) continue;
    const item = el('button', 'tree-menu-item');
    item.type = 'button';
    // 标签 + 可选快捷键提示（终端剪贴板菜单用）；`disabled` 的条目只展示、不执行。
    item.append(el('span', 'tree-menu-label', entry.label));
    if (entry.hint) item.append(el('span', 'tree-menu-hint', entry.hint));
    if (entry.disabled) {
      item.disabled = true;
    } else {
      item.addEventListener('click', () => {
        closeTreeMenu();
        // run 可能抛错：菜单已经收掉了，异常如实冒到控制台，不静默吞掉。
        entry.run();
      });
    }
    menu.appendChild(item);
  }
  if (!menu.childElementCount) return;
  document.body.appendChild(menu);
  // 先落 DOM 再量尺寸，免得贴边时菜单溢出视口。
  menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - menu.offsetWidth - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - menu.offsetHeight - 8))}px`;
  treeMenu = menu;
}

document.addEventListener('mousedown', (event) => {
  if (treeMenu && !treeMenu.contains(event.target)) closeTreeMenu();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeTreeMenu();
});

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

      // 右键任意节点：「加入对话」（引用 chip）；目录带 index.html 时再加
      // 「预览首页」（P0-2）。树是一次递归到底的（build_tree），子节点就在手边，
      // 不必再为判存多发一次请求。
      row.addEventListener('contextmenu', (event) => {
        event.preventDefault();
        const items = [{ label: '加入对话', run: () => addTreeRef(node) }];
        if (node.type === 'dir') {
          const hasIndex = (node.children || []).some(
            (c) => c.type === 'file' && c.name === 'index.html'
          );
          if (hasIndex) {
            items.push({
              label: '预览首页',
              run: () => openPreview(`${node.path}/index.html`),
            });
          }
        }
        openTreeMenu(event.clientX, event.clientY, items);
      });

      li.appendChild(row);
      S.treeNodes.set(node.path, row);

      if (node.children?.length) li.appendChild(build(node.children));
      ul.appendChild(li);
    }
    return ul;
  };

  const roots = S.workspaceRoots || [];
  if (!payload?.nodes?.length) {
    // 本地根为空、且没有任何远端根时才是真的「工作区是空的」；有远端根就照常渲染。
    if (!roots.length) {
      host.appendChild(el('div', 'empty', '工作区是空的。'));
      return;
    }
  } else {
    host.appendChild(build(payload.nodes));
    if (payload.truncated) {
      host.appendChild(el('div', 'empty', '文件过多，仅展示一部分。'));
    }
  }
  // 远端根（M12）：本地根之后追加，未登记任何根时这一整段都不出现 ——
  // 工作区树的外观与改造前逐字节一致。
  if (roots.length) host.appendChild(buildWsRootNodes(roots));
}

/* ── 工作区树里的远端根（M12）─────────────────────────────────────────────
   把「已加入工作区的远端目录」渲染成本地根之后的顶级节点，label 即命名空间路径。
   远端子树**懒加载**：展开一个节点才拉它那一层（远端往往是高延迟链路）。
   键统一是 `${connection}\0${相对 conn.root 的路径}` —— 带上连接名，
   两台机器上的同名路径（各自的 src/app）才不会串味。

   非活动连接：节点上标「点此打开连接」，点击时先打开那条连接再加载。
   `remote_tree` 端点对非活动连接会如实回「该连接未打开」，界面**不自己换连接**
   —— 静默换连接会把当前浏览位置与改动台账串到另一台机器上（M11 纪律）。 */
function wsRootKey(connection, path) {
  return `${connection || ''}\u0000${path || ''}`;
}

/* 连接根层的 entry.path 是 `./名称`（后端那层的 rel 就是 `.`），而落库的 sub 已在
   `store.normalize_sub()` 里压平成 `名称`。两边**原样比较**永远不相等 —— 于是「已在
   工作区」判不出来，右键菜单对已加入的目录恒显示「加入工作区」，点下去才报
   「该目录已在工作区」。比较与传参前统一压平，只压前导 `./` 与前导 `/`：
   其余部分（含 `..`）原样留着，仍然交给后端做越界校验。 */
function normalizeRootSub(sub) {
  return String(sub || '').replace(/^\.\/+/, '').replace(/^\/+/, '');
}

function isRootInWorkspace(connection, sub) {
  const wanted = normalizeRootSub(sub);
  return (S.workspaceRoots || []).some(
    (r) => r.connection === connection && normalizeRootSub(r.sub) === wanted
  );
}

function wsRootChildren(connection, path) {
  const box = el('div', 'ws-remote-children');
  const info = S.wsRootCache.get(wsRootKey(connection, path));
  if (!info) { box.appendChild(el('div', 'empty', '加载中…')); return box; }
  if (info.error) { box.appendChild(el('div', 'empty', info.error)); return box; }
  for (const e of (info.entries || [])) {
    const isDir = e.type === 'dir';
    const key = wsRootKey(connection, e.path);
    const row = el('div', 'tree-node');
    if (isDir) row.classList.add('is-dir');
    row.title = e.link ? `${e.path}（符号链接，不会跟随）` : e.path;
    row.appendChild(el('span', 'twisty', isDir ? (S.wsRootExpanded.has(key) ? '▾' : '▸') : ''));
    row.appendChild(el('span', 'name', e.name || e.path));
    if (!isDir) row.appendChild(el('span', 'size', e.link ? '符号链接' : fmtSize(e.size)));
    row.addEventListener('click', () => {
      if (isDir) toggleWsRemoteDir(connection, e.path);
      else openRemoteInEditor(connection, e.path, !!e.link, row);
    });
    // 远端子树的行与本地树同权（M13）：右键「加入对话」，值就是模型看到的
    // 命名空间路径 —— 两端共用一条 refs 通道，后端按同一条路由解析。
    row.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      openTreeMenu(event.clientX, event.clientY, [
        { label: '加入对话', run: () => addRemoteRef(connection, e.path, isDir) },
      ]);
    });
    box.appendChild(row);
    if (isDir && S.wsRootExpanded.has(key)) {
      box.appendChild(wsRootChildren(connection, e.path));
    }
  }
  return box;
}

function buildWsRootNodes(roots) {
  const ul = document.createElement('ul');
  for (const root of roots) {
    const connection = root.connection || '';
    const sub = root.sub || '';
    const key = wsRootKey(connection, sub);
    const expanded = S.wsRootExpanded.has(key);
    const li = document.createElement('li');
    const row = el('div', 'tree-node is-dir ws-remote-root');
    row.appendChild(el('span', 'twisty', expanded ? '▾' : '▸'));
    row.appendChild(
      el('span', 'name', root.nsPath || `@remote/${connection}${sub ? '/' + sub : ''}`)
    );
    row.appendChild(el('span', 'size', root.ok === false ? '不可用' : '远端'));
    if (root.ok === false) {
      row.classList.add('is-sensitive');
      row.title = `工作区根无法使用：${root.reason || '未知原因'}`;
    } else {
      row.title = `${root.target || connection}：${root.nsPath || ''}`;
      if (connection !== S.remote.active) row.title += '（点此打开连接）';
    }
    row.addEventListener('click', () => toggleWsRoot(root));
    row.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      openTreeMenu(event.clientX, event.clientY, [
        // 根自己也是目录：「加入对话」给的是它的命名空间路径（后端只列一层）。
        {
          label: '加入对话',
          run: () => addRemoteRef(connection, sub, true, root.nsPath),
        },
        { label: '移出工作区', run: () => removeRemoteRoot(connection, sub) },
      ]);
    });
    li.appendChild(row);
    if (expanded) li.appendChild(wsRootChildren(connection, sub));
    ul.appendChild(li);
  }
  return ul;
}

function renderWorkspaceTree() {
  if (S.treePayload) renderTree(S.treePayload);
}

async function toggleWsRoot(root) {
  const connection = root.connection || '';
  const sub = root.sub || '';
  const key = wsRootKey(connection, sub);
  if (S.wsRootExpanded.has(key)) {
    S.wsRootExpanded.delete(key);
    renderWorkspaceTree();
    return;
  }
  if (root.ok === false) {
    toast(`这个工作区根不可用：${root.reason || '未知原因'}`, 'error');
    return;
  }
  S.wsRootExpanded.add(key);
  renderWorkspaceTree();                    // 先出「加载中…」
  if (connection !== S.remote.active) {
    await remoteOpen(connection);           // 先把那条连接打开（一次只活动一条）
  }
  if (!S.wsRootCache.has(key)) await loadWsRootTree(connection, sub);
  else renderWorkspaceTree();
}

async function toggleWsRemoteDir(connection, path) {
  const key = wsRootKey(connection, path);
  if (S.wsRootExpanded.has(key)) {
    S.wsRootExpanded.delete(key);
    renderWorkspaceTree();
    return;
  }
  S.wsRootExpanded.add(key);
  renderWorkspaceTree();
  if (!S.wsRootCache.has(key)) await loadWsRootTree(connection, path);
  else renderWorkspaceTree();
}

async function loadWsRootTree(connection, path) {
  const key = wsRootKey(connection, path);
  try {
    const res = await api.get('/api/remote/tree', { connection, path: path || '' });
    S.wsRootCache.set(
      key,
      res.ok === false ? { error: res.reason || '该连接未打开' } : { entries: res.entries || [] }
    );
  } catch (err) {
    S.wsRootCache.set(key, { error: '读取目录失败：' + err.message });
  }
  renderWorkspaceTree();
}

/* 点工作区树里的远端文件：直接在中间编辑区打开（与本地文件同一套标签、代码
   视图与编辑面），不再把人切到「远程设备」面板去读。连接可能已经被切走
   （用户后来在面板里换了连接），先切回他点的那条再读 —— `remote_*` 端点只认
   活动连接，切错机器会读到同名文件。切换失败时如实放弃，不静默读别的机器。

   符号链接维持旧行为：交给远程面板如实提示「未跟随」，编辑器这边不读。 */
async function openRemoteInEditor(connection, path, isLink, row) {
  if (connection && connection !== S.remote.active) {
    await remoteOpen(connection);
    if (S.remote.active !== connection) return;   // remoteOpen 已经把原因 toast 出来了
  }
  if (isLink) {
    openRemotePanel();
    remoteOpenFile(path, true);
    return;
  }
  if (row) {
    document.querySelectorAll('.tree-node.is-active').forEach((n) => n.classList.remove('is-active'));
    row.classList.add('is-active');
  }
  // 标签 id 用命名空间写法：同一路径不会与本地文件标签撞车，工具提示里看到的
  // 也是模型认的那套路径。
  const ns = `@remote/${connection}/${path}`;
  const id = `file:${ns}`;
  const existing = S.editors.get(id);
  if (existing) {
    if (existing.editing && existing.dirty) { activateEditor(id); return; }
    try {
      const data = await api.get('/api/remote/file', { path });
      existing.data = { ...data, path: ns };
      existing.digest = data.digest || '';
      existing.savedText = normalizeNewlines(data.text);
      existing.dirty = false;
      resetDiagnostics(existing);
      renderFileEntry(existing);
    } catch {
      /* 远端读失败时复用旧内容：总比把已经显示的正文清空好 */
    }
    activateEditor(id);
    return;
  }
  const host = el('div');
  host.appendChild(el('div', 'empty', '读取中…'));
  const entry = openEditor(id, '📄', baseName(path), host, {
    kind: 'file',
    data: { path: ns, lines: '…', encoding: '', newline: '', bom: false },
  });
  entry.remote = connection;
  entry.remotePath = path;
  try {
    const data = await api.get('/api/remote/file', { path });
    entry.data = { ...data, path: ns };
    entry.digest = data.digest || '';
    entry.savedText = normalizeNewlines(data.text);
    entry.dirty = false;
    renderFileEntry(entry);
  } catch (err) {
    entry.node.replaceChildren(el('div', 'empty', `无法打开：${err.message}`));
  }
}

/* ── 加入 / 移出工作区（M12）────────────────────────────────────────────
   两端点都只在 VIBE_REMOTE 开启时存在；返回的 roots 是**权威清单**，
   界面直接照它渲染，不自己推断。 */
async function loadWorkspaceRoots() {
  if (!S.remote.enabled) { S.workspaceRoots = []; return; }
  try {
    const res = await api.get('/api/remote/roots');
    S.remote.enabled = res.enabled !== false;
    S.workspaceRoots = res.roots || [];
  } catch (err) {
    S.workspaceRoots = [];
  }
  renderWorkspaceTree();
}

async function addRemoteRoot(connection, sub) {
  if (!connection) { toast('先打开一个连接', 'error'); return; }
  const normalized = normalizeRootSub(sub);
  const ns = `@remote/${connection}${normalized ? '/' + normalized : ''}`;
  try {
    const res = await api.post('/api/remote/roots', { connection, sub: normalized });
    S.workspaceRoots = res.roots || [];
    S.wsRootCache = new Map();
    S.wsRootExpanded = new Set();
    if (S.remote.state) S.remote.state.roots = S.workspaceRoots.map((r) => r.nsPath);
    renderWorkspaceTree();
    renderRemoteTree();
    toast(`已加入工作区：${ns}`, 'ok');
  } catch (err) {
    toast('加入工作区失败：' + err.message, 'error');
  }
}

async function removeRemoteRoot(connection, sub) {
  try {
    const res = await api.post('/api/remote/roots/delete', {
      connection,
      sub: normalizeRootSub(sub),
    });
    S.workspaceRoots = res.roots || [];
    // 该连接下的远端树缓存 / 展开态一并清掉：根都没了，那些层级不再可达。
    const prefix = `${connection || ''}\u0000`;
    for (const key of [...S.wsRootCache.keys()]) {
      if (key.startsWith(prefix)) S.wsRootCache.delete(key);
    }
    for (const key of [...S.wsRootExpanded]) {
      if (key.startsWith(prefix)) S.wsRootExpanded.delete(key);
    }
    if (S.remote.state) S.remote.state.roots = S.workspaceRoots.map((r) => r.nsPath);
    renderWorkspaceTree();
    renderRemoteTree();
    toast('已移出工作区', 'ok');
  } catch (err) {
    toast('移出工作区失败：' + err.message, 'error');
  }
}

async function refreshTree() {
  try {
    S.treePayload = await api.get('/api/tree');
    renderTree(S.treePayload);
  } catch (err) {
    S.treePayload = null;
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
    // 分支清单与状态并行取；分支取不到（网络抖动）不该把整面板打红，故 catch 成 null。
    const [data, branches] = await Promise.all([
      api.get('/api/git/status'),
      api.get('/api/git/branches').catch(() => null),
    ]);
    S.git = data;
    renderGitPanel(data, branches);
  } catch (err) {
    $('git-head').replaceChildren(el('div', 'empty', `读取 git 状态失败：${err.message}`));
  }
}

function renderGitPanel(data, branches) {
  const head = $('git-head');
  const toolbar = $('git-toolbar');
  const commitbar = $('git-commitbar');
  if (!data || !data.available) {
    // 关闭态：工具栏与提交条整体隐藏 —— 不留任何点不动的交互件（DoD：状态为空 ⟺ 不可见）
    toolbar.hidden = true;
    commitbar.hidden = true;
    head.replaceChildren(el('div', 'empty', (data && data.reason) || '当前工作区不是 git 仓库。'));
    $('git-staged').replaceChildren(el('div', 'empty', ''));
    $('git-unstaged').replaceChildren(el('div', 'empty', ''));
    setGitBadge('badge-git-staged', 0);
    setGitBadge('badge-git', 0);
    return;
  }
  toolbar.hidden = false;
  commitbar.hidden = false;

  const branch = el('span', 'git-branch',
    data.detached ? `@ ${data.branch || 'HEAD'}（游离头）` : (data.branch || '（未知分支）'));
  head.replaceChildren(branch);
  const sub = el('span', 'git-sub');
  if (data.upstream) {
    const ahead = data.ahead || 0;
    const behind = data.behind || 0;
    // 徽标直接用 status() 已有的 ahead/behind，不额外算
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

  fillBranchSelect(branches, data);

  // 双栏：冲突单独成组（动作禁用）；其余按「已 stage / 未 stage」分。
  // 一个文件既 stage 又在工作区改过时会同时出现在两栏（与 VS Code 一致）。
  const entries = data.entries || [];
  const conflicted = entries.filter((e) => e.conflicted);
  const rest = entries.filter((e) => !e.conflicted);
  const staged = rest.filter(gitIsStaged);
  const unstaged = rest.filter(gitIsUnstaged).concat(conflicted);

  renderGitRows($('git-staged'), staged, 'staged');
  renderGitRows($('git-unstaged'), unstaged, 'unstaged');
  setGitBadge('badge-git-staged', staged.length);
  setGitBadge('badge-git', unstaged.length);
  $('btn-git-unstage-all').hidden = staged.length === 0;
  $('btn-git-stage-all').hidden = !unstaged.some((e) => !e.conflicted);
}

function setGitBadge(id, n) {
  const badge = $(id);
  badge.textContent = n;
  badge.classList.toggle('has', n > 0);
}

/** 已进入索引（index 位不是空格/问号）—— 暂存栏的依据。 */
function gitIsStaged(entry) {
  return !!entry.index && entry.index !== ' ' && entry.index !== '?';
}

/** 工作区有改动或未跟踪 —— 工作区栏的依据。 */
function gitIsUnstaged(entry) {
  return entry.status === '??'
    || (!!entry.worktree && entry.worktree !== ' ' && entry.worktree !== '?');
}

function fillBranchSelect(branches, data) {
  const sel = $('git-branch-select');
  if (!branches || !branches.available) {
    sel.disabled = true;
    sel.replaceChildren();
    return;
  }
  sel.disabled = false;
  // 只列本地分支：远端分支拿来 switch 会进 detached，不是这里想给的语义
  const locals = (branches.branches || []).filter((b) => b.kind !== 'remote');
  const opts = locals.map((b) => {
    const o = document.createElement('option');
    o.value = b.name;
    o.textContent = b.current ? `${b.name}（当前）` : b.name;
    o.selected = !!b.current || (!data.detached && b.name === data.branch);
    return o;
  });
  if (!opts.length) {
    const o = document.createElement('option');
    o.value = '';
    o.textContent = data.branch || '（未知分支）';
    o.selected = true;
    opts.push(o);
  }
  sel.replaceChildren(...opts);
}

function renderGitRows(host, entries, kind) {
  host.replaceChildren();
  if (!entries.length) {
    host.appendChild(el('div', 'empty', kind === 'staged' ? '暂存区是空的。' : '工作区干净。'));
    return;
  }
  for (const entry of entries) host.appendChild(gitRow(entry, kind));
}

/** 一行改动：状态码 + 路径 + （stage/unstage/放弃）动作。冲突行禁用动作。 */
function gitRow(entry, kind) {
  const row = el('div', 'git-row' + (entry.conflicted ? ' is-conflict' : ''));
  const meta = el('span', 'change-meta');
  const code = el('span', 'git-code', entry.status);
  code.dataset.c = entry.status;
  meta.appendChild(code);
  meta.appendChild(el('span', 'git-file', entry.path));
  meta.title = '点击查看 diff';
  meta.addEventListener('click', () => {
    // 未跟踪文件没有 diff 可看 —— 直接看文件本身，别给用户一个空标签
    if (entry.status === '??') { openFile(entry.path); return; }
    openGitDiffTab(entry.path);
  });
  row.appendChild(meta);
  if (entry.conflicted) {
    row.appendChild(el('span', 'git-conflict-hint', '需在终端解决冲突'));
    return row;
  }
  const acts = el('span', 'git-row-actions');
  const act = (label, title, fn) => {
    const b = el('button', 'icon-btn', label);
    b.type = 'button';
    b.title = title;
    b.addEventListener('click', (event) => { event.stopPropagation(); fn(); });
    return b;
  };
  if (kind === 'staged') acts.appendChild(act('−', '取消暂存', () => gitUnstage([entry.path])));
  else acts.appendChild(act('＋', '暂存', () => gitStage([entry.path])));
  acts.appendChild(act('⤺', '放弃改动（放弃前先落检查点，可从「回滚」面板找回）', () => gitDiscard([entry.path])));
  row.appendChild(acts);
  return row;
}

/** Git 写操作统一入口（ADR-028：用户本人动作，不过审批浮层）。
 * 成功后局部刷新 status +（改动面板若在 git 模式）刷新它，绝不整页刷新。 */
async function gitWrite(path, body, opts = {}) {
  try {
    const res = await api.post(path, body || {});
    if (res && res.available === false) {
      toast(res.reason || '当前工作区不是 git 仓库', 'error');
      return null;
    }
    if (opts.toast) toast(opts.toast(res), 'ok');
    await refreshGit();
    if (S.changesMode === 'git') refreshChanges();
    return res;
  } catch (err) {
    toast(`${opts.errorPrefix || 'Git 操作失败'}：${err.message}`, 'error');
    return null;
  }
}

function gitStage(paths, all = false) {
  return gitWrite('/api/git/stage', { paths, all }, { errorPrefix: '暂存失败' });
}
function gitUnstage(paths, all = false) {
  return gitWrite('/api/git/unstage', { paths, all }, { errorPrefix: '取消暂存失败' });
}
async function gitDiscard(paths) {
  const list = paths.length === 1
    ? paths[0]
    : `以下 ${paths.length} 个文件：\n${paths.slice(0, 20).join('\n')}${paths.length > 20 ? '\n…' : ''}`;
  if (!confirm(`放弃改动会丢弃工作区里未提交的内容：\n${list}\n\n（放弃前会自动落一个检查点，可从「回滚」面板找回）继续？`)) return null;
  return gitWrite('/api/git/discard', { paths }, {
    errorPrefix: '放弃改动失败',
    toast: (res) => `已放弃改动${res && res.checkpointId ? `（检查点 ${res.checkpointId}，可回滚）` : ''}`,
  });
}
async function gitCommit() {
  const text = ($('git-msg-text').value || '').trim();
  if (!text) { toast('先填写提交信息', 'error'); return; }
  const res = await gitWrite('/api/git/commit', { message: text }, {
    errorPrefix: '提交失败',
    toast: (r) => `已提交${r && r.oid ? ` ${r.oid}` : ''}`,
  });
  if (res) $('git-msg-text').value = '';
}
async function gitPush() {
  const info = S.git || {};
  const okay = confirm(
    `推送到 ${info.upstream || '（无上游，需先在终端设置）'}`
    + `（当前分支 ${info.branch || '?'}，领先 ${info.ahead || 0} 个提交）？\n\n不带 --force，被拒绝时会原样报错。`
  );
  if (!okay) return null;
  return gitWrite('/api/git/push', {}, { errorPrefix: '推送失败', toast: () => '已推送' });
}
function gitPull() {
  return gitWrite('/api/git/pull', {}, { errorPrefix: '拉取失败', toast: () => '已拉取（快进）' });
}
function gitSwitchBranch(name) {
  if (!name) return null;
  return gitWrite('/api/git/branch', { name, action: 'switch' }, {
    errorPrefix: '切换分支失败',
    toast: () => `已切换到 ${name}`,
  });
}
function gitNewBranch() {
  const name = prompt('新分支名（将从当前分支新建并切换）：');
  if (!name) return null;
  return gitWrite('/api/git/branch', { name, action: 'create' }, {
    errorPrefix: '新建分支失败',
    toast: () => `已新建并切换到 ${name}`,
  });
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
    // 起草结果直接填进提交条（可编辑），提交仍由用户点「提交」触发
    $('git-msg-text').value = res.message;
    toast('已起草提交信息 —— 只生成文本，不会自动提交（可编辑后点「提交」）', 'ok');
  } catch (err) {
    toast(`生成提交信息失败：${err.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'AI 生成';
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
  const text = $('git-msg-text').value || '';
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
  // 全选文本让用户自己 Ctrl+C（textarea 用 select()，Range 选不中它的内容）
  $('git-msg-text').focus();
  $('git-msg-text').select();
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

  // 「预览效果」（P1-2）：只有本次改动真的能渲染成页面时才出现。
  // 后端给的是路径清单而不是内容 —— 正文要单独换 token 才取，且审批一结束就作废。
  const draftPaths = Array.isArray(payload.draftPaths) ? payload.draftPaths : [];
  const draftBtn = $('btn-draft-preview');
  draftBtn.hidden = draftPaths.length === 0;
  draftBtn.title = draftPaths.length
    ? `用「批准后会写入」的内容渲染：${draftPaths.join('、')}（不落盘，审批结束后即失效）`
    : '';

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
    S.reasoning = null;
    S.iterations = 0;
    updateStatusBarIter();
    addUser(ev.prompt, ev.userSeq || 0);
    S.lastRun = ev.runId;
    termLine('output', `任务开始：${ev.prompt}`, 'is-dim');
  },

  'run.iteration': (ev) => addIteration(ev.index, ev.total),

  'run.assistant_delta': (ev) => appendDelta(ev.text),

  // 思考增量（reasoning_content）：与回答并行滚动，独立成块。
  'run.reasoning_delta': (ev) => appendReasoning(ev.text),

  'run.tool_call': (ev) => {
    finishReasoning(); // 本轮思考到此为止，收口后再上工具卡片
    finishStreaming();
    addToolCall(ev);
  },

  'run.tool_result': (ev) => completeToolCall(ev),

  'run.tool_output': (ev) => applyToolOutput(ev),

  'run.subagent': (ev) => applySubAgent(ev),

  'run.approval': (ev) => openApproval(ev),

  'run.approval_resolved': (ev) => {
    if (S.awaiting && S.awaiting.approvalId === ev.approvalId) closeApproval();
    closeDraftTabs(ev.approvalId);
    if (ev.timedOut) toast('审批超时，已按最安全的方式处理：拒绝', 'error');
  },

  'run.notice': (ev) => {
    addNotice(ev.text);
    termLine('output', ev.text, 'is-dim');
  },

  'run.plan': (ev) => applyPlan(ev),

  'run.context': (ev) => applyContext(ev),

  // 预览自动刷新（P0）：文件监听检出变化 → 只重载受影响的预览标签。
  // 事件只带路径，界面不做任何内容级判断（也不需要）。
  'preview.changed': (ev) => refreshPreviewTabs(ev.paths, !!ev.truncated),

  'run.finished': (ev) => {
    setState('idle');
    // 用 closeApproval() 而不是只清 S.awaiting：同一个不变式 —— 任务都结束了，
    // 留一个点不动的审批弹窗没有任何意义（同上，decide() 会见状态为空而 return）。
    closeApproval();
    finishReasoning();
    const streamed = finishStreaming(ev.answer || '（模型返回了空内容）');
    if (!streamed) addAnswer(ev.answer || '（模型返回了空内容）');
    updateStats(ev.stats);
    refreshChanges();
    // 预览自动跟上（P1-1）：Agent 这一轮写过的网页文件，已打开的预览标签重载。
    // 与监听事件走同一条判定，尊重「自动刷新」开关（关着只标记「有更新」）。
    if (Array.isArray(ev.changedFiles) && ev.changedFiles.length) {
      refreshPreviewTabs(ev.changedFiles, false);
    }
    if (ev.checkpointId) addCheckpointBanner(ev.checkpointId);
    termLine('output', '任务完成。', 'is-ok');
  },

  'run.failed': (ev) => {
    setState('idle');
    finishReasoning();
    finishStreaming();
    addNotice(`任务失败（${ev.kind}）：${ev.error}`, true);
    termLine('output', `任务失败（${ev.kind}）：${ev.error}`, 'is-err');
  },

  'run.interrupted': (ev) => {
    setState('idle');
    finishReasoning();
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
  // 多终端：按 termId 分发；非激活的那个只更新自己的缓冲（切回来时整屏重画）。
  'term.data': (ev) => {
    const t = TERMS.get(ev.termId);
    if (t) termFeed(t, ev.data);
  },

  'term.exit': (ev) => {
    const t = TERMS.get(ev.termId);
    if (t) termOnExit(t, ev.exitCode);
  },

  // 客户端更新（M9）。与 run.* / test.* 分开：这不是 Agent 的一轮任务。
  // update.available 只在 chip 上提示、**不自动弹窗** —— 启动自动检查必须是静默的，
  // 否则每次开应用都会被一个「有更新」的浮层挡住（写代码的人正在干别的活）。
  'update.available': (ev) => {
    setUpdateView(ev);
    if (!$('update-modal').hidden) renderUpdateModal();
  },

  'update.progress': (ev) => {
    const patch = {
      status: 'downloading',
      progress: { done: Number(ev.done) || 0, total: Number(ev.total) || 0 },
    };
    if (ev.version) patch.availableVersion = String(ev.version);
    setUpdateView(patch);
    if (!$('update-modal').hidden) renderUpdateModal();
  },

  'update.staged': (ev) => applyUpdateStaged(ev),

  'update.failed': (ev) => {
    setUpdateView({
      status: 'failed',
      reason: String(ev.reason || ''),
      kind: String(ev.kind || ''),
      progress: null,
    });
    updateMsg(`更新失败：${ev.reason || '未知原因'}`);
    if (!$('update-modal').hidden) renderUpdateModal();
  },

  'update.applied': (ev) => {
    // 壳侧确认本机已更新：清掉待安装态，chip 随之收起（没有待办动作就不占标题栏）。
    setUpdateView({
      status: 'applied',
      currentVersion: String(ev.version || ''),
      availableVersion: '',
      stagedPath: '',
      progress: null,
    });
  },

  // 后台任务（M9-D）：与 run.* 分开 —— 后台任务不是 Agent 的一轮，是并行队列。
  'task.state': (ev) => applyTaskState(ev),

  // 定时任务（M10）：同样与 run.* 分开 —— 说的是「排期本身」发生了什么。
  'schedule.state': (ev) => applyScheduleState(ev),

  // 远程设备工作面（M11）：状态变化（打开/切换连接、切换浏览目录）驱动连接与权限位徽标。
  'remote.state': (ev) => applyRemoteState(ev),
  // 远端编辑/保存产生 diff：给当前打开文件打「远端已改」，并刷新审查列表。
  'remote.edit': (ev) => applyRemoteEdit(ev),
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

/* `/` Skill 补全（M9-A）：行首敲 `/` 列出已加载工作流。与 `@` 引用完全独立：
   Skill 是「把工作流名作为任务意图」而非「引用文件」。VIBE_SKILLS=0 时
   S.skills 为空，浮层恒不出现（closeSkillPop 在任何打开路径前先判空）。 */
const skillPop = { open: false, start: -1, q: '', items: [], index: 0 };

function closeSkillPop() {
  skillPop.open = false;
  skillPop.start = -1;
  skillPop.q = '';
  skillPop.items = [];
  skillPop.index = 0;
  $('skill-pop').hidden = true;
}

/* 光标前是否处于「行首 `/`」补全语境；返回 {start, q} 或 null。
   与 mentionContext 同一纪律：`/` 前必须是行首或空白，`/` 后无空白。 */
function skillPopContext() {
  if (!S.skills.length) return null;
  const input = $('input');
  const caret = input.selectionStart;
  const before = input.value.slice(0, caret);
  const slash = before.lastIndexOf('/');
  if (slash < 0) return null;
  if (slash > 0 && !/\s/.test(before[slash - 1])) return null;
  const q = before.slice(slash + 1);
  if (/\s/.test(q)) return null;
  return { start: slash, q };
}

function renderSkillPop() {
  const head = $('skill-pop-head');
  head.replaceChildren();
  head.appendChild(el('span', 'mi-hint',
    skillPop.q ? `匹配 “${skillPop.q}”` : '↑↓ 选择 · Enter 填入 · Esc 取消'));
  const list = $('skill-pop-list');
  list.replaceChildren();
  const q = skillPop.q.toLowerCase();
  const items = S.skills.filter((s) => !q || s.name.toLowerCase().includes(q));
  skillPop.items = items;
  if (!items.length) {
    list.appendChild(el('div', 'mention-empty', '没有匹配的工作流'));
    return;
  }
  items.forEach((item, index) => {
    const row = el('div', 'mention-item' + (index === skillPop.index ? ' is-active' : ''));
    row.appendChild(el('span', 'mi-label', item.name));
    row.appendChild(el('span', 'mi-detail', item.description || ''));
    row.addEventListener('mousedown', (event) => {
      event.preventDefault();
      acceptSkillPop(index);
    });
    list.appendChild(row);
    if (index === skillPop.index) row.scrollIntoView({ block: 'nearest' });
  });
}

function acceptSkillPop(index = skillPop.index) {
  const item = skillPop.items[index];
  if (!item) return;
  const input = $('input');
  const caret = input.selectionStart;
  const before = input.value.slice(0, caret);
  const after = input.value.slice(caret);
  // 用 Skill 名替换掉从 `/` 起到光标的内容，保留后续文本
  input.value = before.slice(0, skillPop.start) + item.name + ' ' + after;
  closeSkillPop();
  autoGrow();
  syncSendState();
  input.focus();
  const pos = skillPop.start + item.name.length + 1;
  input.setSelectionRange(pos, pos);
}

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

/* 工作区树右键「加入对话」：把文件/目录变成一个引用 chip，随下一次发送
   交给后端解析（与 `@` 候选、粘贴图片走同一条 refs 通道，不另开数据通路）。
   目录用 `folder` 类型 —— 后端会给一层目录清单，不是整棵递归。 */
function addTreeRef(node) {
  if (!node || !node.path) return;
  const item = { type: node.type === 'dir' ? 'folder' : 'file', value: node.path };
  const before = S.refs.length;
  addRef(item);
  if (S.refs.length > before) toast(`已加入对话：${node.path}`, 'ok');
}

/* 远端文件/目录「加入对话」（M12/M13）：值是命名空间写法
   `@remote/<连接>/<相对连接 root 的路径>` —— 与模型用 read_file 写的是同一个
   路径，后端按 `RemoteRouter` 同一条路由解析，不存在第二套口径。
   `nsPath` 只在根节点有现成值时传入（省一次字符串拼装）。 */
function remoteNsPath(connection, path) {
  const rel = normalizeRootSub(String(path || '')).replace(/\/+$/, '');
  return `@remote/${connection}${rel ? '/' + rel : ''}`;
}

function addRemoteRef(connection, path, isDir, nsPath) {
  if (!connection) { toast('先打开一个连接', 'error'); return; }
  const value = nsPath || remoteNsPath(connection, path);
  if (!value || value === '@remote/') return;
  const before = S.refs.length;
  addRef({ type: isDir ? 'folder' : 'file', value });
  if (S.refs.length > before) toast(`已加入对话：${value}`, 'ok');
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
    // 模型生成会在**下一个 SSE 帧**就断开（不必等整段回答跑完）；
    // 正在执行的工具命令无法从外部掐断，会跑完当前这一步再停。
    // 收到 run.* 结束事件之前，按钮显示「停止中…」并置灰。
    if (res.interrupted) {
      S.interrupting = true;
      syncPauseButton();
      $('composer-status').textContent = '正在停止…';
      toast('已请求停止：生成立即中断，正在执行的工具会跑完当前这一步');
    } else {
      S.interrupting = false;
      syncPauseButton();
      toast('当前没有正在执行的任务');
    }
  } catch (err) {
    S.interrupting = false;
    syncPauseButton();
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
  S.reasoning = null;
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

/* ------------------------------------------------------------------ 后台任务（M9-D） */
const TASK_STATE_LABEL = {
  pending: '排队中', running: '执行中', succeeded: '已完成',
  failed: '失败', cancelled: '已取消',
};
const TASK_STATE_CLASS = {
  pending: 'is-idle', running: 'is-running', succeeded: 'is-ok',
  failed: 'is-bad', cancelled: 'is-dim',
};

async function refreshTasks() {
  const list = $('task-list');
  if (!list) return;
  try {
    const res = await api.get('/api/tasks');
    S.tasks = res.tasks || [];
    renderTasks();
  } catch (err) {
    list.replaceChildren(el('div', 'empty', '加载失败：' + err.message));
  }
}

function renderTasks() {
  const list = $('task-list');
  if (!list) return;
  const running = S.tasks.filter((t) => t.state === 'pending' || t.state === 'running').length;
  const badge = $('badge-tasks');
  if (badge) badge.textContent = S.tasks.length;
  const actBadge = $('act-badge-tasks');
  if (actBadge) {
    actBadge.hidden = running === 0;
    actBadge.textContent = running;
  }
  if (!S.tasks.length) {
    list.replaceChildren(el('div', 'empty', '暂无后台任务。'));
    return;
  }
  const nodes = S.tasks.map((t) => taskRow(t));
  list.replaceChildren(...nodes);
}

function taskRow(task) {
  const row = el('div', 'task-item');
  const head = el('div', 'task-head');
  head.appendChild(el('span', 'task-id', task.taskId || ''));
  const st = task.state || 'pending';
  head.appendChild(el('span', `task-state ${TASK_STATE_CLASS[st] || 'is-idle'}`,
    TASK_STATE_LABEL[st] || st));
  head.appendChild(el('span', 'task-chars', task.chars ? `${task.chars} 字符` : ''));
  row.appendChild(head);
  if (task.summary) row.appendChild(el('div', 'task-summary', task.summary));
  const active = st === 'pending' || st === 'running';
  if (active) {
    const acts = el('div', 'task-actions');
    const cancel = el('button', 'btn btn-small', '取消');
    cancel.type = 'button';
    cancel.title = '请求取消这个后台任务';
    cancel.addEventListener('click', () => cancelTask(task.taskId));
    acts.appendChild(cancel);
    row.appendChild(acts);
  }
  return row;
}

async function submitTask() {
  const input = $('task-input');
  const prompt = (input.value || '').trim();
  if (!prompt) { toast('先写下要交给后台任务的内容', 'error'); return; }
  if (S.running === false && !prompt) return;
  const btn = $('btn-task-submit');
  btn.disabled = true;
  try {
    const res = await api.post('/api/tasks', { prompt });
    input.value = '';
    toast(`后台任务已提交（${res.taskId}）`, 'ok');
    await refreshTasks();
  } catch (err) {
    toast(`提交后台任务失败：${err.message}`, 'error');
  } finally {
    btn.disabled = false;
  }
}

async function cancelTask(taskId) {
  try {
    const res = await api.post(`/api/tasks/${taskId}/cancel`, {});
    if (res.cancelled) toast(`已请求取消任务 ${taskId}`, 'ok');
    else toast('该任务已结束或无法取消', 'error');
    await refreshTasks();
  } catch (err) {
    toast(`取消失败：${err.message}`, 'error');
  }
}

/* task.state 事件（M9-D）：合并进 S.tasks 并重绘。事件只带 taskId/state/summary/chars，
   不含正文与结论（ADR-006），前端据此更新列表即可。 */
function applyTaskState(ev) {
  const id = ev.taskId;
  const idx = S.tasks.findIndex((t) => t.taskId === id);
  const merged = {
    taskId: id,
    state: ev.state || 'pending',
    summary: ev.summary || '',
    chars: ev.chars || 0,
  };
  if (idx < 0) {
    S.tasks.unshift(merged);
  } else {
    S.tasks[idx] = { ...S.tasks[idx], ...merged };
  }
  renderTasks();
}

/* ------------------------------------------------------------------ 定时任务（M10）

   与后台任务的区别：定时任务是**排期本身**（什么时候投），后台任务是**被投出的
   那一次执行**。所以这里只管定义与启停，真正跑起来的东西在「后台任务」页签里。
   列表只回元数据 —— prompt 正文不落前端（ADR-006），界面上也就看不到。 */

async function refreshSchedules() {
  const list = $('job-list');
  try {
    const res = await api.get('/api/schedules');
    S.schedules = res.jobs || [];
    S.scheduleStatus = res.status || null;
    renderSchedules();
  } catch (err) {
    if (list) list.replaceChildren(el('div', 'empty', '加载失败：' + err.message));
  }
}

function renderSchedules() {
  const list = $('job-list');
  if (!list) return;
  const badge = $('badge-schedules');
  if (badge) badge.textContent = S.schedules.length;
  const actBadge = $('act-badge-schedules');
  if (actBadge) {
    const active = S.schedules.filter((j) => j.enabled).length;
    actBadge.hidden = active === 0;
    actBadge.textContent = active;
  }
  if (!S.schedules.length) {
    list.replaceChildren(el('div', 'empty', '暂无定时任务。'));
    return;
  }
  list.replaceChildren(...S.schedules.map(jobRow));
}

function jobRow(job) {
  const row = el('div', 'task-item');
  const head = el('div', 'task-head');
  head.appendChild(el('span', 'task-id', job.jobId || ''));
  head.appendChild(el('span', `task-state ${job.enabled ? 'is-running' : 'is-dim'}`,
    job.enabled ? '已启用' : '已停用'));
  head.appendChild(el('span', 'task-chars', `已跑 ${job.runCount || 0} 次`));
  row.appendChild(head);
  const last = job.lastState ? `｜上次 ${job.lastState}` : '';
  row.appendChild(el('div', 'task-summary',
    `${job.name}｜${job.schedule}｜下次 ${fmtEpoch(job.nextRunAt)}${last}`));
  const acts = el('div', 'task-actions');
  const run = el('button', 'btn btn-small', '立即跑');
  run.type = 'button';
  run.title = '马上投一次只读后台任务（不影响下次排期）';
  run.addEventListener('click', () => runJobNow(job.jobId));
  const toggle = el('button', 'btn btn-small', job.enabled ? '停用' : '启用');
  toggle.type = 'button';
  toggle.title = job.enabled ? '停用后不再到点触发（已投出的任务不受影响）' : '重新开始到点触发';
  toggle.addEventListener('click', () => toggleJob(job.jobId, !job.enabled));
  const del = el('button', 'btn btn-small', '删除');
  del.type = 'button';
  del.title = '删除这条排期（已投出的后台任务不受影响）';
  del.addEventListener('click', () => deleteJob(job.jobId));
  acts.append(run, toggle, del);
  row.appendChild(acts);
  return row;
}

/* epoch 秒 → 本地可读时间。0 表示「不再触发」（如一次性任务已跑过）。 */
function fmtEpoch(sec) {
  const n = Number(sec || 0);
  if (!n) return '不再触发';
  try { return new Date(n * 1000).toLocaleString(); }
  catch { return String(n); }
}

async function submitJob() {
  const name = ($('job-name').value || '').trim();
  const schedule = ($('job-schedule').value || '').trim();
  const prompt = ($('job-prompt').value || '').trim();
  if (!name || !schedule || !prompt) {
    toast('任务名 / 排期 / 内容都要填', 'error');
    return;
  }
  const btn = $('btn-job-submit');
  btn.disabled = true;
  try {
    const res = await api.post('/api/schedules', { name, schedule, prompt });
    $('job-name').value = '';
    $('job-schedule').value = '';
    $('job-prompt').value = '';
    toast(`定时任务已创建（${res.jobId}）`, 'ok');
    await refreshSchedules();
  } catch (err) {
    toast(`创建失败：${err.message}`, 'error');
  } finally {
    btn.disabled = false;
  }
}

async function toggleJob(jobId, enabled) {
  try {
    await api.post(`/api/schedules/${jobId}`, { enabled });
    await refreshSchedules();
  } catch (err) {
    toast(`操作失败：${err.message}`, 'error');
  }
}

async function runJobNow(jobId) {
  try {
    const res = await api.post(`/api/schedules/${jobId}/run`, {});
    toast(`已投出后台任务（${res.taskId}）`, 'ok');
    await refreshSchedules();
  } catch (err) {
    toast(`触发失败：${err.message}`, 'error');
  }
}

async function deleteJob(jobId) {
  try {
    await api.post(`/api/schedules/${jobId}/delete`, {});
    toast('已删除定时任务', 'ok');
    await refreshSchedules();
  } catch (err) {
    toast(`删除失败：${err.message}`, 'error');
  }
}

/* schedule.state 事件（M10）：只带元数据（不含 prompt 正文），合并进列表即可。
   新建事件里没有 name/schedule（后端不回），所以本地拉一次权威视图。 */
function applyScheduleState(ev) {
  const id = ev.jobId;
  if (ev.action === 'deleted') {
    S.schedules = S.schedules.filter((j) => j.jobId !== id);
    renderSchedules();
    return;
  }
  const idx = S.schedules.findIndex((j) => j.jobId === id);
  if (idx < 0) {
    refreshSchedules();
    return;
  }
  S.schedules[idx] = {
    ...S.schedules[idx],
    enabled: ev.state === 'enabled',
    nextRunAt: ev.nextRunAt || 0,
    lastTaskId: ev.taskId || '',
    lastState: ev.lastState || '',
    runCount: ev.runCount || 0,
  };
  renderSchedules();
}

/* ═══════════════════════ 远程设备文件工作面（M11-E） ═══════════════════════
   用户本人对另一台设备上的文件做读取 / 审阅 / 修改 / 保存。后端给的是权威视图，
   界面只忠实渲染它：连接清单、能力降级项、远端文件树、查看·审查·编辑三页签。
   所有请求都是「本人动作」，不经过审批浮层；远端编辑产生的 diff 仅供人审。
   与本地编辑同一纪律：保存带 expectedDigest 乐观锁，远端被改过就拒绝（409）。
   与本地编辑的差异也必须看得见：远端是只读连接时保存按钮直接置灰并给原因，
   不做到点了才报错；能力降级项直接列在面板上，不假装可用。 */

const REMOTE_RTABS = ['view', 'review', 'edit'];

/* 端点路径里的连接名要编码 —— 名字里可能有空格 / 非 ASCII */
function remoteConnURL(name, action) {
  return `/api/remote/connections/${encodeURIComponent(name)}${action ? '/' + action : ''}`;
}

function remoteActiveConn() {
  return S.remote.connections.find((c) => c.name === S.remote.active) || null;
}

/* remote.state / state 载荷里的 changes 是「本会话远端改动条数」（协议如此），
   但也容忍后端给成数组 —— 两种都当成一个数用。 */
function remoteChangeCount() {
  const c = S.remote.state ? S.remote.state.changes : undefined;
  if (Array.isArray(c)) return c.length;
  const n = Number(c);
  return Number.isFinite(n) ? n : S.remote.changes.length;
}

async function refreshRemotePanel() {
  await Promise.all([
    refreshRemoteConnections(),
    refreshRemoteState(),
    loadWorkspaceRoots(),
  ]);
  renderRemoteConns();
  renderRemoteDegraded();
  renderRemoteActive();
  setRemoteBrowsePath(S.remote.browsePath || '');
  renderRemoteTree();
  refreshRemoteSaveState();
  refreshRemoteChanges();
}

async function refreshRemoteConnections() {
  if (!S.remote.enabled) { renderRemoteConns(); return; }
  try {
    const res = await api.get('/api/remote/connections');
    S.remote.enabled = res.enabled !== false;
    S.remote.connections = res.connections || [];
    S.remote.configPath = res.configPath || '';
    S.remote.default = res.default || '';
    S.remote.ssh = res.ssh || null;
    if (res.active && !S.remote.active) S.remote.active = res.active;
  } catch (err) {
    const list = $('remote-conn-list');
    if (list) list.replaceChildren(el('div', 'empty', '加载连接失败：' + err.message));
  }
}

async function refreshRemoteState() {
  if (!S.remote.enabled) { renderRemoteActive(); return; }
  try {
    S.remote.state = await api.get('/api/remote/state');
  } catch (err) {
    const chip = $('remote-active');
    if (chip) { chip.hidden = false; chip.textContent = '状态不可用：' + err.message; }
  }
  renderRemoteActive();
  renderRemoteDegraded();
  refreshRemoteSaveState();
}

/* ── 连接清单 ─────────────────────────────────────────────────────────── */
function updateRemoteBadges() {
  const conns = S.remote.connections.length;
  const badge = $('badge-remote');
  if (badge) {
    badge.textContent = conns;
    badge.classList.toggle('has', conns > 0);
  }
  const drawerBadge = $('badge-remote-drawer');
  if (drawerBadge) {
    drawerBadge.textContent = conns;
    drawerBadge.classList.toggle('has', conns > 0);
  }
  const changes = remoteChangeCount();
  const act = $('act-badge-remote');
  if (act) { act.hidden = changes === 0; act.textContent = changes; }
  const cb = $('badge-remote-changes');
  if (cb) cb.textContent = S.remote.changes.length;
  const count = $('remote-change-count');
  if (count) {
    count.hidden = changes === 0;
    count.textContent = `改动 ${changes}`;
  }
}

function renderRemoteConns() {
  const list = $('remote-conn-list');
  if (!list) return;
  const pathChip = $('remote-config-path');
  if (pathChip) {
    const sshBad = S.remote.ssh && S.remote.ssh.available === false;
    pathChip.textContent = S.remote.configPath
      ? `配置文件 ${S.remote.configPath}${sshBad ? ' · 本机 ssh 不可用' : ''}`
      : '';
    pathChip.title = S.remote.configPath || '';
  }
  updateRemoteBadges();
  renderRemoteActive();      // 连接清单变了，顶部下拉与状态点要跟着走
  if (!S.remote.connections.length) {
    list.replaceChildren(el('div', 'empty', S.remote.enabled
      ? '还没有连接：用下面的表单新增一个。'
      : '远程工作面未启用（VIBE_REMOTE=0）。'));
    return;
  }
  list.replaceChildren(...S.remote.connections.map(remoteConnRow));
  syncRemoteRunning();
}

function remoteConnRow(c) {
  const isActive = c.name === S.remote.active;
  const isDefault = !!(c.default || S.remote.default === c.name);
  const row = el('div', 'remote-conn');
  if (isActive) row.classList.add('is-active');

  const head = el('div', 'remote-conn-head');
  head.appendChild(el('span', 'remote-conn-name', c.name || ''));
  if (isDefault) head.appendChild(el('span', 'task-state is-ok', '默认'));
  if (isActive) head.appendChild(el('span', 'task-state is-running', '已打开'));
  row.appendChild(head);

  row.appendChild(el('div', 'remote-conn-target',
    c.display || `${c.user ? c.user + '@' : ''}${c.host || ''}`));

  // 权限位徽标：活动连接的以 state 为准，其余按清单里的配置值
  const st = isActive && S.remote.state ? S.remote.state : null;
  const allowWrite = st ? !!st.allowWrite : !!c.allow_write;
  const allowShell = st ? !!st.allowShell : !!c.allow_shell;
  const perms = el('div', 'remote-conn-perms');
  perms.appendChild(el('span', `task-state ${allowWrite ? 'is-ok' : 'is-dim'}`, allowWrite ? '可写' : '只读'));
  perms.appendChild(el('span', `task-state ${allowShell ? 'is-ok' : 'is-dim'}`, allowShell ? '可执行' : '禁命令'));
  row.appendChild(perms);

  const problems = c.problems || [];
  if (problems.length) {
    row.appendChild(el('div', 'remote-conn-problem', `配置问题：${problems.join('；')}`));
  }
  if (c.note) row.appendChild(el('div', 'remote-conn-note', c.note));

  const test = S.remote.tests.get(c.name);
  if (test) row.appendChild(remoteTestLine(test));

  // 低频动作收进「⋯」菜单：一行只留主操作「打开」，行高一致、扫读不打架。
  // 执行中只锁改动类动作（后端 409 是同一口径），「测试」仍可用。
  const acts = el('div', 'task-actions');
  const open = el('button', 'btn btn-small', '打开');
  open.type = 'button';
  open.title = '打开这个连接，浏览它的文件树';
  open.addEventListener('click', () => remoteOpen(c.name));
  acts.appendChild(open);

  const more = el('button', 'btn btn-small', '⋯');
  more.type = 'button';
  more.title = '更多动作：测试 / 默认 / 权限 / 编辑 / 删除';
  more.addEventListener('click', (event) => {
    event.stopPropagation();
    const guard = (fn) => () => {
      if (S.running) { toast('任务执行中，等它结束后再改连接', 'error'); return; }
      fn();
    };
    openTreeMenu(event.clientX, event.clientY, [
      { label: '测试连接', run: () => remoteTestConnection(c.name) },
      { label: isDefault ? '默认连接' : '设为默认', run: guard(() => remoteSetDefault(c.name)) },
      {
        label: allowWrite ? '关掉可写' : '打开可写',
        run: guard(() => remoteSetPermission(c.name, 'write', !allowWrite)),
      },
      {
        label: allowShell ? '关掉可执行' : '打开可执行',
        run: guard(() => remoteSetPermission(c.name, 'shell', !allowShell)),
      },
      { label: '编辑…', run: guard(() => editRemoteConn(c.name)) },
      { label: '删除…', run: guard(() => remoteDeleteConn(c.name)) },
    ]);
  });
  acts.appendChild(more);

  row.appendChild(acts);
  return row;
}

/* 测试结果如实呈现：成功给 display + ms + 降级项；失败给 kind + reason。
   失败是「这条连不上」的正常结论，不是致命错误 —— 所以只落在这张卡片上。 */
function remoteTestLine(t) {
  if (t && t.ok) {
    const deg = (t.capabilities && t.capabilities.degraded) || [];
    const line = el('div', 'remote-test is-ok',
      `连通：${t.display || ''} · ${t.ms || 0}ms${deg.length ? ` · 降级 ${deg.length} 项` : ''}`);
    if (deg.length) line.title = deg.join('；');
    return line;
  }
  const kind = t && t.kind ? `${t.kind} ` : '';
  return el('div', 'remote-test is-bad',
    `连不上：${kind}${(t && t.reason) || '未知原因'}`);
}

/* remote.state 驱动：连接与权限位徽标实时更新，不自己推断。 */
function applyRemoteState(ev) {
  S.remote.state = { ...(S.remote.state || {}), ...(ev || {}) };
  if (ev && ev.connection !== undefined) S.remote.active = ev.connection || '';
  if (ev && ev.browsePath !== undefined) setRemoteBrowsePath(ev.browsePath);
  renderRemoteActive();
  renderRemoteDegraded();
  renderRemoteConns();       // 活动连接变了，权限位徽标要跟着走
  refreshRemoteSaveState();
  // 改动条数变了就拉一次权威列表（事件本身不带正文，也不带条目明细）
  if (remoteChangeCount() !== S.remote.changes.length) refreshRemoteChanges();
  // 远端根清单变了（M12）：事件只带 ns 路径，明细要回端点取一遍工作区根。
  if (Array.isArray(ev?.roots)) {
    const shown = (S.workspaceRoots || []).map((r) => r.nsPath);
    if (shown.join('|') !== ev.roots.join('|')) loadWorkspaceRoots();
  }
}

/* remote.edit：远端编辑/保存产生了 diff。把当前打开文件标记为「远端已改」（它是
   基于旧 digest 打开的，再保存必然 409），并刷新审查列表让人看到真实改动。 */
function applyRemoteEdit(ev) {
  const path = (ev && ev.path) || '';
  const conn = (ev && ev.connection) || '';
  if (path && path === S.remote.currentPath && (!conn || conn === S.remote.active)) {
    S.remote.stale = true;
    renderRemoteFileFlag();
    toast('远端已改：当前打开的文件被改过，请重新加载后再修改', 'error');
  }
  refreshRemoteChanges();
}

function renderRemoteActive() {
  const chip = $('remote-active');
  const select = $('remote-conn-select');
  const dot = $('remote-state-dot');
  // 连接下拉：权威清单来自 /api/remote/connections，不自己推断
  if (select) {
    const want = S.remote.connections.map((c) => c.name);
    const shown = [...select.options].slice(1).map((o) => o.value);
    if (want.join('|') !== shown.join('|')) {
      select.replaceChildren(
        el('option', '', S.remote.enabled ? '选择连接' : '未启用'),
        ...S.remote.connections.map((c) => {
          const o = el('option', '', `${c.name}${c.display ? ` · ${c.display}` : ''}`);
          o.value = c.name;
          return o;
        }),
      );
    }
    select.disabled = !S.remote.enabled;
    select.value = S.remote.active || '';
  }
  if (!chip) return;
  const c = remoteActiveConn();
  const st = S.remote.state;
  const setDot = (cls, title) => {
    if (!dot) return;
    dot.classList.remove('is-ok', 'is-warn', 'is-bad');
    if (cls) dot.classList.add(cls);
    dot.title = title;
  };
  if (!S.remote.enabled) {
    chip.hidden = false; chip.textContent = '未启用（VIBE_REMOTE=0）'; chip.title = '';
    setDot('', '未启用');
    return;
  }
  if (!S.remote.active || !c) {
    chip.hidden = false; chip.textContent = '未打开连接'; chip.title = '';
    setDot('', '未打开连接');
    return;
  }
  const display = (st && st.display) || c.display || c.name || '';
  chip.hidden = false;
  chip.textContent = `${display} · ${st && st.allowWrite ? '可写' : '只读'} · ${st && st.allowShell ? '可执行' : '禁命令'}`;
  chip.title = st && st.root ? `root: ${st.root}` : '';
  // 状态点三色与 sb-state 同源：绿=正常、黄=能力降级、红=状态不可用
  if (!st) setDot('is-warn', '状态不可用');
  else if (st.error || st.ok === false) setDot('is-bad', String(st.error || st.reason || '连接异常'));
  else if ((st.capabilities && st.capabilities.degraded || []).length) setDot('is-warn', '有降级项');
  else setDot('is-ok', '连接正常');
}

/* 能力降级项直接列在面板上：这台设备上做不到的事，不装作做得到。
   默认收成一行「能力：N 项降级」，展开才逐条列出 —— 降级是少数情况，
   不该天天占着正文高度；但一旦存在就**必须能看见**（摘要行不许藏）。
   没有降级项时这块整个消失，不摆一行「一切都好」的废话。 */
function renderRemoteDegraded() {
  const host = $('remote-degraded');
  if (!host) return;
  const st = S.remote.state;
  const deg = [];
  if (st && st.capabilities && Array.isArray(st.capabilities.degraded)) {
    for (const d of st.capabilities.degraded) deg.push(d);
  }
  for (const d of S.remote.treeDegraded) if (!deg.includes(d)) deg.push(d);
  const toggle = $('remote-degraded-toggle');
  if (toggle) {
    toggle.hidden = !deg.length;
    toggle.textContent = `能力：${deg.length} 项降级 ${S.remote.degradedOpen ? '▴' : '▾'}`;
    toggle.title = deg.join('；');
  }
  if (!deg.length) { host.hidden = true; host.replaceChildren(); return; }
  host.hidden = !S.remote.degradedOpen;
  if (!S.remote.degradedOpen) { host.replaceChildren(); return; }
  const list = el('div', 'remote-degraded-list');
  for (const d of deg) list.appendChild(el('span', 'chip is-warning', String(d)));
  host.replaceChildren(
    el('div', 'remote-degraded-head', '能力降级（这些能力在这台设备上不可用）'),
    list,
  );
}

/* ── 打开连接 / 目录树 ────────────────────────────────────────────────── */
function setRemoteBrowsePath(p) {
  S.remote.browsePath = p || '';
  renderRemoteCrumb();
}

/* 面包屑：@remote/<连接> › a › b，点任一段回到那一层。
   纯文本路径在长目录下会被省略号吃掉中间层级，回跳就只能靠「⌂ 根」再点回来。 */
function renderRemoteCrumb() {
  const host = $('remote-path-crumb');
  if (!host) return;
  host.replaceChildren();
  if (!S.remote.enabled) {
    host.appendChild(el('span', 'remote-crumb-empty', '未启用（VIBE_REMOTE=0）'));
    return;
  }
  const conn = S.remote.active;
  if (!conn) {
    host.appendChild(el('span', 'remote-crumb-empty', '未打开连接'));
    return;
  }
  const add = (label, target, isLast) => {
    if (host.childElementCount) host.appendChild(el('span', 'remote-crumb-sep', '›'));
    if (isLast) {
      host.appendChild(el('span', 'remote-crumb is-current', label));
      return;
    }
    const b = el('button', 'remote-crumb', label);
    b.type = 'button';
    b.title = target === '' ? '回到连接根目录' : `跳到 ${target}`;
    b.addEventListener('click', () => remoteJumpTo(target));
    host.appendChild(b);
  };
  const parts = String(S.remote.browsePath || '').split('/').filter(Boolean);
  add(`@remote/${conn}`, '', parts.length === 0);
  parts.forEach((seg, i) => {
    const target = parts.slice(0, i + 1).join('/');
    add(seg, target, i === parts.length - 1);
  });
}

function remoteJumpTo(target) {
  setRemoteBrowsePath(target);
  // 上级目录都在展开集合里，否则树上看不到刚跳过去的那一层
  const parts = String(target || '').split('/').filter(Boolean);
  for (let i = 1; i <= parts.length; i++) S.remote.expanded.add(parts.slice(0, i).join('/'));
  if (!S.remote.treeCache.has(target)) { renderRemoteTree(); loadRemoteTree(target); }
  else renderRemoteTree();
}

async function remoteOpen(name) {
  const target = name || S.remote.default || '';
  if (!target) { toast('先选一个连接', 'error'); return; }
  try {
    const res = await api.post('/api/remote/open', { connection: target });
    S.remote.active = res.connection || target;
    S.remote.state = res;
    S.remote.treeCache = new Map();
    S.remote.treeDegraded = new Set();
    S.remote.expanded = new Set(['']);
    S.remote.currentPath = '';
    S.remote.currentFile = null;
    S.remote.dirty = false;
    S.remote.stale = false;
    renderRemoteActive();
    renderRemoteDegraded();
    renderRemoteFile();
    setRemoteBrowsePath(res.browsePath || '');
    await loadRemoteTree('');
    renderRemoteConns();
    toast(`已打开连接 ${res.display || target}`, 'ok');
  } catch (err) {
    toast(`打开失败：${err.message}`, 'error');
  }
}

async function loadRemoteTree(path) {
  const rel = path || '';
  try {
    const res = await api.get('/api/remote/tree', { path: rel });
    S.remote.treeCache.set(rel, res.entries || []);
    for (const d of (res.degraded || [])) S.remote.treeDegraded.add(d);
    renderRemoteDegraded();
    renderRemoteTree();
  } catch (err) {
    const host = $('remote-tree');
    if (host) host.replaceChildren(el('div', 'empty', '读取目录失败：' + err.message));
  }
}

function renderRemoteTree() {
  const host = $('remote-tree');
  if (!host) return;
  if (!S.remote.enabled) {
    host.replaceChildren(el('div', 'remote-empty',
      el('div', 'empty', '远程工作面未启用（VIBE_REMOTE=0）。')));
    return;
  }
  if (!S.remote.active) {
    // 空态必须给出下一步动作：一行灰字等于把用户晾在这里
    const box = el('div', 'remote-empty');
    box.appendChild(el('div', 'empty', '还没打开连接。'));
    const actions = el('div', 'remote-empty-actions');
    if (S.remote.connections.length) {
      const pick = el('button', 'btn btn-small btn-primary', '选择连接');
      pick.type = 'button';
      pick.addEventListener('click', () => {
        const select = $('remote-conn-select');
        if (select) select.focus();
      });
      actions.appendChild(pick);
    }
    const add = el('button', 'btn btn-small', '＋ 新建连接');
    add.type = 'button';
    add.addEventListener('click', () => openRemoteDrawer(true));
    actions.appendChild(add);
    box.appendChild(actions);
    host.replaceChildren(box);
    return;
  }
  if (!S.remote.treeCache.has('')) {
    host.replaceChildren(el('div', 'empty', '目录还没加载，点「⌂ 根」或重新打开连接。'));
    return;
  }
  const entries = S.remote.treeCache.get('') || [];
  if (!entries.length) {
    host.replaceChildren(el('div', 'empty', '这个目录是空的。'));
    return;
  }
  host.replaceChildren(buildRemoteTreeNodes(''));
}

function buildRemoteTreeNodes(path) {
  const frag = document.createDocumentFragment();
  const entries = S.remote.treeCache.get(path) || [];
  for (const e of entries) {
    const isDir = e.type === 'dir';
    const node = el('div', 'tree-node remote-tree-node');
    if (e.path === S.remote.currentPath) node.classList.add('is-active');
    if (isDir) node.classList.add('is-dir');
    node.title = e.link ? `${e.path}（符号链接，不会跟随）` : e.path;
    node.appendChild(el('span', 'twisty', isDir ? (S.remote.expanded.has(e.path) ? '▾' : '▸') : ''));
    node.appendChild(el('span', 'name', e.name || e.path));
    if (!isDir) node.appendChild(el('span', 'size', e.link ? '符号链接' : fmtSize(e.size)));
    node.addEventListener('click', () => {
      if (isDir) remoteToggleDir(e.path);
      else remoteOpenFile(e.path, !!e.link);
    });
    // 右键任意节点「加入对话」（文件与目录同权，与工作区树一致）；
    // 目录再加「加入 / 移出工作区」（M12）：加进去之后就能用本地同名工具写
    // `@remote/<连接>/<这个目录>`。单个文件没有「根」的语义，所以只给引用。
    node.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      const conn = S.remote.active;
      if (!conn) { toast('先打开一个连接', 'error'); return; }
      const items = [
        { label: '加入对话', run: () => addRemoteRef(conn, e.path, isDir) },
      ];
      if (isDir) {
        const inWs = isRootInWorkspace(conn, e.path);
        items.push(
          inWs
            ? { label: '移出工作区', run: () => removeRemoteRoot(conn, e.path) }
            : { label: '加入工作区', run: () => addRemoteRoot(conn, e.path) }
        );
      }
      openTreeMenu(event.clientX, event.clientY, items);
    });
    frag.appendChild(node);

    if (isDir && S.remote.expanded.has(e.path)) {
      const kids = el('div', 'remote-tree-children');
      frag.appendChild(kids);
      if (!S.remote.treeCache.has(e.path)) kids.appendChild(el('div', 'empty', '加载中…'));
      else if (!(S.remote.treeCache.get(e.path) || []).length) {
        kids.appendChild(el('div', 'empty', '这个目录是空的'));
      } else kids.appendChild(buildRemoteTreeNodes(e.path));
    }
  }
  return frag;
}

function remoteToggleDir(path) {
  setRemoteBrowsePath(path);
  if (S.remote.expanded.has(path)) {
    S.remote.expanded.delete(path);
    renderRemoteTree();
    return;
  }
  S.remote.expanded.add(path);
  renderRemoteTree();               // 先渲染出「加载中…」
  if (!S.remote.treeCache.has(path)) loadRemoteTree(path);
}

function remoteGotoRoot() {
  S.remote.expanded = new Set(['']);
  setRemoteBrowsePath('');
  if (!S.remote.treeCache.has('')) { loadRemoteTree(''); return; }
  renderRemoteTree();
}

/* ── 右栏：查看 / 审查 / 编辑 ─────────────────────────────────────────── */
function setRemoteTab(tab) {
  const next = REMOTE_RTABS.includes(tab) ? tab : 'view';
  S.remote.rtab = next;
  document.querySelectorAll('#remote-tabs [data-rtab]').forEach((b) => {
    b.classList.toggle('is-active', b.dataset.rtab === next);
  });
  for (const t of REMOTE_RTABS) {
    const pane = $('remote-pane-' + t);
    if (pane) pane.classList.toggle('is-off', t !== next);
  }
  if (next === 'review') refreshRemoteChanges();
  if (next === 'edit') refreshRemoteSaveState();
}

function renderRemoteFileFlag() {
  const flag = $('remote-file-flag');
  if (flag) flag.hidden = !S.remote.stale;
}

function renderRemoteFile() {
  const path = S.remote.currentPath;
  const f = S.remote.currentFile;
  const pathNode = $('remote-file-path');
  if (pathNode) { pathNode.textContent = path || '未打开文件'; pathNode.title = path || ''; }
  renderRemoteFileFlag();
  const meta = $('remote-view-meta');
  const body = $('remote-view-body');
  const ta = $('remote-edit-text');
  const dh = $('remote-edit-diff');
  if (!f) {
    if (meta) meta.replaceChildren();
    if (body) body.replaceChildren(el('div', 'empty', '从中间选一个文件查看。'));
    if (ta) { ta.value = ''; ta.disabled = true; }
    if (dh) dh.replaceChildren(el('div', 'empty', '打开文件后可在这里改内容。'));
    setRemoteHint('');
    refreshRemoteSaveState();
    return;
  }
  if (meta) {
    const chips = [
      '远端内容，未经审查',
      String(f.encoding || '').toUpperCase(),
      f.newline || '',
      f.bom ? 'BOM' : '无 BOM',
      `${f.lines} 行`,
      fmtSize(f.size),
      `digest ${f.digest || '—'}`,
    ].filter(Boolean);
    meta.replaceChildren(...chips.map((t) => el('span', 'chip', t)));
  }
  if (body) {
    const pre = document.createElement('pre');
    pre.textContent = f.text ?? '';
    body.replaceChildren(pre);
  }
  if (ta) { ta.disabled = false; ta.value = f.text ?? ''; }
  if (dh) dh.replaceChildren(el('div', 'empty', '改完点「保存到远端」，这里会先给出将产生的 diff。'));
  setRemoteHint('');
  refreshRemoteSaveState();
}

/* 打不开的文件（二进制 / 超限 / 无权限）：把后端原因原样摆出来，不吞掉。 */
function renderRemoteFileError(path, message) {
  const pathNode = $('remote-file-path');
  if (pathNode) { pathNode.textContent = path; pathNode.title = path; }
  renderRemoteFileFlag();
  const meta = $('remote-view-meta');
  if (meta) meta.replaceChildren(el('span', 'chip is-error', '打不开'));
  const body = $('remote-view-body');
  if (body) {
    body.replaceChildren(el('div', 'remote-error',
      `这个文件打不开（不支持的二进制 / 超过大小上限 / 其它原因）：${message}`));
  }
  const ta = $('remote-edit-text');
  if (ta) { ta.value = ''; ta.disabled = true; }
  const dh = $('remote-edit-diff');
  if (dh) dh.replaceChildren(el('div', 'empty', '这个文件不支持就地编辑。'));
  refreshRemoteSaveState();
}

function renderRemoteSymlinkNote(path, name) {
  const pathNode = $('remote-file-path');
  if (pathNode) { pathNode.textContent = path; pathNode.title = path; }
  S.remote.stale = false;
  renderRemoteFileFlag();
  const meta = $('remote-view-meta');
  if (meta) meta.replaceChildren(el('span', 'chip is-warning', '符号链接，未跟随'));
  const body = $('remote-view-body');
  if (body) {
    body.replaceChildren(el('div', 'empty', `「${name}」是符号链接，出于安全没有跟随它去读目标文件。`));
  }
}

async function remoteOpenFile(path, isLink) {
  setRemoteBrowsePath(path);
  setRemoteTab('view');
  if (isLink) {
    S.remote.currentPath = path;
    S.remote.currentFile = null;
    renderRemoteSymlinkNote(path, path.split('/').pop());
    renderRemoteTree();
    return;
  }
  try {
    const res = await api.get('/api/remote/file', { path });
    S.remote.currentPath = path;
    S.remote.currentFile = res;
    S.remote.dirty = false;
    S.remote.stale = false;
    renderRemoteFile();
  } catch (err) {
    S.remote.currentPath = path;
    S.remote.currentFile = null;
    S.remote.dirty = false;
    S.remote.stale = false;
    renderRemoteFileError(path, err.message);
    toast(`打开失败：${err.message}`, 'error');
  }
  renderRemoteTree();
}

function remoteReloadFile() {
  if (!S.remote.currentPath) { toast('先打开一个文件', 'error'); return; }
  remoteOpenFile(S.remote.currentPath, false);
}

/* ── 保存（乐观锁） ─────────────────────────────────────────────────────
   与本地保存同一纪律：内容原样回传，digest 作乐观锁基准；远端被改过就 409。
   保存前先把「将产生的 diff」算出来给人看 —— 这里用前后缀对齐的轻量算法，
   只作预览；权威 diff 以远端回包为准（保存后用它覆盖显示）。 */
function buildLocalDiff(oldText, newText, relPath) {
  const a = String(oldText ?? '').replace(/\r\n?/g, '\n').split('\n');
  const b = String(newText ?? '').replace(/\r\n?/g, '\n').split('\n');
  let pre = 0;
  while (pre < a.length && pre < b.length && a[pre] === b[pre]) pre++;
  let suf = 0;
  while (suf < a.length - pre && suf < b.length - pre
         && a[a.length - 1 - suf] === b[b.length - 1 - suf]) suf++;
  const oldMid = a.slice(pre, a.length - suf);
  const newMid = b.slice(pre, b.length - suf);
  if (!oldMid.length && !newMid.length) return '';
  const ctxStart = Math.max(0, pre - 3);
  const ctxLines = a.slice(ctxStart, pre);
  const tail = a.slice(a.length - suf, a.length - suf + 3);
  const out = [
    `--- a/${relPath}`,
    `+++ b/${relPath}`,
    `@@ -${ctxStart + 1},${ctxLines.length + oldMid.length + tail.length}`
      + ` +${ctxStart + 1},${ctxLines.length + newMid.length + tail.length} @@`,
  ];
  for (const l of ctxLines) out.push(' ' + l);
  for (const l of oldMid) out.push('-' + l);
  for (const l of newMid) out.push('+' + l);
  for (const l of tail) out.push(' ' + l);
  return out.join('\n');
}

function renderRemoteDiffHost(host, diffText, fallback) {
  if (!host) return;
  if (!diffText) { host.replaceChildren(el('div', 'empty', fallback || '（无 diff）')); return; }
  const rendered = renderDiff(diffText);
  if (rendered) { host.replaceChildren(rendered); return; }
  const pre = document.createElement('pre');
  pre.className = 'hunk-diff';
  pre.textContent = diffText;
  host.replaceChildren(pre);
}

function setRemoteHint(text, kind, withReload) {
  const host = $('remote-edit-hint');
  if (!host) return;
  if (!text) { host.replaceChildren(); return; }
  host.replaceChildren(el('span', `remote-hint${kind ? ' is-' + kind : ''}`, text));
  if (withReload) {
    const btn = el('button', 'btn btn-small', '重新加载');
    btn.type = 'button';
    btn.title = '丢弃本地改动，重新从远端读一次（拿到最新的 digest）';
    btn.addEventListener('click', remoteReloadFile);
    host.appendChild(btn);
  }
}

function remoteCanWrite() {
  const st = S.remote.state;
  return !!(st && st.allowWrite);
}

function remoteReadOnlyReason() {
  const c = remoteActiveConn();
  const name = c ? c.name : S.remote.active;
  return `该连接为只读（allow_write=false），不能保存${name ? '：' + name : ''}`;
}

function remoteSaveDisabledReason() {
  if (S.running) return 'Agent 正在执行（执行中禁止保存与改连接，与后端 409 一致）';
  if (!S.remote.currentFile || !S.remote.currentPath) return '先打开一个远端文件';
  if (!S.remote.state) return '连接状态未知，先「打开」这个连接';
  if (!remoteCanWrite()) return remoteReadOnlyReason();
  return '';
}

function refreshRemoteSaveState() {
  const save = $('remote-save');
  if (save) {
    const reason = remoteSaveDisabledReason();
    save.disabled = !!reason;
    save.title = reason || '保存到远端（乐观锁：远端被改过会拒绝）';
  }
  const reload = $('remote-reload');
  if (reload) reload.disabled = !S.remote.currentPath;
}

async function remoteSave() {
  const f = S.remote.currentFile;
  const path = S.remote.currentPath;
  const reason = remoteSaveDisabledReason();
  if (reason) { toast(reason, 'error'); return; }
  const ta = $('remote-edit-text');
  const content = ta ? ta.value : '';
  // 保存前先把「将产生的 diff」摆出来（本地预览，权威 diff 以回包为准）
  renderRemoteDiffHost($('remote-edit-diff'), buildLocalDiff(f.text, content, path), '（内容一致）');
  const save = $('remote-save');
  if (save) save.disabled = true;
  try {
    const res = await api.post('/api/remote/save',
      { path, content, expectedDigest: f.digest || '' });
    if (res.changed === false) {
      setRemoteHint(`远端内容与本地一致，未产生改动${res.notice ? '：' + res.notice : ''}`, '');
    } else {
      renderRemoteDiffHost($('remote-edit-diff'), res.diff,
        `已${res.action === 'create' ? '新建' : '保存'}：+${res.added || 0} / -${res.removed || 0}`);
      setRemoteHint(res.notice || `已保存：+${res.added || 0} / -${res.removed || 0}`, 'ok');
      S.remote.currentFile = { ...f, text: content, digest: res.digest || f.digest };
      S.remote.dirty = false;
      S.remote.stale = false;
      renderRemoteFileFlag();
    }
    toast('已保存到远端', 'ok');
    await refreshRemoteChanges();
  } catch (err) {
    if (err.status === 409) {
      // 乐观锁不符：远端已被改过 —— 明确告诉人要重新加载，并给按钮
      S.remote.stale = true;
      renderRemoteFileFlag();
      setRemoteHint('远端已被改过，请重新加载后再改。', 'bad', true);
      toast('远端已被改过，请重新加载', 'error');
    } else {
      setRemoteHint('保存失败：' + err.message, 'bad');
      toast('保存失败：' + err.message, 'error');
    }
  } finally {
    refreshRemoteSaveState();
  }
}

/* ── 审查：本会话远端改动 + diff ──────────────────────────────────────── */
function fmtRemoteTime(v) {
  if (v === null || v === undefined || v === '') return '';
  if (typeof v === 'number') return fmtEpoch(v);
  const s = String(v);
  return /^\d+$/.test(s) ? fmtEpoch(Number(s)) : s;
}

async function refreshRemoteChanges() {
  const host = $('remote-changes');
  if (!S.remote.enabled) {
    if (host) host.replaceChildren(el('div', 'empty', '远程工作面未启用。'));
    return;
  }
  try {
    const res = await api.get('/api/remote/changes');
    S.remote.changes = res.changes || [];
    if (res.connection) S.remote.active = res.connection;
    renderRemoteChanges();
    renderRemoteActive();
  } catch (err) {
    if (host) host.replaceChildren(el('div', 'empty', '加载改动失败：' + err.message));
  }
}

function renderRemoteChanges() {
  updateRemoteBadges();
  const host = $('remote-changes');
  if (!host) return;
  if (!S.remote.changes.length) {
    host.replaceChildren(el('div', 'empty', '本会话还没有远端改动。'));
    return;
  }
  host.replaceChildren(...S.remote.changes.map(remoteChangeRow));
}

function remoteChangeRow(ch) {
  const row = el('div', 'change-item remote-change');
  const meta = el('div', 'change-meta');
  const action = String(ch.action || '');
  meta.appendChild(el('span', action === 'create' ? 'tag create' : 'tag edit',
    action === 'create' ? '新建' : (action || '改动')));
  meta.appendChild(el('strong', '', ch.rel_path || ''));
  row.appendChild(meta);

  const parts = [
    ch.connection || '',
    `+${ch.added || 0} / -${ch.removed || 0}`,
    fmtSize(ch.bytes),
    fmtRemoteTime(ch.edited_at),
  ].filter(Boolean);
  row.appendChild(el('div', 'remote-change-detail', parts.join(' · ')));
  row.appendChild(el('div', 'remote-change-sha', `sha256 ${String(ch.sha256 || '').slice(0, 16)}`));
  if (ch.backed_up) {
    row.appendChild(el('div', 'remote-change-backup', `已备份：${ch.backup_path || ''}`));
  }

  const acts = el('div', 'task-actions');
  const btn = el('button', 'btn btn-small', '看 diff');
  btn.type = 'button';
  btn.title = '拉这个文件在远端产生的 diff（hunk 级）';
  btn.addEventListener('click', () => remoteShowDiff(ch.connection, ch.rel_path));
  acts.appendChild(btn);
  row.appendChild(acts);
  return row;
}

async function remoteShowDiff(conn, path) {
  const host = $('remote-diff');
  if (host) host.replaceChildren(el('div', 'empty', '加载 diff…'));
  try {
    const res = await api.get('/api/remote/diff', { connection: conn || '', path: path || '' });
    renderRemoteDiffResult(res);
  } catch (err) {
    if (host) host.replaceChildren(el('div', 'empty', '读取 diff 失败：' + err.message));
  }
}

/* available:false 是正常分支（可能是二进制、超大、或后端没留底），
   如实把 reason 当提示显示 —— 不弹成错误。 */
function renderRemoteDiffResult(res) {
  const host = $('remote-diff');
  if (!host) return;
  if (!res) { host.replaceChildren(el('div', 'empty', '没有 diff 数据。')); return; }
  if (res.available === false) {
    host.replaceChildren(el('div', 'remote-diff-na',
      `此改动暂无可用 diff：${res.reason || '（后端未给原因）'}`));
    return;
  }
  const frag = document.createDocumentFragment();
  frag.appendChild(el('div', 'sb-caption',
    `${res.path || ''} · +${res.added || 0} / -${res.removed || 0}`));
  const hunks = Array.isArray(res.hunks) ? res.hunks : [];
  if (!hunks.length) {
    const rendered = renderDiff(res.diff || '');
    if (rendered) frag.appendChild(rendered);
    else {
      const pre = document.createElement('pre');
      pre.className = 'hunk-diff';
      pre.textContent = res.diff || '';
      frag.appendChild(pre);
    }
    host.replaceChildren(frag);
    return;
  }
  for (const h of hunks) {
    const card = el('div', 'hunk-card remote-hunk');
    const head = el('div', 'hunk-head');
    head.appendChild(el('span', 'hunk-title', h.header || `hunk ${h.index}`));
    head.appendChild(el('span', 'remote-hunk-stat', `+${h.added || 0} / -${h.removed || 0}`));
    card.appendChild(head);
    const rendered = renderDiff(h.diff || '');
    if (rendered) card.appendChild(rendered);
    else {
      const pre = document.createElement('pre');
      pre.className = 'hunk-diff';
      pre.textContent = h.diff || '';
      card.appendChild(pre);
    }
    frag.appendChild(card);
  }
  host.replaceChildren(frag);
}

/* ── 连接管理动作 ─────────────────────────────────────────────────────── */
async function remoteTestConnection(name) {
  try {
    const res = await api.post(remoteConnURL(name, 'test'), {});
    S.remote.tests.set(name, res);
    // 测试回包里的能力降级项也并到面板上（与 state / tree 的降级项同一处展示）
    for (const d of ((res.capabilities && res.capabilities.degraded) || [])) S.remote.treeDegraded.add(d);
    renderRemoteDegraded();
    renderRemoteConns();
    if (res.ok) {
      const deg = (res.capabilities && res.capabilities.degraded) || [];
      toast(`连接正常：${res.display || name}（${res.ms || 0}ms）`
        + (deg.length ? ` · 降级 ${deg.length} 项` : ''), 'ok');
    } else {
      toast(`连不上：${res.kind || ''} ${res.reason || ''}`.trim(), 'error');
    }
  } catch (err) {
    S.remote.tests.set(name, { ok: false, kind: '', reason: err.message });
    renderRemoteConns();
    toast(`测试失败：${err.message}`, 'error');
  }
}

async function remoteSetPermission(name, field, value) {
  try {
    const body = field === 'write' ? { allowWrite: value } : { allowShell: value };
    const res = await api.post(remoteConnURL(name, 'permissions'), body);
    const updated = res.connection;
    if (updated) {
      const idx = S.remote.connections.findIndex((c) => c.name === name);
      if (idx >= 0) S.remote.connections[idx] = { ...S.remote.connections[idx], ...updated };
    }
    toast('已更新权限位', 'ok');
    renderRemoteConns();
    // 活动连接：用 state 刷新徽标（后端也会推 remote.state）
    if (name === S.remote.active) await refreshRemoteState();
    refreshRemoteSaveState();
  } catch (err) {
    toast(`更新权限失败：${err.message}`, 'error');
  }
}

async function remoteSetDefault(name) {
  try {
    await api.post(remoteConnURL(name, 'default'), {});
    toast(`已把 ${name} 设为默认连接`, 'ok');
    await refreshRemoteConnections();
    renderRemoteConns();
  } catch (err) {
    toast(`设置默认失败：${err.message}`, 'error');
  }
}

async function remoteDeleteConn(name) {
  try {
    const res = await api.post(remoteConnURL(name, 'delete'), {});
    if (res.default !== undefined) S.remote.default = res.default || '';
    if (S.remote.active === name) {
      S.remote.active = '';
      S.remote.state = null;
      S.remote.treeCache = new Map();
      S.remote.expanded = new Set();
      S.remote.currentPath = '';
      S.remote.currentFile = null;
      renderRemoteFile();
      setRemoteBrowsePath('');
      renderRemoteTree();
      renderRemoteActive();
    }
    S.remote.tests.delete(name);
    if (S.remote.editing === name) resetRemoteForm();
    toast(`已删除连接 ${name}`, 'ok');
    await refreshRemoteConnections();
    renderRemoteConns();
  } catch (err) {
    toast(`删除失败：${err.message}`, 'error');
  }
}

function editRemoteConn(name) {
  const c = S.remote.connections.find((x) => x.name === name);
  if (!c) return;
  S.remote.editing = name;
  $('rf-name').value = c.name || '';
  $('rf-name').readOnly = true;
  $('rf-host').value = c.host || '';
  $('rf-user').value = c.user || '';
  $('rf-port').value = c.port ? String(c.port) : '';
  $('rf-root').value = c.root || '';
  $('rf-identity').value = c.identity_file || '';
  $('rf-proxy').value = c.proxy_jump || '';
  $('rf-policy').value = c.host_key_policy || '';
  $('rf-allow-write').checked = !!c.allow_write;
  $('rf-allow-shell').checked = !!c.allow_shell;
  $('rf-note').value = c.note || '';
  $('remote-form-hint').textContent = `正在编辑：${name}（名称不可改）`;
  openRemoteDrawer();
}

function resetRemoteForm() {
  S.remote.editing = '';
  for (const id of ['rf-name', 'rf-host', 'rf-user', 'rf-port', 'rf-root', 'rf-identity',
                    'rf-proxy', 'rf-policy', 'rf-note']) {
    $(id).value = '';
  }
  $('rf-name').readOnly = false;
  $('rf-allow-write').checked = false;
  $('rf-allow-shell').checked = false;
  $('remote-form-hint').textContent = '';
}

async function submitRemoteConnection() {
  const name = ($('rf-name').value || '').trim();
  const host = ($('rf-host').value || '').trim();
  if (!name || !host) { toast('名称与主机都要填', 'error'); return; }
  const payload = {
    host,
    user: ($('rf-user').value || '').trim(),
    root: ($('rf-root').value || '').trim(),
    identity_file: ($('rf-identity').value || '').trim(),
    proxy_jump: ($('rf-proxy').value || '').trim(),
    host_key_policy: ($('rf-policy').value || '').trim(),
    note: ($('rf-note').value || '').trim(),
    allow_write: $('rf-allow-write').checked,
    allow_shell: $('rf-allow-shell').checked,
  };
  const portRaw = ($('rf-port').value || '').trim();
  if (portRaw) {
    const port = Number(portRaw);
    if (!Number.isInteger(port) || port <= 0 || port > 65535) {
      toast('端口要填 1-65535 的整数', 'error');
      return;
    }
    payload.port = port;
  }
  const editing = S.remote.editing;
  const btn = $('remote-conn-submit');
  btn.disabled = true;
  try {
    if (editing) {
      await api.post(remoteConnURL(editing, ''), payload);
      toast(`已更新连接 ${editing}`, 'ok');
    } else {
      await api.post('/api/remote/connections', { connection: { name, ...payload } });
      toast(`已新增连接 ${name}`, 'ok');
    }
    resetRemoteForm();
    await refreshRemoteConnections();
    renderRemoteConns();
    // 新增连接后自动打开，直接出远端文件树（「保存即见文件」）。
    // 编辑不算：避免把当前已打开的活动连接拽走、白白重建一次会话。
    if (!editing) await remoteOpen(name);
  } catch (err) {
    toast(`保存连接失败：${err.message}`, 'error');
  } finally {
    btn.disabled = S.running;
  }
}

/* ── 执行中禁用：与后端 409（BusyError）同一口径 ──────────────────────── */
function syncRemoteRunning() {
  const chip = $('remote-running');
  if (chip) chip.hidden = !S.running;
  const submit = $('remote-conn-submit');
  if (submit) submit.disabled = S.running;
  // 只锁「改连接」类的按钮；「打开 / 测试」是只读路径，执行中照样可用
  document.querySelectorAll('#remote-view .remote-mutate').forEach((b) => {
    b.disabled = S.running || b.dataset.lock === '1';
  });
  refreshRemoteSaveState();
}

/* ── 连接管理抽屉（低频动作，按需滑出）──────────────────────────────────
   reset=true 从「＋新建连接」进来：先清掉上次编辑残留；
   从连接行「⋯ → 编辑…」进来时不重置，表单已由 editRemoteConn 填好。 */
function openRemoteDrawer(reset) {
  const drawer = $('remote-conn-drawer');
  if (!drawer) return;
  if (reset === true) resetRemoteForm();
  drawer.hidden = false;
  const backdrop = $('remote-drawer-backdrop');
  if (backdrop) backdrop.hidden = false;
  renderRemoteConns();
  const name = $('rf-name');
  if (name) name.focus();
}

function closeRemoteDrawer() {
  const drawer = $('remote-conn-drawer');
  if (drawer) drawer.hidden = true;
  const backdrop = $('remote-drawer-backdrop');
  if (backdrop) backdrop.hidden = true;
}

function bindRemote() {
  $('remote-refresh').addEventListener('click', () => refreshRemotePanel());
  $('remote-close').addEventListener('click', closeRemotePanel);
  $('remote-open-conn-drawer').addEventListener('click', () => openRemoteDrawer(true));
  $('remote-drawer-close').addEventListener('click', closeRemoteDrawer);
  $('remote-drawer-backdrop').addEventListener('click', closeRemoteDrawer);
  // 连接切换：路径语义是「重置到该连接根」，remoteOpen 已按这个语义重建树
  $('remote-conn-select').addEventListener('change', (event) => {
    const name = event.target.value;
    if (!name || name === S.remote.active) return;
    remoteOpen(name);
  });
  // 降级摘要：默认收起，点一下展开逐条（有降级时必须看得见）
  $('remote-degraded-toggle').addEventListener('click', () => {
    S.remote.degradedOpen = !S.remote.degradedOpen;
    renderRemoteDegraded();
  });
  // 窄视口下左目录折叠成覆盖层，由这个按钮开合
  $('remote-dir-toggle').addEventListener('click', () => {
    $('remote-view').classList.toggle('has-dir-open');
  });
  bindResizer('remote-resizer', () => ({ id: 'remote-left', min: 180, max: 520, dir: 1 }));
  $('remote-conn-submit').addEventListener('click', submitRemoteConnection);
  $('remote-conn-cancel').addEventListener('click', resetRemoteForm);
  $('remote-up').addEventListener('click', remoteGotoRoot);
  // 「加入工作区」（M12）：把**当前连接根目录**整棵加入（子目录的入口在树的右键菜单里）。
  $('remote-add-root').addEventListener('click', () => {
    const conn = S.remote.active;
    if (!conn) { toast('先打开一个连接', 'error'); return; }
    addRemoteRoot(conn, '');
  });
  $('remote-changes-refresh').addEventListener('click', refreshRemoteChanges);
  $('remote-reload').addEventListener('click', remoteReloadFile);
  $('remote-save').addEventListener('click', remoteSave);
  $('remote-tabs').addEventListener('click', (event) => {
    const btn = event.target.closest('[data-rtab]');
    if (btn) setRemoteTab(btn.dataset.rtab);
  });
  $('remote-edit-text').addEventListener('input', () => {
    S.remote.dirty = true;
    setRemoteHint('');
    refreshRemoteSaveState();
  });
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
      addUser(m.content, m.seq || 0);
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

/* 重启续接（D）：后端开机时已自动 resume 了本工作区最近一次会话（/api/state
   的 sessionId 就是它）。这里把这批历史铺回对话区，让「关掉再打开」能接着看。
   无历史 / 读不到一律静默返回，绝不打断启动。 */
async function restoreHistoryView() {
  const sid = S.session && S.session.sessionId;
  if (!sid) return;
  try {
    const detail = await api.get(`/api/sessions/${encodeURIComponent(sid)}/messages`);
    const messages = detail.messages || [];
    if (!messages.length) return;
    stream.replaceChildren();
    replayHistory(messages);
    hideChatWelcome();
    updateChangesBadge();
    updateStatusBarIter();
  } catch {
    /* 读不到历史就当没有，不影响正常使用 */
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
  // 沿用上一次的来源（用户很可能就是冲着那一页来的）；要不要真重建上下文，
  // 永远由他自己按「切换并重启/设为工作区」决定。
  await setWsTab(WS_TAB);
  if (WS_TAB === 'remote') return; // setWsTab 已经把远程那一页的数据取好了
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
  // 两个来源的提交载荷完全不同，**互斥**：远程走它自己的那条路。
  if (WS_TAB === 'remote') return switchRemoteWorkspace();
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

/* ---- 切换工作区的「远程目录」来源（M13 / ADR-042） ---- */
/* 与本机页签并列，但切换的后果不是「换个本地路径」那么简单：
   切过去之后模型的相对路径全部落到那台远端机器上，本机只留一个
   **锚点目录**放审计链与回滚留底（里面没有任何代码）。

   三条前端纪律：
   1. **不替用户偷偷换连接** —— 浏览前必须显式 POST /api/remote/open（一次只活动
      一条连接是 M11 的纪律），换到另一条就要再点一次。
   2. **选中的就是当前这一层** —— 树里能下钻的是 dir（点进去即下钻），`#ws-remote-cwd`
      显示的那一层就是待设为工作区的那一层（这也是后端要的 `sub`）。
      文件一并列出只是为了「在本机目录旁边也能看见远端有什么」，它们**不是**候选
      （工作区永远是一个目录，ADR-042），所以做成惰性行、不绑点击。
   3. **失败一律显示原因** —— 连不上 / 目录不在 / 未启用 VIBE_REMOTE，
      后端都给了人话 reason，前端只负责显示，不自己猜。 */

let WS_TAB = 'local'; // 'local' | 'remote'
const WS_REMOTE = { conn: '', cwd: '' };

function wsError(message) {
  const host = $('ws-error');
  host.hidden = false;
  host.textContent = message;
}

function wsOption(value, text) {
  const o = document.createElement('option');
  o.value = value;
  o.textContent = text;
  return o;
}

async function setWsTab(mode) {
  WS_TAB = mode === 'remote' ? 'remote' : 'local';
  const remote = WS_TAB === 'remote';
  for (const [id, on] of [['ws-tab-local', !remote], ['ws-tab-remote', remote]]) {
    $(id).classList.toggle('is-on', on);
    $(id).setAttribute('aria-selected', String(on));
  }
  $('ws-local-body').hidden = remote;
  $('ws-remote-body').hidden = !remote;
  $('ws-mode-label').textContent = remote ? '远程目录' : '本机目录';
  $('btn-ws-open').textContent = remote ? '设为工作区并重启' : '切换并重启';
  $('ws-error').hidden = true;
  if (remote) {
    await loadRemoteConnections();
    await loadRemoteAnchors();
  }
}

async function loadRemoteConnections() {
  const sel = $('ws-conn');
  const host = $('ws-remote-dirs');
  let data = null;
  try {
    data = await api.get('/api/remote/connections');
  } catch (err) {
    sel.disabled = true;
    sel.replaceChildren(wsOption('', '读取连接清单失败'));
    $('ws-remote-note').textContent = err.message;
    host.replaceChildren(el('div', 'empty', '连接清单读不出来，无法浏览远端目录。'));
    return;
  }
  const rows = (data.connections || []).map((conn) => {
    const o = wsOption(conn.name, conn.display || conn.name);
    o.title = `${conn.name} → ${conn.root || ''}`;
    return o;
  });
  if (!rows.length) {
    sel.disabled = true;
    sel.replaceChildren(wsOption('', '（还没有配置远程连接）'));
    $('ws-remote-note').textContent = '请先在「远程设备」面板添加一条连接。';
    host.replaceChildren(el('div', 'empty', '清单是空的 —— 这一步还没有可挑的连接。'));
    return;
  }
  sel.disabled = false;
  sel.replaceChildren(wsOption('', '（选择一条连接）'), ...rows);
  // 活动连接优先：点开时大概率就是用户想继续用的那条；没有活动就退到默认那条。
  const candidates = [data.active, data.default];
  sel.value = candidates.find((name) => name && rows.some((o) => o.value === name)) || '';
  await browseRemoteDirs('');
}

async function browseRemoteDirs(rel) {
  const sel = $('ws-conn');
  const host = $('ws-remote-dirs');
  const conn = sel.value;
  if (!conn) {
    WS_REMOTE.conn = '';
    host.replaceChildren(el('div', 'empty', '先在上面选一条连接。'));
    return;
  }
  WS_REMOTE.conn = conn;
  host.replaceChildren(el('div', 'empty', '读取远端目录…'));
  try {
    // 换连接必须显式 open —— 见上方纪律 1。
    await api.post('/api/remote/open', { name: conn });
    const data = await api.get('/api/remote/tree', { connection: conn, path: rel || '' });
    if (data.ok === false) {
      host.replaceChildren(el('div', 'empty', data.reason || '该连接当前不可用。'));
      return;
    }
    // 连接 root 那一层后端回的是 `.`（posixpath.normpath 的结果），界面按「根」处理：
    // 显示成 `/`、并且**禁用**「上级」（HTML 的 title 就是这么写的）。
    WS_REMOTE.cwd = data.path === '.' ? '' : data.path || '';
    $('ws-remote-cwd').textContent = `/${WS_REMOTE.cwd}`;
    $('btn-ws-remote-up').disabled = !WS_REMOTE.cwd;
    const entries = data.entries || [];
    const dirs = entries.filter((entry) => entry.type === 'dir');
    const files = entries.filter((entry) => entry.type !== 'dir');
    const rows = [...dirs.map(remoteDirRow), ...files.map(remoteFileRow)];
    // 目录为空（或只有文件）时把话说全：这一层本身就是可选的候选。
    if (!dirs.length) {
      rows.push(el('div', 'empty', '这层没有子目录 —— 可以直接把当前这层设为工作区。'));
    }
    host.replaceChildren(...rows);
  } catch (err) {
    host.replaceChildren(el('div', 'empty', `读取远端目录失败：${err.message}`));
  }
}

function remoteDirRow(entry) {
  const row = el('button', 'ws-dir');
  row.type = 'button';
  row.title = entry.path;
  row.appendChild(el('span', 'ws-dir-name', entry.name));
  row.addEventListener('click', () => browseRemoteDirs(entry.path));
  return row;
}

/* 文件行：同屏列出，但**不是**工作区候选 —— 工作区永远是一个目录（ADR-042）。
   所以这一行不做成按钮、不绑点击，只在 title 里指路去「远程设备」面板看内容。 */
function remoteFileRow(entry) {
  const row = el('div', 'ws-file');
  row.title = `${entry.path}（文件不是工作区候选；查看内容请到「远程设备」面板）`;
  row.appendChild(el('span', 'ws-dir-name', entry.name));
  if (entry.link) row.appendChild(el('span', 'ws-file-tag', '链'));
  row.appendChild(el('span', 'ws-file-size', fmtSize(entry.size)));
  return row;
}

async function browseRemoteUp() {
  if (!WS_REMOTE.cwd) return;
  const rest = WS_REMOTE.cwd.split('/').filter(Boolean);
  rest.pop();
  await browseRemoteDirs(rest.join('/'));
}

async function loadRemoteAnchors() {
  const host = $('ws-remote-list');
  let res = null;
  try {
    // 与本机列表同一个端点：远程候选由后端**扫锚点目录**反推，不在前端另存一份。
    res = await api.get('/api/workspaces');
  } catch (err) {
    host.replaceChildren(el('div', 'empty', `读取失败：${err.message}`));
    return;
  }
  if (!res.remoteEnabled) {
    host.replaceChildren(
      el('div', 'empty', '远程工作面未启用（VIBE_REMOTE=0），无法切到远程目录。')
    );
    return;
  }
  const items = res.remoteWorkspaces || [];
  if (!items.length) {
    host.replaceChildren(el('div', 'empty', '还没有切过任何远程目录。'));
    return;
  }
  host.replaceChildren(
    ...items.map((item) => {
      const row = el('button', 'ws-item');
      row.type = 'button';
      row.title = item.path || '';
      row.appendChild(
        el('span', 'ws-path', item.nsPath || `${item.connection}:${item.sub || '/'}`)
      );
      row.appendChild(el('span', 'ws-time', shortPath(item.path || '')));
      row.addEventListener('click', () => {
        doSwitchRemote(item.connection, item.sub || '');
      });
      return row;
    })
  );
}

async function doSwitchRemote(connection, sub) {
  if (!connection) {
    wsError('这条记录缺少连接名，请重新在目录树里选一次。');
    return;
  }
  const btn = $('btn-ws-open');
  btn.disabled = true;
  $('ws-error').hidden = true;
  try {
    const res = await api.post('/api/workspaces/open', {
      remote: { connection, sub: sub || '' },
    });
    closeWorkspaceModal();
    if (res.switched === false) {
      toast(res.reason || '这已经是当前工作区。');
      return;
    }
    const at = res.remote && res.remote.sub ? res.remote.sub : '/';
    toast(`已切到远端 ${connection}:${at}，后端上下文已重启`, 'ok');
    await refreshTree();
    await refreshChanges();
    await loadHistory();
  } catch (err) {
    wsError(err.message);
  } finally {
    btn.disabled = false;
  }
}

async function switchRemoteWorkspace() {
  await doSwitchRemote($('ws-conn').value, WS_REMOTE.cwd || '');
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

/* ---- 联网检索段（M7）：总开关关闭时整段置灰（与 MCP 面板同一交互范式）
   两层置灰来源互不打架：环境变量锁定（lockField，优先）与总开关关闭
   （syncWebSection）。syncWebSection 跳过被 env 锁定的字段，避免抹掉
   「被环境变量锁定」的提示。 */
const WEB_FIELD_IDS = [
  'set-web-enabled', 'set-web-provider', 'set-web-url', 'set-web-private',
  'set-web-allow', 'set-web-deny', 'set-web-timeout', 'set-web-bytes', 'set-web-results',
];
let webLocks = {};
let webEndpointShown = '';

function syncWebSection(endpoint) {
  if (typeof endpoint === 'string' && endpoint) webEndpointShown = endpoint;
  const on = $('set-web-enabled').checked;
  for (const id of WEB_FIELD_IDS) {
    if (webLocks[id]) continue; // env 锁定的保持 lockField 状态
    if (id === 'set-web-enabled') continue; // 总开关自身永远可点
    const node = $(id);
    node.disabled = !on;
    if (!on) node.title = '先勾选「启用联网检索」';
    else node.removeAttribute('title');
  }
  const ep = $('set-web-endpoint');
  if ($('set-web-provider').value === 'searxng') {
    ep.textContent = '搜索端点：使用上方 SearXNG JSON 端点';
  } else {
    ep.textContent = `搜索端点：${webEndpointShown || '—'}（由治理面地址派生）`;
  }
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
    // 联网检索（M7）：九字段回填 + envLocked 置灰；再按总开关整段联动。
    webLocks = {
      'set-web-enabled': locked.web_enabled,
      'set-web-provider': locked.web_search_provider,
      'set-web-url': locked.web_search_url,
      'set-web-private': locked.web_fetch_allow_private,
      'set-web-allow': locked.web_allow_hosts,
      'set-web-deny': locked.web_deny_hosts,
      'set-web-timeout': locked.web_timeout,
      'set-web-bytes': locked.web_max_bytes,
      'set-web-results': locked.web_search_max_results,
    };
    $('set-web-enabled').checked = info.webEnabled === true;
    $('set-web-provider').value = info.webSearchProvider || 'gateway';
    $('set-web-url').value = info.webSearchUrl || '';
    $('set-web-private').checked = info.webFetchAllowPrivate === true;
    $('set-web-allow').value = info.webAllowHosts || '';
    $('set-web-deny').value = info.webDenyHosts || '';
    $('set-web-timeout').value = info.webTimeout ?? '';
    $('set-web-bytes').value = info.webMaxBytes ?? '';
    $('set-web-results').value = info.webSearchMaxResults ?? '';
    syncWebSection(info.webSearchEndpoint || '');
    // 模型选择器（M9-I）：只读展示。VIBE_MODEL_ROUTER=0 时整段隐藏（不留一个
    // 点了必报错的入口）；启用时回读策略与默认模型。
    $('set-router').hidden = info.modelRouterEnabled !== true;
    if (info.modelRouterEnabled === true) {
      $('set-router-policy').value = info.modelRouterPolicy || 'none';
      $('set-router-default').value = info.modelRouterDefault || '';
    }
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
    if (info.modelLocked) {
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
  // 联网检索（M7）：bool 可改时始终提交（false 也落盘）；字符串留空 = 不改；
  // 数值留空 = 不改。置灰（env 锁定或总开关关闭）的字段跳过 —— 后端照原样保留。
  if (!$('set-web-enabled').disabled) body.webEnabled = $('set-web-enabled').checked;
  if (!$('set-web-provider').disabled && $('set-web-provider').value) {
    body.webSearchProvider = $('set-web-provider').value;
  }
  for (const [key, id] of [
    ['webSearchUrl', 'set-web-url'],
    ['webAllowHosts', 'set-web-allow'],
    ['webDenyHosts', 'set-web-deny'],
  ]) {
    if ($(id).disabled) continue;
    const text = $(id).value.trim();
    if (text) body[key] = text;
  }
  if (!$('set-web-private').disabled) {
    body.webFetchAllowPrivate = $('set-web-private').checked;
  }
  for (const [key, id] of [
    ['webTimeout', 'set-web-timeout'],
    ['webMaxBytes', 'set-web-bytes'],
    ['webSearchMaxResults', 'set-web-results'],
  ]) {
    if ($(id).disabled) continue;
    const raw = $(id).value.trim();
    if (!raw) continue;
    const parsed = Number(raw);
    if (!Number.isInteger(parsed) || parsed <= 0) {
      $('set-error').hidden = false;
      $('set-error').textContent = '联网检索参数必须是正整数。';
      return;
    }
    body[key] = parsed;
  }
  const hasWeb = Object.keys(body).some((key) => key.startsWith('web'));
  if (!body.model && !body.baseUrl && !body.apiKey && !body.workspace && !hasCompletion && !hasWeb) {
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

/* -------------------------------------------------------- 逐条回退（P3）
   每条用户提问下的「回退到这条之前」：文件 + 对话一起回到发送那一刻。
   文件清单来自 /api/checkpoints 里 userSeq 与这条消息 seq 相等的 kind=run 检查点
   —— 映射在后端 manifest 里，重启后依然有效。
   执行顺序由后端保证（先校验 → 文件 → 截断对话），失败时前端什么都不用回滚。 */
let rbSeq = 0;

async function openRollbackConfirm(seq) {
  if (S.running) {
    toast('任务执行中不能回退，等任务结束或先中断', 'error');
    return;
  }
  rbSeq = seq;
  $('rb-modal').hidden = false;
  $('btn-rb-confirm').disabled = false;
  $('rb-target').textContent = '本条提问与它之后的对话会被删除，本轮改过的文件会回到发送之前的状态。';
  const host = $('rb-files');
  host.replaceChildren(el('div', 'rb-empty', '正在读取本轮的改动…'));
  try {
    const res = await api.get('/api/checkpoints');
    if (rbSeq !== seq || $('rb-modal').hidden) return; // 期间已取消或换了目标
    const cp = (res.checkpoints || []).find((c) => c.kind === 'run' && c.userSeq === seq);
    const files = (cp && cp.files) || [];
    if (!files.length) {
      host.replaceChildren(el('div', 'rb-empty', '本轮没有文件改动：只会回退对话。'));
      return;
    }
    const nodes = files.slice(0, 10).map((p) => el('span', 'rb-file', p));
    if (files.length > 10) nodes.push(el('div', 'rb-more', `等 ${files.length} 个文件`));
    host.replaceChildren(...nodes);
  } catch (err) {
    host.replaceChildren(el('div', 'rb-empty', `读取检查点失败：${err.message}`));
  }
}

function closeRollbackModal() {
  $('rb-modal').hidden = true;
  rbSeq = 0;
}

async function confirmRollback() {
  if (!rbSeq) return;
  const seq = rbSeq;
  $('btn-rb-confirm').disabled = true;
  try {
    const res = await api.post('/api/rollback', { userSeq: seq });
    closeRollbackModal();
    // 与 resumeSession() 同一套重铺：视图与后端权威历史重新对齐
    stream.replaceChildren();
    S.calls.clear();
    S.changed.clear();
    S.created.clear();
    S.iterations = 0;
    S.streaming = null;
    S.reasoning = null;
    updateChangesBadge();
    updateStatusBarIter();
    replayHistory(res.messages || []);
    hideChatWelcome();
    refreshTree();
    refreshChanges();
    loadHistory();
    addNotice(res.notice || '已回退到这条提问之前');
    addRemoteRollbackNote(res.remote);
    if (res.redoId) addRollbackUndo(res.redoId);
    toast('已回退', 'ok');
  } catch (err) {
    $('btn-rb-confirm').disabled = false;
    toast(`回退失败：${err.message}`, 'error');
  }
}

/* 文件回滚可撤销：redo 检查点就是回退前那一刻的反向快照。 */
function addRollbackUndo(redoId) {
  const node = el('div', 'notice');
  node.appendChild(document.createTextNode('文件回滚可撤销：'));
  const btn = el('button', 'btn-rollback-inline', '撤销这次回滚');
  btn.type = 'button';
  btn.disabled = S.running;
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const res = await api.post(`/api/checkpoints/${encodeURIComponent(redoId)}/restore`, {});
      addNotice(res.notice || '已撤销这次回退');
      refreshTree();
      refreshChanges();
      toast('已撤销回滚', 'ok');
    } catch (err) {
      btn.disabled = false;
      toast(`撤销失败：${err.message}`, 'error');
    }
  });
  node.appendChild(btn);
  stream.appendChild(node);
  scrollToEnd();
}

/* 远端回退结果（P4）：后端的 notice 只给条数，这里把具体文件摆出来。
   远端 redo 落在检查点目录里、没有恢复端点（只有本地 redo 有 /restore），
   所以刻意不给「撤销」按钮 —— 宁可少一个入口，也不给一个按下去会失败的按钮。 */
function addRemoteRollbackNote(remote) {
  const restored = (remote && remote.restored) || [];
  const deleted = (remote && remote.deleted) || [];
  const skipped = (remote && remote.skipped) || [];
  if (!restored.length && !deleted.length && !skipped.length) return;
  const conn = (remote && remote.connection) || '远端';
  const parts = [];
  if (restored.length) parts.push(`恢复 ${restored.length} 个文件`);
  if (deleted.length) parts.push(`删除 ${deleted.length} 个新建文件`);
  const node = el('div', 'notice');
  node.appendChild(document.createTextNode(`${conn}：${parts.join('，') || '没有文件改动'}。`));
  const strip = (p) => {
    const prefix = `remote:${conn}:`;
    return p.startsWith(prefix) ? p.slice(prefix.length) : p;
  };
  const paths = [...restored, ...deleted].map(strip);
  if (paths.length) {
    const shown = paths.slice(0, 10).join('、');
    node.appendChild(el('div', 'rb-more', paths.length > 10 ? `${shown} 等 ${paths.length} 个` : shown));
  }
  if (skipped.length) {
    const text = skipped
      .map((s) => `${s.path || '远端回退'}：${s.reason || '已跳过'}`)
      .join('；');
    node.appendChild(el('div', 'rb-more', `跳过：${text}`));
  }
  stream.appendChild(node);
  scrollToEnd();
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
  { chord: 'Esc', id: 'ui.close-gov', label: '关闭治理面登录', palette: false, when: () => !$('gov-modal').hidden, run: closeGovModal },
  { chord: 'Esc', id: 'ui.close-update', label: '关闭客户端更新', palette: false, when: () => !$('update-modal').hidden && !S.awaiting, run: closeUpdateModal },
  { chord: 'Esc', id: 'ui.close-checkpoints', label: '关闭回滚浮层', palette: false, when: () => !$('cp-modal').hidden && !S.awaiting, run: closeCheckpoints },
  { chord: 'Esc', id: 'ui.close-rollback-confirm', label: '取消逐条回退', palette: false, when: () => !$('rb-modal').hidden && !S.awaiting, run: closeRollbackModal },
  // 远程设备：先收抽屉，再关页签；焦点在输入框时不抢 Esc（用户可能在取消输入）
  { chord: 'Esc', id: 'ui.close-remote-drawer', label: '关闭连接管理', palette: false, when: () => !$('remote-conn-drawer').hidden, run: closeRemoteDrawer },
  { chord: 'Esc', id: 'ui.close-remote-panel', label: '关闭远程设备页签', palette: false, when: (event) => S.editors.has('remote') && S.activeEditor === 'remote' && !isTextField(event.target) && !S.awaiting, run: closeRemotePanel },
  { chord: 'Esc', id: 'approval.deny', label: '拒绝审批（默认动作）', palette: false, when: () => !!S.awaiting, run: () => decide('deny') },
  { chord: 'Ctrl+Enter', id: 'approval.approve', label: '批准审批', palette: false, when: () => !!S.awaiting, run: () => decide('approve') },
  // 命令面板自身
  { chord: 'Ctrl+Shift+p', id: 'palette.open', label: '命令面板', group: '视图', run: openPalette },
];

/* 命令面板里的「非快捷键动作」：与 KEYMAP 一起进列表，但没有键位。 */
const PALETTE_ACTIONS = [
  { id: 'settings.model', label: '模型设置…', group: '模型', run: openSettingsModal },
  { id: 'governance.account', label: '治理面：登录 / 注册 / 退出…', group: '模型', run: openGovModal },
  { id: 'update.open', label: '客户端更新…', group: '模型', run: openUpdateModal },
  { id: 'workspace.open', label: '切换工作区…', group: '工作区', run: openWorkspaceModal },
  { id: 'git.commit-message', label: '生成提交信息', group: 'Git', run: generateCommitMessage },
  { id: 'session.rollback', label: '回滚到检查点…', group: '会话', run: () => openCheckpoints() },
  { id: 'session.reset', label: '新建会话（清空对话）', group: '会话', run: reset },
  { id: 'session.interrupt', label: '中断当前任务', group: '会话', run: interrupt },
  { id: 'view.refresh-tree', label: '刷新文件树', group: '视图', run: refreshTree },
  { id: 'view.refresh-changes', label: '刷新改动', group: '视图', run: refreshChanges },
  { id: 'view.refresh-git', label: '刷新 Git 状态', group: '视图', run: refreshGit },
];

const PALETTE_GROUPS = ['面板', '视图', '编辑', '语言', '模型', '工作区', 'Git', '会话', 'Skill'];
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

/* 命令清单 = KEYMAP 动作（去重，Esc 等纯语境键不进面板）∪ 动态动作 ∪ Skill。 */
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
  // 运行 Skill（M9-A）：动态枚举已加载的工作流（VIBE_SKILLS=0 时 S.skills 为空，不出现）。
  for (const skill of S.skills) {
    push({
      id: `skill.run.${skill.name}`,
      label: `运行 Skill：${skill.name}`,
      group: 'Skill',
      detail: skill.description || '',
      run: () => runSkill(skill.name),
    });
  }
  const rank = (group) => {
    const i = PALETTE_GROUPS.indexOf(group);
    return i < 0 ? PALETTE_GROUP_FALLBACK : i;
  };
  return out.sort((a, b) => rank(a.group) - rank(b.group));
}

/* 运行 Skill（M9-A）：把 Skill 名作为一条任务意图填进 composer，让用户确认后发送。
   不做「直接执行」——Skill 正文由模型加载（run_skill 工具，恒 SAFE），
   真正的写盘/执行仍走 edit_* / run_command 审批漏斗（ADR-008）。 */
function runSkill(name) {
  const input = $('input');
  input.value = `运行 Skill：${name}`;
  autoGrow();
  syncSendState();
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
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
    // Skill 动作带一行描述副行（M9-A）：只在有 detail 时出现，其余命令逐字节不变。
    if (item.detail) row.append(el('span', 'palette-detail', item.detail));
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

/* 行把手：拖动改高度。把手在面板上沿，所以往上拖是「变高」（dir = -1）。 */
function bindRowResizer(handleId, resolve) {
  const handle = $(handleId);
  handle.addEventListener('mousedown', (event) => {
    event.preventDefault();
    const { id, min = 120, dir = -1 } = resolve();
    const target = $(id);
    if (target.classList.contains('is-collapsed')) toggleBottom(true);  // 收起态拖一下先展开
    const startY = event.clientY;
    const startH = target.getBoundingClientRect().height;
    const move = (e) => {
      const max = Math.max(min, window.innerHeight - 160);  // 给上面的编辑器留出余地
      const h = Math.min(max, Math.max(min, startH + (e.clientY - startY) * dir));
      target.style.height = `${h}px`;
      target.dataset.height = `${h}px`;  // 收起再展开要还回这个高度（见 toggleBottom）
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
    // `/` Skill 补全（M9-A）：行首敲 `/` 就开浮层（先于 `@`，两者互斥）
    const sk = skillPopContext();
    if (sk) {
      skillPop.open = true;
      skillPop.start = sk.start;
      skillPop.q = sk.q;
      skillPop.index = 0;
      $('skill-pop').hidden = false;
      if (mention.open) closeMention();
      renderSkillPop();
      return;
    }
    if (skillPop.open) closeSkillPop();
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
    if (skillPop.open) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (skillPop.items.length) {
          const step = event.key === 'ArrowDown' ? 1 : -1;
          skillPop.index = (skillPop.index + step + skillPop.items.length) % skillPop.items.length;
          renderSkillPop();
        }
        return;
      }
      if ((event.key === 'Enter' && !event.shiftKey) || event.key === 'Tab') {
        if (skillPop.items.length) {
          event.preventDefault();
          acceptSkillPop();
          return;
        }
        closeSkillPop();
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closeSkillPop();
        return;
      }
    }
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
  input.addEventListener('blur', () => { if (mention.open) closeMention(); if (skillPop.open) closeSkillPop(); });

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
  // 暂停（交互增强一）：与顶部中断同一动作，只是离输入框更近。
  $('btn-pause').addEventListener('click', interrupt);
  $('btn-reset').addEventListener('click', reset);
  $('btn-rollback').addEventListener('click', () => openCheckpoints());
  $('btn-tree-refresh').addEventListener('click', refreshTree);

  // 切换工作区（W1）：标题栏的工作区名就是入口
  $('tb-workspace').addEventListener('click', openWorkspaceModal);
  $('btn-ws-close').addEventListener('click', closeWorkspaceModal);
  $('btn-ws-open').addEventListener('click', switchWorkspace);
  $('btn-ws-pick').addEventListener('click', pickWorkspaceFolder);
  $('btn-ws-up').addEventListener('click', browseUp);
  // 来源页签（M13）：切回本机时把本地那一页的数据补齐（首次打开可能是空的）。
  $('ws-tab-local').addEventListener('click', async () => {
    await setWsTab('local');
    await Promise.all([loadWorkspaceList(), browseDirs($('ws-path').value)]);
  });
  $('ws-tab-remote').addEventListener('click', () => setWsTab('remote'));
  $('btn-ws-remote-up').addEventListener('click', browseRemoteUp);
  $('ws-conn').addEventListener('change', () => browseRemoteDirs(''));
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
  // 联网检索段（M7）：总开关切换整段置灰/解禁；provider 切换时更新端点提示
  $('set-web-enabled').addEventListener('change', () => syncWebSection());
  $('set-web-provider').addEventListener('change', () => syncWebSection());
  // 浮层内回车保存（与工作区弹窗同一套交互）
  $('settings-modal').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && event.target.tagName !== 'BUTTON') {
      event.preventDefault();
      saveSettings();
    }
  });

  // 治理面登录（M3+）：标题栏入口（始终可见）+ 弹窗内按钮
  $('btn-gov').addEventListener('click', openGovModal);
  $('btn-gov-close').addEventListener('click', closeGovModal);
  $('btn-gov-login').addEventListener('click', governanceLogin);
  $('btn-gov-register').addEventListener('click', governanceRegister);
  $('btn-gov-logout').addEventListener('click', governanceLogout);
  $('btn-gov-save-server').addEventListener('click', saveGovernanceServer);

  // 启动登录门（强制登录）：tab 切换 / 登录 / 注册 / 记住地址 / 地址失焦探测
  $('gate-tab-login').addEventListener('click', () => gateTab('login'));
  $('gate-tab-register').addEventListener('click', () => gateTab('register'));
  $('btn-gate-login').addEventListener('click', gateLogin);
  $('btn-gate-register').addEventListener('click', gateRegister);
  $('btn-gate-save-server').addEventListener('click', saveGateServer);
  $('gate-server').addEventListener('change', probeGateRegistration);
  // 门内回车：按当前 tab 提交（与设置浮层同一套交互）
  $('login-gate').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && event.target.tagName !== 'BUTTON') {
      event.preventDefault();
      if ($('gate-register-form').hidden) gateLogin();
      else gateRegister();
    }
  });
  $('gov-modal').addEventListener('click', (event) => {
    if (event.target === $('gov-modal')) closeGovModal();
  });
  // 浮层内回车 = 密码登录（注册段有各自的按钮，不抢回车）
  $('gov-modal').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && event.target.tagName !== 'BUTTON') {
      event.preventDefault();
      governanceLogin();
    }
  });

  // 客户端更新（M9）：标题栏 chip 是提示也是入口（默认隐藏，见 applyUpdateChip）；
  // 弹窗里「稍后」只关窗、不改状态，「下载」带已知目标版本，「安装」才把 stagingPath 交给壳。
  $('chip-update').addEventListener('click', openUpdateModal);
  $('chip-update').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      openUpdateModal();
    }
  });
  $('btn-update-later').addEventListener('click', closeUpdateModal);
  $('btn-update-download').addEventListener('click', downloadUpdate);
  $('btn-update-apply').addEventListener('click', applyStagedUpdate);
  $('update-modal').addEventListener('click', (event) => {
    if (event.target === $('update-modal')) closeUpdateModal();
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

  // Git 写操作（B6/ADR-028）：全部是用户本人动作，不走审批浮层
  $('btn-git-commit').addEventListener('click', gitCommit);
  $('btn-git-pull').addEventListener('click', gitPull);
  $('btn-git-push').addEventListener('click', gitPush);
  $('btn-git-newbranch').addEventListener('click', gitNewBranch);
  $('btn-git-stage-all').addEventListener('click', () => gitStage([], true));
  $('btn-git-unstage-all').addEventListener('click', () => gitUnstage([], true));
  $('git-branch-select').addEventListener('change', (event) => gitSwitchBranch(event.target.value));

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
  // 后台任务（M9-D）：发起 / 刷新按钮（页签切换时也会刷新）
  $('btn-task-submit').addEventListener('click', submitTask);
  $('btn-task-refresh').addEventListener('click', refreshTasks);
  // 定时任务（M10）：新建 / 刷新按钮（页签切换时也会刷新）
  $('btn-job-submit').addEventListener('click', submitJob);
  $('btn-job-refresh').addEventListener('click', refreshSchedules);
  // 远程设备工作面（M11-E）：连接 / 目录树 / 页签 / 编辑保存 / 审查
  bindRemote();
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
    // 「输出」面板是本会话的日志，直接清 DOM；「终端」是交互终端，清的是**当前那个**
    // 终端的本地回滚缓冲（其他终端的缓冲不受影响）。
    if (active === 'terminal') {
      const t = activeTerm();
      if (!t) return;
      termReset(t);
      termRender(t);
    } else { $('output').replaceChildren(); }
  });

  // 交互式终端（P1-7 / 多标签）：新建 + 关闭当前 + 尺寸自适应
  // 每个终端的键盘 / 鼠标 / 粘贴监听在 attachTermEl 里绑定（各自闭包捕获自己的 t）。
  $('btn-term-open').addEventListener('click', () => termOpen());
  $('btn-term-close').addEventListener('click', () => {
    const t = activeTerm();
    if (t) termClose(t.id);
  });
  // 中间区三视图（浏览器 / 终端 / 编辑器）：页签点击 + 浏览器面板的能力探测
  initBrowser();
  const termStage = $('term-stage');
  if (typeof ResizeObserver === 'function') new ResizeObserver(() => termResizeActive()).observe(termStage);
  paintTermOpenBtn();   // 开局就把「新建」按钮的提示与可用性对齐（后端配了 VIBE_TERM_MAX 时）

  // 分栏拖拽（方向/目标随布局模式变化，见 bindResizer 注释）
  bindResizer('sb-resizer', () => isSolo()
    // SOLO：文件树在最右，把手在它左侧 —— 往右拖是「把树拖窄」
    ? { id: 'sidebar', min: 170, max: 480, dir: -1 }
    : { id: 'sidebar', min: 170, max: 480, dir: 1 });
  bindResizer('chat-resizer', () => isSolo()
    // SOLO：把手夹在「对话 | 编辑器」之间，调的是中间编辑器列的宽度
    ? { id: 'editor-stack', min: 320, max: 1200, dir: 1 }
    : { id: 'chatpane', min: 280, max: 640, dir: -1 });
  // 底部面板（终端 / 输出）上沿的把手：自由改高度
  bindRowResizer('bottom-resizer', () => ({ id: 'bottom-panel', min: 120, dir: -1 }));

  // 回滚浮层
  $('btn-cp-close').addEventListener('click', closeCheckpoints);
  $('btn-cp-confirm').addEventListener('click', confirmRestore);
  $('cp-modal').addEventListener('click', (event) => {
    if (event.target === $('cp-modal')) closeCheckpoints();
  });
  $('btn-rb-cancel').addEventListener('click', closeRollbackModal);
  $('btn-rb-confirm').addEventListener('click', confirmRollback);
  $('rb-modal').addEventListener('click', (event) => {
    if (event.target === $('rb-modal')) closeRollbackModal();
  });

  // 审批
  $('btn-approve').addEventListener('click', () => decide('approve'));
  $('btn-approve-all').addEventListener('click', () => decide('approve_all'));
  $('btn-deny').addEventListener('click', () => decide('deny'));
  $('btn-deny-all').addEventListener('click', () => decide('deny_all'));
  $('btn-minimize').addEventListener('click', minimizeApproval);
  $('btn-draft-preview').addEventListener('click', openDraftFromApproval);
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
  // Skill 提示 chip（M9-A）：点击打开命令面板（那里能枚举并运行工作流）
  $('chip-skill').addEventListener('click', openPalette);
  $('chip-skill').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openPalette(); }
  });
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
  // 远端根（M12）：与刷新页面同一口径 —— 根挂在 workspace 上，重启/刷新后要能
  // 重新出现在工作区树里。未启用远端时它直接置空、零请求。
  await loadWorkspaceRoots();
  await refreshChanges();
  // 刷新页面后把最近一次测试结果补回来（后端内存态），否则失败计数徽标会是空的
  await loadTestResults();
  connect();
  // 重启续接（D）：把后端已恢复的历史会话铺回对话区（放在 connect 之后，
  // 事件流已就位；此后再到的 session.ready 只是刷新快照，不会清掉历史）。
  await restoreHistoryView();
}

boot();
