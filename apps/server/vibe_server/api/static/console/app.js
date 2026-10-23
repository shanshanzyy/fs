/* ===========================================================================
 * Vibe 治理面 · 控制台引导（M8）
 * ---------------------------------------------------------------------------
 * 职责：确认有会话 → 读 /auth/me → 按权限建导航 → 分派到对应面板。
 *
 * **前端隐藏只是体验**：界面按 actor.permissions 隐藏无权面板，避免把
 * 「点了必被 403」的入口摆在人面前；真正的闸门始终是服务端路由上的
 * `permissions=(...)`。所以这里隐藏了什么、没隐藏什么，都不构成安全边界。
 * =========================================================================== */

'use strict';

import { accessToken, currentActor, logout } from './api.js';
import { Panels } from './panels.js';

const LOGIN_PAGE = '/login';

const PANELS = [
  { id: 'overview', title: '概览', anyPerm: null, render: Panels.overview },
  { id: 'members', title: '成员与邀请码', anyPerm: ['user:read', 'enrollment:write'], render: Panels.members },
  { id: 'teams', title: '团队', anyPerm: ['team:read'], render: Panels.teams },
  { id: 'devices', title: '设备', anyPerm: ['device:read'], render: Panels.devices },
  { id: 'policy', title: '策略', anyPerm: ['policy:read'], render: Panels.policy },
  { id: 'quota', title: '配额', anyPerm: ['quota:read'], render: Panels.quota },
  { id: 'audit', title: '审计', anyPerm: ['audit:read'], render: Panels.audit },
  { id: 'usage', title: '用量', anyPerm: ['usage:read'], render: Panels.usage },
  { id: 'secrets', title: '密钥台账', anyPerm: ['secret:read'], render: Panels.secrets },
  { id: 'releases', title: '客户端发布', anyPerm: ['release:read'], render: Panels.releases },
];

function setStatus(text) {
  const node = document.getElementById('status');
  if (node) node.textContent = text || '';
}

function render(current, ctx) {
  const panelNode = document.getElementById('panel');
  document.getElementById('panel-title').textContent = current.title;
  while (panelNode.firstChild) panelNode.removeChild(panelNode.firstChild);
  panelNode.appendChild(document.createTextNode('加载中…'));
  setStatus('');
  Promise.resolve()
    .then(() => current.render(ctx))
    .then((nodes) => {
      while (panelNode.firstChild) panelNode.removeChild(panelNode.firstChild);
      (nodes || []).forEach((node) => panelNode.appendChild(node));
      setStatus('已更新');
    })
    .catch((error) => {
      while (panelNode.firstChild) panelNode.removeChild(panelNode.firstChild);
      const box = document.createElement('div');
      box.className = 'card';
      const msg = document.createElement('div');
      msg.className = 'err';
      msg.textContent = '加载失败：' + (error && error.message ? error.message : String(error));
      box.appendChild(msg);
      panelNode.appendChild(box);
      setStatus('加载失败');
    });
}

function buildNav(visible, ctx) {
  const nav = document.getElementById('nav');
  let active = null;
  visible.forEach((panel) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'nav-item';
    button.textContent = panel.title;
    button.addEventListener('click', () => {
      if (active) active.classList.remove('active');
      active = button;
      button.classList.add('active');
      render(panel, ctx);
    });
    nav.appendChild(button);
  });
  if (visible.length) {
    active = nav.firstChild;
    if (active) active.classList.add('active');
    render(visible[0], ctx);
  }
}

async function main() {
  if (!accessToken()) {
    window.location.replace(LOGIN_PAGE + '?next=/console');
    return;
  }
  const payload = await currentActor();
  const actor = payload.actor || {};
  const permissions = new Set(actor.permissions || []);
  const ctx = { actor: actor, can: (perm) => permissions.has(perm) };

  document.getElementById('me-name').textContent = actor.displayName || actor.email || '';
  document.getElementById('me-email').textContent = actor.email || '';
  document.getElementById('logout').addEventListener('click', async () => {
    await logout();
    window.location.replace(LOGIN_PAGE);
  });

  const visible = PANELS.filter((panel) => !panel.anyPerm
    || panel.anyPerm.some((perm) => permissions.has(perm)));

  document.getElementById('boot').hidden = true;
  document.getElementById('app').hidden = false;
  buildNav(visible, ctx);
}

main().catch(() => {
  // 令牌缺失 / 过期 / 服务不可达统一退避到登录页（api.js 已在 401 时清会话并跳转）。
  window.location.replace(LOGIN_PAGE + '?next=/console');
});
