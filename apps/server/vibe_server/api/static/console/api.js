/* ===========================================================================
 * Vibe 治理面 · 控制台 API 封装（M8）
 * ---------------------------------------------------------------------------
 * 三件事：注入 Bearer、401 单飞刷新重试一次、失败清会话跳登录。
 *
 * 接口路径**写成完整字面量**（而不是拼 `API + '/auth/me'`）：
 * test_console.py 用 `routes.API` 拼出同一串来比对，字面量才能被静态钉住。
 * 漂移不会有任何报错，只会「点了没反应」。
 *
 * 会话键与 login.js 一致（vibe.accessToken / vibe.refreshToken），
 * 保证从 /login 跳到 /console 天然共享同一份会话。
 * =========================================================================== */

'use strict';

const TOKEN_KEY = 'vibe.accessToken';
const REFRESH_KEY = 'vibe.refreshToken';
const LOGIN_PAGE = '/login';

const ME_PATH = '/api/v1/auth/me';
const REFRESH_PATH = '/api/v1/auth/refresh';
const LOGOUT_PATH = '/api/v1/auth/logout';

function readKey(key) {
  try { return sessionStorage.getItem(key) || ''; } catch { return ''; }
}
function writeKey(key, value) {
  try { sessionStorage.setItem(key, value || ''); } catch { /* 隐私模式不可用：忽略 */ }
}
function dropSession() {
  try { sessionStorage.removeItem(TOKEN_KEY); sessionStorage.removeItem(REFRESH_KEY); } catch { /* ignore */ }
}
function saveSession(payload) {
  if (!payload) return;
  writeKey(TOKEN_KEY, payload.accessToken);
  writeKey(REFRESH_KEY, payload.refreshToken);
}

export function accessToken() { return readKey(TOKEN_KEY); }

function toLogin() {
  window.location.replace(LOGIN_PAGE + '?next=/console');
}

async function readError(response) {
  try {
    const body = await response.json();
    if (body && body.error && body.error.message) return body.error.message;
  } catch { /* 非 JSON：退回状态码 */ }
  return 'HTTP ' + response.status;
}

/* 单飞刷新：并发多个 401 只发起一次刷新请求，其余等同一个 promise。
   否则每个失败的请求各刷一次，服务端的「刷新令牌一次性 + 重用检测」
   会把整族令牌判为泄漏而吊销 —— 一次过期变成「全设备掉线」。 */
let refreshing = null;

async function refreshOnce() {
  if (!refreshing) {
    refreshing = (async () => {
      const rt = readKey(REFRESH_KEY);
      if (!rt) return false;
      try {
        const response = await fetch(REFRESH_PATH, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refreshToken: rt }),
        });
        if (!response.ok) return false;
        saveSession(await response.json());
        return true;
      } catch {
        return false;
      }
    })();
  }
  try {
    return await refreshing;
  } finally {
    refreshing = null;
  }
}

/* 底层请求：返回原始 Response（2xx 与否都不抛）。 */
export async function request(path, options) {
  const opts = options || {};
  const send = async () => {
    const init = { method: opts.method || 'GET', headers: Object.assign({}, opts.headers) };
    const token = readKey(TOKEN_KEY);
    if (token) init.headers['Authorization'] = 'Bearer ' + token;
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init);
  };

  let response = await send();
  if (response.status === 401) {
    const ok = await refreshOnce();
    if (!ok) {
      dropSession();
      toLogin();
      throw new Error('会话已过期，请重新登录');
    }
    response = await send();
    if (response.status === 401) {
      dropSession();
      toLogin();
      throw new Error('会话已过期，请重新登录');
    }
  }
  return response;
}

function toQuery(params) {
  const parts = [];
  Object.keys(params || {}).forEach((key) => {
    const value = params[key];
    if (value === undefined || value === null || value === '') return;
    parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(value));
  });
  return parts.length ? '?' + parts.join('&') : '';
}

export async function getJson(path, params) {
  const response = await request(path + toQuery(params));
  if (!response.ok) throw new Error(await readError(response));
  return response.json();
}

export async function postJson(path, body) {
  const response = await request(path, { method: 'POST', body: body || {} });
  if (!response.ok) throw new Error(await readError(response));
  return response.json();
}

export async function del(path) {
  const response = await request(path, { method: 'DELETE' });
  if (!response.ok) throw new Error(await readError(response));
  return response.json();
}

export async function currentActor() { return getJson(ME_PATH); }

export async function logout() {
  const rt = readKey(REFRESH_KEY);
  try { await request(LOGOUT_PATH, { method: 'POST', body: { refreshToken: rt } }); } catch { /* 不可达也要清 */ }
  dropSession();
}
