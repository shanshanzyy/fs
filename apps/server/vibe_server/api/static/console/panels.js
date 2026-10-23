/* ===========================================================================
 * Vibe 治理面 · 控制台面板（M8）
 * ---------------------------------------------------------------------------
 * 十个面板：概览 / 成员与邀请码 / 团队 / 设备 / 策略 / 配额 / 审计 / 用量 / 密钥台账 /
 * 客户端发布。
 *
 * 两条纪律：
 *   1. **一律 textContent 渲染服务端数据**，绝不用 innerHTML 拼接 —— 即便审计是
 *      纯元数据（ADR-006），XSS 也不该有任何立足点。
 *   2. 面板只是**体验层**：真正的权限闸门在服务端路由上（`permissions=(...)`）。
 *      这里按权限隐藏/降级，是为了不把「点了会被 403」的按钮摆在人面前。
 * =========================================================================== */

'use strict';

import { getJson, postJson } from './api.js';

/* ------------------------------------------------------------------ DOM 助手 */
function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    Object.keys(attrs).forEach((key) => {
      const value = attrs[key];
      if (value === undefined || value === null) return;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = String(value);
      else node.setAttribute(key, value);
    });
  }
  (children || []).forEach((child) => {
    if (child === null || child === undefined) return;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  });
  return node;
}

function card(title, body) {
  const children = [];
  if (title) children.push(el('h2', { text: title }));
  if (Array.isArray(body)) children.push.apply(children, body);
  else if (body) children.push(body);
  return el('div', { class: 'card' }, children);
}

function cards(items) { return el('div', { class: 'cards' }, items); }

function metric(key, value) {
  return el('div', { class: 'metric' }, [
    el('div', { class: 'k', text: key }),
    el('div', { class: 'v', text: value }),
  ]);
}

function empty(message) { return el('div', { class: 'empty', text: message }); }

function jsonBlock(value) {
  const text = JSON.stringify(value, null, 2);
  return el('pre', { class: 'json', text: text.length > 20000 ? text.slice(0, 20000) + '\n…' : text });
}

function fmtTime(seconds) {
  const n = Number(seconds) || 0;
  if (!n) return '—';
  const d = new Date(n * 1000);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toISOString().replace('T', ' ').slice(0, 19) + 'Z';
}

function fmtValue(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') {
    return el('span', { class: 'tag ' + (value ? 'bad' : 'dim'), text: value ? '是' : '否' });
  }
  if (typeof value === 'number') return String(value);
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

/* 从行集自动推导列（按首次出现顺序），对形状变化免疫。 */
function autoTable(rows, preferred) {
  if (!rows || !rows.length) return empty('暂无数据。');
  const columns = [];
  (preferred || []).forEach((c) => { if (rows.some((r) => c in r)) columns.push(c); });
  rows.forEach((row) => {
    Object.keys(row).forEach((key) => { if (columns.indexOf(key) < 0) columns.push(key); });
  });
  const head = el('thead', {}, [el('tr', {}, columns.map((c) => el('th', { text: c })))]);
  const body = el('tbody', {}, rows.map((row) => el('tr', {}, columns.map((c) => {
    const cell = el('td');
    const value = row[c];
    if (c === 'createdAt' || c === 'expiresAt' || c === 'rotatedAt' || c === 'ts' || c === 'updatedAt'
        || c === 'publishedAt' || c === 'revokedAt') {
      cell.textContent = fmtTime(value);
    } else {
      const node = fmtValue(value);
      cell.appendChild(typeof node === 'string' ? document.createTextNode(node) : node);
    }
    return cell;
  }))));
  return el('table', { class: 'table' }, [head, body]);
}

function field(label, input) {
  return el('div', { class: 'field' }, [el('label', { text: label }), input]);
}

function textInput(placeholder, value) {
  const input = el('input', { type: 'text' });
  if (placeholder) input.placeholder = placeholder;
  if (value !== undefined) input.value = value;
  return input;
}

function numberInput(value) {
  const input = el('input', { type: 'number' });
  if (value !== undefined) input.value = String(value);
  return input;
}

function selectInput(options) {
  const select = el('select');
  options.forEach((opt) => select.appendChild(el('option', { value: opt.value, text: opt.label })));
  return select;
}

function checkboxInput(value) {
  const input = el('input', { type: 'checkbox' });
  input.checked = !!value;
  return input;
}

const ROLE_OPTIONS = [
  { value: 'developer', label: 'developer' },
  { value: 'team_admin', label: 'team_admin' },
  { value: 'auditor', label: 'auditor' },
  { value: 'viewer', label: 'viewer' },
];

/* 一个「按钮 + 结果区」的小表单：提交后把服务端返回或错误写进结果区。 */
function actionButton(label, onClick, cls) {
  const button = el('button', { type: 'button', class: 'btn ' + (cls || '') }, [label]);
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await onClick();
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

/* ------------------------------------------------------------------ 概览 */
async function overview(ctx) {
  const health = await getJson('/healthz');
  const nodes = [];
  nodes.push(cards([
    metric('服务版本', health.version || '—'),
    metric('存储状态', health.ok ? '正常' : '异常'),
    metric('运行时长（秒）', health.uptimeSeconds !== undefined ? health.uptimeSeconds : '—'),
    metric('我的角色', (ctx.actor.roles || []).join(', ') || '—'),
  ]));
  nodes.push(card('我的身份', jsonBlock(ctx.actor)));
  return nodes;
}

/* ------------------------------------------------------ 成员与邀请码 */
async function members(ctx) {
  const nodes = [];
  if (ctx.can('user:read')) {
    const users = await getJson('/api/v1/users', { limit: 100 });
    nodes.push(card('组织成员', autoTable(users.users, ['userId', 'email', 'displayName', 'role', 'status'])));
  } else {
    nodes.push(card('组织成员', empty('当前角色无 user:read 权限，成员列表不可见。')));
  }

  if (ctx.can('enrollment:write')) {
    const enrollments = await getJson('/api/v1/enrollments');
    nodes.push(card('邀请码台账', autoTable(enrollments.enrollments,
      ['id', 'teamId', 'role', 'maxUses', 'uses', 'revoked', 'expiresAt'])));

    const teamInput = textInput('团队 id（可留空）');
    const roleSelect = selectInput(ROLE_OPTIONS);
    const maxUses = numberInput(1);
    const ttl = numberInput(0);
    const result = el('div', { class: 'empty', text: '' });
    const button = actionButton('签发邀请码', async () => {
      result.className = 'empty';
      try {
        const body = {
          teamId: teamInput.value.trim(),
          role: roleSelect.value,
          maxUses: Number(maxUses.value) || 1,
          ttl: Number(ttl.value) || 0,
        };
        const created = await postJson('/api/v1/enrollments', body);
        result.className = 'code';
        // 明文令牌只出现这一次，服务端只存哈希（ADR-006）。
        result.textContent = '邀请码（仅此一次显示）：' + created.enrollmentToken;
      } catch (error) {
        result.className = 'err';
        result.textContent = error.message || String(error);
      }
    });
    nodes.push(card('签发新邀请码', [
      el('div', { class: 'form' }, [
        field('团队 id', teamInput), field('角色', roleSelect),
        field('可用次数', maxUses), field('有效期（秒，0=默认）', ttl), button,
      ]),
      result,
    ]));
  }
  return nodes;
}

/* ------------------------------------------------------------------ 团队 */
async function teams(ctx) {
  const nodes = [];
  const data = await getJson('/api/v1/teams');
  nodes.push(card('团队列表', autoTable(data.teams, ['id', 'slug', 'name', 'memberCount', 'createdAt'])));

  if (data.teams && data.teams.length) {
    const teamSelect = selectInput(data.teams.map((t) => ({ value: t.id, label: t.name + ' (' + t.slug + ')' })));
    const membersBox = el('div');
    membersBox.appendChild(empty('选择团队后加载成员。'));
    const load = actionButton('查看成员', async () => {
      while (membersBox.firstChild) membersBox.removeChild(membersBox.firstChild);
      try {
        const members = await getJson('/api/v1/teams/' + encodeURIComponent(teamSelect.value) + '/members');
        membersBox.appendChild(autoTable(members.members, ['userId', 'email', 'displayName', 'role', 'status']));
      } catch (error) {
        membersBox.appendChild(el('div', { class: 'err', text: error.message || String(error) }));
      }
    });
    nodes.push(card('团队成员', [el('div', { class: 'form' }, [field('团队', teamSelect), load]), membersBox]));
  }

  if (ctx.can('team:write')) {
    const slug = textInput('如 platform');
    const name = textInput('如 平台组');
    const result = el('div', { class: 'empty', text: '' });
    const button = actionButton('创建团队', async () => {
      try {
        const created = await postJson('/api/v1/teams', { slug: slug.value.trim(), name: name.value.trim() });
        result.className = 'code';
        result.textContent = '已创建：' + created.teamId + '（刷新页面可见）';
      } catch (error) {
        result.className = 'err';
        result.textContent = error.message || String(error);
      }
    });
    nodes.push(card('创建团队', [
      el('div', { class: 'form' }, [field('标识 slug', slug), field('名称', name), button]), result,
    ]));
  }
  return nodes;
}

/* ------------------------------------------------------------------ 设备 */
async function devices(ctx) {
  const data = await getJson('/api/v1/devices');
  const nodes = [card('设备与会话', autoTable(data.devices,
    ['deviceId', 'userId', 'name', 'platform', 'lastSeenAt', 'createdAt']))];
  if (ctx.can('device:revoke')) {
    const deviceId = textInput('deviceId');
    const result = el('div', { class: 'empty', text: '' });
    const button = actionButton('撤销该设备会话', async () => {
      try {
        const done = await postJson('/api/v1/devices/' + encodeURIComponent(deviceId.value.trim()) + '/revoke', {});
        result.className = 'code';
        result.textContent = '已撤销：' + JSON.stringify(done);
      } catch (error) {
        result.className = 'err';
        result.textContent = error.message || String(error);
      }
    }, 'danger');
    nodes.push(card('撤销设备', [el('div', { class: 'form' }, [field('设备 id', deviceId), button]), result]));
  }
  return nodes;
}

/* ------------------------------------------------------------------ 策略 */
async function policy(ctx) {
  const nodes = [];
  const current = await getJson('/api/v1/policy');
  nodes.push(card('当前生效策略', jsonBlock(current)));

  const versions = await getJson('/api/v1/policy/versions', { scopeType: 'global' });
  nodes.push(card('版本历史（global）', autoTable(versions.versions)));

  if (ctx.can('policy:write')) {
    const scopeType = selectInput([
      { value: 'global', label: 'global' }, { value: 'team', label: 'team' }, { value: 'user', label: 'user' },
    ]);
    const scopeId = textInput('作用域 id（global 留空）');
    const document_ = el('textarea', { rows: 8, class: 'json', text: '{}' });
    const result = el('div', { class: 'empty', text: '' });
    const button = actionButton('发布策略', async () => {
      result.className = 'empty';
      let parsed;
      try {
        parsed = JSON.parse(document_.value || '{}');
      } catch (error) {
        result.className = 'err';
        result.textContent = 'policy 不是合法 JSON：' + (error.message || error);
        return;
      }
      try {
        const published = await postJson('/api/v1/policy', {
          scopeType: scopeType.value, scopeId: scopeId.value.trim(), policy: parsed,
        });
        result.className = 'code';
        result.textContent = '已发布：' + JSON.stringify(published);
      } catch (error) {
        result.className = 'err';
        result.textContent = error.message || String(error);
      }
    });
    nodes.push(card('发布策略', [
      el('div', { class: 'form' }, [field('作用域', scopeType), field('作用域 id', scopeId), button]),
      document_, result,
    ]));
  }
  return nodes;
}

/* ------------------------------------------------------------------ 配额 */
async function quota(ctx) {
  const nodes = [];
  const status = await getJson('/api/v1/quota');
  nodes.push(cards([
    metric('日期', status.day || '—'),
    metric('今日云 tokens', status.usage ? status.usage.cloudTokens : '—'),
    metric('预留 tokens', status.usage ? status.usage.reservedTokens : '—'),
    metric('今日请求数', status.usage ? status.usage.requests : '—'),
    metric('额度来源', status.source || '—'),
  ]));
  nodes.push(card('配额视图（原始）', jsonBlock(status)));

  const policies = await getJson('/api/v1/quota/policies');
  nodes.push(card('显式配额策略', autoTable(policies.policies)));

  if (ctx.can('quota:write')) {
    const scopeType = selectInput([
      { value: 'user', label: 'user' }, { value: 'team', label: 'team' }, { value: 'global', label: 'global' },
    ]);
    const scopeId = textInput('作用域 id（global 留空）');
    const cloudTokens = numberInput(0);
    const dailyRequests = numberInput(0);
    const rpm = numberInput(0);
    const result = el('div', { class: 'empty', text: '' });
    const button = actionButton('设置配额', async () => {
      try {
        const saved = await postJson('/api/v1/quota', {
          scopeType: scopeType.value, scopeId: scopeId.value.trim(),
          dailyCloudTokens: Number(cloudTokens.value) || 0,
          dailyRequests: Number(dailyRequests.value) || 0,
          requestsPerMinute: Number(rpm.value) || 0,
        });
        result.className = 'code';
        result.textContent = '已保存：' + JSON.stringify(saved);
      } catch (error) {
        result.className = 'err';
        result.textContent = error.message || String(error);
      }
    });
    nodes.push(card('设置显式配额', [
      el('div', { class: 'form' }, [
        field('作用域', scopeType), field('作用域 id', scopeId),
        field('每日云 tokens', cloudTokens), field('每日请求数', dailyRequests),
        field('每分钟请求数', rpm), button,
      ]),
      result,
    ]));
  }
  return nodes;
}

/* ------------------------------------------------------------------ 审计 */
async function audit(ctx) {
  const nodes = [];
  const summary = await getJson('/api/v1/audit/summary');
  nodes.push(cards([metric('事件总数', summary.total), metric('团队', summary.teamId || '（全局）')]));
  nodes.push(card('按类型汇总', autoTable(summary.byType, ['type', 'count'])));

  const events = await getJson('/api/v1/audit/events', { limit: 100 });
  nodes.push(card('最近事件（最多 100 条）', autoTable(events.events,
    ['ts', 'type', 'userId', 'teamId', 'deviceId', 'sessionId', 'hash'])));

  const deviceId = textInput('deviceId');
  const result = el('div', { class: 'empty', text: '' });
  const button = actionButton('校验该设备审计链', async () => {
    try {
      const verified = await postJson('/api/v1/audit/verify', { deviceId: deviceId.value.trim() });
      result.className = 'code';
      result.textContent = JSON.stringify(verified);
    } catch (error) {
      result.className = 'err';
      result.textContent = error.message || String(error);
    }
  });
  nodes.push(card('审计完整性校验', [el('div', { class: 'form' }, [field('设备 id', deviceId), button]), result]));
  return nodes;
}

/* ------------------------------------------------------------------ 用量 */
async function usage(ctx) {
  const days = numberInput(30);
  const seriesBox = el('div');
  const load = async () => {
    while (seriesBox.firstChild) seriesBox.removeChild(seriesBox.firstChild);
    try {
      const data = await getJson('/api/v1/usage', { days: Number(days.value) || 30 });
      const nodes = [cards([
        metric('云 tokens 合计', data.totals ? data.totals.cloudTokens : '—'),
        metric('本地 tokens 合计', data.totals ? data.totals.localTokens : '—'),
        metric('请求数合计', data.totals ? data.totals.requests : '—'),
      ])];
      if (data.truncated) nodes.push(empty('数据被截断：行数已达上限，合计偏低，请缩小窗口。'));
      nodes.push(autoTable(data.series, ['day', 'cloudTokens', 'localTokens', 'requests']));
      nodes.forEach((n) => seriesBox.appendChild(n));
    } catch (error) {
      seriesBox.appendChild(el('div', { class: 'err', text: error.message || String(error) }));
    }
  };
  const button = actionButton('查询', load);
  const panel = card('用量查询', [el('div', { class: 'form' }, [field('最近天数', days), button]), seriesBox]);
  await load();
  return [panel];
}

/* ------------------------------------------------------------ 密钥台账 */
async function secrets(ctx) {
  const data = await getJson('/api/v1/secrets');
  const nodes = [cards([metric('托管 provider', data.provider || '—')])];
  nodes.push(card('密钥台账（只回元数据，永不回值）', autoTable(data.secrets,
    ['name', 'provider', 'fingerprint', 'rotatedAt', 'note'])));
  const overdue = data.rotationOverdue;
  nodes.push(card('轮换提醒', overdue && overdue.length ? autoTable(overdue) : empty('无到期未轮换的密钥。')));
  return nodes;
}

/* ------------------------------------------------------------ 客户端发布 */
async function releases(ctx) {
  const nodes = [];
  const data = await getJson('/api/v1/client/releases', { limit: 100 });
  const kid = (data.pubkey && data.pubkey.kid) || '—';
  nodes.push(cards([metric('签名公钥 kid', kid), metric('记录数', (data.releases || []).length)]));
  // 公钥只能验签不能签名，公开无妨；但**私钥永不出现在这里**（服务端根本不回）。
  nodes.push(card('发布台账', autoTable(data.releases,
    ['id', 'version', 'channel', 'platform', 'arch', 'size', 'rolloutPercent',
      'mandatory', 'publishedAt', 'revokedAt', 'url'])));

  if (ctx.can('release:write')) {
    const version = textInput('如 0.2.0');
    const channel = selectInput([
      { value: 'stable', label: 'stable' }, { value: 'beta', label: 'beta' },
      { value: 'canary', label: 'canary' },
    ]);
    const platform = selectInput([
      { value: 'windows', label: 'windows' }, { value: 'darwin', label: 'darwin' },
      { value: 'linux', label: 'linux' },
    ]);
    const arch = selectInput([{ value: 'x64', label: 'x64' }, { value: 'arm64', label: 'arm64' }]);
    const sha256 = textInput('64 位小写十六进制（安装器 sha256）');
    const size = numberInput(0);
    const url = textInput('/api/v1/client/packages/… 或 http(s)://…');
    const notes = textInput('更新说明（会显示在客户端）');
    const minVersion = textInput('低于此版本的客户端强制升级（可留空）');
    const rollout = numberInput(100);
    const mandatory = checkboxInput(false);
    const result = el('div', { class: 'empty', text: '' });
    const button = actionButton('发布版本', async () => {
      result.className = 'empty';
      try {
        const created = await postJson('/api/v1/client/releases', {
          version: version.value.trim(),
          channel: channel.value,
          platform: platform.value,
          arch: arch.value,
          sha256: sha256.value.trim().toLowerCase(),
          size: Number(size.value) || 0,
          url: url.value.trim(),
          notes: notes.value,
          mandatory: !!mandatory.checked,
          minVersion: minVersion.value.trim(),
          rolloutPercent: Number(rollout.value) || 0,
        });
        result.className = 'code';
        result.textContent = '已发布：' + created.id + '（版本 ' + created.version
          + '，seq ' + created.seq + '，签名 ' + (created.pubkeyKid || '—') + '）';
      } catch (error) {
        result.className = 'err';
        result.textContent = error.message || String(error);
      }
    });
    nodes.push(card('发布新版本', [
      el('div', { class: 'form' }, [
        field('版本', version), field('渠道', channel), field('平台', platform), field('架构', arch),
        field('sha256', sha256), field('体积（字节）', size), field('下载地址', url),
        field('更新说明', notes), field('最低版本', minVersion), field('灰度 %', rollout),
        field('强制升级', mandatory), button,
      ]),
      result,
    ]));

    const revokeId = textInput('rel_xxxx');
    const revokeResult = el('div', { class: 'empty', text: '' });
    const revokeButton = actionButton('撤销该发布', async () => {
      try {
        const done = await postJson('/api/v1/client/releases/revoke', { id: revokeId.value.trim() });
        revokeResult.className = 'code';
        revokeResult.textContent = '已撤销：' + done.id + '（客户端下次检查起不再看到它）';
      } catch (error) {
        revokeResult.className = 'err';
        revokeResult.textContent = error.message || String(error);
      }
    }, 'danger');
    nodes.push(card('撤销发布', [
      el('div', { class: 'form' }, [field('发布 id', revokeId), revokeButton]), revokeResult,
    ]));
  } else {
    nodes.push(card('发布客户端版本', empty('当前角色无 release:write 权限，只能查看台账。')));
  }
  return nodes;
}

export const Panels = {
  overview, members, teams, devices, policy, quota, audit, usage, secrets, releases,
};
