# 浏览器预览功能 —— 修改方案

> 状态：**P0 / P1-1 / P1-2 / P2 / P3 已全部落地并验收**（见 §10 / §11 / §12 / §13 / §14 落地记录）
> 编号说明：本方案初稿把「跨源预览帧」记作 ADR-038、「远端预览转发」记作 ADR-039。
> 定稿时 ADR-038 已被 M11（远程文件工作面）占用，故两条顺延为 **ADR-039 / ADR-040**。
> 范围：`packages/agent-core`（host 层 + UI 层）+ `apps/desktop`（Electron 壳）
> 前置阅读：`docs/ARCHITECTURE.md`（ADR-006 / ADR-011 / ADR-036）、`agent_core/host/protocol.py`

---

## 0. 结论先行

**预览功能已经存在**，不是从零新建。现有实现是 P1-11 的「静态预览 + 网页自检」，`tests/test_preview.py` 已有 315 行 / 24 个用例在守。

所以本方案真正要补的是**三处空白**，而它们各自的风险等级差别很大：

| # | 空白 | 影响 | 落档 | 是否触碰安全红线 |
|---|---|---|---|---|
| 1 | 预览**不会自动更新**（Agent 改完要手点刷新） | 日常体感最差的一条 | **P0** | 否 |
| 2 | 预览只服务静态文件，**跑不起真实应用** | 做 SPA / 前端项目时无法自验 | **P2** | **是**（动 CSP） |
| 3 | Agent 改动与预览**没有闭环** | 生成 → 看 → 改 断链 | **P1** | 轻度（P1-2） |
| 4 | **远端设备**上的应用无法预览 | M11 **之后的**延伸 | **P3** | 是（新增监听口） |

**推荐路径：先做 P0（零红线影响、收益最大），再按需推进 P1 / P2。** P2 与 P3 必须先过 ADR。

---

## 1. 现状盘点

### 1.1 已有能力（含代码位置）

| 能力 | 位置 |
|---|---|
| 静态预览 `GET /preview/<relpath>`：只读服务工作区网页资源 | `host/server.py:1990` `_serve_preview` |
| 预览专用 CSP（`connect-src 'none'` 焊死网络出口） | `host/server.py:138` `PREVIEW_CSP` |
| 预览 MIME 白名单 | `host/server.py:163` `_PREVIEW_MIME` |
| 单次响应体积上限 8 MiB | `host/server.py:191` `_PREVIEW_MAX_BYTES` |
| 目录请求回落该目录 `index.html` | `host/server.py:2025` |
| iframe 预览标签（设备宽度 / 刷新 / 新窗口） | `host/ui/app.js:1950` `openPreview` |
| 预览入口按钮（仅网页文件可见） | `host/ui/index.html:265` `#btn-preview`、`app.js:1933` `WEB_EXT` |
| 网页自检（截图 vs 基线，粗粒度分块色差） | `host/session.py:1830` `visual_check`、`POST /api/visual/check` |
| 离屏截图窄桥（只回传 PNG 字节） | `apps/desktop/src/main.js:64` `vibe:capture-page`、`preload.js:34` |
| 测试覆盖 | `tests/test_preview.py`（越狱 / `.vibe` 拒绝 / MIME / 体积 / 目录回落 / 分基线） |

### 1.2 现有约束（改动时必须尊重）

- 预览**不是** `/api/*`，故不查令牌，但 **Host/Origin 双校验照常**（`protocol.py:118`）。
- `connect-src 'none'` 是**刻意取舍**：注释写明「本功能定位是『看渲染结果』，不是跑应用」（`server.py:136`）。
- 截图只能由 **Electron 壳**产生（host 是 Python 进程，无法反向调 IPC）；浏览器直开时**如实回 `available:false`**，不留「点了必报错」的按钮（`app.js:2016`）。

### 1.3 顺手发现的一处既有缺陷（P0 一并修）

`GET /preview/site`（**无尾斜杠**）会回落 `site/index.html` 并返回 200，但 URL 停在 `/preview/site`，
于是页内相对资源（`<link href="app.css">`）解析到 **`/preview/app.css`** → 404。

- 根因：回落时改了目标文件，却没改 URL 层级。
- 为什么测试没抓到：`test_directory_falls_back_to_index`（`tests/test_preview.py:211`）只校验响应正文含「首页」，**没有校验相对资源能否解析**。
- 修法：目录请求 **301 到带尾斜杠路径**（`/preview/site/`），浏览器随即按正确层级解析相对路径。

---

## 2. 目标 / 非目标

**目标（按档）**

- **P0**：预览随文件变更自动刷新；预览面覆盖 SVG 与目录；修掉 1.3 的相对路径缺陷。
- **P1**：Agent 落盘改动后预览自动跟上；审批期可预览「改动后效果」（草稿预览）。
- **P2**：预览可指向本机 dev server（跑真实应用），且**不破隔离**。
- **P3**：预览可指向远程设备上的应用（SSH 本地转发）。

**非目标（明确不做）**

- ❌ 不引入无头浏览器 / 浏览器自动化（Playwright / Puppeteer / Selenium）—— 违反「零第三方依赖」与离线红线；截图能力继续由 Electron 壳提供。
- ❌ 不把工作区文件内容传到治理面（ADR-006 不变）。
- ❌ 不让预览页读到 `/api/*`（P2 靠**跨源隔离**达成，而不是靠放宽 CSP）。

---

## 3. 设计原则

1. **默认不改行为**：新增能力默认关；关闭时工具清单 / 请求体 / 预览响应与当前**逐字节一致**。
2. **不新增执行面**：预览保持只读。任何「起进程」（dev server / 端口转发）要么由用户手起，要么与 `run_command` 同级走 REVIEW + 审计。
3. **隔离优于限制**：能用跨源（同源策略）解决的，不去放宽 CSP；CSP 的放宽必须最小、可枚举、可审计。
4. **复用既有机制**：自动刷新复用 `IndexWatcher` 的轮询；远端转发复用 `remote/transport.py`；预览沿用 `/preview/` 的单向只读服务。

---

## 4. 分期方案

### P0 —— 让预览「活」起来（零红线影响，推荐先做）

#### P0-1 自动刷新

- **复用** `agent_core/index/watch.py` 的 `IndexWatcher`：它已有 `(mtime, size)` 快照、`interval` 轮询、`poll_once()`。
- **需补齐**：`poll_once()` 目前只回计数（`dict[str, int]`），要新增一个只读方法输出**变化路径集合**（不改它喂索引的既有行为）。
- **新增事件** `preview.changed`，由 `HostSession` 在 watcher 检出变化时经 `EventBus` 发出：
  - payload：`{paths: [...相对路径], count}` —— **只记路径，不含内容**（ADR-006）。
  - 为什么这里可以发事件：`bus.py` 明确是线程安全的（「Agent 线程写，HTTP 线程读」）；watcher 刻意不做回调，是怕在后台线程写坏审计哈希链（`watch.py:63`），而**发事件不写链**——两件事不冲突。
- **节流**：`interval` 本身已是天然节流，再叠加 ~300ms 合并窗口，避免连续保存刷屏。
- **门控**：**跟随 `index_watch`**（`session.py:386`）。收益：零新增轮询、零新增开关；`index_watch=off` 时不推送，前端如实显示「自动刷新未启用」。
  - 备选：独立开关 `VIBE_PREVIEW_WATCH`，默认 `False`。更贴「默认不改行为」，但要另起一套轮询或仍需依赖 `index_watch` → **推荐跟随 `index_watch`**。
- **前端**（`app.js openPreview`）：
  - 订阅 `preview.changed`，**命中本标签路径**（文件本身，或同目录下的 `.css/.js/.svg/.png` 等资源）才 reload。
  - 激活标签立即刷新；**后台标签**只标记「有更新」，切回时再刷（避免无谓重载）。
  - **保留滚动位置**：reload 前记同源 `scrollX/scrollY`，`load` 后恢复（不保留表单状态，UI 上说明）。
  - 工具条加「自动刷新」开关（默认开，状态存 localStorage），与现有 `刷新 / 新窗口 / 设备宽度` 并列。

#### P0-2 预览面扩展与缺陷修复

- `WEB_EXT = /\.(html?|xhtml)$/i`（`app.js:1933`）→ 抽出 `PREVIEW_EXTS` 并加入 **`.svg`**（`_PREVIEW_MIME` 已支持 `image/svg+xml`，**后端零改动**）。
- 资源管理器**右键目录** → 若存在 `index.html` 则「预览」。
- 修 1.3 的无尾斜杠相对路径缺陷（301 到带斜杠）。

### P1 —— 预览与 Agent 闭环

#### P1-1 落盘后自动跟上（零后端改动，价值高）

`run.finished` 已带 `changedFiles[]`（`protocol.py:39`）。前端在任务结束时：若其中含可预览扩展名、且已开着对应预览标签 → 自动刷新 + 状态闪一下「Agent 已改动」。

#### P1-2 草稿预览：审批期看「改动后效果」（**已落地**）

- **现状痛点**：审批时 `run.approval.preview` 给的是 unified diff，改动**尚未落盘**，无法用 `/preview/<path>` 看到改动后的渲染结果。
- **落地形态**：
  - 工具层：`ToolSpec.drafter`（与 `previewer` 同一纪律：只算不写、失败不外抛）+ `DraftFile{path,text,created}`；`edit_file` / `write_file` / `edit_files` 各提供一个，**只对浏览器渲染得了的文本资源**（`preview_media.DRAFT_SUFFIXES`）生成 —— 改 `.py` 时一次都不多算。
  - 循环层：草稿在**审批发起那一刻**算好并随 `ApprovalRequest.draft` 交给会话层。这样「看到的 diff」与「看到的渲染」必然描述同一个结果；若等用户点按钮再算，中间任何一次外部改动都会让两者分叉。
  - 会话层：`preview_draft(approvalId)` 发 token（同一审批幂等）、`draft_scope(token)` 读、`_revoke_drafts` 收；正文只在 `self._drafts` 内存表里。
  - HTTP：`POST /api/preview/draft` 只回 `{token, url, primary, paths, created}`（**不含正文**）；`GET /preview-draft/<token>/<relpath>` 服务内容。
  - 界面：审批弹窗多一个「预览效果」按钮（`payload.draftPaths` 非空才出现），点了开一个 `kind:'draft'` 标签 —— 它**不参与**自动刷新、**不参与**截图自检/基线，并带「草稿 · 未落盘」徽标。
- **必须钉死的不变量**（每条配一个测试，见 `tests/test_preview_draft.py`）：
  - 草稿**绝不写盘**（取完草稿，磁盘字节不变）；
  - token 内存态、**绑定 `approvalId`**、**审批解决即失效**（放行 / 拒绝 / 超时三条路径都撤销）；
  - 不可跨审批复用；未知 token 与已撤销 token 对外**同一条**答复（不给存在性探针）；
  - 响应带 **同一个 `PREVIEW_CSP`** + `no-store` + `no-referrer`，与静态预览同一条口径；
  - 审计只记 `preview.draft`（token 指纹 + 路径 + 字节数），**不记内容**（ADR-006）。

**与草案的差异：把「单次使用」换成「绑定审批 + 解决即失效」。**

草案写的是 token 单次可用。按字面实现会让页面**根本渲染不出来**：一个 HTML 文档必然引出
N 次请求（文档 + CSS + JS + 图片），第二次请求就 401/404，结果是「预览一张裸 HTML」。
真正的威胁不是「同一份草稿被看两次」，而是「审批已经结束，草稿却还能翻」——
撤销时机才是安全边界，所以改成：**有效期内可多次读、审批一了结立即整体作废**。

**同批改动的资源也必须是改后的。** `GET /preview-draft/<token>/<relpath>` 的路径是
**完整工作区相对路径**：命中草稿清单就用内存正文，否则回落磁盘读。这样
`index.html` + `app.css` 同批改动时不会出现「新结构配旧样式」—— 那比看不到预览更误导人。

### P2 —— 真实应用预览（**已落地**，ADR-039）

**阻塞点（现状）**

- `PREVIEW_CSP` 的 `connect-src 'none'`（`server.py:145`）→ 预览页自己的 fetch/XHR 全被拦；
- 父页 `CSP` 的 `frame-src 'self'`（`server.py:126`）→ 不能内嵌别的源。

**方案（推荐）：跨源帧**

- 预览面板的「来源」增加一档：本机地址 `http://127.0.0.1:<port>`（用户填，或从常见端口探测）。
- **跨源天然隔离**：预览应用即使跑满 JS，也**读不到** `/api/*`（同源策略拦跨源读取）
  → **无需**牺牲 `/preview/` 的 `connect-src 'none'`。
- **唯一需要放宽的是父页 CSP 的 `frame-src`**：`'self' http://127.0.0.1:*`
  —— **只环回**，不含任意主机、不含外部域。这条必须记 ADR + 配可枚举的断言测试。
- iframe 加 `sandbox="allow-scripts allow-same-origin allow-forms"`，**不含** `allow-top-navigation` / `allow-popups`（防跳转劫持与钓鱼）。
- **dev server 由用户自己启动**：host 不起进程 → **零新增执行面**。这与 `vibe:capture-page` 只接受 `http://127.0.0.1:PORT/`（`main.js:63`）正好一致，自检桥可继续复用。
- 审计：`preview.external` 只记端口与主机（`{port, host}`），不记路径与内容。
- **要写清楚的边界**：跨源帧里我们**拿不到**页面内部错误（读不到 `contentWindow.onerror`），错误可见性只对同源的 `/preview/` 有效。别在 UI 上承诺做不到的事。

**备选（不推荐）**

- 反代 dev server 到同源 `/preview-app/*`：同源化会破坏隔离，得再补凭据剥离与 CSP，收益不抵复杂度。
- host 代起 `npm run dev`：执行任意命令，风险等级跳变到 `run_command` 同级，必须 REVIEW + 审计 + 用户逐次确认 —— 可作为 P2 之后的独立条目。

### P3 —— 远端设备预览（ADR-040，已落地，见 §14）

- **SSH 本地转发**：`ssh -L <localPort>:127.0.0.1:<remotePort> user@host`，复用 `agent_core/remote/transport.py`（ControlMaster 复用、超时、`BatchMode`、主机指纹策略）。
- 预览面板「来源」加「远端」：`{connection, remotePort} → localPort`。
- **与截图桥天然兼容**：转发落在 `127.0.0.1` → `CAPTURE_URL_RE` 通过 → 自检可用。
- **硬约束**：只转发**环回**端口（远端 `127.0.0.1`，拒绝 `0.0.0.0` / 公网地址）；本地监听也只绑 `127.0.0.1`。
- **生命周期**：随预览标签关闭收转发；随会话结束收；断线重连；**孤儿进程要兜底清理**。
- 审计：只记连接名 / 端口 / 时间，不记内容（ADR-006）。

---

## 5. 改动清单（文件级）

| 文件 | 动作 | 说明 | 档 |
|---|---|---|---|
| `agent_core/index/watch.py` | 改 | `poll_once()` 旁增「变化路径」只读输出；**不改**索引喂入行为 | P0 |
| `agent_core/preview_media.py` | **新增** | 预览面共享口径：MIME 白名单、体积上限、可草稿扩展名、主文档排序。`/preview/`、`/preview-draft/`、工具层草稿三方**导入同一份** | P1-2 |
| `agent_core/preview_media.py` | 改 | 再担一份口径：`validate_external_preview_url` + `EXTERNAL_PREVIEW_HOST`（放这里是为了让 server 与 session 都能静态导入、不成环） | P2 |
| `agent_core/config.py` | 改 | `preview_external`（`VIBE_PREVIEW_EXTERNAL`，默认 `False`） | P2 |
| `agent_core/config.py` | 改 | `preview_remote`（`VIBE_PREVIEW_REMOTE`，默认 `False`；另需 `VIBE_REMOTE=1`） | P3 |
| `agent_core/remote/tunnel.py` | **新增** | 转发通道：进程生命周期、就绪探测、断线重建、引用计数、上限；`-L` 规格由 transport 拼死 | P3 |
| `agent_core/remote/transport.py` | 改 | `base_argv(extra_options=…)` + `forward_argv()` / `spawn_forward()`（**只环回**、`ExitOnForwardFailure`、不做 `-N`） | P3 |
| `agent_core/tools/base.py` | 改 | 新增 `DraftFile` + `ToolSpec.drafter` / `ToolSpec.draft()`（只算不写、失败不外抛、按扩展名过滤） | P1-2 |
| `agent_core/tools/editing.py`、`fs.py`、`multi_edit.py` | 改 | `_draft_edit` / `_draft_write` / `_draft_edit_files` 各挂到对应 spec | P1-2 |
| `agent_core/loop/agent.py` | 改 | `ApprovalRequest.draft`；审批前用 `spec.draft(ctx,args)` 算好（与 diff 同一时刻） | P1-2 |
| `agent_core/host/session.py` | 改 | watcher 变化 → `bus.publish("preview.changed", …)`；`preview_draft()` / `draft_scope()` / `_revoke_drafts()`；`run.approval` 带 `draftPaths[]`；`preview_tunnels/open/close` + 转发状态审计 + 会话收尾关转发 | P0 / P1-2 / P3 |
| `agent_core/host/server.py` | 改 | 目录请求 301；`POST /api/preview/draft`；`GET /preview-draft/<token>/<relpath>`；父页 CSP `frame-src` 放宽（门控）；`GET/POST /api/preview/tunnel` 与 `close` | P0 / P1-2 / P2 / P3 |
| `agent_core/host/protocol.py` | 改 | 补 `preview.changed`、`POST /api/preview/draft`、`/preview-draft/<token>`、`preview.external` 与 P2 CSP 变更说明 | P0 / P1-2 / P2 |
| `agent_core/config.py` | 改 | `preview_*` 门控字段（仅当采纳独立开关） | P0 |
| `agent_core/host/ui/app.js` | 改 | `PREVIEW_EXTS`、订阅 `preview.changed`、自动刷新开关、滚动保留、`run.finished` 触发、来源选择、**草稿标签 + 「预览效果」入口** | P0 / P1 / P1-2 / P2 / P3 |
| `agent_core/host/ui/index.html`、`styles.css` | 改 | 工具条按钮（自动刷新 / 来源）、状态 chip、**`#btn-draft-preview` 与草稿徽标** | P0 / P1-2 / P2 |
| `apps/desktop/src/main.js`、`preload.js` | 多数零改动 | `capture-page` 只认 `127.0.0.1`，P2/P3 天然兼容 | — |
| `docs/ARCHITECTURE.md` | 改 | ADR-039（跨源预览帧）、ADR-040（远端预览转发） | P2 / P3 |
| `docs/PREVIEW.md` | 新增 | 使用与安全说明（对齐 `docs/REMOTE.md` 的写法） | P1+ |
| `tests/test_preview.py` | 扩 | 见第 6 节 | P0 |
| `tests/test_preview_draft.py` | **新增** | 草稿预览的五条不变量 + 沙箱口径 + 同批资源（11 例） | P1-2 |
| `tests/test_preview_external.py` | **新增** | CSP 派生逐条比对 + 地址校验矩阵 + 审计只记端口 + 隔离不退化（36 例） | P2 |
| `tests/test_preview_tunnel.py` | **新增** | 端口矩阵 + `-L` 只环回 + 生命周期（真进程）+ 引用计数 + 上限 + 自清理 + 重连 + 端点门控（34 例） | P3 |
| `tests/fake_ssh.py` | **新增** | 测试替身：按 `-L` 建监听、按 stdin EOF 退出（这两条正是要验的行为） | P3 |

---

## 6. 测试计划（增量，不重复既有 24 例）

- **目录 301**：`/preview/site` → 301 → `/preview/site/` 且相对资源可解析（补上 1.3 的漏网）。
- **自动刷新事件**：
  - 改文件 → 收到 `preview.changed`，payload 只含路径、**不含内容**；
  - 连续改多次 → 合并为一条（节流生效）；
  - `index_watch=off` → **不推送**，且**不新增轮询线程**。
- **草稿预览**：token 单次可用、审批解决即失效、不跨审批复用；**工作区文件字节未被改动**；响应 CSP 与 `PREVIEW_CSP` 一致。
- **P2（已落地）**：父页 CSP 只放宽到环回；跨源帧**读不到** `/api/*`（断言测试 + 手工验收，见 §12）。
- **回归**：既有 `test_preview.py` 24 例 + 全量测试无回归。

---

## 7. 安全影响

| 档 | 影响 | 处置 |
|---|---|---|
| P0 | 无红线影响 | 新事件只带路径，不带内容 |
| P1-2 | 新增「内容来自模型、但不落盘」的响应面 | 钉死「一次性 + 审批绑定 + 不写盘 + 同 CSP」，逐条配测试 |
| P2 | **全项目唯一放宽 CSP 的地方** | 只放宽父页 `frame-src` 到环回；隔离靠 SOP；iframe sandbox 排除 top-navigation / popups；记 ADR + 断言测试 |
| P3 | 新增本机监听口与出站 SSH 转发 | 只绑 / 只转环回；凭据沿用既有 SSH 链路，不新增密钥存储；孤儿转发兜底清理 |

---

## 8. 决策点（需要拍板）

1. **自动刷新的门控**：跟随 `index_watch`（**推荐**）/ 独立开关默认关 / 默认开？
2. **是否做 P1-2 草稿预览**（审批期看改动后效果）？
3. **现在是否推进 P2**（要动 CSP + 立 ADR-039）？
4. **若做 P2**：dev server 由**用户手起（推荐）**还是 host 代起（需 REVIEW 门控）？

---

## 9. 验收标准

1. **未开启任何新门控时**：预览响应、事件流、工具清单与当前**逐字节一致**。
2. **自动刷新**：`index_watch=on` 时改文件 → 预览自动更新（≤ `interval` + 300ms）；`off` 时不推送、UI 如实提示；**不新增轮询线程**。
3. **既有 24 例 + 全量测试无回归**；新增用例覆盖第 6 节。
4. P2 跨源模式下预览页**不能**读到 `/api/*`（有验收记录）。

---

## 10. P0 落地记录（2026-10-19）

### 10.1 已落地（对 §4 P0 的逐条回填）

| 计划项 | 落法 | 位置 |
|---|---|---|
| P0-1 复用 `IndexWatcher` | 未新增只读方法，改用**构造参数 `on_change` 回调**（只收「变化路径」，不碰内容、不写链） | `index/watch.py`（`poll_once` 广播 `added+changed+removed`） |
| P0-1 `preview.changed` 事件 | 载荷 `{paths[], count, truncated}`：路径**去重排序**、上限 200 条，超限置 `truncated=true` 让界面全刷；**只记路径，不含内容**（ADR-006） | `host/session.py::_on_files_changed`、`host/protocol.py` |
| P0-1 节流 | **不叠 300ms 合并窗口** —— 一次 `poll_once` 本身就是天然合并窗口（同轮多文件只发一条） | `index/watch.py` 注释同口径 |
| P0-1 门控 | **跟随 `index_watch`**（采纳 §8 决策点 1 的推荐项）：零新增开关、零新增轮询；`VIBE_INDEX_WATCH=0` 时不推送、界面禁用开关并说明原因 | `host/session.py`（watcher 未建即无回调）、`host/ui/app.js`（`/api/index/status` 探测后置「自动刷新（未启用）」） |
| P0-1 前端订阅 | 命中「本文件或**同目录资源**」才重载；后台标签只标「有更新」，激活时（或开启开关时）补刷；reload 前记同源 `scrollX/scrollY`，`load` 后还原 | `host/ui/app.js`（`previewTouched` / `reloadPreview` / `refreshPreviewTabs`） |
| P0-1 工具条开关 | 「自动刷新」默认开，状态存 `localStorage`（`vibe.preview.auto`）；按钮由 `openPreview` 动态建，**未改** `index.html` | `host/ui/app.js` |
| P0-2 SVG | `WEB_EXT` 扩为 `/\.(html?\|xhtml\|svg)$/i`（**后端零改动**，`_PREVIEW_MIME` 本就含 `image/svg+xml`） | `host/ui/app.js` |
| P0-2 目录首页预览 | 文件树目录节点**右键** → 菜单首项恒为「加入对话」（文件/目录皆可，转成 `file` / `folder` 引用；远端节点给 `@remote/<连接>/<路径>`，见 REMOTE.md §6.6）；目录若带 `index.html` 再追加一项「预览首页」（树递归到底，子节点就地判定、不发额外请求） | `host/ui/app.js`（`openTreeMenu` / `addTreeRef` / `addRemoteRef`）、`styles.css`（`.tree-menu`） |
| P0-2 无尾斜杠缺陷（§1.3） | 目录请求缺尾斜杠 → **301 到带斜杠地址**，浏览器随即按正确层级解析相对资源 | `host/server.py::_serve_preview` |
| P1-1 落盘后自动跟上 | 随 P0 一并落地：`run.finished` 里含可预览扩展名且已开着对应标签 → 自动重载 | `host/ui/app.js` |

### 10.2 测试与验收

- `pytest tests/test_preview.py -q` → **30 passed**。
- 新增三组用例：**目录缺尾斜杠 301 + `Location`**（用不跟随重定向的 opener 直接看 301，证明 URL 层级真的对了，而非仅「最终拿得到首页」）；
  `preview.changed` **载荷契约**（去重排序 / 字段集合恰为 5 个、**无正文键** / 空变化不发 / 超限截断并置 `truncated`）；
  `IndexWatcher.on_change`（只在真变时回调 / 只给相对路径 / **订阅方抛异常被吞**）。
- 未启用任何新门控时：预览响应、事件流、工具清单与改造前**逐字节一致**（不新增门控、不新增端点）。
- **浏览器实测**（真实 UI，需先接入治理面以越过强制登录门）：文件树**右键 `site` 目录** → 浮层菜单出现「预览首页」→ 点击后在应用内打开该目录首页的预览标签，**iframe 成功渲染**（标题文字与 `app.css` 上色均正确）。
  这一条同时**实证了 P0-2 的 301 / 相对路径修复**：`/preview/site` 经 301 落到 `/preview/site/` 后，同级 `app.css` 才被正确解析 —— 若不做 301，样式会丢。
  注：**不含 `index.html` 的目录不弹菜单**这一支在本次测试工作区里无可用样本，**未验证**（如实标注，不假装通过）。

**全量回归**：`unittest discover -s tests` → **1153 tests OK / 14 skipped / 0 fail**（282s），无回归。

**端到端冒烟**（真实 `Session` + 真实 `EventBus`，不依赖 LLM）：

```
index_watch = True | interval = 2
watcher 已建 = True · 回调已接通 = True
poll 结果    = {'added': 0, 'changed': 1, ...}
收到事件      = {'type': 'preview.changed', 'paths': ['site/index.html'],
                'count': 1, 'truncated': False}
```

**前端路径匹配逻辑**：从 `app.js` 原文抽出 `previewRel/previewDir/previewTouched`
在 Node 里跑 13 条断言（自身命中 / 同目录 CSS·JS 命中 / 别目录不命中 /
近似文件名不命中 / Windows 分隔符 / `./` 前缀 / 根目录资源 / 空路径不误判），全部通过。
校验的是**真实代码文本**，不是另写一份实现。

### 10.3 前提条件（运维必须知道）

**自动刷新跟随 `VIBE_INDEX_WATCH`，而它默认是 `false`**（`config.py:201`，J3 的有意设计）。
也就是说：

- 不设该变量 → 不建 watcher、不发事件，预览标签订阅不到任何东西；
- 此时工具条的「自动刷新」会**自动禁用**并显示「自动刷新（未启用）」，悬停说明
  「文件监听已关闭（VIBE_INDEX_WATCH=0）」—— 这是刻意不留「点了没反应」的开关。
- 需要自动刷新时：`VIBE_INDEX_WATCH=1`（可选 `VIBE_INDEX_WATCH_INTERVAL`，下限 2s）。
  桌面端不注入该变量，所以**部署时必须显式设置**。
- 手点「刷新」与 `run.finished` 触发的重载**不受**此限制，始终可用。


### 10.4 与草案的差异（均为有意为之，理由见上表）

1. **`on_change` 回调** 取代草案的「新增只读方法」—— 同样的信息量、少一层 API，且自带「订阅方故障不杀轮询线程」的兜底。
2. **不叠 300ms 节流** —— `index_watch_interval`（默认 5s、下限 2s）已是天然节流，再叠一层只会让事件更晚到。
3. **目录预览用最小右键菜单**，而非引入通用菜单组件 —— 全 UI 此前**无任何菜单基建**，为一个条目引一套组件不划算。

### 10.5 仍待办（本轮不做）

- **P2 真实应用预览**（跨源帧 + 父页 `frame-src` 放宽到环回）：**已落地，见 §12**。
- **P3 远端设备预览**（SSH 本地转发）：**已落地，见 §14**（ADR-040）。

---

## 11. P1-2 落地记录（2026-10-19）

### 11.1 调用链

```
模型提交 edit_file / write_file / edit_files
      ↓  spec.preview(...)           → unified diff（审批人眼前那份）
      ↓  spec.draft(...)             → DraftFile[]（落盘后正文；只对网页类资源）
   ApprovalRequest{preview, draft}
      ↓  session._approve()          → 发布 run.approval（带 draftPaths[]），线程挂住
用户点「预览效果」
      ↓  POST /api/preview/draft {approvalId}
      ↓  session.preview_draft()     → 内存登记 token（同一审批幂等）
      ∅  无正文返回，只回 {token, url, primary, paths, created}
      ↓  GET /preview-draft/<token>/<relpath>
      ↓  session.draft_scope(token)  → 命中草稿清单 → 内存正文；否则回落磁盘
用户批准 / 拒绝 / 超时
      ↓  resolve_approval() 或等待返回 → _revoke_drafts(approvalId)
      ∅  token 立即失效，后续请求 404
```

### 11.2 五条不变量的落点

| 不变量 | 由谁保证 | 测试 |
|---|---|---|
| 草稿绝不写盘 | 工具层只调 `plan_patch` / `plan_overwrite` / `EditTransaction`（`snapshot=None`），**没有** `commit_plan` 调用 | `test_draft_serves_post_patch_html_without_writing_disk`、`test_sibling_asset_from_same_batch_uses_draft_text` |
| token 绑定单次审批 | `_drafts[token].approvalId`；`preview_draft` 只从 `_pending` 取；同审批幂等 | `test_second_request_returns_same_token` |
| 审批解决即失效 | `resolve_approval`（立即）+ `_approve` 等待返回后（含超时路径）都调 `_revoke_drafts` | `test_token_dies_with_approval`（反向验证：把 `_revoke_drafts` 改成空实现即变红） |
| 同 CSP 口径 | `_serve_preview_draft` 与 `_serve_preview` 用同一组响应头 | `test_draft_serves_post_patch_html_without_writing_disk` |
| 审计不记正文 | `audit.record("preview.draft", path, token=digest(token), files, chars)` | `test_audit_has_no_draft_body` |

### 11.3 用户可见的行为

- 审批弹窗里只有**能渲染成页面**的改动才出现「预览效果」按钮；改 `.py` 时按钮不存在
  （不是灰的，是不出现 —— 一个永远点不亮的按钮只会制造疑问）。
- 点它之后弹窗**收起**（与「先看看」同一去向），顶部审批横幅仍在，点它回到弹窗。
- 草稿标签带「草稿 · 未落盘」徽标，**不参与**自动刷新与截图自检：一份还没写下去的内容
  不该成为基线，也不该跟着磁盘变化刷新。
- 审批一了结，草稿标签自动关闭 —— 服务端 token 同时作废，留着一个必然 404 的标签没有意义。

### 11.4 已知边界

- **草稿只在审批窗口内存在。** 想要「审批后回看当时的草稿」做不到，也不打算做：
  那等于给未落盘内容一个长期保存的读取面，与 ADR-004 的落盘纪律冲突。
- **草稿不做运行时校验。** 页面里的 `<script>` 会执行（CSP 允许 `'unsafe-inline'`，
  网络出口仍被 `connect-src 'none'` 封死），但脚本报错读不到 —— 与 P2 的边界说明一致。
- **同源 iframe 这条边界继承自 P1-11，不是 P1-2 新增的。** 预览文档与主界面同源，
  因此它对 `window.parent` 是可达的；`/preview/` 早就是如此，草稿沿用同一处置。
  要收窄这条得给 iframe 加 `sandbox`（代价是 `'self'` 子资源全失效），属于独立议题。
- **`.vibe/` 不对外**：草稿面的磁盘回落对 `.vibe` 与 `/preview/` 同一条拒绝规则。

---

## 12. P2 落地记录（2026-10-19）

### 12.1 调用链

```
用户在预览标签点「本机应用」→ 输入 http://127.0.0.1:5173
      ↓  GET  /api/preview/external            → {enabled}（入口亮不亮）
      ↓  POST /api/preview/external {url}
      ↓  validate_external_preview_url(url, host_port)
      │     只收 http://127.0.0.1:<port>[/path][?query]
      │     拒绝 https / localhost / 外部域 / userinfo / .. / 本应用自身端口
      ↓  audit.record("preview.external", port, host)   ← 只记端口
      ∅  {ok, url, port}（不含任何页面内容）
      ↓  openExternalPreview(url, port)
         iframe sandbox="allow-scripts allow-same-origin allow-forms"
                referrerpolicy="no-referrer"
         标签 kind='external' · 徽标「外部应用 :5173」
```

**host 在这条链上只做两件事：校验地址、留一条审计。** 不起进程、不开端口、不转发字节 ——
dev server 是用户自己跑的那个，页面内容从头到尾没经过内核。

### 12.2 六条不变量的落点

| 不变量 | 由谁保证 | 测试 |
|---|---|---|
| 默认关时界面 CSP **逐字节不变** | `_page_csp()` 在门控关时原样返回 `CSP` | `test_page_csp_is_byte_identical_to_before` |
| 放宽**只有一条指令** | `csp_with_external_preview()` 对 `CSP` 定点替换，不手抄第二份 | `test_only_frame_src_is_relaxed`、`test_page_csp_relaxes_exactly_one_directive` |
| 派生失败时 **fail closed** | 定位不到 `frame-src` 就返回未放宽的 `CSP` | `test_derivation_fails_closed_when_marker_missing` |
| 校验放行集合 ⊆ CSP 放行集合 | 只认 `127.0.0.1` 字面量（`localhost` 明确拒绝） | `test_rejects_localhost_alias`、`test_rejects_lookalike_host`、`test_rejects_foreign_host` |
| 跨源隔离的前提不被绕过 | 拒绝等于本应用端口的地址（否则变同源 iframe） | `test_rejects_own_port`、`test_rejects_own_port_with_400` |
| 静态预览的出口封锁未被削弱 | `/preview/` 仍发 `PREVIEW_CSP`（`connect-src 'none'`） | `test_static_preview_response_still_locks_egress` |
| 审计不记内容 | 只记 `port` / `host`，不记路径与 query | `test_audit_records_port_only` |

**反向验证**（两条都做过，改完即还原）：

1. 把 `csp_with_external_preview()` 改成 `return CSP`（模拟"忘了放宽"）→
   `test_only_frame_src_is_relaxed` 与 `test_page_csp_relaxes_exactly_one_directive` 变红。
2. 去掉 `port == host_port` 判断 → `test_rejects_own_port` 与
   `test_rejects_own_port_with_400` 变红。

### 12.3 与草案的差异

1. **不做「常见端口探测」**（草案曾列为可选）。探测等于替用户猜服务，且会在本机产生
   一串无意义的连接尝试 —— 与本项目"不新增执行面/网络面"的取向相反。改为**手填 + 服务端校验**。
2. **不支持 `localhost`**。草案正文写的是 `127.0.0.1`，这里按字面收窄并写进校验函数的
   文档串：`localhost` 解析取决于 hosts 文件，而 CSP 里只列了 `127.0.0.1` 字面量 ——
   放行它就会造出"填得进、渲染不出来"的落差。要放宽必须同时改 CSP 与校验两处，
   那就成了两条并列的放行项，不再是"一个主机字面量"。
3. **不接截图自检**（草案写「自检桥可继续复用」）。桥技术上可用（`capture-page` 只认
   `127.0.0.1`），但**不接**：一个跑在别人进程里、随时在变的外部页面不该成为基线；
   而且"自检通过"给不出承诺 —— 我们读不到它的 `onerror`。这属于后续独立议题。
4. **`sandbox` 比草案再收窄两项**：除草案已排除的 `allow-top-navigation` / `allow-popups`，
   还排除了 `allow-modals`（防 `alert` 卡住界面）与 `allow-downloads`。

### 12.4 已知边界（都写在界面上，不假装）

- **读不到外部页面的内部错误**：跨源下 `contentWindow.onerror` / 标题一律拿不到。
  错误可见性只对同源的 `/preview/` 有效。
- **不参与自动刷新**：dev server 自带 HMR，强行 reload 只会打断它的状态。
  工具条只留「刷新 / 新窗口 / 设备宽度」。
- **入口在预览面板里**：必须先打开一个网页文件的预览标签才能看到「本机应用」按钮。
  工作区没有任何 `.html` 时没有入口 —— 这是本轮的范围边界，不是遗漏。
- **只 `http`、只 `127.0.0.1`**：https 自签证书在 iframe 里只会白屏，放行它只会制造困惑。
- **只用环境变量开**：`VIBE_PREVIEW_EXTERNAL=1`。**没有**接到设置页 —— 往
  `/api/settings` 加字段会改既有响应契约，而这条门控的受众是部署方，不是日常使用者。

### 12.5 验收

- `python -m unittest tests.test_preview_external -v` → **36 passed**。
- **全量回归**：`unittest discover -s tests` → **1202 tests OK / 14 skipped / 0 fail**（289s）。
  用例数正好是上轮 1166 + 本轮 36，无回归。
- 未开启门控时：界面 CSP 与改造前**逐字节一致**，`POST /api/preview/external` 403，
  审计无 `preview.external` 记录，`/preview/` 响应不变。
- **真实浏览器验收**：已在 §13 补齐（Chromium 内核实测 iframe 真实渲染、跨源隔离、
  默认态对照）。

## 13. P2 真实浏览器实测（2026-10-19）

§12.5 留了一条「未实测」：iframe 到底能不能把本机应用渲染出来。本节把它补上，
并顺带把「跨源隔离」从一个设计论断变成一条可复现的观测。

### 13.1 为什么要单独做这一轮

单元测试能验的是**我方代码的意图**：CSP 头里有没有 `frame-src`、校验函数收不收某个
地址、端点回 200 还是 403。它验不到的是**浏览器的裁决** —— 一段 `frame-src` 写错了
引号、`sandbox` 少一个授权、dev server 因为 X-Frame-Options 拒绝被嵌，测试全绿而
用户看到的是一片白。这一轮就是去拿浏览器的裁决。

### 13.2 环境（全部本机，互不干扰）

| 角色 | 端口 | 启动方式 |
|---|---|---|
| 工作台（门控**开**） | 8791 | `VIBE_PREVIEW_EXTERNAL=1 VIBE_INDEX_WATCH=1 python -m agent_core.host -w <ws> --port 8791 --no-open --print-url` |
| 工作台（门控**关**，对照组） | 8792 | 同上，去掉 `VIBE_PREVIEW_EXTERNAL` |
| 被预览的应用 | 5173 | `python -m http.server 5173 --bind 127.0.0.1`（目录内自备一个会写 `document.title`、会试探父页与 `/api/health` 的页面） |
| 治理面 | 8797 | `python -m vibe_server --data-dir <dir> bootstrap --email … --password …` 后 `serve` |

两条**必须先知道**的前提，否则会得到假结论：

1. **强制登录门**：未接入治理面时 `#login-gate` 全屏遮住工作区。DOM 断言照样能过
   （元素在浮层下面，`querySelector` 找得到），但截图全是登录框。必须先用 owner 账号
   登录，或接受"这次断言有效、视觉证据无效"。
2. **端口可能被旧进程占着**：本机 8787 上躺着一个几天前的 `vibe_server`。新 `serve`
   因为端口冲突没能真正接管，而登录请求打到了那套**另一个数据库**上 —— 现象是
   「账号密码明明对、却 401」。`Get-NetTCPConnection -LocalPort <p>` 先看 pid 和
   启动时间，比盯着代码猜便宜。

### 13.3 怎么驱动浏览器

本机没有 `agent-browser`，装一次要拉 500MB Chromium；改用 **Node 22 内置的
`WebSocket` 直连 CDP** 驱动系统自带的 Edge：

- 零安装，脚本自包含；
- 关键一点：**跨源只挡页面 JS，不挡 DevTools 协议**。父页 `contentDocument` 是
  `null`，而 `Page.createIsolatedWorld({frameId})` 能进子帧读 `document.title` 与
  正文 —— 这正是"跨源"这条论断需要的观测面。

### 13.4 结果

门控开：**32 条断言全过**；门控关：**6 条全过**。其中真正"只有浏览器能回答"的几条：

| 观测 | 结果 |
|---|---|
| 子帧真的渲染出 dev server 的文档 | `document.title === "DEV-SERVER-5173"`，正文 221 字符 |
| 子帧的源 | `http://127.0.0.1:5173`（与父页 8791 不同源） |
| 子帧读父页 DOM | `SecurityError` |
| 子帧读 `/api/health` | `TypeError`（跨源 + 对方不发 CORS） |
| 父页读子帧 | `contentDocument` 为 `null`，取 `.title` 抛 `TypeError` |
| `sandbox` 实际取值 | `allow-scripts allow-same-origin allow-forms` |
| `referrerpolicy` | `no-referrer` |
| 控制台 | 无 `frame-src` 违规 |
| 非法地址（`localhost:5173`） | 就地吐司报错，不新开标签 |
| 同一地址重复打开 | 标签数 3→3，页面里只有 1 个外部 iframe |
| 门控关时 | 按钮常驻禁用、提示写明 `VIBE_PREVIEW_EXTERNAL=1`、点了不展开输入条、直接 POST 仍 403 |
| 未决态（请求被 `Fetch.enable` 扣住） | 按钮保持禁用 —— **fail closed 是被观测到的，不是注释里的承诺** |

**最有价值的一条对照**：改一次工作区里的 `style.css`，然后分别数两个帧的
`Page.frameNavigated`：

```
静态预览帧（同源）  1 → 2   ← 自动刷新真的触发了
外部预览帧（跨源）  1 → 1   ← 没有被连带重载
```

没有左边那半，右边"没变"什么也证明不了（可能监听压根没跑）。这就是为什么这类
"不参与 X"的断言必须配一个正向对照。

### 13.5 还没覆盖的

- **只测了 Chromium**（Edge）。Firefox / Safari 对 `sandbox` 与 CSP 的实现差异没验。
- **没测 Electron 壳**：桌面端走 `capture-page` 离屏截图那条路，与这里直连 CDP 不同。
- **没测 dev server 回 `X-Frame-Options: DENY` 的情形**：那时是对方拒嵌，我方无从干预，
  页面会空白 —— 属于"如实失败"，但本轮没有在界面上给出更明确的提示。
- 端点矩阵里"端口越界 / 非字符串 / 空串"这些是 HTTP 层断言，已由 §12 的单元测试覆盖。

### 13.6 复现

```bash
# 1) 被预览的应用
cd <dev-app> && python -m http.server 5173 --bind 127.0.0.1

# 2) 工作台（两个实例：开 / 关）
cd packages/agent-core
VIBE_PREVIEW_EXTERNAL=1 VIBE_INDEX_WATCH=1 python -m agent_core.host -w <ws> --port 8791 -v --no-open --print-url
python -m agent_core.host -w <ws> --port 8792 --no-open --print-url

# 3) 治理面（登录门要用；用独立 data-dir，别碰别人的库）
cd apps/server
python -m vibe_server --data-dir <tmp> bootstrap --email owner@corp.example --password 'Str0ng!Passw0rd'
python -m vibe_server --data-dir <tmp> --port 8797 serve

# 4) 驱动浏览器（脚本放仓库外，属一次性验证工具）
node cdp-test.js        # LAUNCH_URL / GOV 从环境变量传入
node cdp-gate-off.js
```

---

## 14. P3 落地记录：远端设备预览（2026-10-19）

### 14.1 一句话

预览的第四档来源：**把远端设备上的环回端口搬到本机环回**，再交给 P2 的跨源帧。
`ssh -L 127.0.0.1:<本机随机端口>:127.0.0.1:<远端端口>`，两端由代码钉死。

### 14.2 调用链

```
预览工具条「远端」 → 选连接 + 填远端端口
      ↓  POST /api/preview/tunnel {connection, remotePort}
   Session.preview_tunnel_open
      ↓  TunnelManager.open(conn, remotePort)      # (连接名, 远端端口) 去重 + refs++
      ↓  端口校验（1-65535，布尔/字符串注入一律拒）
      ↓  SSHTunnel.start()
      ↓     transport.spawn_forward()  →  ssh -T -o ExitOnForwardFailure=yes \
      ↓                                      -L 127.0.0.1:<lp>:127.0.0.1:<rp> \
      ↓                                      <target> 'cat >/dev/null'   （stdin=PIPE）
      ↓     等本地端口可连（READY_TIMEOUT=15s）→ state=up
      ↓  再走一遍 validate_external_preview_url（与 CSP 同一道闸）
      ↓  审计 preview.tunnel{action:open, connection, remote_port, local_port, ok, ms}
      ↓  返回 {id, url: http://127.0.0.1:<lp>/, localPort, …}
openExternalPreview(url, lp, {kind:'tunnel'})       # 跨源帧 + sandbox 三条白名单
      ↓  关标签 → POST /api/preview/tunnel/close → refs-- → （到 0）SSHTunnel.stop()
      ↓  页面卸载（刷新/关窗）→ pagehide + keepalive 批量 close
      ↓  会话结束 → TunnelManager.close_all()
```

### 14.3 五条不变量与落点

| 不变量 | 落点 | 测试 |
|---|---|---|
| `-L` 规格**只可能是环回**，用户只能给整数端口 | `transport.forward_argv()` 用 `FORWARD_HOST` 拼死两端；`validate_remote_port()` 拒布尔/小数/带分隔符的字符串 | `test_spec_is_loopback_only` / `test_ports_are_rendered_as_integers` / `test_rejects_injection_shapes` |
| **不用 `-N`**（否则 stdin EOF 自清理不成立） | `forward_argv()` 末尾是 `cat >/dev/null`；`spawn_forward()` 的 `stdin=PIPE` | `test_does_not_use_dash_n` / `test_port_disappears_when_parent_stdin_closes` |
| 本地监听**只绑环回** | `_free_loopback_port()` 绑 `127.0.0.1:0` | 生命周期用例里 `iframe.src` 必须是 `127.0.0.1` |
| **不新增 CSP 放宽点** | `_page_csp()` 在 `preview_external or preview_remote` 时复用同一条派生 | `test_csp_is_relaxed_by_the_same_single_directive` |
| 审计**只有元数据** | `preview.tunnel{connection, action, remote_port, local_port, ok, ms}`（失败补 `reason`，释放补 `closed`/`refs`） | `test_audit_records_ports_but_not_host` |

### 14.4 与草案的偏差点

1. **草案说「孤儿进程要兜底清理」，实现改成「不可能成为孤儿」。**
   记 pid 再回来杀是不可靠的（pid 会重用，且跨平台判身份很难）。
   改用「建会话 + 远端 `cat >/dev/null`」：父进程一死，内核关掉 stdin 管道，
   ssh 读 EOF 退出，本地监听随之消失。**这条链与父进程是正常退出还是 `kill -9` 无关。**
   测试用「关掉子进程 stdin」复现这件事，正因为它和 SIGKILL 在 fd 语义上是同一件事。
2. **新增「页面卸载收尾」**（草案没有）。
   关标签走 `closeEditor`，但**刷新页面不会** —— JS 上下文直接销毁，`onClose` 不执行。
   不补这一手的话，转发会活到会话结束：用户看不见它，却会撞上 4 条的上限，
   而且界面上没有任何入口能收掉。现在 `pagehide` + `keepalive` 一次性释放本页全部转发。
3. **同一 `(连接名, 远端端口)` 做引用计数**（草案未提）。
   两个标签看同一个远端服务时共用一条转发；关掉一个另一个不受影响。
   代价是「关标签」不等价于「立刻断」，`closed:false` 会如实回给界面。
4. **连接被改过就不复用**（草案未提）。
   `conn.key`（含 host/port/root/密钥/指纹策略）变了还复用旧通道，会让人以为
   「连的是现在这台」，实际打到上一台 —— 这条在 M11 里踩过一次同类问题。
5. **不提供「本地端口」「远端主机」两个输入框。** 那是草案里隐含的自由度，
   也正是 `-L` 的注入面。现在本地端口由内核分配、远端主机恒为环回。

### 14.5 测试与实测

- `tests/test_preview_tunnel.py`：**34 例全过**。
- 反向验证（改完即还原）：把 `FORWARD_HOST` 改成 `0.0.0.0` → `test_spec_is_loopback_only` 变红；
  把 `stdin=PIPE` 换成 `DEVNULL` → `test_port_disappears_when_parent_stdin_closes` 变红；
  删掉 `_page_csp` 的 `or preview_remote` → HTTP 层 CSP 断言变红。
- **真实浏览器实测（Edge + CDP）34 条断言全过**，其中只有浏览器能回答的：

| 观测 | 结果 |
|---|---|
| 子帧渲染的是远端那个应用 | `document.title === "REMOTE-APP-5188"` |
| 子帧的源 | 本机转发口 `http://127.0.0.1:59887`（**不是**远端 5188） |
| 子帧读父页 DOM | `SecurityError` |
| 子帧读工作台 `/api/health` | `TypeError` |
| 父页读子帧 | `contentDocument` 取不到（`TypeError`） |
| 徽标 | `远端 devbox:5188`，样式 `is-tunnel`（与本机应用区分） |
| 关标签后 | 名单清空 + 本机转发口不再监听 |
| 刷新页面后 | `pagehide` 收尾生效，旧转发口不再监听 |
| 非法端口（0） | 就地吐司「远端端口需为 1-65535 的整数」，不开标签 |
| 门控探测被 CDP 扣住时 | 「远端」按钮保持禁用（**fail closed 被观测到**） |

- 审计交叉验证：7 条 `preview.tunnel`，**不含主机名、不含页面正文**，哈希链连续。

### 14.6 已知边界

- **只测了假 ssh**：本机没有可连的远端主机，`tests/fake_ssh.py` 替代了 ssh 进程本身。
  它复刻了两条被依赖的行为（按 `-L` 建监听、按 stdin EOF 退出），而**没有**复刻
  握手/认证/指纹/复用 —— 那些仍由 `test_remote.py` 的 argv 断言覆盖，真机联调需另行验证。
- **`up` 不等于「远端服务在跑」**：`-L` 的本地监听先建，远端拒绝只在真正连上去时暴露。
  页面白屏时先看远端 dev server 起没起，别怀疑转发。
- **只支持 http**：与 P2 同口径（https 自签证书在 iframe 里只会白屏）。
- **远端需有 POSIX shell**：生命周期凭证是远端 `cat >/dev/null`。这不是 P3 新增的假设 ——
  M10/M11 的能力探测脚本本身就是 POSIX sh，Windows 远端本来就在降级路径上。
- **断线重连有界**：3 次退避（0.5/1/2s）后置 `down`，界面显示错误摘要；
  不做无限重连（远端没了会变成后台永动机）。
- **每个页面最多 4 条转发**；刷新即释放。这条上限是刻意的小 —— 它是「看一个 dev server」
  的功能，不是通用端口映射工具。
