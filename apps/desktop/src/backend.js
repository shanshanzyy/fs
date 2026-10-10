/* ===========================================================================
 * Vibe Coding Agent · 后端进程启动器（D1）
 * ---------------------------------------------------------------------------
 * 从 main.js 抽出来的原因只有一个：主进程（main.js）与打包冒烟（smoke.js）
 * 需要**同一套**「怎么把后端拉起来」的逻辑。复制两份必然漂移，
 * 而漂移的后果是「打包产物能跑、开发模式跑不起来」这类最难查的问题。
 *
 * 后端来源的优先级（D1.1）：
 *   1. 显式指定：config.backendBin / 环境变量 VIBE_BACKEND_BIN；
 *   2. **内嵌二进制**：PyInstaller onefile 产物（打包后 resources/backend，
 *      开发期 apps/desktop/resources/backend）—— 目标是「机器上没装 Python 也能用」；
 *   3. 回退系统 Python：沿用 M1 的探测逻辑，兼容开发与「用户自己想用 Python 跑」的场景。
 *
 * 设置 VIBE_NO_EMBEDDED=1 可跳过第 2 步，用于验证回退路径。
 * =========================================================================== */

'use strict';

const { spawn, spawnSync } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const http = require('node:http');

const STARTUP_TIMEOUT_MS = 30_000;

/* ------------------------------------------------------------- 内嵌二进制 */
/** 内嵌后端的固定文件名（Windows 带 .exe）。 */
function embeddedBinaryName(platform = process.platform) {
  return platform === 'win32' ? 'vibe-agent-core.exe' : 'vibe-agent-core';
}

/**
 * 内嵌后端的候选路径，按优先级排列。
 * 打包后 process.resourcesPath 指向 <app>/resources；
 * 开发期则看仓库里的 apps/desktop/resources/backend。
 */
function embeddedCandidates({ resourcesPath, desktopDir, platform = process.platform } = {}) {
  const name = embeddedBinaryName(platform);
  const candidates = [];
  if (resourcesPath) candidates.push(path.join(resourcesPath, 'backend', name));
  if (desktopDir) candidates.push(path.join(desktopDir, 'resources', 'backend', name));
  return candidates;
}

function findEmbeddedBinary(options = {}) {
  if (process.env.VIBE_NO_EMBEDDED) return null;
  for (const candidate of embeddedCandidates(options)) {
    try {
      if (fs.existsSync(candidate)) return candidate;
    } catch {
      /* 目录不可读就继续找下一个 */
    }
  }
  return null;
}

/* ----------------------------------------------------------------- 工具 */
/**
 * 逐个尝试候选解释器，返回第一个能跑起来的 Python 3。
 *
 * 刻意用异步 spawn 而不是 spawnSync：同步版本会把主进程整整卡住
 * （每个候选最多好几秒），用户看到的就是「双击图标后长时间没反应」。
 * 界面还没出来就先卡住，是最容易劝退人的一种体验。
 */
function pickPython(config) {
  const candidates = [];
  if (config.pythonPath) candidates.push(config.pythonPath);
  if (process.env.VIBE_PYTHON) candidates.push(process.env.VIBE_PYTHON);
  candidates.push(
    ...(process.platform === 'win32'
      ? ['python.exe', 'python', 'py']
      : ['python3', 'python'])
  );

  const unique = [...new Set(candidates)];
  const probe = (command) =>
    new Promise((resolve) => {
      let stdout = '';
      let child;
      try {
        child = spawn(command, ['-c', 'import sys; print(sys.version_info[0])'], {
          windowsHide: true,
          stdio: ['ignore', 'pipe', 'ignore'],
        });
      } catch {
        resolve(null);
        return;
      }
      const timer = setTimeout(() => {
        child.kill();
        resolve(null);
      }, 6000);
      child.stdout.on('data', (chunk) => {
        stdout += chunk.toString('utf8');
      });
      child.on('error', () => {
        clearTimeout(timer);
        resolve(null);
      });
      child.on('exit', (code) => {
        clearTimeout(timer);
        resolve(code === 0 && stdout.trim() === '3' ? command : null);
      });
    });

  return (async () => {
    for (const candidate of unique) {
      const found = await probe(candidate);
      if (found) return found;
    }
    return null;
  })();
}

/** 等一个 HTTP 接口就绪，并把 /api/health 的 JSON 解析出来（含版本号）。 */
function waitForHealth(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const request = http.get(`${url}/api/health`, { timeout: 1500 }, (response) => {
        let body = '';
        response.on('data', (chunk) => {
          body += chunk.toString('utf8');
        });
        response.on('end', () => {
          if (response.statusCode === 200) {
            let info = {};
            try {
              info = JSON.parse(body);
            } catch {
              info = {};
            }
            resolve(info);
          } else if (Date.now() < deadline) {
            setTimeout(attempt, 200);
          } else {
            reject(new Error(`健康检查返回 ${response.statusCode}`));
          }
        });
      });
      request.on('error', () => {
        if (Date.now() < deadline) {
          setTimeout(attempt, 200);
        } else {
          reject(new Error('后端服务未能在限定时间内就绪'));
        }
      });
      request.on('timeout', () => request.destroy());
    };
    attempt();
  });
}

/* ------------------------------------------------------------- 后端进程 */
class Backend {
  /**
   * @param {object} config  桌面端配置（pythonPath / model / baseUrl / apiKey /
   *                          backendBin / serverUrl / gatewayUrl）
   * @param {string} workspace 工作区绝对路径
   * @param {object} options  { agentCoreDir, resourcesPath, desktopDir }
   */
  constructor(config, workspace, options = {}) {
    this.config = config;
    this.workspace = workspace;
    this.options = options;
    this.child = null;
    this.url = '';
    this.exited = false;
    this.kind = '';
    this.command = '';
    this.version = '';
    this.pythonProbed = false;
  }

  /** agent-core 源码目录 —— 只在开发态存在（打包后 resources/app 旁边没有它）。 */
  _coreDir() {
    const dir = this.options.agentCoreDir;
    try {
      return dir && fs.existsSync(dir) ? dir : null;
    } catch {
      return null;
    }
  }

  /** 解析出本次要用的后端来源，返回 { command, args, env, kind, cwd } 或 null。 */
  async resolve() {
    const coreDir = this._coreDir();
    const explicit = this.config.backendBin || process.env.VIBE_BACKEND_BIN;
    if (explicit) {
      return {
        command: explicit,
        args: this._hostArgs(),
        env: this._baseEnv(),
        kind: 'explicit',
        // 传一个不存在的目录当 cwd，spawn 会报 ENOENT（看起来像「找不到程序」），
        // 所以拿不到有效目录时就交给父进程的 cwd。
        cwd: coreDir || undefined,
      };
    }

    const embedded = findEmbeddedBinary(this.options);
    if (embedded) {
      return {
        command: embedded,
        args: this._hostArgs(),
        env: this._baseEnv(),
        kind: 'embedded',
        cwd: path.dirname(embedded),
      };
    }

    this.pythonProbed = true;
    const python = await pickPython(this.config);
    if (!python) return null;
    const pythonPath = [coreDir, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
    return {
      command: python,
      args: ['-m', 'agent_core.host', ...this._hostArgs()],
      env: {
        ...this._baseEnv(),
        ...(pythonPath ? { PYTHONPATH: pythonPath } : {}),
      },
      kind: 'python',
      cwd: coreDir || undefined,
    };
  }

  _hostArgs() {
    const args = [
      '--workspace',
      this.workspace,
      '--port',
      '0',
      '--print-url',
      '--no-open',
    ];
    if (this.config.model) args.push('--model', this.config.model);
    if (this.config.baseUrl) args.push('--base-url', this.config.baseUrl);
    if (this.config.apiKey) args.push('--api-key', this.config.apiKey);
    return args;
  }

  _baseEnv() {
    const env = {
      ...process.env,
      PYTHONIOENCODING: 'utf-8',
      PYTHONUNBUFFERED: '1',
    };
    // 治理面（M3）走环境变量而不是命令行参数，原因有两个：
    //   1. 后端已经有 `Config.load()` 读 `VIBE_SERVER_URL` / `VIBE_GATEWAY_URL`
    //      的既定路径，再加一组同名 CLI flag 就是第二份真源，迟早漂移；
    //   2. 内嵌二进制与系统 Python 两条启动路径都继承这里，加一次即可全覆盖。
    if (this.config.serverUrl) env.VIBE_SERVER_URL = this.config.serverUrl;
    if (this.config.gatewayUrl) env.VIBE_GATEWAY_URL = this.config.gatewayUrl;
    return env;
  }

  /** 启动并解析出 VIBE_URL。resolve 失败时抛出可直接展示给用户的错误。 */
  start() {
    return (async () => {
      const target = await this.resolve();
      if (!target) {
        throw new Error(
          '找不到可用的后端：既没有内嵌二进制，也没有可用的 Python 3。\n\n' +
            '可以重新解压完整分发包，或在配置文件里指定解释器路径：\n' +
            (this.options.configPath || 'config.json')
        );
      }

      this.kind = target.kind;
      this.command = target.command;

      this.child = spawn(target.command, target.args, {
        cwd: target.cwd,
        env: target.env,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      });

      return new Promise((resolve, reject) => {
        let settled = false;
        let stderr = '';
        const timer = setTimeout(() => {
          if (settled) return;
          settled = true;
          reject(new Error(`后端启动超时（${STARTUP_TIMEOUT_MS / 1000}s）`));
        }, STARTUP_TIMEOUT_MS);

        const finish = (fn, arg) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          fn(arg);
        };

        let buffer = '';
        this.child.stdout.on('data', (chunk) => {
          buffer += chunk.toString('utf8');
          const match = /VIBE_URL=(\S+)/.exec(buffer);
          if (match) {
            this.url = match[1];
            finish(resolve, this.url);
          }
        });

        this.child.stderr.on('data', (chunk) => {
          stderr += chunk.toString('utf8');
          if (stderr.length > 8000) stderr = stderr.slice(-8000);
        });

        this.child.on('error', (error) => finish(reject, error));

        this.child.on('exit', (code) => {
          this.exited = true;
          finish(reject, new Error(`后端进程退出（code=${code}）\n\n${stderr.trim()}`));
          if (this.onUnexpectedExit && !this.stopping) this.onUnexpectedExit(code, stderr);
        });
      });
    })();
  }

  stop() {
    this.stopping = true;
    const child = this.child;
    if (!child || this.exited) return;

    // PyInstaller onefile 是**两层进程**：引导进程 + 真正的服务进程。
    // 只 kill 引导进程会把服务进程留成孤儿 —— 它照样占着端口、照样往工作区写审计，
    // 用户看到的就是「窗口关了，东西还在动」。所以 Windows 上必须按进程树终止。
    if (process.platform === 'win32') {
      // **必须同步**：调用方（尤其是冒烟与退出流程）在 stop() 之后马上就会
      // app.exit()，异步 spawn 出来的 taskkill 会跟着一起被带走，结果是引导进程
      // 死了、真正的服务进程留成孤儿。同步执行才能保证「返回即已死透」。
      // 代价是退出时阻塞几十毫秒，这个位置完全值得。
      try {
        spawnSync('taskkill', ['/pid', String(child.pid), '/T', '/F'], {
          windowsHide: true,
          timeout: 10_000,
        });
        return;
      } catch {
        /* 退回到下面的单进程终止 */
      }
    }

    child.kill();
    // 给一点时间优雅退出，之后强杀，避免留下孤儿进程占着端口
    setTimeout(() => {
      if (!child.killed) {
        try {
          child.kill('SIGKILL');
        } catch {
          /* 已经没了 */
        }
      }
    }, 2500);
  }
}

module.exports = {
  STARTUP_TIMEOUT_MS,
  Backend,
  embeddedBinaryName,
  embeddedCandidates,
  findEmbeddedBinary,
  pickPython,
  waitForHealth,
};