/* ===========================================================================
   零依赖 Markdown 渲染器（预览用）
   ---------------------------------------------------------------------------
   只支持 README / VIBE.md 常见的子集：标题、段落、列表、表格、围栏代码、
   行内代码、引用、分隔线、链接、加粗/斜体。不支持的不渲染出标签，原样展示。

   安全纪律：全部走 DOM API 按节点拼装，**从不 innerHTML 任意拼接** ——
   文件内容是仓库里的一份普通数据，但仓库本身可能是别人给的，
   <img onerror>、<script> 这类东西不能因为「渲染个 README」就放进来。
   =========================================================================== */

function text(node, content) {
  node.appendChild(document.createTextNode(content));
  return node;
}

function code(node, content) {
  const el = document.createElement('code');
  text(el, content);
  node.appendChild(el);
  return node;
}

/** 行内格式：`code`、**bold**、*italic*、[text](url)。其余按纯文本。 */
function inline(target, raw) {
  let rest = raw;
  const pattern = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\([^)\s]+\))/;
  while (rest) {
    const m = rest.match(pattern);
    if (!m) { text(target, rest); break; }
    if (m.index > 0) text(target, rest.slice(0, m.index));
    const piece = m[0];
    if (piece.startsWith('`')) {
      code(target, piece.slice(1, -1));
    } else if (piece.startsWith('**')) {
      const b = document.createElement('strong');
      inline(b, piece.slice(2, -2));
      target.appendChild(b);
    } else if (piece.startsWith('*')) {
      const i = document.createElement('em');
      inline(i, piece.slice(1, -1));
      target.appendChild(i);
    } else {
      // 链接：只允许 http(s)，防 javascript:
      const mm = piece.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/);
      const url = mm ? mm[2] : '';
      if (/^https?:\/\//i.test(url)) {
        const a = document.createElement('a');
        a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer';
        text(a, mm[1]);
        target.appendChild(a);
      } else {
        text(target, piece);
      }
    }
    rest = rest.slice(m.index + piece.length);
  }
}

function isTableDivider(line) {
  return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
}
function splitRow(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
}

function render(md) {
  const root = document.createElement('div');
  root.className = 'md-preview';
  const lines = String(md ?? '').replace(/\r\n?/g, '\n').split('\n');

  let i = 0;
  let list = null; // 当前打开的列表容器
  const closeList = () => { list = null; };
  const flushBlank = () => {};

  while (i < lines.length) {
    const line = lines[i];

    // ---- 围栏代码 ----
    const fence = line.match(/^\s*```(\w*)\s*$/);
    if (fence) {
      closeList();
      const lang = fence[1];
      const body = [];
      i += 1;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
        body.push(lines[i]); i += 1;
      }
      i += 1; // 收尾 ```
      const pre = document.createElement('pre');
      if (lang) pre.dataset.lang = lang;
      const c = document.createElement('code');
      text(c, body.join('\n'));
      pre.appendChild(c);
      root.appendChild(pre);
      continue;
    }

    // ---- 标题 ----
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      closeList(); flushBlank();
      const h = document.createElement('h' + heading[1].length);
      inline(h, heading[2].replace(/\s#+\s*$/, ''));
      root.appendChild(h);
      i += 1; continue;
    }

    // ---- 分隔线 ----
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      closeList();
      root.appendChild(document.createElement('hr'));
      i += 1; continue;
    }

    // ---- 引用 ----
    if (/^\s*>\s?/.test(line)) {
      closeList();
      const q = document.createElement('blockquote');
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        if (q.childNodes.length) q.appendChild(document.createElement('br'));
        inline(q, lines[i].replace(/^\s*>\s?/, ''));
        i += 1;
      }
      root.appendChild(q);
      continue;
    }

    // ---- 表格 ----
    if (
      line.includes('|') && i + 1 < lines.length &&
      splitRow(line).length > 1 && isTableDivider(lines[i + 1])
    ) {
      closeList();
      const table = document.createElement('table');
      const head = splitRow(line);
      const thead = document.createElement('thead');
      const trh = document.createElement('tr');
      for (const cell of head) {
        const th = document.createElement('th');
        inline(th, cell);
        trh.appendChild(th);
      }
      thead.appendChild(trh);
      table.appendChild(thead);
      const tbody = document.createElement('tbody');
      i += 2;
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        const tr = document.createElement('tr');
        const cells = splitRow(lines[i]);
        for (let c = 0; c < head.length; c += 1) {
          const td = document.createElement('td');
          if (cells[c] !== undefined) inline(td, cells[c]);
          tr.appendChild(td);
        }
        tbody.appendChild(tr);
        i += 1;
      }
      table.appendChild(tbody);
      root.appendChild(table);
      continue;
    }

    // ---- 列表（无序/有序，只做单层，嵌套按扁平处理） ----
    const bullet = line.match(/^(\s*)[-*+]\s+(.*)$/);
    const ordered = line.match(/^(\s*)\d+[.)]\s+(.*)$/);
    if (bullet || ordered) {
      const wantTag = bullet ? 'ul' : 'ol';
      if (!list || list.tagName.toLowerCase() !== wantTag) {
        closeList();
        list = document.createElement(wantTag);
        root.appendChild(list);
      }
      const li = document.createElement('li');
      inline(li, (bullet || ordered)[2]);
      list.appendChild(li);
      i += 1; continue;
    }
    list = null;

    // ---- 空行 ----
    if (!line.trim()) { i += 1; continue; }

    // ---- 段落（连续非空行合并） ----
    const p = document.createElement('p');
    while (
      i < lines.length && lines[i].trim() &&
      !/^\s*```/.test(lines[i]) && !/^(#{1,4})\s/.test(lines[i]) &&
      !/^\s*>\s?/.test(lines[i]) && !/^(\s*)[-*+]\s+/.test(lines[i]) &&
      !/^(\s*)\d+[.)]\s+/.test(lines[i])
    ) {
      if (p.childNodes.length) p.appendChild(document.createElement('br'));
      inline(p, lines[i]);
      i += 1;
    }
    root.appendChild(p);
  }
  return root;
}

export function isMarkdown(path) {
  return /\.(md|markdown)$/i.test(path);
}

export { render };
