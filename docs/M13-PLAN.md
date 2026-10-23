# M13 里程碑：远端目录「作为工作区」—— 本地锚点 + 路由默认根

| 项 | 内容 |
|---|---|
| 版本 | v0.1（方案与阶段划分；随落地推进更新） |
| 日期 | 2026-10-20 |
| 用户诉求（原话） | 「在切换工作区的界面在本机目录旁边加一个远程目录，然后可以选择远程的文件。」并已确认方向：**把远程目录当成工作区**（而非挂载子树的轻量版） |
| 已选方向 | **B 把远程目录当成工作区**（用户在三选一里明确挑的；A 挂载子树 = M12 已有能力） |
| 前置 | M0–M12 已交付：`agent_core/remote/`（连接清单 / ssh 传输 / 文本级 Jail / 写前留底）+ M11 工作面（远端树 / 查看 / 审查 / 编辑保存）+ M12 命名空间路由（`@remote/` 路由壳） |
| 定位 | 切换工作区时直接选**远程目录**，之后一切读写都发生在那台机器上；模型写 `src/app.py` 就等于写 `@remote/devbox/src/app.py` |
| 门控 | 沿用既有 `VIBE_REMOTE`，**不新增门控环境变量**；未启用时端点 400（fail-closed，与 M11 的 `/api/remote/*` 同口径）、`switch_workspace` 拒收 `remote` 载荷、`describe()` 不含 `remoteWorkspace` 键、界面「远程目录」页签整键隐藏 |
| 架构决策 | `docs/ARCHITECTURE.md` **ADR-042** |
| 上游文档 | `docs/M12-PLAN.md`（本文档模板）、`docs/REMOTE.md`、`docs/ARCHITECTURE.md`（ADR-003 / 004 / 006 / 008 / 011 / 036 / 037 / 038 / 041） |
| 规模估算 | 不写工期；按 A→F 顺序交付，每段独立可回归、可验收 |

---

## 0. 结论先行：一句话说清这次改的是什么

```
切换工作区 → 选「远程目录」→ 选连接 → 浏览远端目录 → 确认切换
                                        ↓
                Config.workspace      = ~/.vibe/workspaces/remote/<连接>/<指纹>/   ← 本机锚点（只放 .vibe 资产）
                Config.remote_workspace = "@remote/devbox/项目/smart-writer"         ← 路由默认根
                                        ↓
                RemoteRouter.match("src/app.py") → Hit(connection=devbox, rel=项目/smart-writer/src/app.py)
```

三条线各自都不新：`.vibe` 资产照旧在本机（ADR-006 不动），路由照旧是 M12 那个壳
（工具名/schema 一字不改），切换照旧是 W1 的优雅重启。**新的只有四样东西**：
一个配置字段、一个路由分支、一张降级清单、一个 UI 页签。

---

## 1. 为什么不是「把 jail 架到 SSH 上」

把远程目录当成工作区，直觉上有两条路：

| 路线 | 做法 | 代价 |
|---|---|---|
| **换后端**（不选） | 写 SSH 版 `PathGuard` / SSH 版审计 append / 远端快照 restore | 等于重写内核一半；`<workspace>/.vibe/audit.jsonl` 这条**本机真源**搬到远端 → 远端一掉线，本机审计证据链就断档，直接踩 ADR-006 |
| **换默认落点**（选） | `.vibe` 资产留在本机锚点，只把「模型写一个路径时该落到哪台机器」切换成远端 | 本机 Git 面板 / LSP 在该模式下不可用（必须**如实告知**，不能静默返回空） |

选后者的技术原因是：`<workspace>` 这个短语在内核里只有两处用法 ——
**「jail 的根」** 和 **「`.vibe` 资产的落点」**。ADR-042 把它们拆开了：
jail 的根由 `RemoteRouter.default_root` 决定，`.vibe` 的落点仍是本机锚点。
于是 30+ 个注入点（`config.workspace / ".vibe"` 的每一处）一行都不用改。

---

## 2. 阶段划分

| 段 | 内容 | 交付物 | 验收 |
|---|---|---|---|
| **A** | 配置与路由底座 | `Config.remote_workspace` + `RemoteRouter.default_root` + `match()` 裸路径分支 | 单测：裸路径 / 绝对路径 / `..` 越界 / 显式前缀优先 |
| **B** | 切换与会话装配 | `remote/anchor.py` + `switch_workspace(remote=)` + Session 自动 open + `describe()` | 单测：锚点隔离（连接名带 `..` 也出不去）、连不上时不生效 |
| **C** | HTTP 端点 | `POST /api/workspaces/open` 收 `remote`；`GET /api/workspaces` 回远程候选 | 单测：门控关 → 400（fail-closed）；坏连接名 → 400 且未切 |
| **D** | 诚实降级 | `Session.remote_degraded()` 单一真源；Git / LSP / 索引 watch / 提示词 | 单测：清单非空且每条都有非空原因；本机模式回空字典 |
| **E** | 界面 | ws-modal 双页签（本机 / 远程）；远程模式主界面走 `/api/remote/*` | 真实浏览器实测 |
| **F** | 测试与文档 | `tests/test_remote_workspace.py` + 全量回归 + `REMOTE.md` / README | 1236+ 全绿，新增门控反证各使一条断言变红 |

---

## 3. A 段：配置与路由底座

### 3.1 `Config`（`agent_core/config.py`）

```python
# --- 远程工作区（M13 / ADR-042）---
# 非空时该远端目录成为「默认落点」：模型写裸路径也走远端臂。
# 取值形如 `@remote/<连接名>/<子目录>`；空串 = 本机模式（现状，逐字节不变）。
#
# 注意这不是门控 —— 真正的开关是 VIBE_REMOTE；这里只是「启动时的默认锚点」，
# 与 VIBE_REMOTE_DEFAULT 同范式。
remote_workspace: str = ""
```

校验落在**解析方**（`roots.parse_ns_root`），不在 Config：Config 是 frozen dataclass，
把「/ 开头合不合法」这种规则塞进 `__post_init__` 会让 CLI 每条命令都跑一遍。
（与 `remote_default` 同处理。）

### 3.2 `remote/roots.py`：新增两个纯文本工具

```python
def parse_ns_root(text: str) -> tuple[str, str]:
    """`@remote/devbox/a/b` → ("devbox", "a/b")。宽容：无子目录时 sub=""。
    非法（不以 PREFIX 开头 / 无连接名 / 含 `..`）一律 RemoteConfigError。
    """

def default_root_rel(sub: str, raw_path: str) -> str:
    """把模型写的路径收敛成「相对连接 root 的 rel」。

    - 裸相对写（最常见）：join(sub, path)
    - 远端绝对写：必须落在 sub 之下，取相对 sub 的差
    - 结果用 posixpath.normpath 归一后，**再**看是否以 `..` 开头（越界）
    """
```

**为什么贪婪usrghylds先归一化再看 `..`**：
本项目在 `roots.normalize_sub` / `connection.root` / `sandbox/paths.py` 三处已经确立
同一条纪律 —— 涉及路径判定要看得见原始的 `..`，而不是只信某一次归一化。
这里采取「拼接 → 整体归一化 → 事后断言」三步，是因为路径在这里要**拼接**而非纯校验：
拼完再归一化之后，`..` 只可能出现在**开头**，此时判定是完备的。

### 3.3 `RemoteRouter`：默认根分支

```python
def __init__(self, config):
    ...
    self.default_conn, self.default_sub = parse_ns_root(getattr(config, "remote_workspace", "") or "") \
        if getattr(config, "remote_workspace", "") else ("", "")

def match(self, path):
    if is_ns_path(path):
        return self._match_ns(path)      # 原逻辑，一字不动
    if self.default_conn:
        return self._match_default(path) # M13 新增
    return None                           # 本机模式：与现状完全同构
```

`_match_default` 的四条判定：

1. 路径必须以 str 传入（非 str → ToolError，不让 `.strip()` 在 None 上炸）；
2. `default_root_rel()` 越界 → ToolError「路径逃出了远程工作区根目录」；
3. 连接必须在清单里（沿用 `_match_ns` 的报错语气，列出可用连接）；
4. **`PREFIX` 开头的路径不进这个分支**（决策 4：默认根不遮蔽显式前缀）。

返回的 `RouteHit.ns_path` 一律是完整的命名空间写法，
于是 `meta["path"]` / 提示词 / 留底路径自动继承 M11/M12 的既有口径。

### 3.4 提示词（`loop/prompts.py`）

本机模式：**零改动**（`hint_roots()` 已给 M12 的根清单）。
远程模式追加一段「当前工作区在远端 `<连接>` 的 `<目录>`」+ 决策 5 的代价
（「本模式下无法在本机写入任何文件」）+ 决策 9 的降级清单。

---

## 4. B 段：切换与会话装配

### 4.1 `remote/anchor.py`（新）

```python
ANCHOR_ROOT = Path.home() / ".vibe" / "workspaces" / "remote"
ANCHOR_MARKER = ".vibe-workspace-anchor.json"   # {connection, sub, nsPath, createdAt}

def safe_name(connection: str) -> str        # 只留 [A-Za-z0-9._-]，其余换 `_`；空 → `_default`
def anchor_dir(connection, sub, *, home=None) -> Path          # 纯计算
def ensure_anchor(connection, sub, *, home=None) -> Path       # mkdir + 写标记
def list_anchors(*, home=None) -> list[dict]                   # 扫锚点根目录反推历史（读不到标记的跳过）
def read_anchor(path) -> dict | None
```

**为什么锚点路径要做 `safe_name`**：连接名是用户填的自由文本，
`"../../etc"` 这种名字会让 `<home>/.vibe/workspaces/remote/` 被穿越出去。
替换后仍需断言最终路径在 `ANCHOR_ROOT` 之下（`relative_to`），双保险。

**为什么history用「扫锚点」而不是另开一份 JSON**：少一份格式、少一处漂移。
标记文件里已经写了 connection / sub，扫目录就能还原「切过哪些远程工作区」。

### 4.2 `ServerContext.switch_workspace(path, *, remote=None)`

新增 `remote` 形参，缺省 `None`（**本机行为逐字节不变**）。

```
if remote is not None:
    _require_remote()                      # 门控：VIBE_REMOTE 未开 → AgentError
    conn, sub = 解析并校验（连接存在 / sub 归一化）
    ① 验证：临时 RemoteSessions().open(name)   —— 握手 + 能力探测
       失败 → 抛错，旧会话原封不动（决策 7「先连后切」）
       成功 → close() 临时会话（释放这条传输）
    ② anchor = ensure_anchor(conn.name, sub)
    ③ new_config = replace(cfg, workspace=anchor, remote_workspace=ns_root, session_id=new)
    ④ Session(new_config, ...) → 其 __init__ 内自动 open(conn.name)（第二次握手）
    ⑤ bus.publish(session.ready)
```

关于 ④ 的第二次握手：`close_transport` 不删 ControlMaster 套接字
（`ControlPersist=120`），所以第二次握手几乎零成本；换来的是
「验证失败时零副作用」这条明确语义 —— 代价写在注释里。

**Session 内自动 open 失败怎么办**：不同于「切换时」，这里不致命。
它留在 `Session.remote_error()` 里，端点据此回原因（`GET /api/remote/state` 带出）。
理由：已经切过来了，让界面能显示「远端连不上：<原因>」比让进程起不来好。

### 4.3 `Session.describe()`

仅当 `remote_workspace` 非空时**追加**键（本机模式字典完全不变）：

```python
"remoteWorkspace": ns_root,
"remoteConnection": conn_name,
"remoteSub": sub,
"remoteDegraded": {...},      # 决策 9 的清单
```

审计：`remote.connection(action="workspace_switch")` / `("workspace_open_fail")`
—— 沿用 M11 既有字符串 action 字段，**不动审计 schema**。

---

## 5. C 段：HTTP 端点

| 端点 | 变更 | 门控关时 |
|---|---|---|
| `POST /api/workspaces/open` | body 支持 `{remote:{connection, sub}}`（与 `path` 互斥） | `remote` 载荷 → 400（fail-closed，与 `/api/remote/*` 同口径） |
| `GET /api/workspaces` | 追加 `remoteEnabled` 与 `remoteWorkspaces`（扫锚点） | 两个键照常返回，`remoteWorkspaces=[]`（这是只读列表，不足以单列） |
| `GET /api/remote/tree` | 不变（M11 已有；要求连接已打开） | 不变 |

---

## 6. D 段：诚实降级（决策 9）

`Session.remote_degraded() -> dict[str, str]`：**唯一真源**，键是能力名、值是人话原因。
本机模式恒返回 `{}` —— 于是「本机模式行为不变」有一条可断言的锚。

| 键 | 端点/调用方 | 处置 |
|---|---|---|
| `git` | `/api/git/*` | 回 `{ok:false, reason:…}`，UI 面板显示原因 |
| `lsp` | `_lsp_registration()` | 远程模式返回 `False`（不注册 `lsp_diagnostics`） |
| `index_watch` | Session 启动处 | 不启动 watch 线程 |
| `repo_map` / `semantic_search` | 提示词 | 保留工具，但口径写明「只覆盖本机锚点」 |
| `local_write` | 提示词 | 写明「本模式无法在本机写文件」 |

收纳在一个方法里，是为了让「哪些能力降级了」这件事只有一处可改；
三处使用方（端点 / 提示词 / UI）都从它取值，不会出现「UI 说支持、内核说不支持」。

---

## 7. E 段：界面

- `#ws-modal` 顶部加两个页签：**本机目录** / **远程目录**（默认本机，与现状一致）。
- 远程页签三步：`连接下拉` → `连接`（复用 M11 `POST /api/remote/open`）→
  `目录树`（复用 `/api/remote/tree`）→ `切换并重启`。
- 按钮文案改为「切换并重启（远端）」，弹窗里把代价写清：
  **重建会话 / 新审计链 / 本机 Git 与 LSP 面板不可用**。
- 远程模式下主界面的文件树 / 编辑器 / 改动面板切到 `/api/remote/*`；
  顶部状态栏显示 `远端 <连接>:<目录>`；Git 面板显示降级原因而非空白。

---

## 8. F 段：验收清单

- [x] `tests/test_remote_workspace.py`：配置解析 / 路由四判定 / 锚点穿越 / 切换原子性 / 降级清单 / 门控。
- [x] **五个反向锚点**（改完即还原，各自必须让对应断言变红）：
  1. `match()` 里把默认根分支放到 ns 分支**之前** → 显式前缀测试红；
  2. `safe_name` 直接原样返回 → 锚点穿越测试红；
  3. 验证失败时仍继续切换 → 「离线时切换不生效」测试红；
  4. `remote_degraded()` 本机模式返回非空 → 「本机模式字典不变」测试红；
  5. 门控关时不早退 → `400` 测试红。
- [x] **第六个反向锚点（E 段追加）**：`tests/test_host.py::test_ws_modal_remote_tab_contract`
  —— 把 `function remoteFileRow(entry)` 改名 → 断言 `'function remoteFileRow(' not found` **转红**，复原后复绿。
- [x] 全量 `unittest` 回归 ≥ 1236 全绿。
- [x] `docs/REMOTE.md` 新增「把远端目录当成工作区」一节；
      `packages/agent-core/README.md` 环境变量表与端点表同步。

---

## 9. 明确不做

- **不做 SSH 版 PathGuard / 审计 append**（见 §1）。
- **不做 `@local/` 逃生前缀** —— 两个命名空间会把「判断在哪台机器」重新丢回给模型。
- **不做「多连接同时作为工作区」** —— 沿用 M11/M12 的一次一条活动连接。
- **不把远端 git 输出 parse 成本机 Git 面板** —— 半吊子桥接比明确不支持更误导。
- **不做跨设备的 `edit_files` 事务** —— 沿用 ADR-041 的判定（开箱既有限制照旧）。
- **不做锚点目录自动清理** —— 删锚点 = 删审计证据与留底，必须是用户显式动作。

---

## 10. 交付验证结果

### 10.1 交付物

| 类别 | 文件 | 要点 |
|---|---|---|
| 内核 | `agent_core/config.py` | `remote_workspace` 字段 + `VIBE_REMOTE_WORKSPACE` |
| 内核 | `agent_core/remote/roots.py` | `parse_ns_root()` / `default_root_rel()`（拼接 → 整体归一化 → 事后断言 `..`） |
| 内核 | `agent_core/remote/routing.py` | 默认根分支（**排在 ns 前缀分支之后**，默认根不遮蔽显式写法） |
| 内核 | `agent_core/remote/anchor.py`（新） | 本机锚点目录：`safe_name()` / `ensure_anchor()` / `list_anchors()` |
| 内核 | `agent_core/remote/degrade.py` | `remote_degraded()` 单一真源（本机模式恒 `{}`） |
| 内核 | `agent_core/host/server.py` | `switch_workspace(path, *, remote=None)` **先连后切**；`/api/workspaces` 增 `remoteEnabled` / `remoteWorkspaces` |
| 界面 | `agent_core/host/ui/index.html` | `#ws-tab-local` / `#ws-tab-remote` 双页签；`ws-remote-cwd` / `ws-remote-list` |
| 界面 | `agent_core/host/ui/app.js` | `browseRemoteDirs()` 同屏渲染 **目录（BUTTON，可下钻）** 与 **文件（DIV，只列出不可选）**；根层路径显示 `/`、根层「↑ 上级」禁用；门控关时页签**整键隐藏** |
| 界面 | `agent_core/host/ui/styles.css` | `.ws-file` 弱化样式（`cursor:auto`、无 hover 反馈 —— 避免误以为可点） |
| 文档 | `docs/REMOTE.md` | 新增 §6「把远端目录当成工作区（M13 / ADR-042）」；原 §6–§8 顺延为 §7–§9 |
| 文档 | `packages/agent-core/README.md` | M13 小节：流程图 / 环境变量表 / 组件表 / 端点表 / 降级口径 / 边界 |
| 文档 | `.env.example` | 登记 `VIBE_REMOTE_WORKSPACE=` |
| 文档 | `README.md` | M13 里程碑行 + 文档索引行 + `ADR-001\~042`、`M0–M13` + 测试计数 |
| 测试 | `tests/test_remote_workspace.py` | 44 用例 / 25 子测试 |
| 测试 | `tests/test_host.py::test_ws_modal_remote_tab_contract` | **新增** UI 三层契约（DOM id / app.js 渲染分支 / css 弱化） |

### 10.2 专项测试

```
tests/test_host.py tests/test_remote_ui.py tests/test_remote_workspace.py
→ 177 passed, 30 subtests passed in 39.55s
```

### 10.3 反向验证（六个锚点，均**先转红、随后全部还原并复绿**）

| # | 改动 | 期望变红的断言 | 结果 |
|---|---|---|---|
| ① | `routing.py::match()` 把默认根分支放到 ns 前缀分支**之前** | 显式 `@remote/` 前缀用例 | 转红 → 复绿 |
| ② | `anchor.py::safe_name()` 直接原样返回 | 锚点穿越用例 | 转红 → 复绿 |
| ③ | 验证失败时仍继续切换 | 「离线时切换不生效」 | 转红 → 复绿 |
| ④ | `remote_degraded()` 本机模式返回非空 | 「本机模式字典不变」 | 转红 → 复绿 |
| ⑤ | 门控关时不早退 | `400` 用例 | 转红 → 复绿 |
| ⑥ | `app.js::function remoteFileRow(entry)` 改名 | `test_ws_modal_remote_tab_contract`（`'function remoteFileRow(' not found`） | 转红 → 复绿 |

### 10.4 全量回归

- agent-core：**1353 passed / 3 skipped / 169 subtests**（242.75s）。跳过的 3 项为环境相关（ConPTY 与符号链接）。
- 服务端：**419 passed / 25 skipped / 599 subtests**（108.49s）。`test_chain_compat` 全绿（本轮未动三处同步）。
- 既有用例**无一变红**，总数只增不减。

### 10.5 浏览器实测（两轮，真实宿主 + 真实浏览器）

| 场景 | 门控 | 结果 |
|---|---|---|
| 页签与远端浏览 | `VIBE_REMOTE=1` | **12/12 PASS**：`.ws-file` 共 34 个元素**全为 `DIV`**、`cursor:auto`（点不动）；`.ws-dir` 为 `BUTTON`；当前路径显示 `/`、根层「↑ 上级」`disabled`；下钻「文档」→ `/文档` → 返回 `/`；按钮文案「设为工作区并重启」；无控制台报错 |
| 未启用时无入口 | `VIBE_REMOTE=0` | **5/5 PASS**：`#ws-tab-remote` `hidden=true` / `display:none`，`#ws-tab-local` 正常可见，`.act[data-panel="remote"]` 隐藏，弹层本机浏览不受影响 |

> 前置：宿主 `governance.status !== 'governed'` 时登录门为全屏遮罩、会拦截一切点击 —— 实测前先起治理面（`VIBE_SRV_PORT=8798` + bootstrap）并用 `VIBE_SERVER_URL` 让宿主热接入，进入 `governed` 后重测。

### 10.6 端点级口径核对

- `VIBE_REMOTE=1`：`GET /api/workspaces` → `remoteEnabled:true` + `remoteWorkspaces:[{connection,sub,nsPath}]`；`POST /api/remote/open` → ok、root `/home/gdhd`、`degraded:["连接复用不可用（每条命令一次握手，Windows 客户端常见）"]`；`GET /api/remote/tree?connection=…&path=` → **目录与文件两类 entries 齐备**（文件含 `size`）。
- `VIBE_REMOTE=0`：`GET /api/workspaces` → `remoteEnabled:false`；`POST /api/remote/open` / `POST /api/workspaces/open`（带 `remote` 载荷）→ **400**。

**口径修正（403 → 400）**：本文件初稿四处写「未启用时 403」，实测与 `/api/remote/connections` 一致为 **400 fail-closed**。已按「口径修正落盘」纪律把 §1 / §2 / §5 / §8 四处 403 全改为 400（与 M11 纪律及 `tests/test_remote_workspace.py` / `test_remote_ui.py` 的断言一致）。

### 10.7 红线核对结论

| 红线 | 结论 |
|---|---|
| ADR-003 零依赖 | `dependencies` 仍为 `[]`；前端零构建零库；远端仍只用系统 `ssh` |
| ADR-004 结构化补丁 | 远端臂复用同一份三级匹配；`write_file(overwrite=true)` 是受控例外，仍留底 + 返回真实 diff |
| ADR-006 审计只记元数据 | **不新增事件**（复用 `remote.connection` 的 `action="workspace_switch"` / `"workspace_open_fail"`）；远端正文不出机 |
| ADR-008 高危强制人审 | 远端写 / 执行恒 REVIEW，沿用 M11/M12 闸门 |
| ADR-011 用户本人动作 | 选目录并切换是用户经 UI/API 的动作；**不新增 Agent 侧工具** | 
| ADR-042 锚点隔离 | 连接名为自由文本，经 `safe_name()` 压平 + `relative_to` 双保险，出不去锚点根 |
| 默认关 + 逐字节一致 | **不新增门控**；未设 `VIBE_REMOTE` 时无页签 / 无端点 / 无 `remote` 载荷（400） |

### 10.8 遗留

- 切换后**本机 Git / LSP / 索引 watch / repo_map 不可用**，由 `remote_degraded()` 如实报出人话原因（设计如此，非缺陷）。
- `remote/review` 面板的远端模式接线沿用 M11 口径，本轮未改。
- 远端真机链路仍**不进自动回归**（需外部设备与凭据）；本轮界面实测以**假传输 + 真实宿主**完成，真链路代理由 `tests/test_remote_workspace.py` 的注入式假传输覆盖。
