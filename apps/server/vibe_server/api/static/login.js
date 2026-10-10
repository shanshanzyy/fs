/* ===========================================================================
 * Vibe 治理面 · 登录页脚本
 * ---------------------------------------------------------------------------
 * 只做三件事：把表单值 POST 给服务端、把拿到的令牌存进 sessionStorage、
 * 把服务端的错误如实显示出来。**不含任何业务逻辑** —— 谁能登录、密码对不对、
 * 令牌怎么签发，全在服务端。
 *
 * 服务端接口路径必须与 `vibe_server/api/routes.py` 的 `API` 常量一致：
 *   POST {API}/auth/login        —— 邮箱 + 口令
 *   GET  {API}/auth/oidc/begin   —— 发起 SSO（未配置时返回 404）
 * 漂移不会有任何报错，只会「点了没反应」，所以 test_static.py 会钉住这两个路径。
 * =========================================================================== */

'use strict';

/* 接口路径**写成完整字面量**（而不是在脚本里拼 `API + '/auth/login'`）：
   test_static.py 用 `routes.API` 拼出同一串来比对，字面量才能被静态钉住。
   服务端改了前缀、这里没跟上，测试会立刻变红，而不是「点了没反应」。 */
const LOGIN_PATH = '/api/v1/auth/login';
const OIDC_BEGIN_PATH = '/api/v1/auth/oidc/begin';

const TOKEN_KEY = 'vibe.accessToken';
const REFRESH_KEY = 'vibe.refreshToken';
const FP_KEY = 'vibe.login.fingerprint';

/* 设备指纹：仅用于服务端把「同一次登录」关联到一台设备。
   存 localStorage 以便同一浏览器重复登录时保持一致；服务端不强制要求。 */
function fingerprint() {
  try {
    let value = localStorage.getItem(FP_KEY);
    if (!value) {
      value = 'web-' + Math.random().toString(36).slice(2) + Date.now().toString(36);
      localStorage.setItem(FP_KEY, value);
    }
    return value;
  } catch {
    return 'web-anonymous';
  }
}

function show(text, kind) {
  const msg = document.getElementById('msg');
  msg.textContent = text;
  msg.className = 'msg' + (kind ? ' ' + kind : '');
}

function showSuccess(payload) {
  try {
    sessionStorage.setItem(TOKEN_KEY, payload.accessToken || '');
    sessionStorage.setItem(REFRESH_KEY, payload.refreshToken || '');
  } catch {
    /* 隐私模式下 sessionStorage 可能不可用；令牌存不下不影响「已登录」这件事本身 */
  }
  document.getElementById('login-view').hidden = true;
  const ok = document.getElementById('ok-view');
  ok.hidden = false;
  const email = payload.user && payload.user.email ? payload.user.email : '';
  document.getElementById('ok-sub').textContent =
    (email ? email + ' 已登录。' : '已登录。') +
    '凭据已保存到本浏览器会话（sessionStorage），可关闭本页回到桌面端继续。';
}

async function readError(response) {
  try {
    const body = await response.json();
    if (body && body.error && body.error.message) return body.error.message;
  } catch {
    /* 非 JSON 响应：退回状态码描述 */
  }
  return 'HTTP ' + response.status;
}

async function submit(event) {
  event.preventDefault();
  const button = document.getElementById('submit');
  const email = document.getElementById('email').value.trim();
  const password = document.getElementById('password').value;
  if (!email || !password) {
    show('请填写邮箱与密码。', 'error');
    return;
  }
  button.disabled = true;
  show('正在登录…');
  try {
    const response = await fetch(LOGIN_PATH, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        email: email,
        password: password,
        deviceName: 'web',
        platform: 'web-login',
        fingerprint: fingerprint(),
      }),
    });
    if (!response.ok) {
      // 服务端对「邮箱或密码错误」返回 401 + 明确文案，原样透出。
      show(await readError(response), 'error');
      return;
    }
    showSuccess(await response.json());
  } catch (error) {
    show('无法连接服务端：' + (error && error.message ? error.message : error), 'error');
  } finally {
    button.disabled = false;
  }
}

async function startSso() {
  const button = document.getElementById('sso');
  button.disabled = true;
  show('正在跳转到 SSO…');
  try {
    const response = await fetch(OIDC_BEGIN_PATH);
    if (!response.ok) {
      show('未配置 SSO，请用邮箱密码登录。', 'error');
      return;
    }
    const body = await response.json();
    if (body && body.authorizationUrl) {
      window.location.href = body.authorizationUrl;
      return;
    }
    show('SSO 未返回授权地址，请用邮箱密码登录。', 'error');
  } catch (error) {
    show('无法连接服务端：' + (error && error.message ? error.message : error), 'error');
  } finally {
    button.disabled = false;
  }
}

document.getElementById('login-form').addEventListener('submit', submit);
document.getElementById('sso').addEventListener('click', startSso);