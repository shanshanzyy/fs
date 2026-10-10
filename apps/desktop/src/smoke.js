/* ===========================================================================
 * Vibe Coding Agent · 打包冒烟（D1.2）
 * ---------------------------------------------------------------------------
 * 这不是产品代码，而是**打包产物的验收工具**：把真实的分发包（含内嵌后端
 * 二进制）当成用户那样启动一遍，跑通一轮 mock 对话，截一张图，然后退出。
 *
 *   它刻意是**独立入口**而不是 main.js 里的一段 if：
 *   壳的职责是「起后端 + 开窗口 + 管进程」，冒烟剧本（发什么任务、什么时候
 *   截图）属于测试关注点，混进去只会让壳越来越不像壳。
 *   （壳里只留一行 `VIBE_SMOKE=1 → require('./smoke').runAsApp()`，
 *   因为 Electron 只认 exe 旁的 resources/app，命令行换不了入口。）
 *
 * 通过环境变量驱动（由 main.js 在 VIBE_SMOKE=1 时转交进来）：
 *   VIBE_SMOKE=1           由壳识别并转交给本模块
 *   VIBE_SMOKE_WORKSPACE   工作区（默认仓库根）
 *   VIBE_SMOKE_TASK        发给 Agent 的任务文本
 *   VIBE_SMOKE_SCREENSHOT  截图输出路径
 *   VIBE_SMOKE_REPORT      结果 JSON 输出路径（驱动脚本据此判定成败）
 *   VIBE_MODEL / VIBE_BASE_URL / VIBE_API_KEY   mock 模型接入点
 *   VIBE_NO_EMBEDDED=1     强制跳过内嵌二进制（验证回退系统 Python 的路径）
 * =========================================================================== */

'use strict';

const { app, BrowserWindow } = require('electron');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

const { Backend, waitForHealth } = require('./backend');

const DESKTOP_DIR = path.resolve(__dirname, '..');
const REPO_ROOT = path.resolve(DESKTOP_DIR, '..', '..');
const AGENT_CORE = path.join(REPO_ROOT, 'packages', 'agent-core');

const WORKSPACE = process.env.VIBE_SMOKE_WORKSPACE || REPO_ROOT;
const TASK = process.env.VIBE_SMOKE_TASK || '看看项目结构，然后告诉我这个项目是做什么的。';
const SCREENSHOT = process.env.VIBE_SMOKE_SCREENSHOT || '';
const REPORT = process.env.VIBE_SMOKE_REPORT || '';
const TIMEOUT_MS = Number(process.env.VIBE_SMOKE_TIMEOUT_MS || 90_000);

/* ------------------------------------------------------------ HTTP 小工具 */
function requestJson(url, { method = 'GET', token = '', body = null, timeoutMs = 20_000 } = {}) {
  return new Promise((resolve, reject) => {
    const payload = body ? Buffer.from(JSON.stringify(body), 'utf8') : null;
    const headers = { 'X-Vibe-Token': token };
    if (payload) {
      headers['Content-Type'] = 'application/json';
      headers['Content-Length'] = payload.length;
    }
    const request = http.request(url, { method, headers, timeout: timeoutMs }, (response) => {
      let text = '';
      response.on('data', (chunk) => {
        text += chunk.toString('utf8');
      });
      response.on('end', () => {
        try {
          resolve({ status: response.statusCode, json: JSON.parse(text || '{}') });
        } catch {
          resolve({ status: response.statusCode, json: {} });
        }
      });
    });
    request.on('error', reject);
    request.on('timeout', () => request.destroy(new Error('请求超时')));
    if (payload) request.write(payload);
    request.end();
  });
}

/** 订阅事件流，把事件喂给 onEvent；返回可关闭的句柄。 */
function subscribe(url, token, onEvent) {
  const request = http.get(
    url,
    { headers: { Accept: 'text/event-stream', 'X-Vibe-Token': token } },
    (response) => {
      let buffer = '';
      let eventType = '';
      response.on('data', (chunk) => {
        buffer += chunk.toString('utf8');
        let index;
        while ((index = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, index).replace(/\r$/, '');
          buffer = buffer.slice(index + 1);
          if (line.startsWith('event: ')) {
            eventType = line.slice(7);
          } else if (line.startsWith('data: ')) {
            try {
              onEvent(eventType, JSON.parse(line.slice(6)));
            } catch {
              /* 非 JSON 的分片（心跳等）忽略 */
            }
          }
        }
      });
      response.on('error', () => {});
    }
  );
  request.on('error', () => {});
  return request;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* ---------------------------------------------------------------- 主流程 */
async function run() {
  const config = {
    pythonPath: process.env.VIBE_PYTHON || '',
    backendBin: process.env.VIBE_BACKEND_BIN || '',
    model: process.env.VIBE_MODEL || 'mock-agent-model',
    baseUrl: process.env.VIBE_BASE_URL || '',
    apiKey: process.env.VIBE_API_KEY || 'mock',
  };

  const report = {
    ok: false,
    stage: 'init',
    backendKind: '',
    backendCommand: '',
    pythonProbed: false,
    healthVersion: '',
    task: TASK,
    workspace: WORKSPACE,
    events: [],
    answered: '',
    screenshot: '',
    error: '',
  };

  const backend = new Backend(config, WORKSPACE, {
    agentCoreDir: AGENT_CORE,
    desktopDir: DESKTOP_DIR,
    resourcesPath: process.resourcesPath,
    configPath: '(smoke: 仅用环境变量)',
  });

  let window = null;
  let events = null;
  let approved = false;

  try {
    report.stage = 'boot';
    const url = await backend.start();
    report.backendKind = backend.kind;
    report.backendCommand = backend.command;
    report.pythonProbed = backend.pythonProbed;

    const health = await waitForHealth(new URL(url).origin, 20_000);
    report.healthVersion = health.version || '';

    report.stage = 'window';
    window = new BrowserWindow({
      width: 1420,
      height: 900,
      backgroundColor: '#0d1117',
      title: 'Vibe Coding Agent · 打包冒烟',
      webPreferences: {
        nodeIntegration: false,
        contextIsolation: true,
        sandbox: true,
        preload: path.join(__dirname, 'preload.js'),
      },
    });
    await window.loadURL(url);
    await sleep(1200); // 等界面把 SSE 接上、首屏画完

    const token = new URL(url).searchParams.get('token') || '';
    const origin = new URL(url).origin;
    const finished = new Promise((resolve) => {
      events = subscribe(`${origin}/api/events?token=${token}`, token, async (type, payload) => {
        report.events.push(type || payload?.type || '');
        if (type === 'run.approval' && !approved && payload?.approvalId) {
          approved = true;
          await requestJson(`${origin}/api/approval`, {
            method: 'POST',
            token,
            body: { approvalId: payload.approvalId, decision: 'approve' },
          }).catch(() => {});
        }
        if (['run.finished', 'run.failed', 'run.interrupted'].includes(type)) {
          resolve(payload || {});
        }
      });
    });

    report.stage = 'run';
    await requestJson(`${origin}/api/run`, { method: 'POST', token, body: { text: TASK } });

    const outcome = await Promise.race([finished, sleep(TIMEOUT_MS).then(() => null)]);
    if (!outcome) throw new Error(`等待 run.finished 超时（${TIMEOUT_MS} ms）`);
    report.answered = String(outcome.answer || '').slice(0, 200);
    if (outcome.type && outcome.type !== 'run.finished') {
      throw new Error(`运行未成功结束：${outcome.type}`);
    }

    report.stage = 'screenshot';
    await sleep(800); // 让最后一帧渲染出来再拍
    if (SCREENSHOT) {
      const image = await window.webContents.capturePage();
      fs.mkdirSync(path.dirname(SCREENSHOT), { recursive: true });
      fs.writeFileSync(SCREENSHOT, image.toPNG());
      report.screenshot = SCREENSHOT;
    }

    report.ok = true;
    report.stage = 'done';
    return 0;
  } catch (error) {
    report.error = String(error?.message || error);
    return 1;
  } finally {
    try {
      events?.destroy();
    } catch {
      /* 已断开 */
    }
    try {
      window?.destroy();
    } catch {
      /* 已销毁 */
    }
    backend.stop();
    if (REPORT) {
      try {
        fs.mkdirSync(path.dirname(REPORT), { recursive: true });
        fs.writeFileSync(REPORT, JSON.stringify(report, null, 2), 'utf8');
      } catch {
        /* 报告写不出去时只能靠 stdout */
      }
    }
    // 冒烟必须自己退出：留着的窗口会让 CI 挂住
    setTimeout(() => app.exit(report.ok ? 0 : 1), 300);
  }
}

/** 由 main.js 在 VIBE_SMOKE=1 时调用。 */
function runAsApp() {
  app.whenReady().then(async () => {
    const code = await run();
    console.log(`VIBE_SMOKE_RESULT=${code === 0 ? 'ok' : 'fail'}`);
  });
}

module.exports = { runAsApp };