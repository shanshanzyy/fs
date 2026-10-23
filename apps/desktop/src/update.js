/* ===========================================================================
 * Vibe Coding Agent · 客户端更新的壳侧执行器（M9）
 * ---------------------------------------------------------------------------
 * 更新这件事的**全部业务**都在内核（agent-core）：读签名清单、验 RSA 签名、
 * 算 sha256、流式下载、原子落盘到 staging 目录。壳只接着做最后一步，
 * 且刻意只做三件事（ADR-030 的「壳不碰更新业务」纪律）：
 *
 *   1. **形态校验一个字符串** —— 内核给的 stagingPath 是不是「绝对路径 +
 *      文件名形如 VibeAgent-Setup-<版本>.exe + 本机确实存在这个普通文件」；
 *   2. **spawn 安装器** —— NSIS 静默覆盖安装（/S），detached、stdio 丢弃；
 *   3. **退出自己** —— 让安装器有机会覆盖正在运行的 exe。
 *
 * 壳在这里绝不读 manifest、不算 sha256、不下载 —— 那些是内核的信任锚。
 *
 * 为什么抽成独立模块：主进程（main.js）要起窗口 / 建菜单 / 管后端，本就够厚；
 * 而「判断一个路径长得像不像安装包」「怎么记账一次安装」全是纯逻辑，抽出来
 * 才能用 Node 自带的 node:test 直接测，不必启动 Electron。
 *
 * 副作用（fs / spawn / app / backend）一律**由调用方注入**：本模块刻意不
 * require electron，于是它既是壳的一部分，也是一个可被单元测试 import 的普通模块。
 * =========================================================================== */

'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');

/* 安装包文件名的白名单。与内核 `update/download.py::package_filename` 同口径，
 * 也与治理面 `releases.py` 的穿越防护同源 —— 三处任一处放宽，都会让「这个路径
 * 可信」这条最薄的假设失效。 */
const STAGING_NAME_RE = /^VibeAgent-Setup-([0-9A-Za-z._-]{1,40})\.exe$/;

/* ------------------------------------------------- 路径形态校验（纯函数） */
/**
 * staging 路径的**纯形态校验**：绝对 + 文件名匹配 + 存在且是普通文件。
 *
 * 刻意**不做目录前缀判断**（§4 ④ 第 2 步）：staging 目录可由 `VIBE_UPDATE_DIR`
 * 指向任意位置，壳去猜「合法前缀」既猜不准也拦不住任何真实威胁；真正的信任来自
 * 「内核已验签 + 文件名白名单 + 本机真实存在的一个文件」。校验失败一律返回 false，
 * 调用方据此拒绝 spawn。
 *
 * `deps.fs` 可注入一个假 fs（便于测试），缺省用真实的。
 */
function isStagingPathValid(candidate, deps = {}) {
  const io = (deps && deps.fs) || fs;
  if (typeof candidate !== 'string' || !candidate) return false;
  if (!path.isAbsolute(candidate)) return false;
  if (!STAGING_NAME_RE.test(path.basename(candidate))) return false;
  try {
    // statSync 同时覆盖「不存在」与「是目录」两种反例：只有普通文件才放行。
    return io.statSync(candidate).isFile();
  } catch {
    return false; // 不存在 / 没权限 / 读取失败 —— 全都不是可执行安装包
  }
}

/** 从 staging 文件名里取出版本号（记 pending 与「关于」页显示都要用）。取不到回空串。 */
function stagingVersion(candidate) {
  const name = typeof candidate === 'string' ? path.basename(candidate) : '';
  const match = STAGING_NAME_RE.exec(name);
  return match ? match[1] : '';
}

/* --------------------------------------------------------- pending marker
 * pending.json 是壳给自己留的一张「本次安装的记账」：
 * `{fromVersion, toVersion, stagingPath, startedAt}`。
 *
 * 壳 spawn 完安装器就立刻退出，**没人能当场知道安装成没成**；下次启动读它、对照
 * 当前版本，才能判断是「已更新到 vX」还是「更新没生效」。这就是 §4 ④ 第 7 步的
 * marker 兜底 —— 也正因为有它，才**不需要**（也不该）用 app.relaunch()。
 *
 * 文件路径由调用方传入（壳用自己的 userData），因此下面三个都是纯函数，可以拿
 * 临时目录直接测。
 */
function readPending(file, deps = {}) {
  const io = (deps && deps.fs) || fs;
  try {
    const data = JSON.parse(io.readFileSync(file, 'utf8'));
    return data && typeof data === 'object' ? data : null;
  } catch {
    return null; // 不存在 / 损坏 / 读不了：一律视为「没有未决安装」
  }
}

/** 写 pending（自动补 startedAt），返回落盘后的对象。 */
function writePending(file, payload, deps = {}) {
  const io = (deps && deps.fs) || fs;
  const document = {
    fromVersion: String(payload?.fromVersion || ''),
    toVersion: String(payload?.toVersion || ''),
    stagingPath: String(payload?.stagingPath || ''),
    startedAt: payload?.startedAt || new Date().toISOString(),
  };
  io.mkdirSync(path.dirname(file), { recursive: true });
  io.writeFileSync(file, JSON.stringify(document, null, 2), 'utf8');
  return document;
}

/** 清 pending。文件本就不存在也算「清干净了」，不报错。 */
function clearPending(file, deps = {}) {
  const io = (deps && deps.fs) || fs;
  try {
    io.unlinkSync(file);
    return true;
  } catch {
    return false;
  }
}

/* ----------------------------------------------------------- 执行器 */
/**
 * 一次「应用更新」的完整动作序列（§4 ④，**顺序敏感**）：
 *   写 marker → 停后端 → spawn 安装器 → 退出自己。
 *
 * - **先写 marker**：一旦退出进程，内存里的状态全丢，只有落盘的东西能跨过这次重启；
 * - **先停后端**：Windows 上运行中的 exe 会被锁住，不先用同步 taskkill 收干净后端子
 *   进程树，安装器覆盖文件就会失败（backend.stop() 已处理这条）；
 * - **最后退出**：用 app.exit(0) 立刻退出，而不是 app.quit() 走优雅关闭 —— 此刻
 *   exe 正被安装器覆盖，拖得越久越可能被判定「文件占用」。
 *
 * 返回 `{ok, ...}`；成功路径下进程随即退出，返回值只是给（测试里的）调用方看的。
 */
function applyStaging(options = {}) {
  const {
    stagingPath,
    pendingFile,
    fromVersion = '',
    toVersion = '',
    fs: io = fs,
    spawn: spawnImpl = spawn,
    app,
    backend,
  } = options;

  if (!isStagingPathValid(stagingPath, { fs: io })) {
    return { ok: false, reason: 'staging 路径形态非法（已拒绝执行安装器）' };
  }
  if (!app || !pendingFile) {
    return { ok: false, reason: '缺少必需依赖（app / pendingFile）' };
  }

  const target = String(toVersion || stagingVersion(stagingPath) || '');
  writePending(
    pendingFile,
    { fromVersion: String(fromVersion || ''), toVersion: target, stagingPath: String(stagingPath) },
    { fs: io }
  );

  try {
    backend?.stop();
  } catch {
    /* 停机失败也要继续：安装器自己会等文件释放，这里不该把更新卡死 */
  }

  let child;
  try {
    child = spawnImpl(stagingPath, ['/S'], {
      detached: true,
      stdio: 'ignore',
      windowsHide: true,
    });
  } catch (error) {
    return { ok: false, reason: `无法启动安装器：${String(error?.message || error)}` };
  }
  // detached + stdio 丢弃：壳一退出，安装器就脱离父进程继续跑。
  // 绝不能在此调 app.relaunch()：exe 正被安装器覆盖，relaunch 拉起的多半是残骸。
  if (child && typeof child.unref === 'function') child.unref();
  app.exit(0);
  return { ok: true, stagingPath: String(stagingPath), version: target };
}

module.exports = {
  STAGING_NAME_RE,
  isStagingPathValid,
  stagingVersion,
  readPending,
  writePending,
  clearPending,
  applyStaging,
};
