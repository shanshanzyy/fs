# 远程设备文件操作

> 标号说明：本链路的**本体**（连接清单 / ssh 传输 / 路径 Jail / 8 个 Agent 工具）随 M10 落地；
> 面向用户的**文件工作面**（连接管理界面 / 文件树与查看 / 代码审查 / 编辑保存）为 **M11**
> （见 `docs/M11-PLAN.md` 与 `docs/ARCHITECTURE.md` 的 ADR-038）。两段共用同一份实现，故合并在此文件。
> 在其之上还有两段：**M12**「远端目录加入工作区」的命名空间透明路由（ADR-041，工具名一字不加），
> 以及 **M13**「把远端目录当成工作区」——本地锚点 + 路由默认根（见 §6，ADR-042）。

把本机的 Agent 接到**你自己的远程设备**上：直接读取、审阅、修改、查看、保存那台设备上的文件
（也能在远端跑命令，见 §3.2）。

执行面的编排仍在本机 —— 模型、审批闸门、审计链、补丁引擎都不动，
变的只是「文件与命令打在哪台机器上」。

> **与「代码不出本机」红线的关系**
>
> ADR-006 的原文是「代码与提示词正文**不上传治理面**」，它约束的方向是
> **本机 → 治理面**，目的是私有化部署下源码不被集中收集。
>
> 本能力引入的是另一条链路：**本机 → 你自己的设备**，走你既有的凭据与网络路径，
> 等价于「你本来就要 SSH 过去敲命令」这件事的自动化。治理面**依旧收不到任何代码**：
> 远端相关审计事件只含连接名 / 路径 / 哈希 / 计数 / 耗时，**不含正文**。
>
> 详见 `docs/ARCHITECTURE.md` 的 ADR-036 / ADR-037 / ADR-038。

---

## 1. 快速开始

### 1.1 前置条件

- 本机装了 **OpenSSH 客户端**（`ssh` 在 PATH 里）。Windows 10+ 自带
  `C:\Windows\System32\OpenSSH\ssh.exe`。
- 你的**公钥已装到远端**（`ssh-copy-id user@host` 或在远端
  `~/.ssh/authorized_keys` 里追加）。**不支持密码认证** —— 见 §4.1。
- 远端目录对登录用户可写（要改代码的话）。

### 1.2 加一条连接

```bash
python -m agent_core.remote add devbox dev@10.0.0.12 \
    --root /srv/app \
    --key ~/.ssh/id_ed25519 \
    --allow-write --allow-shell
```

`add` 会自动做一次连通性探测并打印报告；用 `--no-probe` 跳过。

### 1.3 验证

```bash
python -m agent_core.remote test devbox
```

```
✓ 连接 devbox 成功
连接      : devbox
目标      : dev@10.0.0.12:22 → /srv/app
工作区根  : /srv/app
可写      : 是
可执行命令: 是
指纹策略  : accept-new

系统      : Linux 6.1.0 (x86_64)
登录用户  : dev    shell: /bin/bash
工作目录  : /srv/app
git       : git version 2.40.1
python3   : 3.11.4
连接复用  : 是
目录可写  : 是
```

### 1.4 让 Agent 用起来

```bash
export VIBE_REMOTE=1
```

然后正常对话即可，例如：

> 「先看看远端 devbox 上 `src/parser.py` 的结构，把 `parse_int` 里没处理负号的地方补上，
> 然后在远端跑一遍 pytest。」

Agent 会依次用 `remote_connections` → `remote_read_file` → `remote_edit` →
`remote_run_command`，每个写操作都会先弹审批并展示真实的 unified diff。

---

## 2. 命令清单

```bash
python -m agent_core.remote add <名称> <[用户@]主机> [选项]
python -m agent_core.remote list
python -m agent_core.remote show [名称]
python -m agent_core.remote test [名称]
python -m agent_core.remote set-default <名称>
python -m agent_core.remote remove <名称>
```

全局参数：`--workspace <目录>`（清单默认落在 `<目录>/.vibe/remote.json`）、
`--config <文件>`（显式指定清单路径）、`--json`（机器可读输出）。

`add` 的常用选项：

| 选项 | 默认 | 说明 |
|---|---|---|
| `--user <用户名>` | — | 目标里没写 `user@` 时用 |
| `--port <端口>` | `22` | SSH 端口 |
| `--root <路径>` | `/` | 远端工作目录（绝对路径）。所有远端相对路径相对它解析 |
| `--key <私钥>` | 交给 ssh-agent | 本地私钥路径 |
| `--jump <[user@]host[:port]>` | — | 跳板机（内网设备常需要） |
| `--host-key <策略>` | `accept-new` | 见 §4.2 |
| `--connect-timeout <秒>` | `10` | 建连超时 |
| `--command-timeout <秒>` | `120` | 单条远端命令超时（上限 3600） |
| `--allow-write` | **关** | 允许写远端文件 |
| `--allow-shell` | **关** | 允许执行远端命令 |
| `--note <文本>` | — | 备注 |

---

## 3. Agent 侧工具

`VIBE_REMOTE=1` 时注册以下 9 个工具；不设时**根本不注册**（工具列表逐字节不变）。

| 工具 | 风险 | 说明 |
|---|---|---|
| `remote_connections` | SAFE | 列出连接（含默认项与权限位） |
| `remote_probe` | SAFE | 连通性 + 环境 + 能力降级报告 |
| `remote_read_file` | SAFE | 读远端文件（内容按不可信文本包裹） |
| `remote_list_dir` | SAFE | 列目录 |
| `remote_review_changes` | SAFE | 本会话远端改动的只读自审（读留底 + 远端回读，**不发写请求**） |
| `remote_grep` | **REVIEW** | 远端检索。模式串会发到远端，故恒需审批 |
| `remote_edit` | **REVIEW** | 结构化补丁（展示真实 diff） |
| `remote_write_file` | **REVIEW** | 整文件新建/覆盖（例外路径） |
| `remote_run_command` | **REVIEW** | 在远端跑命令（只读子命令如 `git status` 为 SAFE） |

### 3.1 三层门控

1. **全局开关** `VIBE_REMOTE=1` —— 关着时工具不存在，模型选不到。
2. **连接级能力位** `allow_write` / `allow_shell` —— 每条连接独立，默认都关。
   加一条**只读连接**是合法且推荐的做法：能读代码、能检索，但改不了任何东西。
3. **审批闸门** —— 写盘与执行恒 REVIEW，走与本地工具完全相同的漏斗。

三层是纵深防御而非冗余：第 1 层防误用，第 2 层限制单条连接的爆炸半径，
第 3 层把最终裁决权交回人。

### 3.2 远端命令的沙箱

远端命令**先过本地命令沙箱**（`CommandGuard`），与本地 `run_command` 同一套规则：

- 管道、重定向、`&&`、`$( )` 一律拒绝 —— 请拆成单条命令。
- `rm -rf` / `git push --force` / `sudo` / `curl … | sh` 等高危模式直接拒绝。
- 网络类命令（`curl` / `wget` / `ssh` / `scp` / `nc` …）默认禁止。
- 参数里引用**远端工作区之外**的绝对路径会触发审批理由提示。

通过分析后，命令以 `argv` 形式送到远端，**远端也不经过 shell**。

### 3.3 远端编辑的语义

与本地 `edit_file` **完全一致**（ADR-004 / ADR-037）：

- 只能定位替换，不能整文件重写；`search` 必须能唯一定位。
- 编码、BOM、换行风格（LF/CRLF）、末尾换行全部保真。
- 三级匹配：`exact` → `normalized`（忽略行尾空白）→ `flexible`（忽略缩进并自动对齐）。
- 写回原子（同目录临时文件 + `mv` 顶替），落盘后回读字节数校验。
- **改动前的内容会先存到本机** `<工作区>/.vibe/checkpoints/remote/<连接>/<会话>/<相对路径>`；
  Agent 轮次内的写入再分层到 `<会话>/<检查点id>/<相对路径>`，并在该轮目录里维护
  `manifest.json`（`existed / sha256Before / sha256After / bytes`）。存不下就**中止写入**。

---

## 4. 安全说明

### 4.1 为什么不支持密码认证

ssh 以 `BatchMode=yes` + `NumberOfPasswordPrompts=0` 调起，任何交互式提示都会直接失败。
在 Agent 上下文里**没有安全的密码输入通道** —— 把密码写进配置文件或环境变量，
风险远高于收益。请使用公钥或 ssh-agent。

### 4.2 主机指纹策略（三态）

| 策略 | 行为 | 适用 |
|---|---|---|
| `strict` | 只接受 `known_hosts` 里已有的指纹 | 生产推荐 |
| `accept-new` | 没有就记录，**指纹变了则拒绝** | 默认 |
| `insecure` | 完全不校验，且不读写 `known_hosts` | 仅限临时排障 |

指纹校验失败时错误信息会**明确提示可能存在中间人**，并建议先确认远端是否被重装或换过密钥，
而不是让你直接清 `known_hosts`。

### 4.3 远端路径 Jail

本地 `Path.resolve()` 够不着远端文件系统，因此远端 Jail 是**文本级**的：
`posixpath` 归一化 + 与前缀比对。它能挡住 `..` 逃逸、绝对路径逃逸、空字节。

它**挡不住**「工作区里一个指向 `/etc` 的符号链接」—— 那需要在远端执行 `realpath`。
因此本实现在远端能力允许时会用 `realpath` **二次确认**，能力不可用时把这一项
列进「能力降级」报告，不假装安全。

敏感文件（`.env` / `*.pem` / `id_rsa` / `.aws/` …）与治理面下发的策略拒绝清单
**直接复用本地那一份**，远端同样生效。远端 `.vibe/` 一律禁写
（那可能是另一台机器上 Agent 的审计目录）。

### 4.4 ssh 参数注入面

- **没有** `extra_ssh_args` 之类的逃生口 —— 否则一行配置就能用
  `-o StrictHostKeyChecking=no` 推翻全部加固项。需要的能力都做成具名字段。
- 所有会进 argv 的字段都**不允许以 `-` 开头**。主机名字段在 argv 最后，
  一个名为 `-oProxyCommand=…` 的「主机名」会被 ssh 当成选项吃掉 —— 正则强制
  首字符为字母数字，从根上堵死。
- 本机侧全程传 argv 数组（不经本地 shell）；远端侧逐项 `shlex.quote`；
  写回载荷走 stdin，不拼进命令字符串。

### 4.5 审计

远端相关事件只记元数据，**不含代码正文**：

```
tool.remote_read_file    connection / path / bytes / encoding
tool.remote_edit         connection / path / strategy / added / removed / changed
                         / old_sha256 / backup
tool.remote_write_file   connection / path / bytes / created
tool.remote_run_command  connection / executable / exit_code / duration_ms
                         / stdout_bytes / stderr_bytes
tool.remote_probe        connection / target / os / multiplexed / degraded
tool.remote_review_changes  connection / exists / …（M11 只读自审）
remote.connection        connection / action / allow_write / allow_shell / ok / ms（M11）
remote.file              connection / path / action / bytes / sha256 / added / removed
                         / backed_up（M11）
remote.rollback          connection / path / action / bytes / backed_up（P4 轮次恢复）
sandbox.deny_command     tool=remote_run_command + 拒绝理由
```

8 个既有 `tool.remote_*` 与 `remote_review_changes` 一律登记为**纯本地不上传**（`upload=""`）：
它们带连接名、远端路径与本机留底绝对路径，属 ADR-006 明令外发之外的机器信息。
`remote.connection` / `remote.file` / `remote.rollback` 三个事件**只记元数据**，
**远端文件正文、diff 正文、命令输出一个字符都不出机**。

远端读回的一切文本（文件内容、目录列表、命令输出）都按**不可信内容**包裹
（`<<<UNTRUSTED_DATA`），与本地读文件同理 —— 远端代码里可能藏着注入指令。

---

## 5. 界面工作面（M11）

`VIBE_REMOTE=1` 时，宿主界面（`host/ui`，桌面端与浏览器直开同一份）多出一个「**远程**」面板；
`VIBE_REMOTE` 未设时**面板连按钮都不渲染**，端点也一律不注册（`POST` 400 / `GET` 回空）。

### 5.1 面板三栏

| 栏 | 内容 |
|---|---|
| **左：连接列表** | 名称 / 目标 / **权限位徽标**（`可写` / `可执行`，关闭时明确标「只读」「禁执行」）/ 默认项标记；动作：打开、测试连通性、设为默认、权限位开关、编辑、删除；新增走一个轻量表单 |
| **中：远端文件树** | 目录优先排序、点目录展开、点文件在右栏打开；面包屑显示当前浏览路径；符号链接**如实标注但不跟随** |
| **右：查看 / 审查 / 编辑** | 三个页签。**查看**：只读文本，标「远端内容，未经审查」，附编码 / 换行 / 大小 / `sha256`。**审查**：本会话远端改动台账 + 逐 hunk 的 unified diff。**编辑**：文本域 + 保存按钮，保存前展示将产生的 diff |

### 5.2 端点清单

| 端点 | 说明 |
|---|---|
| `GET /api/remote/connections` | 连接清单（只读）：含 `enabled` / `configPath` / `default` / `active` / `ssh{available,path}` / 每条连接的对象原形与 `problems[]`；**不含私钥内容**（只回路径） |
| `POST /api/remote/connections` | 新增连接。**写前校验**，坏 spec 拒收且**不落盘**（400） |
| `POST /api/remote/connections/{name}` | 更新连接（字段缺省保留原值；**不支持改名**） |
| `POST /api/remote/connections/{name}/delete` | 删除连接；删默认后**不自动选接任者**（与 CLI 一致） |
| `POST /api/remote/connections/{name}/default` | 设为默认连接 |
| `POST /api/remote/connections/{name}/permissions` | 改权限位（**热开关**，改完立即对活动连接生效，不重握手）；至少给一项 |
| `POST /api/remote/connections/{name}/test` | 测连通性：只读无副作用，**不落盘、不改清单、不改活动连接**；连不上回 `ok:false` + `kind` + `reason`（**不抛错**：连不上是正常结果） |
| `GET /api/remote/state` | 工作面状态（同 `remote.state` 事件载荷）；未打开连接时如实回空 |
| `POST /api/remote/open` | 打开 / 切换活动连接；换连接 = **关旧开新**（不泄漏复用的 ControlMaster 套接字） |
| `GET /api/remote/tree?path=` | 远端目录树（只读，**只看条目不读内容**）；越界经 `RemotePathGuard` 拒绝 |
| `GET /api/remote/file?path=` | 只读查看；回 `text` / `size` / `encoding` / `newline` / `hadBom` / `trailingNewline` / `sha256`；**二进制与超限（`MAX_SCAN_BYTES`）明确拒绝（400）**，不做截断假预览 |
| `POST /api/remote/save` | 保存（见 §5.3） |
| `POST /api/remote/patch` | 结构化补丁：`{path, search, replace, replaceAll?}`，复用 `RemoteWorkspace.edit()` 的三级匹配（`exact` → `normalized` → `flexible`）；`changed=false` **不写回** |
| `GET /api/remote/changes` | 本会话远端改动台账：`rel_path / connection / action / bytes / sha256 / added / removed / backed_up / backup_path / edited_at`；**元数据优先**，不含正文 |
| `GET /api/remote/diff?connection=&path=` | **留底 vs 远端当前**的 unified diff + `hunks[]`；无记录 / 属别的连接 / 远端已删时 `available:false` + `reason`（**不假装成功**）；留底不存在（新建文件）按**全新增**处理 |
| `POST /api/remote/restore` | 按轮次 manifest 恢复远端改动（见 §5.3.1）；`{checkpointId}`，乐观锁不匹配**整体拒绝**，纯新建文件才删除 |

执行中（`run.*` 活跃）改连接清单返回 **409**（与设置类端点同口径）。错误码映射：
`BusyError` / `StaleFileError` → 409、`SandboxViolation` → 403、`ValueError` → 400、
`RemoteError(notfound)` → 404、`RemoteError(unreachable|transport)` → 502、其余 `RemoteError` → 400。

### 5.3 保存语义（**与本地 `/api/edit/save` 同口径**）

界面保存是**用户本人动作**（ADR-011），不重复走 Agent 审批漏斗；但**五道约束一步不省**：

1. **写前本机留底** —— 改动前内容先落到
   `<工作区>/.vibe/checkpoints/remote/<连接>/<会话>/<相对路径>`（Agent 轮次内为
   `<会话>/<检查点id>/<相对路径>` + 轮次 `manifest.json`）；**存不下就中止写入**（400），远端文件保持原样。
2. **`expectedDigest` 乐观锁** —— 与远端当前 `sha256` 不符即 **409 + 远端当前摘要**，**绝不覆盖**（提示「远端已被改过，请重新加载」）。
3. **受控覆盖 / 结构化补丁**（ADR-004）—— `path` 不存在时是**新建**（`created:true`，无留底）；存在时覆盖属**受控例外**（显式声明 overwrite）。
4. **回读校验** —— 写回后回读字节数，不符即报 `integrity` 错，不表现为「看起来成功了」。
5. **返回真实 unified diff** —— 供人在界面上当场核对改了哪几行。

改动同时登记进本会话台账 (`GET /api/remote/changes`)，随后可在「审查」页签用
`GET /api/remote/diff` 把**留底与远端当前**逐 hunk 比对。留底路径 `backup_path` 只回**路径**，
正文不出机。

远端改动**不参与**本地检查点的 restore，但参与**逐条回退**（P4）：每条用户提问下的
「回退到这条之前」会由 `POST /api/rollback` 内部同时调用远端轮次恢复，恢复细节见 §5.3.1。
更早的（不在轮次 manifest 里的）改动仍只能**手工**从留底目录捞回。

#### 5.3.1 轮次恢复（`POST /api/remote/restore`）

```json
POST /api/remote/restore
{ "checkpointId": "cp-20261021-..." }
→ { "connection": "devbox", "restored": ["src/app.py"], "deleted": ["src/new.py"],
    "skipped": [{ "path": "src/keep.py", "reason": "远端内容已变化" }], "redoId": "rollback-20261021-..." }
```

- **只认 id，不认路径**：`checkpointId` 必须是本会话检查点目录下**存在轮次 manifest** 的轮次
  （`locate_remote_turn()` 按连接逐条查找）；含 `..` / 路径分隔符的 id 一律拒绝，
  前端无法用它指向任意目录。
- **两阶段**：先逐条校验（连接 `allow_write`、相对路径过 `RemotePathGuard.check_write`、
  远端当前 `sha256` == manifest 的 `sha256After`）。**任一已存在文件不匹配 → 整体 400，一个文件都不写**，
  绝不覆盖别人的改动。
- **已存在文件**：先把当前内容留底到同连接会话目录下的 `rollback-<时间戳>/`（冲突自动加 `-2`），
  再 `write_bytes` 写回原始字节（原子写 + 字节校验）。
- **纯新建文件**（`existed=false`）：当前 `sha256` 仍等于 `sha256After` 才 `transport.delete_file()`；
  被外部改过 → `skipped` 并如实报告，**不删**。
- **审计只记元数据**：`remote.rollback`（`connection / path / action / bytes / backed_up`），
  正文与 diff 不出机。
- 界面**不提供**远端「撤销」按钮：远端 redo 目录没有恢复端点（只有本地 redo 走
  `/api/checkpoints/{id}/restore`），与其给一个按下去会失败的入口，不如只如实展示恢复/删除/跳过清单。

### 5.4 事件（SSE，不持久化）

| 事件 | 载荷 |
|---|---|
| `remote.state` | `connection` `display` `root` `browsePath` `allowWrite` `allowShell` `capabilities{degraded[]}` `changes`（本会话改动条数）；**不含任何文件正文** |
| `remote.edit` | `connection` `path` `action`(`save`/`create`/`edit`) `added` `removed` `strategy` `sha256` `diff`；diff 与本地 `run.approval.preview.body` 同一条口径 —— **供人审的改动内容，不进审计、不落盘** |

### 5.5 只读自审工具

`VIBE_REMOTE=1` 时额外注册 `remote_review_changes`（**恒 SAFE**）：纯本地计算
（读留底 + 远端只读回读），**不发写请求、不落盘、不弹审批**，结论按 `wrap_untrusted` 包裹。
它**不进** `build_readonly_registry()` —— 子 Agent 恒不触远端（沿用既有纪律）。

### 5.6 远端预览：把远端端口搬到本机（P3 / ADR-040）

`VIBE_REMOTE=1` **且** `VIBE_PREVIEW_REMOTE=1` 时，预览面板的工具条多一个「远端」入口：
选一条连接 + 填远端端口，host 会起一条
`ssh -L 127.0.0.1:<本机随机端口>:127.0.0.1:<远端端口>`，然后把这个本机地址交给
预览帧（跨源，与 P2 完全同一套隔离）。

```bash
# 远端（开发机上）先自己把服务起起来
npm run dev            # 监听 127.0.0.1:5173

# 本机：开两个门控再启动工作台
VIBE_REMOTE=1 VIBE_PREVIEW_REMOTE=1 python -m agent_core.host --workspace . --port 8765
```

| 端点 | 请求 | 返回 |
|---|---|---|
| `GET /api/preview/tunnel` | —— | `{enabled, tunnels[], max}`；`enabled` 要求两个门控都开 |
| `POST /api/preview/tunnel` | `{connection, remotePort}` | `{ok, id, url, localPort, remotePort, connection, state}` |
| `POST /api/preview/tunnel/close` | `{id}` | `{ok, closed, refs}`（`closed:false` = 还有别的标签在用） |

四条要记住的边界：

1. **`-L` 两端都是环回，用户只能给整数端口**。没有「远端主机」输入框 ——
   那正是 `-L` 的注入面（`-L 8080:10.0.0.5:6379` 会把远端内网的任意服务拽到本机）。
2. **本地监听只绑 `127.0.0.1`**，端口由内核分配，不是固定值。
3. **不用 `-N`**：建会话 + 远端 `cat >/dev/null`，父进程一死 ssh 就跟着退，
   不留孤儿监听。关标签 / 刷新页面 / 会话结束三处都会释放。
4. **审计只有元数据**：`preview.tunnel{connection, action, remote_port, local_port, ok,
   ms, reason, closed, refs}` —— 状态跃迁与开转发带 `ok/ms`，失败时多一个 `reason`
   （传输层短类型标志，不是报错正文），释放时带 `closed/refs`；
   **不含主机名、用户名与被转发的字节**（ADR-006）。

⚠ 状态 `up` 只表示**本地监听已建立**，不代表远端服务在跑（`-L` 的语义）。页面白屏时先确认
远端那个 dev server 起没起、端口是不是写错了。只支持 http（与 P2 同口径）。
远端需有 POSIX shell —— 生命周期凭证是远端 `cat >/dev/null`（与 §7 的探测脚本同一假设）。

---

## 6. 把远端目录当成工作区（M13 / ADR-042）

M12 是「把某个远端目录**加入**本机工作区的多根列表」（工具里用 `@remote/<连接>/<子路径>` 指名）；
M13 是更彻底的一步 —— **切换工作区时直接把某个远端目录当成工作区本体**：

```
切换工作区 → 选「远程目录」 → 选连接 → 浏览远端目录 → 「设为工作区并重启」
                              ↓
  Config.workspace        = ~/.vibe/workspaces/remote/<连接>/<指纹>/   ← 本机锚点（只放 .vibe 资产）
  Config.remote_workspace = "@remote/<连接>/<子目录>"                  ← 路由默认根
```

切过去之后，模型写**裸相对路径**（`src/app.py`）也落在那一层远端目录上 ——
不必每句话都点名 `@remote/...`。而**本机 `.vibe` 资产原地不动**：审计链、会话库、
检查点、远端写的本机留底全部落在**锚点目录**里（ADR-006 不动）。

内核里 `<workspace>` 这个短语只有两处用法 ——「Jail 的根」与「`.vibe` 资产的落点」，
ADR-042 把它们拆开了：根的默认值由路由决定，资产的落点仍是本机锚点。

### 6.1 怎么用

`VIBE_REMOTE=1` 时，标题栏的工作区按钮 → 弹层顶部多一个「远程目录」页签：

1. 选一条连接（下拉里活动连接优先）；
2. 目录树**边逛边选**：目录行点进去即下钻，**文件行只列出、不可选**（工作区只能是目录，ADR-042）——
   同屏列文件只是为了让「本机目录旁边也能看见远端有什么」，免得以为文件也能切过去；
3. 「设为工作区并重启」→ 后端**先连后切**（握手 + 目标目录存在性校验），
   失败则旧会话原封不动（不会出现「切过去了、但一条远端命令都打不出去」的半成品状态）。

```bash
VIBE_REMOTE=1 python -m agent_core.host --workspace . --port 8765

# 也可以在启动时就指定默认锚点（省一次手动切换，但绕不开 VIBE_REMOTE）
VIBE_REMOTE=1 VIBE_REMOTE_WORKSPACE='@remote/devbox/项目/smart-writer' \
    python -m agent_core.host --workspace .
```

### 6.2 代价（**如实降级，不静默返回空**）

| 能力 | 远程工作区模式下 |
|---|---|
| 本机 Git 面板 | 不可用（改的是远端仓库）——面板显示原因，不是空白 |
| LSP 诊断 | 不注册 `lsp_diagnostics`（诊断服务只覆盖本机锚点） |
| 索引 watch | 不启动 |
| 在本机写文件 | 不可能 —— 提示词明确写明「本模式无法在本机写文件」 |
| 远端 git / 命令 | 走「远程设备」面板的远端会话（§5） |

清单的唯一真源是 `Session.remote_degraded()`（**本机模式恒为空字典**）；
端点、提示词、界面三处都从它取值，不会出现「界面说支持、内核说不支持」。

### 6.3 端点与门控

| 端点 | 变更 | 未启用 `VIBE_REMOTE` 时 |
|---|---|---|
| `POST /api/workspaces/open` | body 收 `{remote:{connection, sub}}`（与 `path` 互斥） | **400**（fail-closed，与 `/api/remote/*` 同口径） |
| `GET /api/workspaces` | 追加 `remoteEnabled` 与 `remoteWorkspaces`（**扫锚点反推**历史） | 两个键照常返回（远程列表本来就是**只读**的，不足以单独门控） |
| `GET /api/remote/tree` | 复用（M11 已有），不改 | 不变（400） |

界面同理：未启用时「远程目录」页签**整键隐藏** —— 点进去只能看到拒绝，那不算入口。

### 6.4 审计

**不新增事件、不动三处同步**：复用 `remote.connection` 的
`action="workspace_switch"` / `"workspace_open_fail"`，只记连接名 / 子目录 / 成败，
**不含正文**。锚点目录**不自动清理** —— 删锚点等于删审计证据与留底，必须是用户的显式动作。

### 6.5 收尾修正：根层路径必须压平后再比较（M12）

`GET /api/remote/tree` 在**连接根层**回的 `entry.path` 是 `./名称`（那一层的 rel 就是 `.`），
而落库的工作区根名已被 `store.normalize_sub()` 压平成 `名称`。前端原样比较时两边永不相等，
于是「已在工作区」判不出来：右键一个**已经加入**的根层目录，菜单恒显示「加入工作区」，
点下去才报「该目录已在工作区」。

现口径：前端在**判在不在工作区**、**加入**、**移出**三处统一走 `normalizeRootSub()`
（只压前导 `./` 与前导 `/`，其余含 `..` 原样留着，越界校验仍归后端）。
契约由 `tests/test_host.py::test_remote_root_sub_normalized_contract` 锁住。后端返回值**未改**，
所以 `path` 仍带 `./` 前缀 —— 依赖它做键的场景（展开态、树缓存）不受影响。

### 6.6 远端节点也能「加入对话」（M12/M13）

工作区树右键「加入对话」在**远端节点上同样可用**：本地树给相对路径，远端三处入口
（工作区树的远端子节点、远端根、远程设备面板的文件与目录）给命名空间写法
`@remote/<连接>/<相对连接 root 的路径>`（前端 `addRemoteRef` / `remoteNsPath`）。
文件映射 `file`、目录映射 `folder`，与本地节点**同一条 refs 通道**，不新增端点。

后端由 `Session._remote_ref_resolver()` 承接：连接、根的判定直接复用 `RemoteRouter.match()`,
读取走 `RemoteWorkspace.read/list_dir` —— 与模型 `read_file("@remote/...")` 是**同一条路由**。
`context.references.resolve_refs()` 只按前缀分流：未注入解析器（`VIBE_REMOTE` 关）时
`@remote/...` 引用**降级为提示**，绝不回退到本机同名路径；远端只支持 `file` / `folder`
（符号扫描只在本机索引上做）。远端读失败同样只让这一条引用降级，不阻断整条任务。

---

## 7. 能力降级

远端环境差异很大，没法假设一定有 GNU coreutils 或 python3。启动探测一次并如实报告：

| 降级项 | 影响 |
|---|---|
| 连接复用不可用 | Windows 客户端的 OpenSSH 不支持 `ControlMaster`，每条命令一次握手 |
| 无可用 `stat` | 无法在保留权限的前提下覆盖**已存在**文件（新建不受影响） |
| `find` 不支持 `-printf` | 目录列举走 `ls`，拿不到条目类型与大小 |
| `head` 不支持 `-c` | 大文件先整段传输再本地截断 |
| 远端无 `python3` | 结构化操作退化为 POSIX 工具解析 |

`python -m agent_core.remote test <名称>` 与 `remote_probe` 工具都会把降级项列出来。

---

## 8. 排障

| 现象 | 处置 |
|---|---|
| `远端认证失败` | 确认公钥已装到远端；确认 `--key` 指向正确；确认远端 sshd 允许公钥认证。**不支持密码。** |
| `远端主机指纹校验未通过` | 先确认远端是否被重装/换过密钥（**可能是中间人**）。确认无误后清理对应 `known_hosts` 条目，或对新主机用 `accept-new` |
| `远端不可达` | 检查主机名/端口、内网/VPN 连通性、远端 sshd 是否在监听；需要跳板机时设 `--jump` |
| `连接 X 是只读的（allow_write=off）` | 该连接加的时候没开写权限。用 `add` 重新加并带 `--allow-write` |
| `未开启命令执行权限（allow_shell=off）` | 同上，带 `--allow-shell` |
| `远端路径经符号链接解析后越出工作区` | 工作区内有软链指到外部。改用软链的真实路径，或把 `--root` 指到实际位置 |
| `写回校验失败：期望 N 字节…` | 传输被截断或远端磁盘满。检查远端磁盘空间后重试 |
| `远端缺少可用的 stat` | 覆盖已存在文件需要保留权限位。装 coreutils，或先删远端目标文件再写 |
| 退出码 255 且报错像 ssh 的 | 连接层失败，不是你的命令失败。按上面几行分类排查 |
| Windows 上 `--root /srv/app` 被报「root 必须是绝对路径」 | Git Bash / MSYS 会把看起来像 POSIX 路径的参数**改写成 Windows 路径**再交给进程。用 `MSYS_NO_PATHCONV=1` 前缀，或改用 PowerShell / cmd 运行。注意该变量同时会禁用 `PYTHONPATH` 的转换，需写成 Windows 形式 |

### 8.1 在 Windows 上快速自检

```bash
# Git Bash
MSYS_NO_PATHCONV=1 PYTHONPATH="E:/path/to/packages/agent-core" \
    python -m agent_core.remote list --workspace .
```

若不便设置环境变量，用 PowerShell 运行同样的命令即可（PowerShell 不做路径改写）。

---

## 9. 当前边界

- 只做 SSH：不做 WinRM / 容器 exec / 云厂商 SDK。
- 命令受本地命令沙箱约束，**不能跑复合命令**（管道、`&&` 等）。
- Windows 客户端无连接复用。
- 不做多跳代理链（只支持单级 `ProxyJump`）。
- 不做远端文件监听 / 增量索引：远端检索走 `remote_grep`，不走本地索引。
- 远端改动**不参与**本地检查点 restore；Agent 轮次内的改动可由 `POST /api/remote/restore`
  按轮次 manifest 恢复（并接进「逐条回退」），更早的改动只能从留底目录手工捞回；
  留底目录（含 `rollback-*` redo 目录）目前不自动清理。
- 子 Agent 无法触达远端（远程工具不进只读工具面）。
- **一次只活动一条连接**（§5 的 `POST /api/remote/open` 换连接 = 关旧开新），不做多连接并发工作面。
- §5 的界面只做**文本类文件**：不做远端二进制 / 图片预览，不做远端构建 / 测试面板
  （需要时用 `remote_run_command`），不做远端文件的双向同步 / 镜像。
- 远端预览（§5.6）只做 **TCP 环回转发**：不做 SOCKS 代理、不做远端任意主机/网段转发、
  不做端口自动发现（端口要手填 —— 探测等于替用户猜服务）。同时最多 4 条转发。
- 远端工作区（§6）**不做 SSH 版 PathGuard / 审计 append**（`.vibe` 资产恒在本机锚点，ADR-006 不动）；
  不做 `@local/` 逃生前缀（两个命名空间会把「判断在哪台机器」重新丢回给模型）；
  不做多连接同时作为工作区（沿用「一次一条活动连接」）；不把远端 git 输出 parse 成本机 Git 面板；
  锚点目录不自动清理（删锚点 = 删审计证据与留底）。
