/* ===========================================================================
 * isStagingPathValid 的白名单正 / 反例（node:test，零第三方依赖）
 * ---------------------------------------------------------------------------
 * 只用 Node 内置的 node:test + node:assert：桌面壳运行期依赖为零，测试也不该
 * 破例引入任何测试框架。被测函数刻意是纯函数（副作用可注入），所以这里能直接
 * `require('../src/update.js')` —— 不需要启动 Electron。
 * =========================================================================== */

'use strict';

const { test, after } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { isStagingPathValid } = require('../src/update');

// 一个临时目录装所有样本文件；跑完整体清掉。
const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), 'vibe-update-path-'));

/** 在 sandbox 里造一个「形态合法且真实存在」的普通文件，返回其绝对路径。 */
function createStagingFile(name = 'VibeAgent-Setup-1.2.3.exe') {
  const file = path.join(sandbox, name);
  fs.writeFileSync(file, 'stub');
  return file;
}

after(() => {
  fs.rmSync(sandbox, { recursive: true, force: true });
});

test('正例：绝对路径 + 名字符合白名单 + 真实存在的普通文件 → true', () => {
  assert.equal(isStagingPathValid(createStagingFile()), true);
});

test('正例：带 v 前缀 / 点 / 连字符 / 下划线的版本号也放过', () => {
  assert.equal(isStagingPathValid(createStagingFile('VibeAgent-Setup-v1.0.0-beta_1.exe')), true);
});

test('反例：相对路径 → false', () => {
  assert.equal(isStagingPathValid('VibeAgent-Setup-1.2.3.exe'), false);
  assert.equal(isStagingPathValid(path.join('.', 'VibeAgent-Setup-1.2.3.exe')), false);
});

test('反例：非 .exe 后缀 → false', () => {
  const file = path.join(sandbox, 'VibeAgent-Setup-1.2.3.zip');
  fs.writeFileSync(file, 'stub');
  assert.equal(isStagingPathValid(file), false);
});

test('反例：文件名不符合白名单（前缀 / 结构不对） → false', () => {
  assert.equal(isStagingPathValid(createStagingFile('VibeAgent-1.2.3.exe')), false);
  // 只有前缀、版本段为空
  assert.equal(isStagingPathValid(createStagingFile('VibeAgent-Setup-.exe')), false);
  // 后缀大小写不符（白名单按字面匹配）
  assert.equal(isStagingPathValid(createStagingFile('VibeAgent-Setup-1.2.3.EXE')), false);
  // 版本段超长（>40）也要挡下
  assert.equal(isStagingPathValid(createStagingFile(`VibeAgent-Setup-${'1'.repeat(41)}.exe`)), false);
});

test('反例：文件不存在 → false', () => {
  assert.equal(isStagingPathValid(path.join(sandbox, 'VibeAgent-Setup-9.9.9.exe')), false);
});

test('反例：路径指向的是目录（哪怕是合法文件名） → false', () => {
  const dir = path.join(sandbox, 'VibeAgent-Setup-9.9.9.exe');
  fs.mkdirSync(dir, { recursive: true });
  assert.equal(isStagingPathValid(dir), false);
});

test('反例：非字符串入参（null / 数字 / 对象 / 空串） → false', () => {
  assert.equal(isStagingPathValid(null), false);
  assert.equal(isStagingPathValid(42), false);
  assert.equal(isStagingPathValid({}), false);
  assert.equal(isStagingPathValid(''), false);
});
