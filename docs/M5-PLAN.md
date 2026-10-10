# M5 里程碑：IDE 能力补齐（P0 五条）—— 任务清单与收口

| 项 | 内容 |
|---|---|
| 版本 | v1.1（**A–E 五条全部闭合**） |
| 日期 | 2026-09-29 |
| 前置 | M0/M1/M1.5/M2/M4 均已交付；本轮开工基线沿用 `.trae/documents/P0-FEATURES-PLAN.md` §9.1 的记载（agent-core 309 / 服务端 262 个用例） |
| 定位 | 补上「和 Trae 这类工具比，最直观能感知到的缺能力」：上下文引用、语义检索、Git 视图、编辑面、语言服务器 |
| 上游文档 | `.trae/documents/P0-FEATURES-PLAN.md`（任务表与决策点原文）、`docs/ARCHITECTURE.md`（ADR-011/012/013/014、§6「P0 能力补齐实际落地情况」） |
| 规模估算 | 不写工期；每条按 A→E 顺序交付，各自独立可验收 |

---

## 0. 为什么单开一个里程碑

盘点「界面上能直接看见、却没有」的能力时，剩下的欠账集中在五处：想@一个文件没有入口、
搜代码只能靠关键字、看不到仓库状态、打开文件只能读不能改、改完没有语法诊断。
这五条不是「效果调优」，而是**功能缺失**，且互相独立、可逐条验收，因此单独成篇。

治理面（M2）与运维面（M4）本轮**保持冻结**，避免三线作战。

---

## 1. 范围

| 条 | 工作流 | 内容 | 状态 |
|---|---|---|---|
| A | 上下文 | 上下文引用 `@file` / `@symbol` / `@folder` | ✅ 已交付 |
| B | 效果 | 语义检索（可选 embedding + 词法/依赖图回退） | ✅ 已交付 |
| C | 集成 | Git 集成（只读仓库视图：status / branches / diff） | ✅ 已交付 |
| D | 体验 | 编辑面：内联补全（Tab）+ 行内改写（Cmd+K）+ 逐 hunk + 保存 | ✅ 已交付 |
| E | 集成 | LSP 集成（诊断 / 跳转定义 / 重命名） | ✅ 已交付 |

### 明确不做（本轮）

- ❌ 向量索引落盘（进程内缓存即可，见 ADR-013）
- ❌ Git 自动提交（只生成 message 文本；提交仍走既有 REVIEW 级 `run_command`）
- ❌ 编辑面走审批浮层（用户本人动作不走审批，见 ADR-011）
- ❌ 多 Agent / Repo Wiki（仍归 M3）
- ❌ 为 CLI 单独做交互（桌面端 UI 是主交付面，CLI 只复用后端能力）

---

## 2. 任务分解

> 通用约束：保持「核心链路零强制依赖」红线（不新增运行时依赖）；审计只记 digest / 长度 /
> 计数（ADR-006）；写盘一律走 `plan_patch` / `plan_overwrite` + `commit_plan`，不新增
> 写盘路径（ADR-004）。

### A 上下文引用（✅ 已交付）

| 子任务 | 内容 |
|---|---|
| A1 | `context/references.py`：引用**意图**（类型 + 值）→ 正文展开，经 `PathGuard` 读取 |
| A2 | `GET /api/refs?type=&q=&limit=`：`@` 候选列表，type ∈ file/symbol/folder，上限 50 |
| A3 | `POST /api/run` 新增 `refs` 字段；缺省/空数组时行为与不传**逐字节一致** |
| A4 | 形状非法一律 400（前端 bug 当场暴露）；解析失败**不阻断任务**，逐条降级为 `run.notice` |

**验收**：`@` 能列出候选；选中后任务提示里出现该文件正文；越权/敏感文件逐条降级不报错。
专项测试：`tests/test_refs.py`。

### B 语义检索（✅ 已交付）

| 子任务 | 内容 |
|---|---|
| B1 | `llm/embeddings.py`：OpenAI 兼容 `/embeddings`；未配 `VIBE_EMBED_MODEL` 则调用方**不构造** |
| B2 | `index/lexical.py`（词法倒排）、`index/depgraph.py`（依赖图） |
| B3 | `index/store.py`：索引门面，**进程内单例、不落盘**；签名一致则复用，否则重建 |
| B4 | `tools/semantic.py`：`semantic_search`；无 embedding 时回退纯词法 + 符号/依赖图 |
| B5 | `config.py`：`embed_model` / `semantic_max_files`，接 `VIBE_EMBED_MODEL` / `VIBE_SEMANTIC_MAX_FILES` |

**验收**：无 embedding 环境下检索仍可用（精度降一档）；重复检索命中缓存不重建。
专项测试：`tests/test_semantic.py`。

### C Git 集成（✅ 已交付）

| 子任务 | 内容 |
|---|---|
| C1 | `gitinfo.py`：**只读**仓库视图，固定 argv + `shell=False`；非仓库返回 `available=false` 而非报错 |
| C2 | `GET /api/git/status` / `/api/git/branches` / `/api/git/diff?ref=&path=&staged=` |
| C3 | `tools/git.py`：`git_status` / `git_diff` 两个只读工具 |
| C4 | 前端 Git 面板：仓库状态、分支、diff 查看 |

**验收**：非 git 工作区界面显示「不是 git 仓库」而不是内部错误；`path` 参数过 sandbox。
专项测试：`tests/test_gitinfo.py`。

### D 编辑面：补全 + 改写 + 逐 hunk + 保存（✅ 已交付）

**D-前置：可编辑面**

| 子任务 | 内容 |
|---|---|
| D1 | `host/ui/app.js` + `styles.css`：工具条新增「编辑模式」开关（`btn-edit-toggle`）。开启时把只读视图换成 `<textarea class="code-edit">`，保留独立行号 gutter 并随 scroll 同步。**只读视图仍是默认**，避免「打开文件就误编辑」 |
| D2 | `caretMirror(area, index)`：用隐藏的等尺寸镜像复刻 textarea 内容与样式，测量光标像素坐标以定位 ghost text；失败时回退为「光标处浮条」提示，不阻塞功能 |

**D-主体：补全与改写**

| 子任务 | 内容 |
|---|---|
| D3 | `config.py`：新增 `fim_enabled: bool = True`、`completion_max_tokens: int = 256`；`load()` 接 `VIBE_FIM` / `VIBE_COMPLETION_MAX_TOKENS` |
| D4 | `llm/openai_compat.py`：新增 `complete(prefix, suffix, max_tokens, fim=)`。**先试 FIM**（`/completions` + `suffix`），不支持则回退一次 `chat()`；能力探测结果用 `_fim_supported` 三态缓存（仅 400/404/405/422/501 永久降级）。两者都返回 `(text, backend)` |
| D5 | `host/session.py` 的 `complete()`：直接调模型，**不进 Agent 循环、不动消息历史、不写盘**；前置 `allow_write` + `check_read`；**运行中允许**；审计记 `path/prefix_digest/suffix_digest/chars/backend` |
| D6 | `POST /api/complete`：`{path, prefix, suffix, maxTokens?}` → `{text, backend, path, chars}`；非法路径 403，缺字段 400 |
| D7 | 新增 `edit/hunks.py`：`split_hunks(old, new)` 用 `difflib.SequenceMatcher.get_opcodes()`（`autojunk=False`）聚合 hunk；`apply_hunks(old, hunks, accepted)` 按 opcode 精确重组，**不解析 unified diff 文本**（避免解析歧义） |
| D8 | `session.rewrite()` / `rewrite_apply()` + `POST /api/rewrite` / `/api/rewrite/apply`：改写只算不落盘，返回 `token` + hunk 列表；落盘按勾选的子集走 `plan_overwrite` + `commit_plan`，审计策略标签 `hunk-subset` |
| D9 | 前端：ghost text（绝对定位半透明 div，Tab 接受 / Esc 取消）；Cmd/Ctrl+K 在选区上浮出改写条（输入指令 → 生成）；结果以 hunk 卡片渲染（每 hunk 一个 checkbox + diff），底部「应用选中」 |
| D10 | 新增 `tests/test_hunks.py`、`tests/test_completion.py` |
| D11 | `session.save()` + `POST /api/edit/save`：`expectedDigest` 乐观锁（文件打开时的指纹），磁盘已被改则 409，**绝不静默覆盖**；落盘前收检查点 + 记审计（`strategy="overwrite"`） |

**验收**：编辑模式输入触发补全、Tab 接受、Esc 取消；无 FIM 时回退 chat（`backend=chat`）；
Cmd+K 勾掉部分 hunk 只应用选中的；应用后改动面板与检查点可见；运行中 apply 409；
`expectedDigest` 不匹配被拒。专项测试：`test_hunks.py` / `test_completion.py`。

### E LSP 集成（✅ 已交付）

> 与 B（tree-sitter）同一范式：**零强制依赖 + 运行时探测降级**。装没装语言服务器是
> 环境问题，缺了就降级，绝不因为「本机没装 pyright」而让核心链路失败（ADR-012/ADR-014）。

**E-探测与客户端**

| 子任务 | 内容 |
|---|---|
| E1 | 新增 `lsp/servers.py`：服务器探测表（python→`pyright-langserver --stdio` / `pylsp`；js/ts→`typescript-language-server --stdio`；go→`gopls`；rust→`rust-analyzer`），`shutil.which` 判定，返回 `(available, argv, reason)`；**argv 只由常量 + which 结果拼成，绝不来自用户输入** |
| E2 | 新增 `lsp/client.py`：标准库 stdio JSON-RPC —— 按 `Content-Length`（**字节**）分帧、后台读线程 + 事件配对、`initialize`/`initialized` 握手、`didOpen`/`didChange`、收 `publishDiagnostics`、`definition`、`rename`、`shutdown`/`exit`；`cwd=workspace`、`env=scrub_env()`、`shell=False` |
| E3 | 新增 `lsp/manager.py`：按 `(工作区, 语言)` 缓存实例；**空闲 `lsp_idle_seconds` 自动 shutdown，不常驻**；`atexit` 兜底清理；崩溃**自动重启一次**，再失败即标记该语言不可用（不再 spawn） |

**E-配置与会话**

| 子任务 | 内容 |
|---|---|
| E4 | `config.py`：新增 `lsp_mode: str = "auto"`（auto/on/off）、`lsp_idle_seconds: int = 300`；`load()` 接 `VIBE_LSP` / `VIBE_LSP_IDLE`，非法取值启动前报错 |
| E5 | `host/session.py` 五个方法 + `host/server.py` 五个端点：`/api/lsp/status`、`/api/lsp/diagnostics`、`/api/lsp/definition`、`/api/lsp/rename/preview`（逐文件 diff，**不落盘**）、`/api/lsp/rename/apply`（按服务端缓存 token 落盘）。`lsp_mode=off` 或探测不到时一律 `{available:false, reason}`，同步 `host/protocol.py` 命令表 |
| E6 | 新增 `tools/diagnostics.py`（`LSP_DIAGNOSTICS_SPEC`，`Risk.SAFE`，**仅 LSP 可用时注册**）：让模型改完代码先看静态诊断，省掉一轮「跑测试→报错→再改」 |
| E7 | 前端：只读视图 gutter 诊断标记（error/warning 着色 + hover 原文）、编辑面 `F12` / Ctrl+Click 跳转定义、`F2` 重命名走 diff 审阅浮层（复用 hunk 卡片）+ 一键应用 |
| E8 | 新增 `tests/test_lsp.py`：内置**假语言服务器**（`sys.executable` 起的子进程）逐条钉住分帧、生命周期、降级；任何装了 Python 的机器都能跑，不存在「CI 上被跳过」的测试空洞 |

**验收**：装了对应语言服务器时诊断/跳转/重命名可用，重命名落盘前收检查点并记审计；
`lsp_mode=off`、未装服务器、服务器崩溃三种情况下均**只降级不报错**；空闲实例被回收、
退出后无残留进程。专项测试：`test_lsp.py`（40 例）。

**边界**：语言服务器只在本机跑，**不含远端服务器模式**（ADR-014）；语言服务器进程只连本机
标准输入输出，不做任何网络连接。

---

## 3. 里程碑完成定义（DoD）

- [x] A/B/C/D/E 五条全部交付，且各有独立测试：`tests/test_refs.py` / `test_semantic.py` /
      `test_gitinfo.py` / `test_hunks.py` + `test_completion.py` / `test_lsp.py`
- [x] 全量测试数**只增不减**：agent-core 309 → **467 用例（466 通过 / 1 跳过）**；
      服务端 **262 用例（260 通过 / 2 跳过 / 338 subtests）**，与开工基线的收集数**完全一致** ——
      开工基线的写法「262 通过 / 2 跳过」把**收集数**误写成了通过数，本轮复核确认为
      同一批用例（D 与 E 两轮只在 `test_chain_compat.py` 的 `_sample_locals()` 里**增加样本数据**，
      新增样本不构成新用例，故用例数不变；subtests 由 323 增至 338）。
      两侧相较开工基线均无既有用例变红
- [x] 反向验证已做（D）：临时在 `apply_hunks` 里忽略 `accepted` → `test_hunks` 子集用例
      **4 项变红**（已还原）
- [x] 反向验证已做（E）：临时让 `lsp_mode=off` 仍然返回诊断 → `test_lsp` 的降级用例
      `test_mode_off_degrades_without_spawning` **变红**（已还原）
- [x] 治理面 E2E：`scripts/e2e_governance.py` **8 档全绿**（含上传 4 条 `edit.*` 事件、
      客户端链头一致、退出无残留进程）
- [x] 编辑面 HTTP 级活体冒烟：读文件返回 `digest`；补全 200 且 `backend=chat`（mock 无 FIM）；
      越权 403；改写预览拿到 token + hunk；只勾 1 块应用后正文变化；空 accepted 不改动；
      `expectedDigest` 不匹配 409；四类 `edit.*` 审计均落盘且不含代码正文
- [x] LSP HTTP 级路由冒烟（`test_lsp.py::LspHttpTests`）：`/api/lsp/status` /
      `/api/lsp/diagnostics` 正常返回；缺参与非整数位置参数 400；重命名预览 → 应用
      round-trip 后正文确实变化
- [x] `docs/ARCHITECTURE.md` 新增 ADR-011 / ADR-012 / ADR-013 / **ADR-014**，并补记
      §6「P0 能力补齐实际落地情况」（含 E）
- [x] 两份 README 补「环境变量 ↔ 代码字段」与「新端点契约」对应表（含 `VIBE_LSP` /
      `VIBE_LSP_IDLE` 与五个 `/api/lsp/*` 端点）
- [x] E（LSP）—— **已交付**，见 §2 的 E 任务表

---

## 4. 实测测试数（2026-09-29 收口）

| 套件 | 开工基线（P0 计划记载） | 收口实测 |
|---|---|---|
| agent-core | 309 个用例 | **467 收集 = 466 passed / 1 skipped / 54 subtests** |
| 服务端 | 262 个用例 | **262 收集 = 260 passed / 2 skipped / 338 subtests** |
| e2e_governance | 8 档全绿 | **8 档全绿** |

新增专项用例：`test_refs.py` / `test_semantic.py` / `test_gitinfo.py`（A/B/C，合计 88）、
`test_hunks.py` / `test_completion.py`（D，合计 29）、`test_lsp.py`（E，合计 40）。

---

## 5. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| FIM 探测把「网络抖动」误判为「不支持」 | 补全永久退化为 chat，质量下降 | 只有 400/404/405/422/501 判永久降级；网络类错误不写缓存，下次再试（ADR-012） |
| 编辑面与 Agent 同时写同一文件 | 用户改动被覆盖或反之 | 落盘前收检查点 + `edit/save` 的 `expectedDigest` 乐观锁；`rewrite/apply` 与 `edit/save` 运行中 409 |
| hunk 子集重组后行尾/缩进漂移 | 静默改坏格式 | `apply_hunks` 基于 opcode 重组而非文本 diff；`TextDoc` 负责换行/BOM/末尾换行往返保真，并用专项用例钉住 |
| 补全把读不到的文件正文喂给模型 | 越权读取 | `complete()` 前置 `check_read`，与「界面能打开的文件」同一口径 |
| 编辑面把人的动作塞进审批 | 稀释真正该审的 Agent 高危操作 | 编辑面不走审批浮层（ADR-011），风险改由「跑同一套引擎 + 留痕 + 运行中互斥 + 乐观锁」承担 |
| 语言服务器常驻 → 用户机器上一串孤儿进程 | 内存/句柄泄漏，排障时看不出用途 | 空闲 `lsp_idle_seconds` 自动 shutdown（默认 300s）+ `atexit` 兜底；崩溃自动重启**一次**，再失败即标记不可用、不再 spawn（E3） |
| 诊断是「服务器主动推」的通知 | 按行读帧会把正文里的换行当成消息边界，症状是「偶尔解析失败」 | 严格按 `Content-Length`（**字节**）分帧，且必须有后台读线程 —— 否则「等响应」与「先收通知」互相死锁；假语言服务器把这三点钉死（E2/E8） |
| 改完文件后拿到改动前的旧诊断 | **不报错、只是错**，用户看着过期诊断改代码 | `didChange` 后等「比最后一次同步更新的一份 publish」；超时则回退到已有结果，不抛异常（E2 的 `wait_diagnostics`） |
| 重命名跨文件落盘「改一半」 | 代码库处于半改状态，比不改更糟 | 预览阶段算好逐文件编辑并缓存 token；apply 时**先整批校验**（`check_write` + 文本指纹一致）再逐文件落盘，冲突整体 409（E5） |

---

## 6. 补记

### D 阶段落地细节（2026-09-28）

- **`rewrite` 的 `token` 是落盘唯一凭据**：`/api/rewrite/apply` 按 token 取**服务端缓存**的
  改写结果，前端回传的正文一律不被信任 —— 否则伪造整段内容就能绕过「逐块勾选」语义。
- **`complete` 有意不设 409**：它只产文本、不写盘、不与写盘路径争用，任务跑着时补全反而是
  想要的行为。这与另外三条端点不同，需逐条分辨而不能一刀切。
- **前后端契约同步**：客户端侧 `governance/vocabulary._RULES` 与服务端侧
  `vibe_server/redaction._SCHEMA` 必须同步增删，两端对齐由 `test_chain_compat.py`
  的双向核对钉住。

### E 阶段落地细节（2026-09-29）

- **位置口径**：接口层 `line` 是 **1 基**（与 gutter、`rewrite` 的 `startLine` 一致），`char`
  是 **0 基 UTF-16 列**（LSP 原生口径）。内部转 LSP 时才 `line - 1`；这一处不一致必须
  只存在于一个地方，否则每加一个端点就多一个差一行的地方。
- **重命名是「用户本人的动作」**：与编辑面同一判据（ADR-011），**不走审批浮层**，
  但落盘前收检查点、记审计、运行中 409；apply 只认**服务端缓存的 token**，前端回传的
  编辑内容一律不信任。
- **`wait_diagnostics` 等的是「与当前内容对应的一份」而不是「第一份」**：早期实现里
  缓存命中即返回，于是改完文件再问诊断会立刻拿到改动前的旧结果 —— 不报错、只是错。
  现在以 `didChange`/`didOpen` 时刻的诊断序号为基线，等更新的那份；服务器压根不重推时
  超时回退到已有结果（宁可旧一点，也不能卡住界面）。
- **条件注册而非条件失败**：`lsp_diagnostics` 只在探测到语言服务器时才进工具清单
  （`build_default_registry(lsp=None)` 自动探测）。一个必然失败的工具会白烧弱模型一轮
  上下文相关测试因此对「装了 / 没装」两种情况都成立。
- **`tests/test_host.py` 的工具数断言改为机器无关写法**：核心 10 个工具必须 ⊆ 实际清单、
  无重复、总数 ≤ 11 —— 不再写死 `== 10`，否则「本机恰好装了 pyright」就会让用例变红。
- **前后端契约同步（E 新增三条事件）**：`tool.lsp_diagnostics` / `lsp.rename.preview` /
  `lsp.rename.apply` 同时登记在 `governance/vocabulary._RULES` 与
  `vibe_server/redaction._SCHEMA`，并在 `test_chain_compat._sample_locals()` 补样本 ——
  漏登记一处，`test_every_recorded_event_is_classified` 会直接把事件名报出来。
  新标识符名本地只存 `new_name_digest` + `new_name_chars`（ADR-006）。

架构侧口径见 `docs/ARCHITECTURE.md` §6「P0 能力补齐实际落地情况」；
ADR-011（用户直接编辑的边界）/ ADR-012（可选能力探测与降级总则）/ ADR-013（语义索引留在本地）
/ **ADR-014（语言服务器只在本机）** 见同文档 §5。