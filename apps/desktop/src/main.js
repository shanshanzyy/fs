/* ===========================================================================
 * Vibe Coding Agent · Electron 主进程
 * ---------------------------------------------------------------------------
 * 这是一个**薄壳**，只做三件事：
 *   1. 找到 Python，起 agent-core 的 host 服务；
 *   2. 等它报出访问地址，把窗口指过去；
 *   3. 管好这个子进程的生死（退出、崩溃、超时）。
 *
 * 它刻意不碰任何业务逻辑 —— 不在 Node 里读文件、不拼 Prompt、不管审批。
 * 原因是架构上的一条硬约束（docs/ARCHITECTURE.md 第 3.1 节）：
 * 壳必须可替换。M1 用 Electron，M2 想换 Tauri 或直接跑浏览器，
 * 都不应该需要动 agent-core 一行代码。
 *
 * 因此这里的判断标准很简单：**任何一行「Agent 应该怎么工作」的逻辑，
 * 都不属于这个文件。**
 * =========================================================================== */

'use strict';

const { app, BrowserWindow, dialog, shell, Menu, ipcMain, clipboard } = require('electron');
const path = require('node:path');
const fs = require('node:fs');

const { Backend, waitForHealth } = require('./backend');
const {
  applyStaging,
  isStagingPathValid,
  readPending,
  clearPending,
  stagingVersion,
} = require('./update');

/* ------------------------------------------------- 壳专有能力的窄桥（W1）
 * 只有一个通道：`vibe:pick-folder` —— 弹系统目录对话框，**只回传一个路径字符串**
 * （用户取消回传 null）。界面据此在「切换工作区」弹窗里显示「从磁盘选择…」按钮；
 * 浏览器里没有这个桥，按钮自动隐藏，改用「最近打开 + 手输路径」。
 *
 * 为什么值得破一次「壳不碰业务、不开 IPC」的口子：这里没有任何业务判断，
 * 只是把「用户想打开哪个目录」这件事从渲染进程渡到主进程 —— 而渲染进程面对的
 * Agent 输出是可能被注入的内容，**绝不能**给它 fs / child_process / 裸 ipcRenderer。
 * 所以桥只回传一个字符串，收不到任何可执行的回调。
 */
ipcMain.handle('vibe:pick-folder', async () => {
  const options = { properties: ['openDirectory'], title: '选择工作区' };
  const result = mainWindow
    ? await dialog.showOpenDialog(mainWindow, options)
    : await dialog.showOpenDialog(options);
  if (result.canceled || !result.filePaths.length) return null;
  return result.filePaths[0];
});

/* ------------------------------------------------- 网页自检（O/P1-11）窄桥
 * 第二个跨进程能力：`vibe:capture-page` —— 起一个**离屏** BrowserWindow 加载
 * 指定的本机地址，等页面画完，`webContents.capturePage()` 截一张 PNG。
 *
 * 与 pick-folder 同一纪律：**只回传图片字节**（PNG base64），不暴露 fs /
 * child_process / 任何可执行回调。接口收的是「URL + 尺寸」而不是任意命令；
 * URL 只接受指向本机 host 服务的地址（界面侧拼接 /preview/…，见 app.js），
 * 所以这里再焊一层：只放行本机 127.0.0.1 的 http 地址，其余一律拒绝。
 *
 * 离屏窗口刻意不进任务栏、不抢焦点：它只是「截个图给比对用」。
 */
const CAPTURE_URL_RE = /^http:\/\/127\.0\.0\.1:\d+\//;
ipcMain.handle('vibe:capture-page', async (_event, payload) => {
  const target = String((payload && payload.url) || '');
  if (!CAPTURE_URL_RE.test(target)) {
    return { ok: false, error: '截图只接受本机地址' };
  }
  const width = Math.max(64, Math.min(Number(payload && payload.width) || 1280, 4000));
  const height = Math.max(64, Math.min(Number(payload && payload.height) || 800, 4000));
  let win = null;
  try {
    win = new BrowserWindow({
      width,
      height,
      show: false,
      frame: false,
      webPreferences: { sandbox: true, nodeIntegration: false, contextIsolation: true },
    });
    await win.loadURL(target);
    const image = await win.webContents.capturePage();
    const png = image.toPNG();
    return { ok: true, image: png.toString('base64') };
  } catch (error) {
    return { ok: false, error: String(error && error.message || error) };
  } finally {
    if (win) { win.destroy(); win = null; }
  }
});

/* ------------------------------------------------- 桌面壳配置（解释器 / 治理面地址）
 * 模型 / 接入地址 / API Key 走 host 的 POST /api/settings（后端热替换 LLM 客户端），
 * 但「用哪个 Python、用哪个后端二进制」与「治理面地址」是**壳专有**的：前者决定壳
 * 怎么起子进程，后者在启动时作为 VIBE_SERVER_URL 注入后端 —— 后端自己都不知道用户
 * 想连哪个治理面。于是这里开两条窄桥：
 *
 *   vibe:save-config —— 只接受 {pythonPath, backendBin, serverUrl, gatewayUrl}
 *                       （其余键一律丢弃，界面无法通过它改 model / apiKey 等敏感项）；
 *   vibe:read-config —— 只回传这几个非敏感字段，**绝不回传任何密钥**。
 *
 * 治理面地址不是秘密（就是运维填的地址），但**只经此桥写盘**：界面没有 fs，改不动
 * 别的键。约定与 pick-folder / capture-page 一致：参数是白名单字段而非任意对象。
 */
ipcMain.handle('vibe:save-config', async (_event, patch) => {
  const source = patch && typeof patch === 'object' ? patch : {};
  const safe = {};
  if (typeof source.pythonPath === 'string') safe.pythonPath = source.pythonPath.trim();
  if (typeof source.backendBin === 'string') safe.backendBin = source.backendBin.trim();
  if (typeof source.serverUrl === 'string') safe.serverUrl = source.serverUrl.trim();
  if (typeof source.gatewayUrl === 'string') safe.gatewayUrl = source.gatewayUrl.trim();
  try {
    writeConfig(safe);
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error && error.message || error) };
  }
});

ipcMain.handle('vibe:read-config', async () => {
  const config = readConfig();
  return {
    pythonPath: config.pythonPath || '',
    backendBin: config.backendBin || '',
    serverUrl: config.serverUrl || '',
  };
});

/* ------------------------------------------------- 终端剪贴板窄桥（P1-7 补强）
 * 终端里的复制 / 粘贴是高频动作，不能时灵时不灵：渲染进程直接走
 * `navigator.clipboard` 时，读取要过 Chromium 的权限与 focus 闸门，行为随版本漂移。
 * 因此把这两下交给壳（Electron 的 clipboard 模块），与 pick-folder 同一纪律：
 * 白名单方法、**只渡纯文本**、长度封顶，不暴露任何文件 / 进程能力。
 */
const CLIPBOARD_MAX = 1024 * 1024;

ipcMain.handle('vibe:clipboard-read', async () => {
  try {
    return String(clipboard.readText() || '');
  } catch {
    return '';
  }
});

ipcMain.handle('vibe:clipboard-write', async (_event, text) => {
  if (typeof text !== 'string' || !text) {
    return { ok: false, reason: '拒绝：只接受非空字符串' };
  }
  if (text.length > CLIPBOARD_MAX) {
    return { ok: false, reason: '拒绝：内容过长' };
  }
  try {
    clipboard.writeText(text);
    return { ok: true };
  } catch (error) {
    return { ok: false, reason: String((error && error.message) || error) };
  }
});

/* 解释器变更必须重启才生效（后端进程要按新路径重起）。先把子进程收干净，
 * 再 app.relaunch() 让 Electron 用同一组参数把自己重新拉起。 */
ipcMain.handle('vibe:relaunch', async () => {
  try { backend?.stop(); } catch { /* 停机失败也不能卡住重启 */ }
  app.relaunch({ args: process.argv.slice(1) });
  app.exit(0);
  return { ok: true };
});

/* ------------------------------------------------- 客户端更新（M9）窄桥
 * 第四条跨进程能力：`vibe:update-apply`。它**只接一个 staging 路径字符串** ——
 * 由内核 check/download 校验 sha256 + RSA 验签后原子落盘的那个安装包路径。
 * 壳在这里刻意不做任何业务判断：不读 manifest、不算 sha256、不下载
 * （ADR-030 的「壳不碰更新业务」纪律），只做「形态校验 + spawn + 退出自己」。
 *
 * 安装包内容是否可信由内核负责；壳只保证「绝对路径 + 文件名是 Setup-<版本>.exe +
 * 本机确实存在这个普通文件」。非法一律拒绝，**绝不 spawn**。
 */
ipcMain.handle('vibe:update-apply', async (_event, stagingPath) => {
  // 只认字符串：对象 / null / 数字一律当非法（窄面纪律与 save-config 一致）
  const target = typeof stagingPath === 'string' ? stagingPath : '';
  if (!isStagingPathValid(target)) {
    return { ok: false, reason: '拒绝：staging 路径形态非法' };
  }
  return applyStaging({
    stagingPath: target,
    pendingFile: PENDING_PATH,
    fromVersion: backend?.version || '',
    app, // 装完立刻退出自己，把 exe 让给安装器
    backend, // 先停后端子进程树（Windows 同步 taskkill，放掉文件句柄）
  });
});

/** 向就绪的内核发一次只读 GET（token 从 VIBE_URL 里取，与渲染进程同一口径）。 */
async function kernelGet(pathname) {
  if (!backend || !backend.url) throw new Error('内核尚未就绪');
  const base = new URL(backend.url);
  const target = new URL(pathname, base.origin);
  const token = base.searchParams.get('token') || '';
  if (token) target.searchParams.set('token', token);
  const res = await fetch(target, { headers: token ? { 'X-Vibe-Token': token } : {} });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

/** 启动时的「未决安装」核对：上次 spawn 安装器后壳就退出了，这里读 marker 判断装成没成。 */
async function notifyPendingUpdate() {
  const pending = readPending(PENDING_PATH);
  if (!pending) return;
  const current = String(backend?.version || '');
  const from = String(pending.fromVersion || '');
  const want = String(pending.toVersion || '');
  // 有 fromVersion 就按「版本变了没」判定；没有就退化为「读到了 marker 就算装过」。
  const changed = from ? Boolean(current && current !== from) : Boolean(want);
  clearPending(PENDING_PATH);
  await dialog.showMessageBox(mainWindow, {
    type: changed ? 'info' : 'warning',
    title: '更新结果',
    message: changed ? `已更新到 v${current}` : '更新没有生效',
    detail: changed
      ? `上次尝试安装 v${want || from}，当前版本 ${current}。`
      : `上次尝试安装 v${want || '新版本'}，但当前仍是 v${current || '未知'}。可以重新在界面里触发更新。`,
  });
}

/** 检查更新（M9）：内核编排双通道与验签，壳只负责发请求并在手动检查时呈现结论。 */
async function checkForUpdates({ auto = true } = {}) {
  try {
    const view = await kernelGet(`/api/update/check?auto=${auto ? 1 : 0}`);
    if (auto) return view; // 静默检查：内核自己通过事件流提示，不弹框
    let detail = `状态：${view.status || '未知'}`;
    if (view.status === 'available') {
      detail = `发现新版本 v${view.version}（当前 v${view.currentVersion}）。` +
        (view.mandatory ? '该版本为强制更新。' : '可在界面顶部按提示下载。');
    } else if (view.status === 'idle') {
      detail = `已是最新版本（当前 v${view.currentVersion || backend?.version || '未知'}）。`;
    } else if (view.status === 'disabled') {
      detail = '更新通道已关闭（VIBE_UPDATE_DISABLE=1）。';
    } else if (view.status === 'failed') {
      detail = `检查失败：${view.reason || '未知原因'}`;
    }
    await dialog.showMessageBox(mainWindow, {
      type: view.status === 'failed' ? 'error' : 'info',
      title: '检查更新',
      message: '检查完成',
      detail,
    });
    return view;
  } catch (error) {
    if (!auto) {
      await dialog.showMessageBox(mainWindow, {
        type: 'error',
        title: '检查更新',
        message: '检查失败',
        detail: String((error && error.message) || error),
      });
    }
    throw error;
  }
}

/** 「帮助 → 关于」：只展示本机状态，不做任何业务动作。 */
async function showAbout() {
  const detail =
    `工作区：${pickWorkspace()}\n` +
    `内核版本：${backend?.version || '（未知）'}\n` +
    `后端来源：${backendKindLabel(backend)}\n` +
    `后端：${backend?.url || '（未启动）'}\n` +
    `配置文件：${CONFIG_PATH}\n\n` +
    '本地执行 + 人工审批：所有高危动作都会先征求你的同意，代码不会离开这台机器。';
  await dialog.showMessageBox(mainWindow, {
    type: 'info',
    title: '关于',
    message: 'Vibe Coding Agent · M1 桌面客户端',
    detail,
  });
}

/* ----------------------------------------------------------------- 配置 */
const DESKTOP_DIR = path.resolve(__dirname, '..');
const REPO_ROOT = path.resolve(DESKTOP_DIR, '..', '..');
const AGENT_CORE = path.join(REPO_ROOT, 'packages', 'agent-core');
const CONFIG_PATH = path.join(app.getPath('userData'), 'config.json');

/* 「未决安装」的记账文件（M9）。刻意放在壳自己的 userData 下，而**不是**内核的
 * ~/.vibe/updates：它是「壳发起的这次安装」的记账，属于壳的职责；安装这件事
 * 内核既不知道也不该知道（内核永不执行安装）。 */
const PENDING_PATH = path.join(app.getPath('userData'), 'update', 'pending.json');

const DEFAULT_CONFIG = {
  // 留空表示自动探测
  pythonPath: '',
  // 留空表示自动使用内嵌后端二进制（找不到才回退 Python）
  backendBin: '',
  // 留空表示用「打开时选择 / 上次使用」的目录
  workspace: '',
  model: '',
  baseUrl: '',
  apiKey: '',
  // 治理面（M3）。留空 = 纯本地，M0/M1 行为一行不变。
  // 填了它但没登录时，后端会以「已配置但未登录」的降级状态启动；
  // 界面顶部的「治理面」入口可在应用内直接登录 / 邀请码注册（登录后热接入，
  // 不必重启）；这里保存的地址会在下次启动时作为 VIBE_SERVER_URL 注入后端。
  serverUrl: '',
  // 网关基址。留空 = 由 serverUrl 派生，只在网关单独部署时才需要填。
  gatewayUrl: '',
  // 客户端更新（M9）。两项都**非敏感**，仅作为环境变量透传给后端（内核自己读）：
  //   updateChannel —— 走 stable/beta/canary 哪个通道，留空用内核默认 stable；
  //   updateUrl     —— 本地/局域网镜像基址（file:// 或内网 http），留空只走治理面。
  // 刻意**不在这里放任何公钥/密钥**：验签公钥是内核内置常量，密钥永不经过壳。
  updateChannel: '',
  updateUrl: '',
};

function readConfig() {
  let stored = {};
  try {
    stored = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
  } catch {
    stored = {};
  }
  const config = { ...DEFAULT_CONFIG, ...stored };
  // 环境变量覆盖：冒烟脚本与 CI 靠它注入 mock 模型地址，不必改用户配置文件
  if (process.env.VIBE_MODEL) config.model = process.env.VIBE_MODEL;
  if (process.env.VIBE_BASE_URL) config.baseUrl = process.env.VIBE_BASE_URL;
  if (process.env.VIBE_API_KEY) config.apiKey = process.env.VIBE_API_KEY;
  if (process.env.VIBE_BACKEND_BIN) config.backendBin = process.env.VIBE_BACKEND_BIN;
  // 治理面地址同样允许环境变量注入：运维在机器级环境变量里配一次，
  // 所有用户的桌面端就都接上了治理面，不必逐个改 config.json。
  if (process.env.VIBE_SERVER_URL) config.serverUrl = process.env.VIBE_SERVER_URL;
  if (process.env.VIBE_GATEWAY_URL) config.gatewayUrl = process.env.VIBE_GATEWAY_URL;
  // 更新通道 / 镜像基址同理：运维在机器级环境变量里配一次即可（与 serverUrl 同款）。
  if (process.env.VIBE_UPDATE_CHANNEL) config.updateChannel = process.env.VIBE_UPDATE_CHANNEL;
  if (process.env.VIBE_UPDATE_URL) config.updateUrl = process.env.VIBE_UPDATE_URL;
  return config;
}

function writeConfig(patch) {
  const merged = { ...readConfig(), ...patch };
  fs.mkdirSync(path.dirname(CONFIG_PATH), { recursive: true });
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(merged, null, 2), 'utf8');
  return merged;
}

/* ----------------------------------------------------------------- 工作区 */
let mainWindow = null;

function createWindow(url, workspace) {
  mainWindow = new BrowserWindow({
    width: 1420,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: '#0d1117',
    title: `Vibe Coding Agent — ${path.basename(workspace)}`,
    show: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      // 界面全部由本地 host 服务提供，不需要任何 Node 能力
      preload: path.join(__dirname, 'preload.js'),
      // 中间区「浏览器」要显示用户自己跑起来的本机应用。不少页面带
      // X-Frame-Options / CSP frame-ancestors，用 iframe 会被拒成白屏；
      // webview 是独立 WebContents 的顶层导航，不受这两条限制。
      webviewTag: true,
    },
  });

  // webview 纵深防御：只允许挂本机环回地址，且绝不给它任何 Node 能力。
  mainWindow.webContents.on('will-attach-webview', (event, prefs, params) => {
    delete prefs.preload;
    prefs.nodeIntegration = false;
    prefs.contextIsolation = true;
    prefs.sandbox = true;
    if (!/^http:\/\/127\.0\.0\.1:\d{1,5}(\/|$)/.test(String(params.src || ''))) {
      event.preventDefault();
    }
  });

  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.loadURL(url);

  // 界面里出现的任何外部链接一律交给系统浏览器，绝不在应用内打开
  mainWindow.webContents.setWindowOpenHandler(({ url: target }) => {
    if (/^https?:/.test(target) && !target.includes('127.0.0.1')) {
      shell.openExternal(target);
    }
    return { action: 'deny' };
  });

  // 禁止窗口被导航离开本地服务（例如被注入的链接带跑）
  mainWindow.webContents.on('will-navigate', (event, target) => {
    if (!target.startsWith('http://127.0.0.1:')) {
      event.preventDefault();
      shell.openExternal(target);
    }
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

/* ----------------------------------------------------------------- 工作区 */
function pickWorkspace(explicit) {
  if (explicit && fs.existsSync(explicit)) return explicit;
  const config = readConfig();
  if (config.workspace && fs.existsSync(config.workspace)) return config.workspace;
  return REPO_ROOT;
}

/* ----------------------------------------------------------------- 启动 */
let backend = null;

/** 把后端来源翻成人话，便于排查「为什么用了系统 Python」。 */
function backendKindLabel(instance) {
  const labels = {
    embedded: '内嵌二进制（无需 Python）',
    python: '系统 Python',
    explicit: '指定路径',
  };
  if (!instance?.kind) return '（未启动）';
  return `${labels[instance.kind] || instance.kind}：${instance.command}`;
}

async function boot() {
  const config = readConfig();

  // 支持 `npm start -- /path/to/project` 直接指定工作区
  const cliWorkspace = process.argv
    .slice(1)
    .find((arg) => !arg.startsWith('-') && fs.existsSync(arg));
  const workspace = path.resolve(pickWorkspace(cliWorkspace));

  writeConfig({ workspace });

  backend = new Backend(config, workspace, {
    agentCoreDir: AGENT_CORE,
    desktopDir: DESKTOP_DIR,
    resourcesPath: process.resourcesPath,
    configPath: CONFIG_PATH,
  });
  backend.onUnexpectedExit = (code, stderr) => {
    if (!mainWindow) return;
    dialog.showMessageBox(mainWindow, {
      type: 'error',
      title: '后端进程意外退出',
      message: `agent-core 服务已停止（code=${code}）。`,
      detail: stderr.trim().slice(0, 1500) || '没有捕获到错误输出。',
      buttons: ['退出'],
    }).then(() => app.quit());
  };

  let url;
  try {
    url = await backend.start();
    const health = await waitForHealth(new URL(url).origin, 15_000);
    // 版本号打通（D1.2）：界面显示的版本一律来自后端 /api/health，
    // 而不是打包时写死的常量 —— 免得「壳说 0.1.0、内核其实是别的版本」。
    backend.version = health.version || '';
  } catch (error) {
    dialog.showErrorBox('启动失败', String(error.message || error));
    app.quit();
    return;
  }

  createWindow(url, workspace);

  // 上次退出前留下的「未决安装」（M9 §4 ④ 第 7 步）：读 marker 判断装成了没。
  // 放 createWindow 之后，弹框才能挂在窗口上；它不挡启动。
  notifyPendingUpdate().catch(() => {});

  // 启动静默检查更新（M9）：**非阻断** —— 不 await、失败只记日志、绝不弹框。
  checkForUpdates({ auto: true }).catch(() => {});
}

function buildMenu() {
  const template = [
    {
      label: '文件',
      submenu: [
        {
          label: '切换工作区…',
          accelerator: 'CmdOrCtrl+O',
          click: async () => {
            const result = await dialog.showOpenDialog(mainWindow, {
              properties: ['openDirectory'],
              title: '选择工作区',
            });
            if (result.canceled || !result.filePaths.length) return;
            const workspace = result.filePaths[0];
            writeConfig({ workspace });
            // 与浏览器共用同一条路径（W1）：调后端的工作区接口优雅重启上下文，
            // 而不是重启整个应用。这样「最近工作区」、审计链切换的行为两边完全一致。
            if (await switchWorkspaceViaHost(workspace)) {
              if (mainWindow) {
                mainWindow.setTitle(`Vibe Coding Agent — ${path.basename(workspace)}`);
              }
              return;
            }
            restartWith(workspace); // 后端不可达时才回退到老做法（重启应用）
          },
        },
        { type: 'separator' },
        {
          label: '打开配置文件',
          click: () => {
            if (!fs.existsSync(CONFIG_PATH)) writeConfig({});
            shell.openPath(CONFIG_PATH);
          },
        },
        { type: 'separator' },
        { role: 'quit', label: '退出' },
      ],
    },
    {
      label: '视图',
      submenu: [
        { role: 'reload', label: '重新加载界面' },
        { role: 'toggleDevTools', label: '开发者工具' },
        { type: 'separator' },
        { role: 'resetZoom', label: '实际大小' },
        { role: 'zoomIn', label: '放大' },
        { role: 'zoomOut', label: '缩小' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: '全屏' },
      ],
    },
    {
      label: '帮助',
      submenu: [
        {
          // 手动检查更新（M9）：转发内核 check?auto=0，把结论在本壳里呈现给用户。
          label: '检查更新',
          click: () => { checkForUpdates({ auto: false }).catch(() => {}); },
        },
        { type: 'separator' },
        {
          label: '关于',
          click: () => { showAbout().catch(() => {}); },
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function restartWith(workspace) {
  backend?.stop();
  app.relaunch({ args: process.argv.slice(1).concat([workspace]) });
  app.exit(0);
}

/**
 * 调后端的工作区接口切换（W1）。返回是否成功。
 *
 * 只做「把用户选的目录告诉后端」这一件事 —— 重启上下文、换审计链、
 * 刷新界面都在后端与页面里完成，壳不重复实现一遍（ADR-002 的薄壳原则）。
 */
async function switchWorkspaceViaHost(workspace) {
  if (!backend?.url) return false;
  const target = new URL(backend.url); // http://127.0.0.1:PORT/?token=…
  const token = target.searchParams.get('token') || '';
  try {
    const response = await fetch(`${target.origin}/api/workspaces/open`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Vibe-Token': token },
      body: JSON.stringify({ path: workspace }),
    });
    return response.ok;
  } catch {
    return false; // 后端已经死了等情况：交给调用方回退到重启应用
  }
}

/* ----------------------------------------------------------------- 生命周期 */
function startDesktopApp() {
  // 单实例：避免用户双击图标开出第二个后端、占两个端口
  if (!app.requestSingleInstanceLock()) {
    app.quit();
    return;
  }

  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(() => {
    buildMenu();
    boot();
  });

  app.on('window-all-closed', () => {
    backend?.stop();
    app.quit();
  });

  app.on('before-quit', () => backend?.stop());
  app.on('activate', () => {
    if (!mainWindow && backend?.url) createWindow(backend.url, pickWorkspace());
  });
}

// 打包冒烟（D1.2）的入口切换。这一处环境变量分支是唯一留在壳里的「非产品」逻辑，
// 原因是结构性的：只要 exe 旁边存在 resources/app，Electron 就只认它，
// 命令行再传别的 .js 路径也不会被当成入口 —— 想换入口只能在壳里分支。
if (process.env.VIBE_SMOKE === '1') {
  require('./smoke').runAsApp();
} else {
  startDesktopApp();
}
