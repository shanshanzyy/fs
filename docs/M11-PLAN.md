# M11 里程碑：远程设备文件工作面 —— 任务清单与方案

| 项 | 内容 |
|---|---|
| 版本 | v1.0（A–F 六阶段全部闭合；本文件为落盘正式版） |
| 日期 | 2026-10-16 |
| 前置 | M0–M10 已交付；`agent_core/remote/` 已落地「远程设备接入」（CLI + 8 个 Agent 工具）。开工基线：agent-core **992 passed / 2 skipped / 98 subtests**，服务端 **419 passed / 25 skipped / 574 subtests** |
| 定位 | **把「远程设备接入」从 CLI + Agent 工具，补成面向用户的远程文件工作面** —— 连接管理 / 远程文件树与查看 / 远程代码审查（diff）/ 远程编辑与保存。**在既有实现上补齐，不另起灶** |
| 口径修正 | 此前把它称作「**远程开发机**」—— **改掉**。它的语义是「**远程连接其他设备，并对该设备上的文件做读取、审阅、修改、查看与保存**」；本轮连 `docs/REMOTE.md` 与 ADR-036/037 的措辞一并改（决策内容不变） |
| 上游文档 | `docs/REMOTE.md`（既有能力说明书）、`docs/ARCHITECTURE.md`（ADR-003 零依赖 / ADR-004 结构化补丁 / ADR-006 审计只记元数据 / ADR-008 高危审批 / ADR-011 用户本人动作 / ADR-014 只连本机 / **ADR-036 / ADR-037 远程链路**）、`docs/M10-PLAN.md`（本文档模板） |
| 编号说明 | `docs/REMOTE.md` 自标 M10，与 `docs/M10-PLAN.md`（定时任务调度，ADR-035）同号。本文件按 `docs/` 官方序列取 **M11**，ADR 自 **ADR-038** 起（036 / 037 已被远程链路占用） |
| 已后置 | 「调试器与插件生态」方案已另存 `.trae/documents/调试器与插件生态-实施方案.md`，本轮**不做**，后续里程碑再回来 |
| 规模估算 | 不写工期；按 A→F 顺序交付，每阶段独立可回归、可验收 |

---

## 0. 调研结论：现状与缺口界定（**先划清，不重造轮子**）

### 0.1 已落地可复用（**本轮一律复用、不改语义**）

| 能力 | 位置 | 本轮怎么用 |
|---|---|---|
| **连接清单 + 落盘**：默认 `<workspace>/.vibe/remote.json`（`remote_config` 可覆盖），**原子写**（同目录临时文件 + `os.replace`）+ 权限收口 | `agent_core/remote/store.py` | 连接管理面**直接读写这份清单**，不新造配置文件、不新造落盘路径 |
| **连接模型**：`name / host / user / port / root / key / identity_file / proxy_jump / host_key_policy / connect_timeout / command_timeout / allow_write / allow_shell / note / created_at` | `agent_core/remote/connection.py` | UI 增删改的字段面 = 这份模型；`writable` / `executable` 是 UI 上的权限位 |
| **ssh 传输**：`BatchMode=yes` + `NumberOfPasswordPrompts=0` + 三态 `StrictHostKeyChecking` + `ControlMaster` 复用 + 跳板机；argv 数组直连、字段不许以 `-` 开头 | `agent_core/remote/transport.py` | **一行不改**；连接测试 / 探测复用同一实例（按 `conn.key` 缓存） |
| **远端操作面**：`read` / `write` / `edit`（**复用本地 `plan_patch` 三级匹配引擎**，含编码 / BOM / CRLF / 末尾换行保真）/ `list_dir` / `exists` / `grep` / `run_argv` / `capabilities` / `report` | `agent_core/remote/workspace.py` | 文件树、查看、保存、审查的**唯一入口**（模块头明写：所有远端访问必须先过 jail，收口在此） |
| **路径 Jail**：文本级 `posixpath` 归一化 + 前缀比对 + 空字节 / 逃逸检测，**加 realpath 二次确认**（能力不可用时列进「能力降级」报告，不假装安全） | `agent_core/remote/pathguard.py` + `workspace.py::_assert_realpath_inside` | 文件树 / 查看 / 保存**全部走它**；F 阶段反向验证③专门锁这条 |
| **写前本机留底**：改动前内容落 `<workspace>/.vibe/checkpoints/remote/<连接>/<会话>/<相对路径>`，存不下就**中止写入** | `agent_core/remote/workspace.py` 的写入路径（`_backup_before_write`） | **远程代码审查的数据源已经存在** —— 审查无需新数据源，只需把「留底 vs 远端当前内容」比对出来 |
| **8 个 Agent 工具**：`remote_connections` / `remote_probe` / `remote_read_file` / `remote_list_dir` / `remote_grep`(REVIEW) / `remote_edit`(REVIEW) / `remote_write_file`(REVIEW) / `remote_run_command`(REVIEW，只读子命令 SAFE)；内容一律 `wrap_untrusted` | `agent_core/tools/remote.py` | **不动**；只在 `VIBE_REMOTE=1` 时注册（`host/session.py`） |
| **界面保存的既有范式**：`POST /api/edit/save` = 用户编辑保存、`expectedDigest` **乐观锁**、**不经审批**、审计 `edit.save` | `host/protocol.py`、`host/session.py` | 远端保存**照此范式**（`expectedDigest` 换成远端文件的 sha256） |
| **UI / 事件 / 审计三件套** | `host/protocol.py`（事件表 + 端点表）、`host/ui/index.html`（11 个 `data-panel`）、`governance/vocabulary.py`（现有 71 个事件，含 `tool.remote_*`） | 新增端点与事件**同款登记**；新审计事件**三处同步** |

### 0.2 本轮真正的缺口

| # | 缺口 | 证据 |
|---|---|---|
| 1 | **连接管理只有 CLI** —— 加连接 / 改权限位（`allow_write` / `allow_shell`）/ 设为默认 / 测试连通性，全得回命令行 | `remote/cli.py` 是唯一入口；`protocol.py` 里 **`remote` 零命中**（无任何 `/api/remote/*` 端点） |
| 2 | **没有任何界面入口** —— 看不到远端文件树、打不开远端文件、看不到远端 diff | `host/ui/index.html` 的 11 个 `data-panel` 无 remote；`ui/app.js` 里除一处无关的 git 分支过滤（`kind !== 'remote'`）**零「远程」痕迹** |
| 3 | **没有会话级「当前连了哪条 / 远端浏览路径」状态** | `workspace.py` 只有 `transport_for(conn)` 的**传输缓存**与 `close_all_transports()`；没有「活动连接 + 浏览路径」这一层 |
| 4 | **没有「远程代码审查」** —— 改动前内容虽已留底，但**没有把留底与远端当前内容比出 unified diff** 的能力，也没有逐 hunk 审阅 | 全仓无 `remote_review*`；本地已有 M9-E 的 `review_changes`（只读自审），远端**没有对应物** |
| 5 | **守不住「假安全」的界面** —— 权限位（可写 / 可执行）目前只在 CLI 与工具报错里体现，界面上看不见 | `conn.writable` / `conn.executable` 只在 `workspace.report()` 里成文本 |

### 0.3 既有范式（**不新造轮子**）

- **远端编辑 = 结构化补丁**（ADR-004 在远端**完全保留**）：`远端内容 → 本地临时文件 → 复用本地 plan_patch（纯计算）→ 原子写回远端`，参数分离、临时文件 `finally` 删除 —— 界面保存**沿同一条链**。
- **原子写 + 权限收口**：`remote/store.py` 的连接清单写入 —— UI 的连接增删改**直接调它**。
- **乐观锁**：`/api/edit/save` 的 `expectedDigest` —— 远端保存同款，防「界面看到的内容与远端现状不一致时静默覆盖」。
- **门控默认关 + 逐字节一致**：`VIBE_REMOTE` 未设时工具根本不注册（既有纪律）—— 新端点与页签**挂在同一道门上**。

---

## 1. 范围

| 阶段 | 内容 | 风险 | 状态 |
|---|---|---|---|
| **A** | 基础与契约（会话级状态 `remote/session.py` + 审计 `remote.connection` / `remote.file` 三同步 + 口径修正文案 + `.env.example`） | 中 | ✅ 已完成 |
| **B** | 连接管理面（复用 `store.py` 读写 + 端点：增 / 改 / 删 / 设为默认 / 测试 / 权限位） | 中 | ✅ 已完成 |
| **C** | 远程文件面（文件树 / 只读查看 / 编辑保存：jail + 写前留底 + 乐观锁 + 返回 diff） | 高 | ✅ 已完成 |
| **D** | 远程审查面（本会话远端改动清单 + unified diff + 逐 hunk + `remote_review_changes` 只读自审工具） | 中 | ✅ 已完成 |
| **E** | UI（「远程设备」面板：连接列表 / 文件树 / 查看器 / diff 审查 / 编辑保存） | 中 | ✅ 已完成 |
| **F** | 测试与收口（两个测试文件 + 反向验证 + 全量回归 + 文档与 ADR-038 + 口径修正落盘） | 中 | ✅ 已完成 |

### 1.1 已定档的边界（用户已确认 + 红线推导）

1. **在既有实现上补齐，不另起灶**（用户已选）：传输、Jail、结构化补丁、留底、连接清单**一律复用**；本轮**只新增用户面**（会话层 + 端点 + 事件 + 面板 + 一个只读自审工具）。
2. **四块全做**（用户已选）：连接管理 / 远程文件树与查看 / 远程代码审查（diff）/ 远程编辑与保存。
3. **不新增门控**：新面**挂在既有的 `VIBE_REMOTE` 上** —— 未设时**无端点、无页签、无事件、无新工具**，行为与改造前**逐字节一致**。
4. **界面上的远端写 = 用户本人动作**（ADR-011）：与本地 `POST /api/edit/save` 同口径 —— 用户点「保存」就是显式决定，**不重复走 Agent 审批漏斗**；但仍**必须**满足三件事：① 走结构化补丁 / 受控覆盖（ADR-004）；② **写前本机留底**（存不下就中止）；③ `expectedDigest` 乐观锁 + 返回真实 unified diff 供界面展示。
5. **Agent 侧口径一字不改**：Agent 发起的远端读 / 写 / 执行**仍是原样**（写与执行恒 `REVIEW`，只读恒 `SAFE`，`remote_grep` 恒 `REVIEW`），审批漏斗与沙箱分析（`CommandGuard`）不动。
6. **审计只记元数据**（ADR-006）：新增 `remote.connection` 与 `remote.file` **只记** 连接名 / 路径 / 动作 / 字节数 / 增删行 / 指纹 / 权限位 / 是否留底成败；**远端文件正文、diff 正文、命令输出一个字符都不出机**（diff 只走端点响应与界面，供人审）。
7. **只连本机之外的那台设备，不经第三处**（ADR-014 沿用）：链路是 **本机 → 用户自己的设备**，SSH 走既有凭据与既有网络路径；**治理面依旧收不到任何代码**。
8. **不做远端写盘的扩大**：远端 `.vibe/` 一律禁写（既有 jial 规则），UI 保存**不新增**任何可写根。

### 明确不做（本轮）

- ❌ **远程调试 / 远程断点**（调试器整体后置，见已另存的「调试器与插件生态-实施方案」）
- ❌ **插件生态 / 扩展目录**（同上后置）
- ❌ **远端构建 / 远端测试面板**（既有 `remote_run_command` 工具够用；本轮不做「远端 CI 面板」）
- ❌ **新增传输协议**（SFTP / rsync / WinRM / RDP）：仍只用**系统 `ssh`**（ADR-003 零依赖 + ADR-036 决策 2）
- ❌ **密码认证 / 交互式输入通道**（ADR-036 决策；`BatchMode` + `NumberOfPasswordPrompts=0` 不动）
- ❌ **远端文件的双向同步 / 镜像 / 自动上传**：只做**按需读与按需写**，不做同步引擎
- ❌ **多连接并发工作面**（一次只活动一条连接；切连接 = 关旧开新）
- ❌ **远端二进制 / 图片预览**（只做文本类文件；二进制如实提示「不支持预览」）
- ❌ **改动前留底的自动清理 / 保留策略**（沿用既有：随编辑次数增长，不自动清理）
- ❌ **治理面参与**（不改 `apps/server/**`，除两个**必改**的审计词汇同步点）
- ❌ 远端路径的**符号链接跟随**（Jail 语义不动：越界即拒）

---

## 2. 任务分解

> 通用约束（全程适用）：核心链路**零强制依赖**（`dependencies` 保持 `[]`、前端零第三方库）；
> 新审计事件 `remote.connection` / `remote.file` **三处同批同步**
> （`agent_core/governance/vocabulary.py` 的 `_RULES` / `apps/server/vibe_server/redaction.py` 的 `_SCHEMA` /
> `apps/server/tests/test_chain_compat.py` 的 `_sample_locals()`），且审计调用点**写事件名字面量**
> （AST 扫描只认 `record("remote.file", ...)`）；`Config` 是 frozen dataclass，改字段用 `replace()`；
> **`VIBE_REMOTE` 未设时行为与改造前逐字节一致**。

### A 基础与契约（会话级状态 + 审计三同步 + 口径修正）

| 子任务 | 内容 |
|---|---|
| A1 | 新增 `agent_core/remote/session.py`：`RemoteSessions` —— **会话级状态层**（这是缺口 3 的正解）。持有 `active: RemoteConnection \| None`、`browse_path: str`（相对 root 的浏览位置）、`workspace: RemoteWorkspace \| None`、`changes: dict[str, ChangeEntry]`（本会话远端改动台账：`rel_path / abs_path / bytes / sha256 / backed_up / edited_at / action(read\|save\|write\|edit)`）。方法：`open(name) -> RemoteWorkspace`（建/换连接；**已有连接先关**，一次只活动一条）、`close()`、`active()`、`chdir(rel)`（浏览位置；走 `RemotePathGuard.check_list`）、`note_change(entry)`、`list_changes()`、`snapshot()`（给端点用的元数据快照）。**不新增传输实现** —— 一律经 `open_workspace()` 与 `transport_for()` |
| A2 | **口径修正（用户要求）**：`docs/REMOTE.md` 标题与全文措辞 —— 「远程开发机」→「**远程设备**」/「远程设备文件操作」；`docs/ARCHITECTURE.md` 的 **ADR-036 / ADR-037** 内文同款改措辞（**决策内容与理由一字不改**，只改称呼）；`config.py` 的注释与 `remote/__init__.py` 模块头的措辞同步。本轮**不落盘**（A 阶段只出改法清单，F 阶段统一落盘，避免与并行在途工作互相打脸） |
| A3 | 审计词汇**三同步**：新增两个事件 —— `remote.connection`（`connection: str` / `action: str`（`added`/`updated`/`removed`/`default`/`permissions`/`test`）/ `allow_write: bool` / `allow_shell: bool` / `ok: bool` / `ms: int`）与 `remote.file`（`connection: str` / `path: str` / `action: str`（`list`/`read`/`save`/`create`）/ `bytes: int` / `sha256: str` / `added: int` / `removed: int` / `backed_up: bool`），**全元数据、不含正文**。登记 —— ① `governance/vocabulary.py` `_RULES`（**`Rule.fields` 键序 = 上传名 → (本地名, 类型)**）；② `apps/server/vibe_server/redaction.py` `_SCHEMA`；③ `apps/server/tests/test_chain_compat.py` `_sample_locals()` |
| A4 | `host/protocol.py`：登记两条事件（`remote.state` / `remote.edit`，见 C/D 阶段）与新增端点表；`.env.example`：若需新增变量**一律不加**（沿用现有 `VIBE_REMOTE` / `VIBE_REMOTE_CONFIG` / `VIBE_REMOTE_DEFAULT`），仅在注释中写明「`VIBE_REMOTE=1` 同时打开远程工具与远程工作面」 |

**验收**：`VIBE_REMOTE` 未设时行为**逐字节一致**（无端点 / 无页签 / 无事件 / 无新工具）；`RemoteSessions.open()`
切换连接时旧连接被关（不泄漏 `ControlMaster` 套接字）；`snapshot()` 输出**不含任何文件正文**。

**红线核对**：零依赖；不新增传输 / 不新增可写根；两个新事件只记元数据、三处同步到位。

---

### B 连接管理面（复用 store + 端点）

| 子任务 | 内容 |
|---|---|
| B1 | `host/session.py` 门面：`remote_connections()`（清单 + 默认项 + 权限位 + **探测结论**（`shutil.which("ssh")` 与连接命名合法性））、`remote_connection_add(spec)`、`remote_connection_update(name, spec)`、`remote_connection_remove(name)`、`remote_connection_default(name)`、`remote_connection_permissions(name, *, allow_write, allow_shell)`、`remote_connection_test(name)`。**写路径直接调 `remote/store.py` 的原子写**（不新造配置读写） |
| B2 | **入参校验前移**：新增 / 更新连接时，字段校验（`host` 首字符字母数字、端口范围、`root` 必须绝对路径、`--` 开头字段一律拒）与 `errors.py` 的错误类型**在端点层就拦住**，错误信息用人话；**坏 spec 不落盘**（写前校验，对齐 MCP 配置的纪律） |
| B3 | `remote_connection_test` 复用 `RemoteWorkspace.report()`（连通性 + 环境 + **能力降级**），并记审计 `remote.connection(action=test, ok=?, ms=?)`；**测试不落盘、不改清单** |
| B4 | **权限位是「热」开关**：改 `allow_write` / `allow_shell` 后**立即生效**（下一次 `open()` 建 `RemoteWorkspace` 时读新值）；界面必须**如实显示**当前权限位，并在关闭时把「只读 / 禁执行」标在面板上（守缺口 5） |
| B5 | `host/server.py` 端点：`GET /api/remote/connections`、`POST /api/remote/connections`、`POST /api/remote/connections/{name}`、`DELETE /api/remote/connections/{name}`、`POST /api/remote/connections/{name}/default`、`POST /api/remote/connections/{name}/permissions`、`POST /api/remote/connections/{name}/test`。**执行中（`run.*` 活跃）改连接清单返回 409**（与设置类端点同口径） |

**验收**：`test_remote_ui.py` —— ① 增 / 改 / 删 / 设默认 / 权限位全链路，清单**原子写**且 round-trip 一致；
② **坏 spec 被拒且不落盘**；③ `test` 不改清单、返回能力降级项；④ `VIBE_REMOTE=0` 时全部端点 404 / 门面 fail-closed；
⑤ 权限位改动下一次 `open()` 生效。

**红线核对**：复用既有 store 与 ssh 加固；不新增写盘路径；审计只记元数据。

---

### C 远程文件面（树 / 查看 / 编辑保存）

| 子任务 | 内容 |
|---|---|
| C1 | 文件树：`GET /api/remote/tree?path=` → 经 `RemoteSessions.chdir()` + `RemoteWorkspace.list_dir()`；返回**目录优先排序**、`name / kind(dir\|file\|other) / size / mtime / symlink?`。**不看内容**。路径越界 → 走 `RemotePathGuard` 拒绝（`SandboxViolation`）；**符号链接如实标注但不跟随** |
| C2 | 只读查看：`GET /api/remote/file?path=` → `RemoteWorkspace.read()` → 返回 `text / size / encoding / newline / had_bom / trailing_newline / sha256 / rel_path`。**sha256 是保存时的乐观锁基准**。内容按**不可信文本**包裹（沿用既有 `wrap_untrusted` 纪律，界面上如实标注「远端内容，未经审查」）。二进制 / 超限（`MAX_SCAN_BYTES`）→ **明确拒绝并给原因**，不做截断假预览 |
| C3 | 编辑保存：`POST /api/remote/save` `{connection, path, content, expectedDigest}` → ① **写前本机留底**（复用既有留底路径；**存不下就中止写入**，返回明确原因）；② `expectedDigest` 与远端当前 sha256 不一致 → **409 + 返回远端当前内容摘要**（不覆盖）；③ 写回（受控覆盖，ADR-004 的受控例外：显式声明 overwrite 且**返回真实 unified diff**）；④ 回读字节数 + sha256 校验；⑤ 记 `remote.file(action=save)` 审计 + **登记到 `RemoteSessions.changes`**。**不产生 `remote.edit` 事件**（正文只走响应） |
| C4 | 新建远端文件：`POST /api/remote/save` 且 `path` 不存在时 = 创建（与本地 `write_file` 同性质，受控例外）；返回 `created=true` + diff |
| C5 | 结构化补丁（界面侧）：`POST /api/remote/patch` `{connection, path, search, replace, replaceAll?}` → 复用 `RemoteWorkspace.edit()`（**三级匹配引擎原样**），返回 `diff / strategy / added / removed / changed / written`；`changed=false` 时**不写回**（既有语义，防「无改动却统一换行」的静默破坏） |
| C6 | 事件：`remote.state`（`connection` `browsePath` `allowWrite` `allowShell` `capabilities{degraded[]}` `ok`）—— 只记**元数据与降级项**；`remote.edit`（`connection` `path` `action` `added` `removed` `strategy` `sha256` + **`diff` 供界面审阅**）—— diff 属「供人审的改动内容」，**与本地 `run.approval.preview.body` 同一条口径**，**不进审计** |

**验收**：`test_remote_ui.py` —— ① 树 / 查看走 jail（**逃逸路径被拒**）；② 保存的**乐观锁**（digest 不符返回 409 且**远端文件未被改**）；
③ **留底不可写 → 中止写入**（远端文件保持原样）；④ 保存返回真实 diff 且 `RemoteSessions.changes` 有登记；
⑤ `changed=false` 的 patch **不写回**；⑥ 二进制 / 超限**明确拒绝**；⑦ 审计**不含正文**。

**红线核对**：ADR-004 结构化补丁 / 受控覆盖；ADR-011 用户本人动作；不走新写盘通道；审计只记元数据。

---

### D 远程审查面（改动清单 + unified diff + 逐 hunk）

| 子任务 | 内容 |
|---|---|
| D1 | `GET /api/remote/changes` → 本会话远端改动台账（`RemoteSessions.list_changes()`）：`path / action / added / removed / bytes / sha256 / backed_up / editedAt`。**元数据优先**，正文按需取 |
| D2 | `GET /api/remote/diff?path=` → **审查的主体**：把 **本机留底**（`.vibe/checkpoints/remote/<连接>/<会话>/<相对路径>`）与**远端当前内容**比出 unified diff；返回 `hunks[]`（`header / oldStart / oldLines / newStart / newLines / lines[]`）供**逐 hunk 审阅**（复用本地 M5-D 的 diff hunk 解析路径）。留底不存在（新建文件）→ diff 视为全新增；远端文件已删 → 如实标注「远端已不存在」 |
| D3 | 新增只读自审工具 `remote_review_changes`（**对齐本地 M9-E 的 `review_changes`**）：`ToolSpec(name="remote_review_changes", risk=Risk.SAFE, ...)` —— 纯本地计算（读留底 + 远端只读回读），**不发写请求、不落盘、不弹审批**；结论按 `wrap_untrusted` 包裹。**只在 `VIBE_REMOTE=1` 时注册**（挂既有门控），**不进 `build_readonly_registry`**（子 Agent 恒不触远端，沿用既有纪律） |
| D4 | 审计：审查动作本身记 `remote.file(action=read, path=?, bytes, sha256)`（复用既有语义，**不新造事件**）；**diff 正文不进审计** |

**验收**：`test_remote_review.py` —— ① 改动台账如实登记（save / patch / write 三类）；
② 留底 vs 远端产出**正确 unified diff**（含新增 / 修改 / 删除三种）；③ hunks 切分正确（逐 hunk 的行号与内容）；
④ **审计不含 diff 正文**（反向验证④的靶子）；⑤ `remote_review_changes` **恒 SAFE 且不进只读 registry**。

**红线核对**：只读自审不新增写盘 / 执行面；ADR-006 审计只记元数据。

---

### E UI（「远程设备」面板）

| 子任务 | 内容 |
|---|---|
| E1 | 活动栏新增 `data-panel="remote"`（**仅在 `config.remote_enabled` 时出现**，否则**连按钮都不渲染**）。面板三栏 —— **① 左：连接列表**（名称 / 目标 / 权限位徽标（可写 / 可执行，关闭时明确标「只读」）/ 默认项标记；按钮：打开 / 测试 / 设为默认 / 权限位开关 / 编辑 / 删除，新增走一个轻量表单）；**② 中：远端文件树**（目录优先、点击展开、点文件在右栏打开；面包屑显示当前浏览路径）；**③ 右：查看器 / 审查 / 编辑**（三个页签：`查看`（只读、标「远端内容，未经审查」+ 编码 / 换行 / 大小）、`审查`（本会话改动列表 + 逐 hunk diff）、`编辑`（文本域 + 保存按钮；保存前显示将产生的 diff、`expectedDigest` 不符时明确提示「远端已被改过」）） |
| E2 | 前端纪律（既有约定照旧）：**零构建、零第三方库**；新增面板只加 `data-panel` 按钮 + 容器 + `app.js` 渲染函数 + `styles.css`；文案与现有面板同款中文；**执行中（`run.*` 活跃）禁用「保存」与「改连接」**（与 C 阶段端点 409 一致）；`remote.state` 驱动连接与权限位徽标实时更新 |
| E3 | **如实呈现，不假装**：能力降级项（远端无 `realpath` / 无 `grep` 等）在面板上直接列出；权限位关闭时**保存按钮置灰**并给原因（而不是点了报错）；二进制 / 超限文件打开时**明确提示不支持** |

**验收**：桌面 / 浏览器直开 `host/ui` 可见面板；打开连接 → 树可浏览 → 文件可查看（含 digest 与编码信息）→ 编辑保存
弹出将产生的 diff → 审查页签能逐 hunk 看我会话内改动；`VIBE_REMOTE=0` 时**面板完全不出现**；权限位关闭时保存置灰。

**红线核对**：前端零依赖；界面只呈现元数据与端点响应；远端写仍走留底 + 乐观锁 + 结构化补丁 / 受控覆盖。

---

### F 测试与收口

| 子任务 | 内容 |
|---|---|
| F1 | 新增 `packages/agent-core/tests/test_remote_ui.py`：连接管理（增 / 改 / 删 / 默认 / 权限位 / 测试 / 坏 spec 拒绝 / 原子写 round-trip）+ 文件面（树 / 查看 / 保存乐观锁 / 留底失败中止 / patch 不写回 / 二进制拒绝）+ 门控 fail-closed。**SSH 一律用假传输**（注入 `transport` 桩，不起真 ssh） |
| F2 | 新增 `packages/agent-core/tests/test_remote_review.py`：改动台账 / 留底 vs 远端 diff（新增·修改·删除）/ hunk 切分 / `remote_review_changes` 恒 SAFE 且不进只读 registry / 审计不含正文 |
| F3 | 扩充既有 `packages/agent-core/tests/test_remote.py`：只补「门控与新工具注册」两处断言（**不改既有用例语义**） |
| F4 | **反向验证**（随后**还原**）：① 远端保存**去掉** `expectedDigest` 校验 → 并发覆盖用例变红；② 远端保存**去掉**写前留底 → 「留底不可写则中止」用例变红；③ 树 / 查看**绕过** `RemotePathGuard` → 逃逸路径用例变红；④ `remote_review_changes` 把 **diff 正文写进审计** → 审计用例命中敏感词变红 |
| F5 | **全量回归 + E2E**：agent-core 全量、服务端全量（`test_chain_compat` 词汇表必须认 `remote.connection` / `remote.file`）、`scripts/e2e_governance.py` 8 档、`scripts/e2e_session.py`、**打包冒烟等效验证**（内嵌 `vibe-agent-core.exe` → `/api/health` 200，用法同 M10）。**远端链路不进自动回归**（需真 ssh 目标），改为**手动冒烟脚本**并如实记录：无可用远端时该项标「未执行」+ 原因 |
| F6 | 文档收口：`docs/M11-PLAN.md` 落盘（本文件改写为正式版，含「交付验证结果」节）、`docs/REMOTE.md` **口径修正落盘**（标题 / 措辞）+ **新增「§5 界面工作面」小节**（面板 / 端点 / 事件 / 保存语义 / 乐观锁）、`docs/ARCHITECTURE.md` 新增 **ADR-038（远程文件工作面 = 在既有远程链路上补用户面：复用传输与 Jail、界面写 = 用户本人动作 + 留底 + 乐观锁、审查复用留底、不新增门控）** 并把 **ADR-036 / ADR-037 的措辞**改掉、根 `README.md`（M11 行 + 文档索引 + 用例数）、`packages/agent-core/README.md`（新小节 + 用例数与用例表）、`apps/desktop/README.md`（远程面板说明 + 安全边界 + 「留底目录不自动清理」的运维提示） |

**验收**：两个新测试文件全绿；反向验证四条**均先转红、复原后复绿**；全量回归**无红**；`test_chain_compat` 词汇表覆盖两个新事件；到手一台真远端时手动冒烟通过（否则如实标注未执行）。

---

## 3. DoD（完成定义）

- [x] `VIBE_REMOTE` 未设时，端点 / 事件 / 页签 / 新工具**与改造前逐字节一致**
- [x] 连接管理（增 / 改 / 删 / 设默认 / 权限位 / 测试连通性）**全部可在界面上完成**，不必回命令行
- [x] 远端文件树可浏览、文件可只读查看（含编码 / 换行 / 大小 / sha256），**二进制与超限如实拒绝**
- [x] 界面保存：**写前本机留底 + `expectedDigest` 乐观锁 + 受控覆盖/结构化补丁 + 回读校验 + 返回真实 diff**
- [x] 远程代码审查：本会话改动台账 + **留底 vs 远端**的 unified diff + 逐 hunk；`remote_review_changes` 恒 SAFE 且不进只读 registry
- [x] 权限位（`allow_write` / `allow_shell`）在界面上**如实显示**，关闭时保存按钮**置灰并给原因**
- [x] 新审计事件 `remote.connection` / `remote.file` **三处同步**且**只记元数据**（远端正文 / diff 正文 / 命令输出不出机）
- [x] **Agent 侧口径一字未改**（8 个既有工具的风险级别、审批、沙箱分析、`wrap_untrusted` 全不动）
- [x] 不新增第三方依赖（`dependencies` 仍为 `[]`）；前端仍零构建零库；**不新增传输协议、不新增门控**
- [x] `docs/REMOTE.md` 与 ADR-036 / ADR-037 的「远程开发机」措辞**已按新口径修正**（决策内容不变）
- [x] 两个新测试文件全绿；反向验证四条转红后已复原；全量回归无红；词汇表覆盖两个新事件

---

## 4. 红线核对

| 红线 | 本轮的落法 |
|---|---|
| **ADR-003 零第三方依赖** | 不新增任何包；传输仍是系统 `ssh`；diff / hunk 解析复用本地既有实现 |
| **ADR-004 结构化补丁** | 界面侧「结构化补丁」走 `RemoteWorkspace.edit()`（复用本地 `plan_patch` 三级匹配）；整份内容保存是**受控例外**（显式 `overwrite` + 返回真实 diff），与本地 `write_file` / `/api/edit/save` 同一口径 |
| **ADR-006 审计只记元数据 / 代码不出机** | `remote.connection` / `remote.file` 只记 连接 / 路径 / 动作 / 字节 / 增删行 / sha256 / 权限位 / 留底成败；**正文与 diff 只走端点响应与界面**；治理面**依旧收不到任何代码** |
| **ADR-008 高危强制人审** | Agent 侧远端写 / 执行**恒 REVIEW 不变**；界面侧远端写是**用户本人动作**（ADR-011），不需要重复审批，但**必须**留底 + 乐观锁 + 展示真实 diff |
| **ADR-011 用户本人动作** | 连接管理与远端编辑保存均由**用户经界面 / API** 发起；`build_default_registry` 只新增一个**只读 SAFE** 工具，`build_readonly_registry` **不新增** |
| **ADR-014 只连本机（+ 用户自己的设备）** | 链路 = 本机 → 用户自己的设备，SSH 走既有凭据与网络路径；治理面不在链路中；不做第三跳 |
| **ADR-036 / ADR-037（远程链路）** | 决策**一字不改**（BatchMode + 无密码通道 + 三态指纹 + argv 防注入 + 文本级 Jail + realpath 二次确认 + 留底 + 审计只记元数据）—— 本轮**只改「远程开发机」这一称呼** |
| **默认关 + 逐字节一致** | **不新增门控**，新面挂在既有 `VIBE_REMOTE` 上；未设时无端点 / 无页签 / 无事件 / 无新工具 |
| **治理面不参与** | 不改 `apps/server/**`，除两个**必改**的审计词汇同步点（`redaction.py` 的 `_SCHEMA`、`test_chain_compat.py` 的 `_sample_locals()`） |

---

## 5. 风险与对策

| 风险 | 对策 |
|---|---|
| **静默覆盖远端改动**（界面看到的内容与远端现状不一致） | `expectedDigest` 乐观锁；不符即 **409 + 返回远端当前摘要**，**绝不覆盖**（F4 反向验证①锁住） |
| **留底失败却仍然写入**（远端改动无法回滚） | 沿用既有「**存不下就中止写入**」；F4 反向验证②锁住 |
| **界面绕开 Jail**（自造路径拼接） | 树 / 查看 / 保存**一律经 `RemotePathGuard` + `_assert_realpath_inside`**；F4 反向验证③锁住 |
| **审查把正文写进审计** | diff **只走端点响应**；审计只记 `path / added / removed / sha256`；F4 反向验证④锁住 |
| **权限位被误解为「已授权」** | 三层面板徽标 + 关闭时按钮置灰 + 能力降级项直列；`allow_write` / `allow_shell` 默认**都关** |
| **ssh 连接泄漏**（切连接 / 会话收尾） | `RemoteSessions.open()` 先关旧；`close_all_transports()` 在会话收尾调用（既有） |
| **大目录 / 大文件拖慢界面** | 树只列一层（点开才展开）；查看受 `MAX_SCAN_BYTES` 上限，超限**明确拒绝** |
| **无可用真远端 → 端到端测不了** | SSH 全部用**注入式假传输**做单元；真链路改为**手动冒烟脚本**，无目标时如实标「未执行」（不假装通过） |
| **与并行在途工作（客户端自动更新 / 远程链路本体 / M10）同在未提交工作树** | 本轮**只碰 remote 相关新增 + REMOTE.md / ADR 措辞 + 两个必改同步点**；**不重构**既有 remote 模块内部 |

---

## 附录 A2：口径修正改法清单（**F 阶段已按此清单落盘**）

> 定性（用户原话）：「**不是远程开发机，是远程连接其他设备，并对这个设备上的文件进行读取、
> 审阅和修改、查看等，对远程文件进行代码审查和修改与保存**」。
> 因此 A2 只做**称呼替换**，**决策内容、理由、约束一字不改**。

**替换规则**（机械可执行，落盘时按此逐条改）：

| 原文 | 改为 |
|---|---|
| `远程开发机` | `远程设备` |
| `远端开发机` | `远端设备` |
| `本机 → 用户自己的开发机` | `本机 → 用户自己的设备` |
| `远程开发机接入`（标题 / 文件主题） | `远程设备文件操作` |

**改（指向本链路本体的措辞）**：

| # | 文件 | 位置 | 说明 |
|---|---|---|---|
| 1 | `docs/REMOTE.md` | L1 标题、L3、L13、L104 | 标题改「远程设备文件操作」（工作面属 M11，既有链路本体为 M10，标号在正文注明）；**F6 同时新增「§5 界面工作面」** |
| 2 | `docs/ARCHITECTURE.md` | L1072（ADR-036 标题）、L1076、L1077、L1083、L1086 | ADR-036 标题与四条内文；**决策与理由不动** |
| 3 | `packages/agent-core/agent_core/config.py` | L414、L418、L631 | 字段注释区与 `load()` 内注释 |
| 4 | `packages/agent-core/agent_core/remote/__init__.py` | L1、L3、L12 | 模块头（含「本机 → 用户自己的开发机」一处） |
| 5 | `packages/agent-core/agent_core/remote/errors.py` | L1 | 模块头 |
| 6 | `packages/agent-core/agent_core/remote/connection.py` | L1、L55、L65 | 模块头 / 类 docstring / 跳板机注释 |
| 7 | `packages/agent-core/agent_core/remote/pathguard.py` | L1 | 模块头 |
| 8 | `packages/agent-core/agent_core/remote/transport.py` | L1、L7 | 模块头 |
| 9 | `packages/agent-core/agent_core/remote/cli.py` | L51 | `--description`（用户可见） |
| 10 | `packages/agent-core/agent_core/tools/remote.py` | L1、L30、L613、L667、L678、L694、L714、L732、L753、L781、L802 | 模块头 + **8 条工具描述**（呈现给模型与用户，属对外措辞）+ L613 审批理由文案 |
| 11 | `packages/agent-core/agent_core/tools/__init__.py` | L133、L135、L196 | 条件注册说明与注册顺序注释 |
| 12 | `packages/agent-core/agent_core/loop/agent.py` | L362 | 注册处注释 |
| 13 | `packages/agent-core/agent_core/host/session.py` | L285 | 注册处注释 |
| 14 | `packages/agent-core/tests/test_remote.py` | L1 | 模块 docstring |

**不改（泛指「本机 / 开发环境」或「部署形态」，改了反而失真）**：

| 文件 | 位置 | 为什么不改 |
|---|---|---|
| `docs/ARCHITECTURE.md` | L373（「每台开发机需自行安装语言服务器」）、L1262（「把 Core 放到远程开发机」，F6 前为 L1207） | 前者泛指开发环境，后者是**部署形态**（壳可替换那一节），都不是本链路的称呼 |
| `packages/agent-core/agent_core/host/protocol.py` | L12（「把 Core 部署到远程开发机」） | 同上，部署形态 |
| `apps/desktop/README.md` | L252（「把后端放到远程开发机」，F6 前为 L204） | 同上，部署形态 |
| `packages/agent-core/tests/*.py` | `test_compact.py` L148、`test_gitinfo.py` L65、`test_git_write.py` L57、`test_host_settings.py` L270、`test_host_mcp.py` L52、`test_host_governance.py` L129；`apps/server/tests/test_config_multi_instance.py` L8、`test_secrets.py` L51 | 全是「不要污染本机开发环境的 ~/.vibe / git 配置」这类泛指，与本链路无关 |

**落盘时机**：与 F6 同批（`docs/REMOTE.md` / `docs/ARCHITECTURE.md` 的 ADR-036·037 措辞 + 其余内文注释），
一次性改完并跑一次全量回归确认**无因措辞导致的测试断言偏移**（`test_remote.py` L1 的 docstring 变更不影响断言）。

**落盘结果（F6 执行后回填）**：上述 14 处代码侧改点**已全部按清单落盘**，替换规则为
`远程开发机接入→远程设备文件操作`、`远程开发机→远程设备`、`远端开发机→远端设备`、
`本机 → 用户自己的开发机→本机 → 用户自己的设备`，并把裸词 `开发机` 一并改为 `设备`（`内网设备` / `设备必有` / `共享的设备`）。
文档侧同批改：`docs/REMOTE.md` 标题与正文、`docs/ARCHITECTURE.md` 的 ADR-036（标题 + 四条内文）/ ADR-037。
全仓 `.py` 校验：仅剩 `host/protocol.py` L12 含「远程开发机」（按清单**刻意保留**的部署形态措辞），`远端开发机` 全仓归零。
12 个改动文件 `py_compile` 通过；随后的全量回归（agent-core 1142 passed / 服务端 419 passed）**无因措辞导致的断言偏移**。

---

## 6. 交付验证结果

### 6.1 交付物

| 类别 | 内容 |
|---|---|
| 会话层 | `packages/agent-core/agent_core/remote/session.py`（`RemoteSessions` / `ChangeEntry`） |
| 门面与端点 | `agent_core/host/session.py`（连接 CRUD / test / open / tree / read / save / patch / changes / diff）、`agent_core/host/server.py`（`/api/remote/*` 路由与错误映射） |
| 事件与端点登记 | `agent_core/host/protocol.py`（`remote.state` / `remote.edit` + 端点表） |
| 审计三同步 | `agent_core/governance/vocabulary.py`（`remote.connection` / `remote.file` + 8 条 `tool.remote_*` 登记为**纯本地不上传**）、`apps/server/vibe_server/redaction.py`、`apps/server/tests/test_chain_compat.py` |
| 只读自审工具 | `agent_core/tools/remote.py`（`remote_review_changes`，恒 SAFE，不进只读 registry） |
| UI | `agent_core/host/ui/{index.html,app.js,styles.css}`（「远程设备」面板：连接列表 / 文件树 / 查看·审查·编辑三页签） |
| 测试 | `packages/agent-core/tests/test_remote_ui.py`、`packages/agent-core/tests/test_remote_review.py`，并扩充 `tests/test_remote.py` |
| 手动冒烟脚本 | `packages/agent-core/scripts/e2e_remote.py`（远端链路真机冒烟，参数化、不进 CI，见 §6.6） |
| 文档 | 本文件、`docs/REMOTE.md`（口径修正 + 界面工作面）、`docs/ARCHITECTURE.md`（ADR-038 + ADR-036/037 措辞）、根 `README.md`、`packages/agent-core/README.md`、`apps/desktop/README.md` |

### 6.2 专项测试

- 远程工作面 + 审查面：`pytest tests/test_remote.py tests/test_remote_ui.py tests/test_remote_review.py -q`
  → **150 passed / 1 skipped / 36 subtests**（其中两个新文件 `test_remote_ui.py` + `test_remote_review.py` 合 **50 passed**）。
- 服务端词汇表：`pytest tests/test_chain_compat.py -q` → **25 passed / 351 subtests**（较 M10 的 339 增 12，覆盖 `remote.connection` / `remote.file` 与 8 条 `tool.remote_*` 的 `upload=""` 登记）。

### 6.3 反向验证（四条，均**先转红、随后全部还原并复绿**）

| # | 靶子 | 改动 | 结果 |
|---|---|---|---|
| ① | 远端保存乐观锁 | `remote_save` 去掉 `expectedDigest` 校验 | `test_save_optimistic_lock_refuses_stale` **转红**（提示语缺失）；还原后复绿 |
| ② | 写前留底 | `remote_save` 去掉 `_backup_before_write`（`backup_path=""`） | `test_save_aborts_when_backup_unwritable` **转红**；还原后复绿 |
| ③ | 路径 Jail | `RemoteWorkspace.read` / `RemoteSessions.chdir` 改为裸拼接绕过 `guard.check_read` / `check_list` | `test_read_escape_rejected` **转红**（403 变 404）；还原后复绿。注：`test_tree_escape_rejected` 仍绿 —— Jail 是双重防线（`guard.relative` 二次归一化也拒绝 `..`），绕过一处不足以放行 |
| ④ | 审查审计不含正文 | `_review_changes` 把留底正文写进 `record("tool.remote_review_changes", body=…)` | `test_review_tool_audit_has_no_body` **转红**（审计里出现 `"body": "line0\nline1…"`）；还原后复绿 |

### 6.4 全量回归

- agent-core：**1142 passed / 3 skipped / 134 subtests**（215.75s）。跳过的 3 项为环境相关（ConPTY 与符号链接）。
- 服务端：**419 passed / 25 skipped / 584 subtests**（119.34s）。
- 既有用例**无一变红**，总数只增不减。

### 6.5 E2E 与打包冒烟

- `scripts/e2e_governance.py`：**8 档全绿**，档 8 进程快照比对**无新增残留进程**。
- `scripts/e2e_session.py`：**通过**（提交 → 关 host → 重开同库 → 会话可见 → resume 恢复 4 条 → 续聊）。
- **打包冒烟等效验证**：重打内嵌后端 `python scripts/build_desktop.py --backend` → `apps/desktop/resources/backend/vibe-agent-core.exe`（23.4 MB）；启动该 onefile 二进制并解析 `VIBE_URL` →
  ① `VIBE_REMOTE=1`：`GET /api/health` **200**（版本 `0.1.0` 一致）、`GET /api/remote/connections` **200**（清单为空，证明新端点在冻结核心里）；
  ② `VIBE_REMOTE` 未设：`GET /api/health` **200**、`GET /api/remote/connections` **400**（门控 fail-closed）。
  退出按进程树强杀，**无残留后端进程**。
  注：PyInstaller onefile 解包需可写临时目录 —— 本机 `%TEMP%` 对冻结进程不可写（bootloader 报
  `Could not create temporary directory!`），验证时显式把 `TMP`/`TEMP` 指到本盘可写目录即通过。
- **GUI 路径（Electron 壳）不参与本轮**：`dist/desktop/VibeAgent-*/resources/default_app.asar` 被 Trae CN
  以「禁删除」共享模式持有，分发目录无法重建；本轮未改打包与桌面壳，故沿用「内嵌后端二进制等效验证」口径（与 M10 同因）。

### 6.6 远端链路真机冒烟

**已执行（真实 SSH + 真实远端文件系统）—— 50/50 项 PASS。**

| 项 | 内容 |
|---|---|
| 承载形态 | 参数化手动脚本 `packages/agent-core/scripts/e2e_remote.py`（与 `e2e_package.py` 同属「手动执行、不进 CI」范式）。**真机链路需外部设备与凭据，不进自动回归**；无目标时**如实报错退出，不假装通过** |
| 实测目标 | `root@10.0.1.50`（用户自有设备），身份 `~/.ssh/id_ed25519_remote`，端口 22 |
| 远端环境 | `Linux 6.8.0-106-generic x86_64` / `root` / `python3 3.10.12`；`stat / find / head / ls / realpath` 齐备（`find -printf`、`head -c` 可用） |
| 实测时间 | 2026-10-19 |
| 覆盖 | ① 远端环境 → ② 起 host（`VIBE_REMOTE=1` 门控：`/api/health` 200、`config.remoteEnabled=true`）→ ③ 连接 add（清单落盘）/test（`root_writable=true`）/open → ④ tree/file/save → ⑤ changes/diff（**留底真实存在且内容 == 改动前远端内容**；diff `available=true`、hunk `oldStart=5`(1 基)）→ ⑥ 负面路径（过期 digest **409** 且远端未被覆盖 / `../../../etc/passwd` **403** / 二进制 **400** / 3 MB 超限 **400** / `allow_write=false` 拒写 **400** 且热改回 true 即恢复）→ ⑦ patch（`strategy=exact`）/create（`backed_up=false`、全新增）→ ⑧ 审计（含 `remote.connection` / `remote.file`；**不含**远端正文与 diff 正文）→ ⑨ 清理（删除连接 200、远端 `/tmp/vibe-m11-smoke` 已删、host 按进程树终止、无残留） |
| 能力降级 | 实测仅剩一项：`连接复用不可用（每条命令一次握手，Windows 客户端常见）`（Windows 客户端 `ControlMaster` 不支持，为**预期降级**）；其余降级项在目标机上均不触发 |
| 未覆盖 | `remote_review_changes` 只读自审工具（需模型驱动一次 `run`，由单测 `test_remote_review.py` 覆盖代替）；Agent 侧 `remote_edit` / `remote_run_command`（属 M10 既有链路，非本工作面） |

`ssh` 客户端为本机 `C:\Windows\System32\OpenSSH\ssh.exe`；按 PLAN 的约定，真链路改为手动冒烟承载，其余分支继续由**注入式假传输**（`FakeTransport`）做单元覆盖。

### 6.7 红线核对结论

| 红线 | 结论 |
|---|---|
| ADR-003 零依赖 | `dependencies` 仍为 `[]`；前端零构建零库；diff / hunk 复用本地既有实现 |
| ADR-004 结构化补丁 | 界面 patch 走 `RemoteWorkspace.edit()`（同一套三级匹配）；整份保存是**受控例外**（显式 `overwrite` + 返回真实 diff） |
| ADR-006 审计只记元数据 | `remote.connection` / `remote.file` 只记连接 / 路径 / 动作 / 字节 / 增删行 / sha256 / 权限位 / 留底成败；**正文与 diff 只走端点响应与界面**（反向验证④锁住） |
| ADR-008 高危强制人审 | Agent 侧远端写 / 执行**恒 REVIEW 不变**；界面写属用户本人动作，仍必须留底 + 乐观锁 + 展示真实 diff |
| ADR-011 用户本人动作 | 连接管理与远端编辑保存均由用户经界面发起；`build_default_registry` 只新增一个只读 SAFE 工具，`build_readonly_registry` **不新增** |
| ADR-014 只连本机（+ 用户自己的设备） | 链路 = 本机 → 用户自己的设备，SSH 走既有凭据与网络路径；治理面不在链路中 |
| 默认关 + 逐字节一致 | **不新增门控**；`VIBE_REMOTE` 未设时无端点 / 无页签 / 无事件 / 无新工具（打包冒烟第二条已实测 400） |

### 6.8 遗留

- `dist/desktop/VibeAgent-*/resources/default_app.asar` 被 Trae CN 持有（`ERROR_SHARING_VIOLATION(32)`），
  本会话内无法重建分发目录；下次发版前需关闭 Trae CN 后重新走 `build_desktop.py --dist`。
- 远端改动**不参与**检查点 restore（沿用既有口径：只能从留底目录手工捞回），留底目录**不自动清理**。

---

**交付完成**：A→F 六阶段全部闭合。

---

## 7. 补强（A→F 之后追加）：内核自带远端工作面，不依赖 sshfs

**背景**：A→F 把「远端工作面」做成了**工具级 + 界面级**能力，但用户仍要面对一个现实问题 ——
怎么让**模型**知道「这句话里的文件其实在远端设备上」。当时的绕法是把远端目录用 sshfs 挂到本地
（`Z:\项目`）再当普通工作区用：**代价是把整条远端安全链路旁路掉了**（写前留底、乐观锁、
diff 审批全都不走），还要求每台机器单独装 WinFsp + SSHFS-Win、配私钥、建计划任务。
安装包**不含**这两样（`apps/desktop/package.json` 的 `files` / `extraResources` 只有 `src/*.js`
与 `resources/backend`），所以换一台机器就得重来一遍。

**做法**（方案 1，经用户确认）：**不碰 sshfs，改用内核自带的远端工作面**。

1. **专用工作区**：新建一个**只放 `.vibe/remote.json`** 的**空本地目录**当工作区
   （本次落地为 `%USERPROFILE%\VibeWorkspaces\gdhd-10.0.1.50`）。它自身就是「远端项目的工作区」，
   连接清单落在它自己的 `.vibe/` 下 —— 与 A 阶段「清单默认落 `<workspace>/.vibe/remote.json`」完全同构，
   **没有新增规则**。
2. **提示词指引**（本补强的唯一新增代码）：`loop/prompts.py::build_remote_section()` +
   `loop/agent.py::_remote_hint()`，把本工作区那条（可自动解析的）连接**告知模型**：
   名字 / `user@host` / 远端根目录 / 能力位 + 该用哪几个 `remote_*` 工具。
   于是「读 `smart-writer/README.md`」直接打远端，**不必在每句话里点名连接**，
   也不会误读到本地同名文件。

**三条纪律（与既有口径一致，未新增门控）**：

| 情形 | 行为 |
|---|---|
| `VIBE_REMOTE` 未设 / 清单不存在 / 清单为空 | **一个字不加** —— `_remote_hint()` 直接返回 `{}`（**零 I/O**），提示词与改造前**逐字节一致** |
| 有可自动解析的连接（清单 `default` 或唯一那条） | 注入该连接 + 能力位 + 工具清单 |
| 多条且没设默认 | **不猜**，只列名字并指引先调 `remote_connections`（与 `RemoteStore.resolve` 同款纪律：猜错可能把改动写到错的机器上） |
| 子 Agent | **恒不注入** —— 远端工具不在 `build_readonly_registry()` 里，注入只会诱导它试不存在的能力 |

工具层的解析优先级本就**不需要点名**（`_resolve_conn`：工具参数 > `VIBE_REMOTE_DEFAULT` > 清单 `default` > 唯一那条），
本补强只补上「模型不知道该用 `remote_*`」这一处缺口。

**补强：终端也必须跟到远端。** 专用工作区里的终端曾经起本机 shell，提示符停在
`%USERPROFILE%\VibeWorkspaces\<连接>` 这种空目录里 —— 用户以为在操作远端，命令却全跑在本机
（用户实测反馈）。现在 `POST /api/term/open` 在这类工作区里起 `ssh -tt <加固参数> <host>`，
起始目录为连接 root；判据是「工作区除 `.vibe/` 外没有任何本地内容」（本地项目只是顺手配了
远端连接时终端仍在本机）。见 `host/session.py::manifest_terminal_argv`。

**交付与验证**：

- 代码：`loop/prompts.py`（`build_remote_section`）、`loop/agent.py`（`_remote_hint` + 装配）
- 测试：`tests/test_remote_prompt.py` **11 条**（5 条纯函数 + 6 条装配，含「远端关闭时提示词逐字节一致」的**反向验证锚点**）
- 全量回归：agent-core **1244 passed / 3 skipped / 134 subtests**（基线 1233 + 11）
- 落地配置：专用工作区 `.vibe/remote.json` 已落；`%APPDATA%\VibeAgent\config.json` 的 `workspace` 已切到该目录；
  客户端重启后审计 `session.start` 的 `workspace` 指向新目录；`python -m agent_core.remote test` 真机连通性 ✓

**代价（如实列出）**：工作区不再是本地代码仓库，切换回来需要改 `config.json` 的 `workspace`；
`VibeAgent-RemoteMount-Z` 计划任务与 `Z:` 挂载在方案 1 下**不再必需**（可停用，也可保留作过渡）。

