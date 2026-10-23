# M12 里程碑：远端文件夹「加入工作区」+ 命名空间透明路由

| 项 | 内容 |
|---|---|
| 版本 | v1.0（A–F 六阶段全部闭合；本文件为落盘正式版） |
| 日期 | 2026-10-20 |
| 用户诉求（原话） | 「远端连接之后可以选择远程连接的文件夹，右键加入到工作区，工作区可以加入远程连接的文件夹。」 |
| 已选方向 | **A 透明路由**（体验最好，改动最大）—— 用户已确认 |
| 前置 | M0–M11 已交付：`agent_core/remote/`（连接清单 / ssh 传输 / 文本级 Jail / 写前留底）与 M11 工作面（远端树 / 只读查看 / 审查 / 编辑保存）已就位 |
| 定位 | 让**远端目录**进入**工作区**视野，且模型用**完全相同的本地工具名**操作它 —— **换实现，不加工具** |
| 门控 | 一律挂既有 `VIBE_REMOTE`，**不新增环境变量**；未启用时无端点 / 无右键 / 无事件字段 / 无新工具 / 提示词逐字节一致 |
| 唯一真源方案 | `.trae/documents/M12-远端根加入工作区-透明路由-方案.md`（12 节） |
| 上游文档 | `docs/M11-PLAN.md`（本文档模板）、`docs/REMOTE.md`、`docs/ARCHITECTURE.md`（ADR-003 / ADR-004 / ADR-006 / ADR-008 / ADR-011 / ADR-036 / ADR-037 / ADR-038 / **ADR-041**） |
| 编号说明 | 方案里写的「ADR-039」在落盘时已被「真实应用预览 = 跨源帧」占用、ADR-040 已被「远端预览 = `ssh -L`」占用 → 本里程碑的架构决策记录**实为 ADR-041**，已按此落盘 |
| 规模估算 | 不写工期；按 A→F 顺序交付，每阶段独立可回归、可验收 |

---

## 0. 结论先行：这一轮到底改了什么

把用户选中的**远端目录**登记成一个「命名空间根」：

```
@remote/<连接名>/<相对该连接 root 的 POSIX 路径>
```

该根出现在**工作区树**里（多根）；模型用**完全相同的本地工具名**
（`read_file` / `list_dir` / `find_files` / `grep` / `edit_file` / `write_file`）写
`@remote/...` 路径；内核在**两道 Jail 之前**按前缀把调用分流到远端链路。

三条纪律贯穿全程：

1. **工具面一字不改**：`ToolRegistry.register` 重名会抛错 → 只能「**换实现**」，不能「再加一条」。
   路由壳替换的是 `ToolSpec` 的 `handler` / `assessor` / `previewer` / `drafter` 四个成员，
   `name` / `description` / `parameters` / `risk` **原样保留** → 工具清单与 schema **逐字节不变**。
2. **审计不加事件、不改三处同步**：路由调用沿既有 `tool.execute`（`tool=call.name`、
   `path=result.meta["path"]` 填命名空间路径）；根管理复用 `remote.connection` 的
   `action="root_add"` / `"root_remove"`（两处 `action` 均为 `str`、不做枚举 → 零 schema 成本）。
3. **远端写一条不省**：结构化补丁或受控覆盖（`overwrite=true`）+ **写前本机留底**
   （存不下则中止）+ 返回**真实 diff** + 审批漏斗（ADR-004 / M11 红线）。

### 0.1 已就位、本轮一律复用（不改语义）

| 能力 | 位置 | 本轮怎么用 |
|---|---|---|
| 连接清单 + 原子写 + 0600 | `remote/store.py`（payload `{version, default, connections}`） | 新增顶层 `roots` 键，**不升** `FORMAT_VERSION` |
| 远端操作面 | `remote/workspace.py`：`read` / `write` / `edit` / `list_dir` / `exists` / `grep` / `run_argv` | 路由臂的远端实现层（`rel` 一律**相对 `conn.root`**） |
| 远端 Jail | `remote/pathguard.py` | 路由臂的唯一安全检查点 |
| 写前本机留底 | `tools/remote.py::_backup_before_write` | 路由臂的 `edit_file` / `write_file` **直接调用**，不重写 |
| 结构化补丁引擎 | `tools/remote.py::_plan_remote_edit` + `workspace.edit()` | 路由 `edit_file` 复用同一份三级匹配 |
| 会话级状态层 | `remote/session.py::RemoteSessions`（一次只活动一条连接） | 远端根**共享同一活动连接**；非活动根如实报「该连接未打开」 |
| 端点/事件既有面 | `host/session.py` 的 `remote_*` / `host/protocol.py` | 根管理端点**照同一套写法**新增 |
| 右键菜单组件 | `host/ui/app.js::openTreeMenu` | 由单条**扩成多项**（旧三参调用内部转数组，既有「预览首页」调用点零改动） |

### 0.2 本轮真正的缺口

1. **远端目录无法进入工作区视野** —— 工作区树只有本地单根（`GET /api/tree`）。
2. **没有「命名空间根」这个状态** —— `.vibe/remote.json` 只有 `connections` / `default`。
3. **模型无法用本地工具名操作远端文件** —— 只有 8 个 `remote_*` 工具，模型必须显式感知「这是远端」。
4. **没有根管理端点/右键入口** —— `protocol.py` 里无 `/api/remote/roots`；远端面板文件树无 `contextmenu`。
5. **非路由工具收到 `@remote/...` 时行为未定义** —— 会静默落到本地 Jail 变成「文件不存在」。

---

## 1. 范围

| 阶段 | 内容 | 状态 |
|---|---|---|
| **A** | `remote/roots.py`（`RemoteRoot` / `normalize_sub` / `validate_root`）+ `store.py` 的 `roots` 落盘与往返 + 单测 | ✅ 已完成 |
| **B** | `remote/routing.py`（`RemoteRouter` + `routed_specs` + 路由壳四件套）+ `ToolContext.remote_router` + `build_default_registry` 接入 + `PathGuard.reserved_prefixes` + 单测 | ✅ 已完成 |
| **C** | `remote/workspace.py` 的 `find_files`（递归 `list_dir` 走查，**不调 shell**）+ 六个工具的远端臂 | ✅ 已完成 |
| **D** | `host/session.py` 三个根端点 + `remote_tree` 扩参 + 审计复用 + `router.invalidate()` + `agent.refresh_system_prompt()` + `protocol.py` 登记 | ✅ 已完成 |
| **E** | UI：`S.workspaceRoots` / 多项 `openTreeMenu` / 多根工作区树 / 两处 `contextmenu` / 加移端点接线 | ✅ 已完成 |
| **F** | 五个反向验证锚点 + agent-core 全量回归 + 服务端回归 + 文档（`docs/M12-PLAN.md` / `docs/ARCHITECTURE.md` ADR-041 / `README` 补 `@remote/` 说明）+ 真机手动冒烟 | ✅ 已完成 |

### 1.1 已定档的边界

1. **不新增门控**：新面**挂在既有的 `VIBE_REMOTE` 上** —— 未设时无端点、无右键、无事件字段、无新工具，提示词**逐字节一致**。
2. **`build_readonly_registry()` 不动** —— 子 Agent 恒不触远端（M11 红线）。
3. **一次只活动一条连接**（M11 纪律）：展开非活动根的子树前先打开该连接；`remote_tree` 对非活动连接**如实报错，不静默换连接**。
4. **「加入工作区」只允许 `conn.root` 的子目录**（含 root 本身）；校验**纯文本**（不探远端）→ 离线也能加根，不可达时由 `remote_tree` 如实报「读取失败」，**不假装成功**。
5. **前缀词形 = `@remote/`**，`PREFIX` 常量为单一真源（`remote/roots.py`）。
6. **`@remote/` 是精确遮罩**：仅当第二段是**已登记连接名**时才遮蔽，不是整前缀封禁。

### 明确不做（本轮）

- ❌ 新增工具名（`remote_*` 仍只有 M10/M11 那 8 个）；❌ 新增审计事件；❌ 新增环境变量
- ❌ `edit_files` / `run_command` / `repo_map` / `semantic_search` / `symbols` / `git_diff` / `review_changes` 等**不路由**（跨设备多文件原子事务不成立）
- ❌ 远端 shell 经 `run_command` 执行（请用既有 `remote_run_command`）
- ❌ 远端目录的双向同步 / 镜像 / 自动上传（沿用 M11：只做按需读与按需写）
- ❌ 远端路径的符号链接跟随（Jail 语义不动：越界即拒）
- ❌ 不改 `GET /api/tree`（仍只回本地根）

---

## 2. 任务分解

> 通用约束（全程适用）：核心链路**零强制依赖**（`dependencies` 保持 `[]`、前端零构建零库）；
> 审计调用点**写字面量事件名**（AST 扫描只认 `record("事件名", ...)`）；
> `Config` 是 frozen dataclass，改字段用 `replace()`；**`VIBE_REMOTE` 未设时行为与改造前逐字节一致**。

### A 基础（命名空间根）

| 子任务 | 内容 |
|---|---|
| A1 | 新增 `agent_core/remote/roots.py`：`PREFIX = "@remote/"`、`RemoteRoot`（`connection` / `sub` / `label` + 派生 `ns_path()` / `abs_root()` / `to_dict()` / `from_dict()`） |
| A2 | `normalize_sub(raw)`：反斜杠转正斜杠 → `posixpath.normpath` → 去前导 `/` 与 `./` → 拒绝 `..` 逃逸、空字节、`-` 开头。**纯文本校验，不碰远端** |
| A3 | `validate_root(root, store) -> str`：返回空串表示合法，否则**人话**原因（连接不存在 / sub 越出 root） |
| A4 | `remote/store.py`：`RemoteStore` 增 `roots: list[RemoteRoot]` + `problems: list[str]`；`load()` 解析顶层 `roots`（**非法项丢弃并记进 `problems`，不抛错** —— 避免一条坏根毁掉整份连接清单）；`save()` 写出 `roots`；新增 `roots_for` / `get_root` / `add_root` / `remove_root` / `match_root`（均经既有原子写落盘） |

### B 路由（换实现，不加工具）

| 子任务 | 内容 |
|---|---|
| B1 | 新增 `agent_core/remote/routing.py`：`RouteHit`（`connection` / `sub` / `rel_to_root()` / `ns_path`）、`is_ns_path()`、`RemoteRouter`（`snapshot()` 惰性 load + `invalidate()` + `roots()` + `match()` + `describe()`）、`routed_specs()` |
| B2 | `match()` 四条纪律：非前缀 → `None`（本地臂）；前缀但**连接未登记** → 抛错并列出可用连接名；前缀但**未加入工作区** → 抛「该目录未加入工作区」；**非法路径** → 抛错。**一律不回退本地**（最坏情况是写到了错的机器上） |
| B3 | 路由壳 `_shell(spec, remote_impl)`：四个成员**共用同一份选臂逻辑**（按 `args["path"]` 选臂），结构上排除「审批时按本地预演、执行时却打到远端」这种错配；`ctx.remote_router is None` → 抛 `ToolError("远端路由未初始化")`（**不静默回退本地**） |
| B4 | `tools/base.py`：`ToolContext` 增 `remote_router: Any \| None = None`（用 `Any` 声明避免 tools ↔ remote 模块级循环导入） |
| B5 | `tools/__init__.py`：`build_default_registry(..., remote=True)` 时在 `registry.register` 循环**之前**做 `specs = routed_specs(specs)`；`build_readonly_registry()` **不动** |
| B6 | `sandbox/paths.py`：`PathGuard` 增 `reserved_prefixes: tuple[str, ...] = ()`；`resolve()` 命中前缀即抛 `SandboxViolation(kind="reserved_ns")`，消息给出「该命名空间请改用文件工具」的指引。默认空元组 → 关闭时**零影响** |

### C 远端臂

| 子任务 | 内容 |
|---|---|
| C1 | `remote/workspace.py::find_files(rel, pattern, *, max_entries=2000)`：递归 `list_dir()` 走查 + basename `fnmatch` 匹配，**不调 shell**（避免 quoting 与 shell 依赖）；命中上限即截断并在结果里标 `truncated` |
| C2 | `tools/remote.py` 六个远端臂：`_arm_read_file` / `_arm_list_dir` / `_arm_find_files` / `_arm_grep` / `_arm_edit_file` / `_arm_write_file`，以及对应的 5 个 assessor（`_arm_assess_grep` 恒 `Risk.REVIEW` 且理由含 `hit.ns_path` 即**连接名**）+ 编辑/写入的 previewer |

### D 端点与即时生效

| 子任务 | 内容 |
|---|---|
| D1 | `host/session.py` 新增 `remote_roots()` / `remote_root_add(connection, sub)` / `remote_root_remove(connection, sub)`；扩展 `remote_tree(path, connection="")` |
| D2 | 根变更后固定动作链：`store.save()` → `_remote_audit(action="root_add"/"root_remove")` → `router.invalidate()` → **`agent.refresh_system_prompt()`** → `_publish_remote_state()` |
| D3 | `loop/agent.py`：`ToolContext(... remote_router=RemoteRouter(config) if config.remote_enabled else None)`（延迟导入）；`_remote_hint` 增读 `store.roots`；新增 `refresh_system_prompt()`（把提示词拼装抽成 `_compose_base_prompt()`；显式 `system_prompt` 传入时**不重算**） |
| D4 | `loop/prompts.py`：`build_remote_section(*, ..., roots=())` —— `roots` 非空时追加「已加入工作区的远端目录」逐行列 ns 路径；**空时输出与现状逐字节一致** |
| D5 | `host/server.py`：`/api/remote/roots` 三分支（GET / POST / `roots/delete`）+ 异常映射（`RemoteConfigError` → 400、`notfound` → 404、`unreachable/transport` → 502、`SandboxViolation` → 403、`BusyError/StaleFileError` → 409） |
| D6 | `host/protocol.py`：事件表 `remote.state` 加 `roots`；端点表加三条根端点行 + `remote/tree` 补 `connection=` 参数 |
| D7 | `governance/vocabulary.py`：**仅** `remote.connection` 的 `note` 文案补 `root_add` / `root_remove` 两个动作词（**不动 `fields`**） |

### E UI

| 子任务 | 内容 |
|---|---|
| E1 | `S.workspaceRoots` + `loadWorkspaceRoots()`（`GET /api/remote/roots`，仅在 `remoteOn` 时） |
| E2 | `openTreeMenu(x, y, items)` 多项化（旧三参调用内部转数组，既有调用点零改动） |
| E3 | 工作区树 `renderTree` 增「远端根」顶级节点（label = ns 路径，展开按 `${conn}\0${sub}` 键懒加载；含连接的键避免同名路径串味） |
| E4 | `#remote-tree` 目录节点加 `contextmenu` → 「加入工作区 / 移出工作区」；工作区树的远端根节点右键 → 「移出工作区」 |
| E5 | 加/移后：toast + `loadWorkspaceRoots()` + 刷新树 + 同步 `remote.state.roots` |

### F 测试与收口

| 子任务 | 内容 |
|---|---|
| F1 | 新增 `tests/test_remote_roots.py`：`RemoteRoot` 归一化 / 校验 / store 落盘往返 / 坏根丢弃进 problems |
| F2 | 新增 `tests/test_remote_routing.py`：`match()` 命中与各类报错 / 长根优先 / `routed_specs` 换实现不改名与 schema / 四件套选臂一致 / 端到端（复用 `test_remote.FakeTransport`） |
| F3 | 扩充 `tests/test_remote_ui.py`（根端点 + `remote_tree?connection=` 非活动分支）与 `tests/test_remote_prompt.py`（M12 提示词段 + `refresh_system_prompt`） |
| F4 | **五个反向验证锚点**（临时改错 → 断言变红 → 还原） |
| F5 | agent-core 全量回归 + 服务端全量回归（确认三处同步未被动过，`test_chain_compat` 仍绿） |
| F6 | 文档收口：本文件、`docs/ARCHITECTURE.md` 追加 **ADR-041**、`packages/agent-core/README.md` 补 M12 小节、`governance/vocabulary.py` 的 note |

> **F 阶段的实际落法偏差（如实记录）**：方案 §9 的 F 行原写「ADR-039」，落盘时发现 039 / 040 已被并行在途的预览里程碑占用 → 改为 **ADR-041**；其余按方案执行。

---

## 3. DoD（完成定义）

1. `VIBE_REMOTE` 未设：工具清单 / schema / 提示词**逐字节一致**（反向锚点 1 自动化）。
2. `VIBE_REMOTE=1` 且登记 1 个根：真实设备上 `read_file` / `list_dir` / `grep` / `edit_file` / `write_file` 经 `@remote/...` 路径跑通；`grep`/`edit`/`write` 进审批且理由含**连接名**。
3. 写前留底文件存在于 `<workspace>/.vibe/checkpoints/remote/<conn>/<session>/…`（反向锚点 3）。
4. 未登记目录：`@remote/<conn>/<未加根目录>/x` → 人话报错「该目录未加入工作区」；未路由工具 → `reserved_ns` 报错（反向锚点 4）。
5. UI：右键「加入工作区」→ 工作区树出现远端根 → 展开可浏览 → 移出即消失；**无根时与改造前一致**。
6. agent-core 全量回归 + 服务端回归全绿；`test_chain_compat` 仍绿（未动三处同步）。

---

## 4. 红线核对

| 红线 | 本轮的落法 |
|---|---|
| **ADR-003 零第三方依赖** | 不新增任何包；前端仍零构建零库；远端仍只用系统 `ssh` |
| **ADR-004 结构化补丁** | 路由 `edit_file` 走 `_plan_remote_edit` + `workspace.edit()`（同一份三级匹配）；`write_file` 的整份覆盖是**受控例外**（显式 `overwrite=true` + 留底 + 真实 diff），与本地同口径 |
| **ADR-006 审计只记元数据** | **不新增事件**；路由调用沿用 `tool.execute`（`path` 填命名空间路径）；根管理复用 `remote.connection(action=root_add/root_remove)`，字段口径不变；远端正文 / diff 正文**不出机** |
| **ADR-008 高危强制人审** | 路由 `grep` / `edit_file` / `write_file` 恒 `REVIEW`，理由与摘要写明「远端设备 <连接名>」 |
| **ADR-011 用户本人动作** | 「加入 / 移出工作区」由用户经 UI/API 发起，不新增 Agent 侧工具；`build_readonly_registry()` 不新增 |
| **ADR-036 / ADR-037（远端链路）** | 传输、Jail、留底、审计口径**一字不改**，路由臂直接复用 |
| **ADR-038（M11 工作面）** | 树 / 查看 / 审查 / 保存语义不动；`remote_tree` 只**扩参**，对非活动连接如实报错 |
| **默认关 + 逐字节一致** | **不新增门控**，新面挂既有 `VIBE_REMOTE`；未设时无端点 / 无右键 / 无事件字段 / 无新工具 / 提示词一致 |
| **治理面不参与** | 不改 `apps/server/**`（本轮**未动**三处同步，只改了 `vocabulary.py` 的 `note` 文本） |

---

## 5. 风险与对策

| 风险 | 对策 |
|---|---|
| **前缀遮蔽**（字面 `@remote/` 的本地目录被命名空间抢走） | 决策 1 的**精确遮罩**：仅当第二段是**已登记连接名**时才遮蔽；非路由工具由 `reserved_prefixes` 明确报错兜底（反向锚点 4） |
| **审批与执行错配**（审批时按本地预演、执行时打到远端） | 路由壳的 `handler/assessor/previewer/drafter` **共用同一份选臂逻辑**；`RoutedSpecsShapeTest` 钉住四件套一致 |
| **静默「文件不存在」** | `match()` **一律不回退本地**，未知连接 / 未加根 / 非法路径各自抛**人话**错误（反向锚点 2） |
| **留底失败却仍然写入** | 沿用 M11「存不下就中止写入」（反向锚点 3） |
| **远端不可达** | 加根**不探远端**（纯文本校验）→ 离线也能登记；展开时才由 `remote_tree` 如实报「读取失败」，不假装成功 |
| **根状态落盘出错**（一条坏根毁掉整份清单） | 坏根载入时**丢弃并记进 `problems`**，不抛错；原子写沿用既有纪律 |
| **提示词刷新污染子 Agent** | `refresh_system_prompt()` 在**显式 `system_prompt` 传入**时**不重算**（子 Agent 行为不变） |
| **变更面大**（tools 层 + 沙箱路径判定 + 提示词 + 服务端 + UI） | A→F 分段、每阶段独立回归、**五个反向锚点** |
| **每调用一次读盘** | `RemoteRouter` 持快照 + `invalidate()`（仅根/连接变更才重读），非每次调用读盘 |
| **无可用真远端 → 端到端测不了** | 单测全用**注入式假传输**；真链路改为**手动冒烟**，无目标时如实标「未执行」，不假装通过 |

---

## 6. 交付验证结果

### 6.1 交付物

| 类别 | 内容 |
|---|---|
| 新增模块 | `agent_core/remote/roots.py`（命名空间根）、`agent_core/remote/routing.py`（路由器 + 路由壳） |
| 状态与端点 | `agent_core/remote/store.py`（`roots` / `problems` / 五个根方法）、`agent_core/host/session.py`（三个根端点 + `remote_tree` 扩参 + `refresh_system_prompt` 触发）、`agent_core/host/server.py`（`/api/remote/roots` 三分支 + 异常映射） |
| 契约登记 | `agent_core/host/protocol.py`（事件表加 `roots`、端点表加三条根端点行 + `remote/tree` 补 `connection=`） |
| 工具与沙箱 | `agent_core/tools/remote.py`（六个远端臂 + 5 个 assessor + 2 个 previewer）、`agent_core/tools/base.py`（`ToolContext.remote_router`）、`agent_core/tools/__init__.py`（`routed_specs` 接入）、`agent_core/sandbox/paths.py`（`reserved_prefixes`） |
| 提示词 | `agent_core/loop/prompts.py`（`build_remote_section` 的 `roots` 段）、`agent_core/loop/agent.py`（`_remote_hint` / `_compose_base_prompt` / `refresh_system_prompt`） |
| UI | `agent_core/host/ui/app.js`（`S.workspaceRoots` / 多项 `openTreeMenu` / 多根工作区树 / 两处 `contextmenu` / `loadWorkspaceRoots` / `addRemoteRoot` / `removeRemoteRoot`） |
| 审计口径 | `agent_core/governance/vocabulary.py`（`remote.connection` 的 `note` 补两个动作词，**`fields` 未动**） |
| 测试 | 新增 `tests/test_remote_roots.py`、`tests/test_remote_routing.py`；扩充 `tests/test_remote_ui.py`、`tests/test_remote_prompt.py` |
| 文档 | 本文件、`docs/ARCHITECTURE.md`（**ADR-041**）、`packages/agent-core/README.md`（M12 小节） |
| 手动冒烟 | 临时脚本（不留仓）：真机加根 → 六个路由工具 → 留底 → 负面路径 → 审计 → 清理还原 |

### 6.2 专项测试

- 远端相关六个文件（`test_remote.py` / `test_remote_ui.py` / `test_remote_review.py` /
  `test_remote_roots.py` / `test_remote_routing.py` / `test_remote_prompt.py`）
  → **225 passed / 1 skipped / 46 subtests**。
- 本轮新增的两个文件：`test_remote_roots.py` **15** 条、`test_remote_routing.py` **24** 条；
  被扩充的：`test_remote_ui.py` **55** 条、`test_remote_prompt.py` **17** 条。
- 服务端词汇表：`test_chain_compat` 仍在服务端全量里跑绿（本轮**未动**三处同步）。

### 6.3 反向验证（五个锚点，均**先转红、随后全部还原并复绿**）

| # | 靶子 | 改动 | 结果 |
|---|---|---|---|
| ① | 关闭路由时提示词逐字节一致 | `loop/prompts.py::_remote_roots_section` 让空输入返回非空串 | 一度**无牙齿**（原用例比较两次都带空 roots 的调用，两边一起变）；改写为直接钉住私有函数契约 `_remote_roots_section(()) == ""`（含 `({},)` 与 `({"ns_path": ""},)` 边界）后**转红**（`'（反向验证：故意破坏空输入）\n' != ''`）；还原后复绿 |
| ② | 取消失败分流（不静默回退本地） | `remote/routing.py::match()` 把 `root = store.match_root(...)` 改成 `root = None` | `test_read_writes_namespace_path_in_meta`、`test_edit_backs_up_then_writes` **转红**（报「该远端目录还没有加入工作区」）；还原后复绿 |
| ③ | 拆掉留底 | `tools/remote.py::_arm_edit_file` 跳过 `_backup_before_write` | 上述 edit 断言**转红**（留底不存在）；还原后复绿 |
| ④ | 拆掉前缀守卫 | `sandbox/paths.py` 把 `for prefix in self.reserved_prefixes:` 改成 `for prefix in ():` | `test_reserved_prefix_gives_explicit_reason` **转红**（`SandboxViolation not raised`）；还原后复绿 |
| ⑤ | 审批理由含连接名 | `_arm_assess_grep` 的 `Risk.REVIEW` 改 `Risk.SAFE` | `test_grep_assess_is_review_with_connection_name` **转红**；还原后复绿 |

> 还原后已全仓 grep 确认**无 `反向验证：` 残留**。

### 6.4 全量回归

- agent-core：**1308 passed / 3 skipped / 144 subtests**（246.42s）。跳过的 3 项为环境相关（ConPTY 与符号链接）。
- 服务端：**419 passed / 25 skipped / 599 subtests**（110.37s）。`test_chain_compat` 全绿（本轮未动三处同步）。
- **首轮全量回归出现过 1 例与本轮无关的既有 flaky**：`tests/test_preview_external.py::ExternalPreviewEnabledTests::test_audit_records_port_only`
  断言 `assertNotIn("abc", log)`（URL 里的 `?token=abc` 不该进审计），而该次审计链某条记录的 sha256 **恰好含子串 `abc`** → 断言随机失败。
  证据：隔离重跑该用例 **8/8 通过**；复跑全量 **1308 passed 全绿**（246.42s）。该文件属**并行在途的预览里程碑**（工作树中的未跟踪文件），本轮未改动它。
- 既有用例**无一变红**；总数只增不减（工作树中并行在途的其他里程碑亦在增删用例，故不单列全局差值）。

### 6.5 E2E 与打包冒烟

- 本轮**未改**打包链路与桌面壳，沿用 M11 口径：`scripts/e2e_governance.py` / `scripts/e2e_session.py` 与打包冒烟属**发版前手动**项目，本里程碑**未重跑**（如实记录，不假装通过）。
- 新增端点/事件**已在冻结核心里可验证**这一条由 D 阶段的 host 端点单元测试覆盖（`test_remote_ui.py` 的 `RemoteRootApiTest` / `RemoteTreeForConnectionTest`）。

### 6.6 真机手动冒烟

**已执行（真实 SSH + 真实远端文件系统）。**

| 项 | 内容 |
|---|---|
| 承载形态 | 临时手动脚本（与 `scripts/e2e_remote.py` 同属「手动执行、不进 CI」范式，脚本本身**不留仓**）。**真链路需外部设备与凭据，不进自动回归**；无目标时**如实报错退出，不假装通过** |
| 实测目标 | `gdhd@10.0.1.50:22` → 远端根 `/home/gdhd`，身份 `~/.ssh/id_ed25519_remote`；`allow_write` / `allow_shell` 均 true |
| 实测工作区 | `C:\Users\Administrator\VibeWorkspaces\gdhd-10.0.1.50`（清单 `.vibe/remote.json`，连接 `gdhd-10.0.1.50`） |
| 实测时间 | 2026-10-20 |
| 覆盖 | L1 根落盘与重载（7 项）+ L2 六个工具远端臂与负面路径（19 项）+ L3 Agent 循环审批与审计（5 项），共 **31 项全部通过**（明细见下） |
| 能力降级 | 沿用 M11 实测：`连接复用不可用（每条命令一次握手，Windows 客户端常见）` —— **预期降级** |
| 未覆盖 | 浏览器端右键 → 工作区树多根渲染（需 GUI E2E；由 `test_remote_ui.py` 的端点契约测试 + 应用内实测代替）；Agent 驱动的完整 `run`（需真实模型；由路由端到端单测代替） |

**结果：31/31 全通过。** 离场时 `remote.json` 已还原（`roots=（无）`）、`.vibe/` 无 `.vibetmp` 残渣、远端 `/home/gdhd/vibe-m12-smoke` 已清理。

| 段 | 实测项 | 结果 |
|---|---|---|
| L1（7/7） | 远端测试目录准备；连接在清单里；`add_root` 返回规范化根 `@remote/gdhd-10.0.1.50/vibe-m12-smoke`；`remote.json` 顶层 `version` 未升版仍为 `1`；`roots` 往返一致 `[{"connection","sub","label"}]`；无 `*.vibetmp` 残渣；重载后根仍在且 `problems=[]` | 全 PASS |
| L2（19/19） | `read_file`（`meta["path"]`=命名空间路径、`meta["remote"]={connection,rel}`）；`list_dir`；`find_files`（回填 ns 路径）；`grep` 审批恒 `risk=review` 且理由含连接名；`grep` 命中远端；`edit_file`（`strategy=exact`，`+1 / -1`）；远端内容已改；**写前本机留底存在且内容 == 改动前远端内容**；改动登记在命名空间路径下；`write_file` 无 `overwrite` 被拒；`write_file(overwrite=true)` 成功；未知连接被拒且列出可用连接（**不回退本地**）；未加根报「该远端目录还没有加入工作区」；`@remote/` 对未路由工具是保留命名空间（`reserved_ns`）；移出工作区后同一路径**立刻**不可用；重新加根后可用 | 全 PASS |
| L3（5/5） | Agent 循环跑完（假模型驱动）；远端内容被 Agent 改动（`value = 4`）；审批漏斗触发且理由为 `edit_file\|修改远端 @remote/gdhd-10.0.1.50/vibe-m12-smoke/README.md`；审计 `tool.execute` 带命名空间路径与工具名；审计不含远端正文 / 敏感串；`verify_chain` 链完整（7 条记录） | 全 PASS |

**承载脚本的三次假挂死（已定位，非产品缺陷）**：本轮复跑时连续三次卡在 `store.save()` 的 `tempfile.mkstemp`。根因是 Windows 上 `tempfile.TMP_MAX = 2**31-1`：一旦 `os.open` 偶发 `PermissionError`，`_mkstemp_inner` 会**无休止换名重试**（`continue` 分支要求 `isdir(dir)` 且 `access(dir, W_OK)`，而 Windows 的 `access` **不看 ACL**，所以该分支永远命中）。脚本侧把 `TMP_MAX` 收到 8 后复跑即 31/31；同目录、同 flags 的独立探针 300/300 成功。**该现象只影响冒烟脚本的观测，不涉及产品代码，故不改 `store.py`。**

**另修正两处脚本自身问题**（曾导致 30/31 中的链校验失败）：① 审计文件必须由**同一个 `AuditLog` 实例**写（原先 L2 的 ctx 与 Agent 各建一个，两条链交错 → `verify_chain` 判篡改）；② 审计 / 留底落盘改到**独立临时工作区**（原先落在用户真实工作区，跨轮累积污染链）。

### 6.7 红线核对结论

| 红线 | 结论 |
|---|---|
| ADR-003 零依赖 | `dependencies` 仍为 `[]`；前端零构建零库；远端仍只用系统 `ssh` |
| ADR-004 结构化补丁 | 路由 `edit_file` 复用同一份三级匹配；`write_file(overwrite=true)` 是受控例外，仍留底 + 返回真实 diff |
| ADR-006 审计只记元数据 | **不新增事件**；`path` 填命名空间路径；根动作复用 `remote.connection` 且 `fields` 未动；远端正文不出机 |
| ADR-008 高危强制人审 | 路由 `grep` / `edit_file` / `write_file` 恒 REVIEW，理由含连接名 |
| ADR-011 用户本人动作 | 加 / 移根是用户经 UI/API 的动作；不新增 Agent 侧工具，只读 registry 不动 |
| ADR-014 / 036 / 037 / 038 | 传输、Jail、留底、M11 工作面语义一字不改；`remote_tree` 只扩参 |
| 默认关 + 逐字节一致 | **不新增门控**；未设 `VIBE_REMOTE` 时无端点 / 无右键 / 无事件字段 / 无新工具 / 提示词一致（反向锚点 1） |

### 6.8 遗留

- `dist/desktop/VibeAgent-*/resources/default_app.asar` 被 Trae CN 以「禁删除」共享模式持有，
  本会话内无法重建分发目录；下次发版前需关闭 Trae CN 后重新走 `build_desktop.py --dist`（沿用 M11）。
- `~/.vibe/credentials.json` 待从 `e:\VibeCoding\.tmp-check\credentials.backup.json` 拷回（与本轮无关）。
- `tests/test_preview_external.py` 的 `assertNotIn("abc", log)` 是**既有 flaky 断言**（3 字符子串撞 sha256），
  属并行在途的预览里程碑；本轮**未改**（不在 M12 范围内）。
- 可选待定：`apps/desktop/src/backend.js::_baseEnv()` 是否显式透传 `VIBE_REMOTE`（本轮未改，沿用既有门控）。

---

**交付完成**：A→F 六阶段全部闭合。
