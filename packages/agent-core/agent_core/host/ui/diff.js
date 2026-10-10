/* ===========================================================================
   unified diff 解析与渲染
   ---------------------------------------------------------------------------
   后端（agent_core.edit.patcher）产出的是标准 difflib unified_diff，
   带 a/ b/ 前缀与 @@ 块头。这里不依赖任何第三方库，自己解析成行模型再渲染。

   为什么要行内高亮：审批人真正要判断的是「这一行到底改了什么」。
   整行染红染绿在只改了一个词的时候噪音很大 —— 而「改一个词」恰好是
   Agent 编辑的高频形态。这里用「公共前缀 + 公共后缀」做差分标注，
   比完整 LCS 便宜得多，但对绝大多数真实改动已经够用。
   =========================================================================== */

const HUNK_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$/;

/** 把一段 unified diff 文本解析成 {files: [{path, hunks: [{rows}]}]} */
export function parseUnifiedDiff(src) {
  const files = [];
  let file = null;
  let hunk = null;
  let oldNo = 0;
  let newNo = 0;

  const ensureFile = () => {
    if (!file) file = { path: '', oldPath: '', newPath: '', hunks: [] };
    return file;
  };
  const closeFile = () => {
    if (file) files.push(file);
    file = null;
    hunk = null;
  };

  const lines = String(src ?? '').split(/\r?\n/);

  lines.forEach((raw, index) => {
    // 末尾 split 出来的空串不是内容的空行，跳过
    if (raw === '' && index === lines.length - 1) return;

    if (raw.startsWith('--- ')) {
      ensureFile().oldPath = raw.slice(4).trim();
      return;
    }
    if (raw.startsWith('+++ ')) {
      const f = ensureFile();
      f.newPath = raw.slice(4).trim();
      f.path = stripPrefix(f.newPath) || stripPrefix(f.oldPath) || '未知文件';
      return;
    }

    const m = HUNK_RE.exec(raw);
    if (m) {
      const f = ensureFile();
      oldNo = Number(m[1]);
      newNo = Number(m[3]);
      hunk = {
        header: raw,
        section: m[5] || '',
        rows: [],
        oldStart: oldNo,
        newStart: newNo,
        added: 0,
        removed: 0,
      };
      f.hunks.push(hunk);
      return;
    }

    if (!hunk) return;

    if (raw.startsWith('\\')) {
      hunk.rows.push({ type: 'meta', text: raw });
      return;
    }
    if (raw.startsWith('+')) {
      hunk.rows.push({ type: 'add', newNo: newNo++, text: raw.slice(1) });
      hunk.added++;
      return;
    }
    if (raw.startsWith('-')) {
      hunk.rows.push({ type: 'del', oldNo: oldNo++, text: raw.slice(1) });
      hunk.removed++;
      return;
    }
    // 上下文行以单个空格开头；容错处理没有前缀的情况
    const text = raw.startsWith(' ') ? raw.slice(1) : raw;
    hunk.rows.push({ type: 'ctx', oldNo: oldNo++, newNo: newNo++, text });
  });

  closeFile();
  return { files };
}

function stripPrefix(p) {
  if (!p) return '';
  if (p === '/dev/null') return '';
  const cleaned = p.replace(/^[ab]\//, '');
  return cleaned;
}

/** 统计整段 diff 的增删行数 */
export function diffStat(parsed) {
  let added = 0;
  let removed = 0;
  for (const file of parsed.files) {
    for (const hunk of file.hunks) {
      added += hunk.added;
      removed += hunk.removed;
    }
  }
  return { added, removed };
}

/* --------------------------------------------------------------------------
   行内标注：找出两行真正不同的那一段
   -------------------------------------------------------------------------- */
function inlineSpans(oldText, newText) {
  const a = oldText;
  const b = newText;

  let prefix = 0;
  const maxPrefix = Math.min(a.length, b.length);
  while (prefix < maxPrefix && a[prefix] === b[prefix]) prefix++;

  let suffix = 0;
  const maxSuffix = Math.min(a.length - prefix, b.length - prefix);
  while (
    suffix < maxSuffix &&
    a[a.length - 1 - suffix] === b[b.length - 1 - suffix]
  ) {
    suffix++;
  }

  // 整行完全相同（不该发生，但别渲染出空标注）
  if (prefix + suffix >= a.length && prefix + suffix >= b.length) {
    return [null, null];
  }
  // 共同部分太少说明这行基本被重写了，此时整行染色已足够，
  // 再叠一层行内高亮只会变成噪声
  if (prefix + suffix < 2) {
    return [null, null];
  }
  return [
    [prefix, a.length - suffix],
    [prefix, b.length - suffix],
  ];
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function withInline(text, span) {
  if (!span) return escapeHtml(text);
  const [from, to] = span;
  if (from >= to) return escapeHtml(text);
  return (
    escapeHtml(text.slice(0, from)) +
    '<span class="hl">' +
    escapeHtml(text.slice(from, to)) +
    '</span>' +
    escapeHtml(text.slice(to))
  );
}

/* --------------------------------------------------------------------------
   渲染
   -------------------------------------------------------------------------- */
/**
 * 渲染一段 unified diff 为 DOM 节点。
 * @param {string} diffText  后端给的 diff 原文
 * @returns {HTMLElement|null}
 */
export function renderDiff(diffText) {
  const parsed = parseUnifiedDiff(diffText);
  if (!parsed.files.length || !parsed.files.some((f) => f.hunks.length)) {
    return null;
  }

  const wrap = document.createElement('div');
  wrap.className = 'diff';

  for (const file of parsed.files) {
    const head = document.createElement('div');
    head.className = 'diff-head';
    const pathEl = document.createElement('span');
    pathEl.className = 'diff-path';
    pathEl.textContent = file.path || '（未知文件）';
    head.appendChild(pathEl);

    const stat = document.createElement('span');
    stat.className = 'diff-stat';
    const fileAdded = file.hunks.reduce((n, h) => n + h.added, 0);
    const fileRemoved = file.hunks.reduce((n, h) => n + h.removed, 0);
    stat.innerHTML =
      `<span class="plus">+${fileAdded}</span> / <span class="minus">-${fileRemoved}</span>`;
    head.appendChild(stat);
    wrap.appendChild(head);

    const hunksEl = document.createElement('div');
    hunksEl.className = 'diff-hunks';

    for (const hunk of file.hunks) {
      const header = document.createElement('div');
      header.className = 'hunk-header';
      header.textContent = hunk.header;
      hunksEl.appendChild(header);

      // 相邻的删除块 + 新增块按位置配对，配对上的两行做行内标注
      const rows = hunk.rows;
      let i = 0;
      while (i < rows.length) {
        if (rows[i].type !== 'del') {
          hunksEl.appendChild(buildRow(rows[i]));
          i++;
          continue;
        }

        const dels = [];
        while (i < rows.length && rows[i].type === 'del') dels.push(rows[i++]);
        const adds = [];
        while (i < rows.length && rows[i].type === 'add') adds.push(rows[i++]);

        const pairs = Math.min(dels.length, adds.length);
        for (let k = 0; k < dels.length; k++) {
          const span = k < pairs ? inlineSpans(dels[k].text, adds[k].text)[0] : null;
          hunksEl.appendChild(buildRow(dels[k], span));
        }
        for (let k = 0; k < adds.length; k++) {
          const span = k < pairs ? inlineSpans(dels[k].text, adds[k].text)[1] : null;
          hunksEl.appendChild(buildRow(adds[k], span));
        }
      }
    }

    wrap.appendChild(hunksEl);
  }

  return wrap;
}

function buildRow(row, inlineSpan) {
  const el = document.createElement('div');
  el.className = 'diff-row ' + row.type;

  const gutter = document.createElement('div');
  gutter.className = 'gutter';
  const oldCell = document.createElement('span');
  const newCell = document.createElement('span');
  oldCell.textContent = row.oldNo ?? '';
  newCell.textContent = row.newNo ?? '';
  gutter.append(oldCell, newCell);

  const sign = document.createElement('div');
  sign.className = 'sign';
  sign.textContent =
    row.type === 'add' ? '+' : row.type === 'del' ? '-' : row.type === 'meta' ? '' : ' ';

  const code = document.createElement('div');
  code.className = 'code';
  if (row.type === 'meta') {
    code.textContent = row.text;
  } else {
    code.innerHTML = withInline(row.text, inlineSpan);
  }

  el.append(gutter, sign, code);
  return el;
}
